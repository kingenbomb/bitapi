#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grok adapter —— 直连上游(不经中间代理)+ 自持 OAuth 续期。

自持型:凭据存 accounts.secret、续期自己打 auth.x.ai、对话自己转协议。

注意 xAI 刷新时会**轮换 refresh_token**,所以新拿到的那份必须写回 —— 漏了下次
刷新就用旧的,直接 invalid_grant。也因此同一批号**不能被两处同时持有**:两边
各刷一次,后刷的把先刷的作废。本渠道的凭据存在 accounts 表里,那是唯一真源。

反检测:实测(2026-09-02)标准库 urllib 的 TLS 指纹 + 下面五个 CLI header 就能
过,不需要 uTLS / curl_cffi 那套指纹伪装。少带 X-XAI-Token-Auth 会 401。

协议:上游是 OpenAI Responses API(不是 chat/completions)。实测的对应关系:
  system 消息        → instructions 字段
  其余 messages      → input 数组 [{role, content}](content 用纯字符串,
                       分块形式 [{type:input_text}] 实测会挂在那里不返回)
  非流式取值         → output[*].content[*].text 拼接
  流式               → 只认 response.output_text.delta 的 delta;usage 在
                       response.completed 事件里
  usage              → 上游给真实值(含 cached/reasoning tokens),所以
                       billing_mode=upstream。这同时省掉了 tiktoken 估算,
                       那是热路径上最贵的一段 CPU。

额度按模型算:同一个号 grok-4.6 能用而 grok-code-fast-1 报 402
spending-limit,所以 models 只列实测有额度的(见 config.GROK_MODELS),
402 交给 core.pool 的失败分类落 exhausted 而不是 dead。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from core.adapter import (BILL_UPSTREAM, CAP_CHAT, CAP_HEALTH, CAP_REFRESH,
                          Adapter, register_adapter)

# 上游只认这两个域名。base_url 存在凭据里(每个号可能不同),用白名单挡住
# 被改写的凭据把 Bearer token 发到别处 —— 那是把号直接送人。
_ALLOWED_HOSTS = {"cli-chat-proxy.grok.com", "api.x.ai"}

# 冒充 grok 官方 CLI。值照 CLIProxyAPI 的常量抄(internal/runtime/executor
# /xai_executor.go),版本号跟着上游 CLI 走,过期了会 401。
_CLI_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.120",
    "x-grok-client-identifier": "grok-shell",
    "x-authenticateresponse": "authenticate-response",
    "User-Agent": "xai-grok-workspace/0.2.120",
}


def _models():
    return [m.strip() for m in str(config.GROK_MODELS or "").split(",") if m.strip()]


def _upstream_model(model):
    """对外模型名 → 上游认的模型名。

    目前两者同名(config.GROK_MODELS 直接写上游 id),留这层是为了以后要挂
    别名时不必改调用点。
    """
    prefix = config.GROK_MODEL_PREFIX or ""
    if prefix and str(model or "").startswith(prefix):
        return model[len(prefix):]
    return model


def _iso_to_unix(text):
    """把凭据里的 expired(ISO8601,可能带 Z)转 unix 秒;解析不了返回 0。"""
    s = str(text or "").strip().replace("Z", "+00:00")
    if not s:
        return 0
    try:
        import datetime
        return int(datetime.datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError):
        return 0


class GrokError(RuntimeError):
    """带 HTTP 状态码的上游错误,供 core.pool.classify_failure 按 code 分类。"""

    def __init__(self, code, detail):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail


