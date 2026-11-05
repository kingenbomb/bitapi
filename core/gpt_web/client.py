"""A single Web image request using an existing bit-api account and proxy.

The protocol in protocol.py is adapted from chatgpt2api. Pool selection,
credential persistence, retries and image storage belong to bit-api.
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request
import uuid

from curl_cffi import requests

import config
from adapters.openai_compat import UpstreamError
from core import sse
from core.adapter import GenerationSubmittedError
from core.gpt_transport import StreamResponse
from .protocol import DEFAULT_POW_SCRIPT, ImageProtocol, ensure_ok
from .proof import parse_pow_resources

MAX_IMAGE_BYTES = 64 * 1024 * 1024
HTTP_PROFILE = "chrome152"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


def mime_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("网页生图未返回有效的 PNG、JPEG 或 WebP 图片")


def is_progress(line):
    data = sse.data_of(line)
    if not data:
        return False
    if data == "[DONE]":
        return True
    try:
        event = json.loads(data)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    # Web's v1 message/delta/patch events contain real output. A connection
    # heartbeat or a repeated lifecycle event must not prolong generation.
    return bool(event.get("error") or event.get("message") or event.get("v")
                or event.get("o") in ("append", "patch", "replace")
                or event.get("type") in ("moderation", "server_ste_metadata"))


class WebSession:
    """Reuse bootstrap cookies while using bit-api's cancelable SSE transport."""
    def __init__(self, proxy):
        self.proxy = proxy
        self.raw = requests.Session(impersonate=HTTP_PROFILE, trust_env=False, proxy=proxy)
        self.headers = self.raw.headers

    def get(self, *args, **kwargs):
        return self.raw.get(*args, **kwargs)

    def post(self, url, *, headers=None, json=None, timeout=30, stream=False):
        if not stream:
            return self.raw.post(url, headers=headers, json=json, timeout=timeout, allow_redirects=False)
        request_headers = {**self.headers, **(headers or {})}
        cookies = self.raw.cookies.get_dict()
        if cookies:
            request_headers["Cookie"] = "; ".join(key + "=" + value for key, value in cookies.items())
        from json import dumps
        request = urllib.request.Request(url, data=dumps(json, ensure_ascii=False).encode(),
                                         headers=request_headers, method="POST")
        response = StreamResponse(request, config.STREAM_IDLE_TIMEOUT, self.proxy, impersonate=HTTP_PROFILE)
        response.status_code = response.response.status_code
        return response

    def close(self):
        self.raw.close()