class GrokAdapter(Adapter):
    name = "grok"
    capabilities = [CAP_CHAT, CAP_REFRESH, CAP_HEALTH]
    models = _models()
    columns = [
        {"key": "token_exp", "label": "Token到期", "type": "time"},
        {"key": "meta.sub", "label": "订阅", "type": "text"},
    ]
    # token 实测 6 小时。刷新周期取「有效期 - 提前量」,由巡检提前换掉。
    refresh_interval = 21600 - config.GROK_REFRESH_MARGIN
    billing_mode = BILL_UPSTREAM   # 上游返回真实 usage,不必 tiktoken 估算

    # ---- 凭据与 URL ----

    @staticmethod
    def _secret(acct):
        s = acct.get("secret")
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except (ValueError, TypeError):
                s = {}
        return s or {}

    @classmethod
    def _url(cls, acct, path, query=""):
        base = cls._secret(acct).get("base_url") or config.GROK_BASE_URL
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
            raise GrokError(0, f"untrusted base_url: {parts.hostname}")
        full = parts.path.rstrip("/") + path
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, full, query, ""))

    @staticmethod
    def _headers(acct, accept):
        h = dict(_CLI_HEADERS)
        h["Content-Type"] = "application/json"
        h["Accept"] = accept
        h["Authorization"] = "Bearer " + str(acct.get("token") or "")
        return h

    @staticmethod
    def _open(req, timeout):
        """统一出口:把 HTTPError 翻成带 code 的 GrokError。

        code 必须留给 core.pool.classify_failure —— 它按 401/402/403 判 terminal,
        其余(超时、5xx、连接重置)判 transient。丢了 code 就只能靠字符串猜。
        """
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(600).decode("utf-8", "replace")
            except Exception:
                pass
            raise GrokError(e.code, body[:400]) from e

    # ---- OpenAI ↔ Responses 协议转换 ----

    @staticmethod
    def _to_payload(messages, model, stream):
        """OpenAI messages → Responses 请求体。

        system 走 instructions 而不是塞进 input:实测 input 里带 role=system
        不报错但不生效,提示词会被当普通用户消息。多条 system 按顺序拼。
        """
        instructions, items = [], []
        for m in messages or []:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                # 多模态请求里 content 是分块数组,这里只取文本块
                content = "".join(p.get("text", "") for p in content
                                  if isinstance(p, dict) and p.get("type") in
                                  ("text", "input_text"))
            content = "" if content is None else str(content)
            if role == "system":
                instructions.append(content)
            elif role in ("user", "assistant"):
                items.append({"role": role, "content": content})
        payload = {"model": _upstream_model(model), "input": items,
                   "stream": bool(stream)}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _text_of(resp):
        out = []
        for item in resp.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    out.append(part.get("text") or "")
        return "".join(out)

    @staticmethod
    def _usage_of(resp):
        """Responses 的 usage → OpenAI usage。缓存读命中单独给出,计费层要用。"""
        u = resp.get("usage") or {}
        ind = u.get("input_tokens_details") or {}
        outd = u.get("output_tokens_details") or {}
        return {
            "prompt_tokens": u.get("input_tokens") or 0,
            "completion_tokens": u.get("output_tokens") or 0,
            "total_tokens": u.get("total_tokens") or 0,
            "prompt_tokens_details": {"cached_tokens": ind.get("cached_tokens") or 0},
            "completion_tokens_details": {
                "reasoning_tokens": outd.get("reasoning_tokens") or 0},
        }

    # ---- 网关对话 ----

    def chat(self, acct, messages, stream=False, model=None, on_token=None,
             body=None):
        model = model if model in self.models else (self.models or ["grok-4.6"])[0]
        payload = self._to_payload(messages, model, stream)
        data = json.dumps(payload).encode()
        accept = "text/event-stream" if stream else "application/json"
        req = urllib.request.Request(
            self._url(acct, "/responses"), data=data, method="POST",
            headers=self._headers(acct, accept))
        if not stream:
            with self._open(req, config.GROK_TIMEOUT) as resp:
                raw = json.loads(resp.read().decode("utf-8", "replace"))
            return {"content": self._text_of(raw), "reasoning": None,
                    "usage": self._usage_of(raw)}
        return self._stream(req, on_token)

    def _stream(self, req, on_token):
        """流式:只认 output_text.delta,usage 在 completed 事件里。

        其余事件(created / in_progress / output_item.* / content_part.*)一律丢弃 ——
        它们只表达状态机推进,没有对下游有用的内容。不做 JSON 全解析:先按前缀
        筛掉不关心的事件,再解析剩下的,几千条并发流下这一步的 CPU 差别很大。
        """
        text, usage = [], None
        with self._open(req, config.GROK_TIMEOUT) as resp:
            for line in resp:
                s = line.decode("utf-8", "replace").strip()
                if not s.startswith("data:"):
                    continue
                chunk = s[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                if '"response.output_text.delta"' in chunk:
                    delta = (json.loads(chunk).get("delta") or "")
                    if delta:
                        text.append(delta)
                        if on_token:
                            on_token(delta, "content")
                elif '"response.completed"' in chunk:
                    usage = self._usage_of(
                        json.loads(chunk).get("response") or {})
        return {"content": "".join(text), "reasoning": None, "usage": usage}

    # ---- 续期与探活 ----

    def refresh_token(self, acct):
        """用 refresh_token 换新 access_token。

        xAI 刷新时会**轮换 refresh_token**,所以新的那份必须写回 secret ——
        漏了这一步下次刷新就用旧的,直接 invalid_grant。也因为会轮换,同一批号
        不能让两处同时持有:两边各刷一次,后刷的把那边的作废。
        """
        sec = self._secret(acct)
        rt = sec.get("refresh_token")
        if not rt:
            raise GrokError(0, "no refresh_token in secret")
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": sec.get("client_id") or config.GROK_CLIENT_ID,
        }).encode()
        endpoint = sec.get("token_endpoint") or config.GROK_TOKEN_ENDPOINT
        req = urllib.request.Request(endpoint, data=data, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": _CLI_HEADERS["User-Agent"],
        })
        with self._open(req, 60) as resp:
            got = json.loads(resp.read().decode("utf-8", "replace"))
        token = got.get("access_token")
        if not token:
            raise GrokError(0, "refresh returned no access_token")
        new_secret = dict(sec)
        if got.get("refresh_token"):
            new_secret["refresh_token"] = got["refresh_token"]
        exp = int(time.time()) + int(got.get("expires_in") or 21600)
        return {"token": token, "token_exp": exp, "secret": new_secret}

    # ---- 导入 ----

    def import_account(self, db, item):
        """导入一个号 → 返回 (action, identity),action ∈ {"imported", "skipped"}。

        接受 xAI OAuth 凭据(CPA 导出的 auths JSON 同形状):
          {"email"|"sub", "access_token"|"token", "refresh_token",
           "expired"|"token_exp", "base_url", "token_endpoint"}

        **refresh_token 是必须的**:只有 access_token 的号几小时后就失效,而本渠道
        没有别的续期途径,号会一直卡在取号失败上。凭据存 accounts.secret,
        库是唯一真源 —— 导入之后不再读任何外部文件。
        """
        if not isinstance(item, dict):
            raise ValueError("account must be a JSON object")
        # 兼容两种写法:整个对象就是凭据,或凭据包在 secret 里
        sec = item.get("secret") if isinstance(item.get("secret"), dict) else item
        refresh = str(sec.get("refresh_token") or "").strip()
        if not refresh:
            raise ValueError("refresh_token is required")
        identity = str(item.get("identity") or sec.get("email")
                       or sec.get("sub") or "").strip()
        if not identity:
            raise ValueError("email / identity is required")

        secret = {"refresh_token": refresh,
                  "client_id": sec.get("client_id") or config.GROK_CLIENT_ID}
        for k in ("base_url", "token_endpoint"):
            if sec.get(k):
                secret[k] = sec[k]
        token = str(sec.get("access_token") or sec.get("token") or "").strip()
        exp = _iso_to_unix(sec.get("expired")) or int(sec.get("token_exp") or 0)

        from core import db as dbmod
        existed = db.get_by_identity(self.name, identity) is not None
        acct_id = db.upsert_account(self.name, identity, secret=secret,
                                    status=dbmod.ST_ACTIVE)
        upd = {}
        if token:
            upd["token"] = token
        if exp:
            upd["token_exp"] = exp
        if upd:
            db.update_account(acct_id, **upd)
        return ("skipped" if existed else "imported"), identity

    def health(self, acct):
        """只读 user 端点。只有 userBlockedReason 才是账号级封禁;
        hasGrokCodeAccess 是权限位不是额度,不能拿来判死号。"""
        req = urllib.request.Request(
            self._url(acct, "/user", "include=subscription"),
            headers=self._headers(acct, "application/json"))
        with self._open(req, 30) as resp:
            payload = json.loads(resp.read(65536).decode("utf-8", "replace"))
        return not payload.get("userBlockedReason")


register_adapter(GrokAdapter())