class WebImageClient(ImageProtocol):
    def __init__(self, account, proxy=None, notify=None, cancelled=None):
        secret = account.get("secret") or {}
        self.access_token = account.get("token") or secret.get("access_token") or ""
        if not self.access_token or self.access_token.startswith("at-") or secret.get("auth_mode") == "agentIdentity":
            raise ValueError("网页生图需要此账号的 ChatGPT access_token")
        self.base_url = "https://chatgpt.com"
        self.client_version = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
        self.client_build_number = "6708908"
        self.user_agent = UA
        identity = str(account.get("id") or account.get("identity") or secret.get("account_id"))
        self.device_id = str(uuid.UUID(hashlib.sha256(("web-image:" + identity).encode()).hexdigest()[:32]))
        self.session_id = str(uuid.uuid4())
        # Current script references and network cookies are loaded over HTTP.
        # No interactive browser or account login is involved.
        self.pow_script_sources, self.pow_data_build = [DEFAULT_POW_SCRIPT], ""
        self.session = WebSession(proxy)
        self.notify = notify or (lambda message: None)
        self.cancelled = cancelled
        self.conversation_id = ""
        self.submitted = False
        self.proxy = proxy
        self.session.headers.update({
            "Authorization": "Bearer " + self.access_token, "User-Agent": UA,
            "Origin": self.base_url, "Referer": self.base_url + "/", "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="152", "Google Chrome";v="152", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "no-cache", "Pragma": "no-cache", "Priority": "u=1, i",
            "OAI-Device-Id": self.device_id, "OAI-Session-Id": self.session_id,
            "OAI-Language": "en-US", "OAI-Client-Version": self.client_version,
            "OAI-Client-Build-Number": self.client_build_number,
        })
        if secret.get("account_id"):
            self.session.headers["ChatGPT-Account-ID"] = secret["account_id"]
        self.session.raw.cookies.set("oai-did", self.device_id, domain="chatgpt.com")

    def close(self):
        self.session.close()

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError("生图请求已取消")

    def _bootstrap(self):
        response = self.session.get(self.base_url + "/", timeout=30)
        ensure_ok(response, "/")
        self.pow_script_sources, self.pow_data_build = parse_pow_resources(response.text)

    def _download(self, url):
        # Signed download URLs may use a separate CDN. Never forward the
        # account's Authorization header to that host.
        headers = {"User-Agent": UA}
        if urllib.parse.urlsplit(url).hostname == "chatgpt.com":
            headers["Authorization"] = "Bearer " + self.access_token
        with requests.Session(impersonate=HTTP_PROFILE, trust_env=False, proxy=self.proxy) as session:
            response = session.get(url, headers=headers, timeout=60, stream=True)
            try:
                ensure_ok(response, "/image-download")
                out = bytearray()
                for chunk in response.iter_content():
                    self._check_cancelled()
                    out.extend(chunk)
                    if len(out) > MAX_IMAGE_BYTES:
                        raise ValueError("网页生图文件超过 64 MB")
                data = bytes(out)
                mime_type(data)
                return data
            finally:
                response.close()

    def _receive(self, response):
        try:
            ensure_ok(response, "/backend-api/f/conversation")
            self.submitted = True
            for line in sse.iter_sse_lines(response, config.STREAM_IDLE_TIMEOUT, output_check=is_progress):
                self._check_cancelled()
                raw = sse.data_of(line)
                if not raw:
                    continue
                if raw == "[DONE]":
                    break
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                value = event.get("v")
                self.conversation_id = str(event.get("conversation_id") or
                    (value.get("conversation_id") if isinstance(value, dict) else "") or self.conversation_id)
                if event.get("error"):
                    raise UpstreamError(400, json.dumps(event["error"], ensure_ascii=False))
                if event.get("type") == "moderation" and (event.get("moderation_response") or {}).get("blocked"):
                    raise UpstreamError(400, "网页生图被上游内容策略拒绝")
                if is_progress(line):
                    self.notify("网页正在生成图片")
        finally:
            response.close()

    def _result(self):
        if not self.conversation_id:
            raise RuntimeError("网页已受理生图，但未返回会话 ID")
        deadline = time.monotonic() + config.STREAM_IDLE_TIMEOUT
        previous = None
        while time.monotonic() < deadline:
            self._check_cancelled()
            conversation = self._get_conversation(self.conversation_id)
            records = self._extract_image_tool_records(conversation)
            for record in reversed(records):
                downloads = [(self._get_file_download_url, (fid,)) for fid in record["file_ids"]]
                downloads += [(self._get_attachment_download_url, (self.conversation_id, fid)) for fid in record["sediment_ids"]]
                for resolve, args in downloads:
                    try:
                        url = resolve(*args)
                    except UpstreamError as exc:
                        if exc.code == 404:
                            continue
                        raise
                    if url:
                        self.notify("正在接收网页生成的图片")
                        return self._download(url)
            # Reset the output-idle budget only when message content changes,
            # not on repeated polling, metadata timestamps or heartbeat traffic.
            content = [((node or {}).get("message") or {}).get("content")
                       for node in (conversation.get("mapping") or {}).values()]
            current = json.dumps(content, sort_keys=True)
            if current != previous:
                deadline = time.monotonic() + config.STREAM_IDLE_TIMEOUT
                previous = current
                self.notify("网页图片仍在生成，正在等待结果")
            if self.cancelled is not None:
                self.cancelled.wait(2)
            else:
                time.sleep(2)
        raise sse.StreamIdleTimeout("网页生图长时间无新输出，结果尚未取回")

    def generate(self, prompt, size=None):
        try:
            self._check_cancelled()
            self.notify("正在准备网页生图会话")
            self._bootstrap()
            self._check_cancelled()
            requirements = self._get_chat_requirements()
            if size and size != "auto":
                prompt += "\n图片尺寸：" + size
            conduit = self._prepare_image_conversation(prompt, requirements, "gpt-image-2")
            self._check_cancelled()
            self.notify("正在向网页提交生图请求")
            # A lost HTTP response can still mean the upstream accepted it.
            self.submitted = True
            response = self._start_image_generation(prompt, requirements, conduit, "gpt-image-2")
            try:
                self._receive(response)
            except sse.StreamIdleTimeout:
                if not self.conversation_id:
                    raise
                # A disconnected stream does not justify another generation.
                # Continue looking up the same accepted conversation only.
            return self._result()
        except Exception as exc:
            if self.submitted and not isinstance(exc, InterruptedError) and not (
                    isinstance(exc, UpstreamError) and exc.code in (400, 401, 403, 429)):
                raise GenerationSubmittedError(self.conversation_id or "unknown", "gpt",
                    "网页生图已提交，结果暂未取回；请勿重复提交") from exc
            raise
        finally:
            self.close()
