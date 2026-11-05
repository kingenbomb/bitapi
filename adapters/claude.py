#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude adapter —— Anthropic 订阅号池(Claude Code 那条 Messages 路径)。

自持型:凭据存 accounts.secret,续期自己打 platform.claude.com,对话自己转协议。

凭据形状:
  {"refresh_token": "...",      # 必须
   "account_uuid": "...",       # 可选,写进 metadata.user_id
   "client_id": "...",          # 可选,默认 Claude Code 的公开 client id
   "base_url": "..."}           # 可选,白名单校验

上游就是 Anthropic Messages API,所以这一层只做两件事:
  1. OpenAI messages ↔ Anthropic messages 互转(bit-api 内部统一是 OpenAI 形状)
  2. 冒充 Claude Code —— 订阅号只认这个客户端

冒充不是可选项:少了 Claude Code 的身份块与 anthropic-beta 头,上游要么 403
(only authorized for use with claude code),要么按「第三方应用」计费走 extra
usage 而不是订阅额度。所以 system 第一块固定是身份声明,调用方自己的 system
排在它后面。

刷新会**轮换 refresh_token**,新拿到的那份由 core.pool.refresh_fields 写回 secret。
"""
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from core.adapter import (BILL_UPSTREAM, CAP_CHAT, CAP_REFRESH, Adapter,
                          header_get, register_adapter, to_unix)

_ALLOWED_HOSTS = {"api.anthropic.com"}
_ERROR_BODY_LIMIT = 600

# 冒充 Claude Code 的客户端指纹。上游按这套头认「这是官方 CLI」,
# 换掉任何一个都可能被判成第三方应用。版本号会随上游轮转,过期表现为 403。
_CLI_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


def _models():
    return [m.strip() for m in str(config.CLAUDE_MODELS or "").split(",") if m.strip()]


def _cli_headers():
    """Claude Code 客户端会带的那一组头。每个请求现算,版本号取自配置。"""
    version = config.CLAUDE_CLI_VERSION
    return {
        "User-Agent": f"claude-cli/{version} (external, cli)",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.94.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "arm64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "600",
        "X-App": "cli",
    }


# OAuth 令牌必须走 Authorization: Bearer,并带上这个 beta 头 ——
# 用 x-api-key 发 OAuth 令牌上游一律 401。
_OAUTH_BETA = "oauth-2025-04-20"
# 除 oauth 外还要声明的特性。context-management 与 prompt-caching 上游有对应
# 的 body 字段,少声明会 400(声明了但 body 没带则无事)。
_CLI_BETAS = ("claude-code-20250219", _OAUTH_BETA,
              "interleaved-thinking-2025-05-14",
              "context-management-2025-06-27")


class ClaudeError(RuntimeError):
    """带 HTTP 状态码的上游错误,供 core.pool.classify_failure 按 code 分类。

    headers 也带出来:窗口打满时上游回 429,而最新的用量百分比就在 429 的
    响应头里(anthropic-ratelimit-unified-*)—— 不从这里取,面板会一直停在
    旧数上,而那个号其实已经调不动了。
    """

    def __init__(self, code, detail, headers=None):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail
        self.headers = headers


class ClaudeAdapter(Adapter):
    name = "claude"
    capabilities = [CAP_CHAT, CAP_REFRESH]
    models = _models()
    columns = [
        {"key": "meta.usage_5h", "label": "5h 窗口", "type": "window"},
        {"key": "meta.usage_7d", "label": "7d 窗口", "type": "window"},
        {"key": "token_exp", "label": "Token到期", "type": "time"},
        {"key": "meta.account_uuid", "label": "账号UUID", "type": "text"},
    ]
    refresh_interval = 21600 - config.CLAUDE_REFRESH_MARGIN
    billing_mode = BILL_UPSTREAM   # Messages API 返回真实 usage
    credentials_hint = ('Anthropic 订阅号凭据:{"refresh_token":"...",'
                        '"account_uuid":"..."(可选),"email":"..."}')

    # streaming=True:走 stream_chat,把上游 Anthropic SSE 翻成 OpenAI SSE。
    # 普通型(chat + on_token)那条路上工具调用与 thinking 都没有位置。
    streaming = True

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
    def _url(cls, acct):
        base = cls._secret(acct).get("base_url") or config.CLAUDE_BASE_URL
        parts = urllib.parse.urlsplit(str(base))
        if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
            raise ClaudeError(0, f"untrusted base_url: {parts.hostname}")
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")) + "/v1/messages"

    @staticmethod
    def _headers(acct, stream):
        h = dict(_cli_headers())
        h.update({
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": "Bearer " + str(acct.get("token") or ""),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": ",".join(_CLI_BETAS),
        })
        return h

    @staticmethod
    def _open(req, timeout):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace")
            except Exception:
                pass
            raise ClaudeError(e.code, body[:400], headers=e.headers) from e

    # ---- OpenAI ↔ Anthropic 协议转换 ----

    @staticmethod
    def _to_anthropic(messages, body):
        """OpenAI messages → (system 块, messages 数组, tools)。"""
        system_text, turns = [], []
        for m in messages or []:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content
                                  if isinstance(p, dict) and p.get("type") in
                                  ("text", "input_text"))
            content = "" if content is None else str(content)
            if role == "system":
                system_text.append(content)
            elif role == "tool":
                turns.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": content}]})
            elif role == "assistant":
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {"_raw": fn.get("arguments") or ""}
                    blocks.append({"type": "tool_use", "id": call.get("id") or "",
                                   "name": fn.get("name") or "", "input": args})
                if blocks:
                    turns.append({"role": "assistant", "content": blocks})
            elif role == "user":
                turns.append({"role": "user", "content": content})

        # 身份块必须在最前:它是订阅号认客户端的凭据,位置换了就不算 Claude Code
        system = [{"type": "text", "text": _CLI_IDENTITY}]
        if system_text:
            system.append({"type": "text", "text": "\n\n".join(system_text)})

        tools = []
        for t in (body or {}).get("tools") or []:
            fn = t.get("function") or {}
            if not fn.get("name"):
                continue
            tools.append({"name": fn["name"],
                          "description": fn.get("description") or "",
                          "input_schema": fn.get("parameters")
                          or {"type": "object", "properties": {}}})
        return system, turns, tools

    @staticmethod
    def _to_chat(msg, model):
        """Anthropic message → OpenAI chat.completion。

        thinking 块走 reasoning_content:bit-api 的 Anthropic 侧会把
        reasoning_content 翻回 thinking 事件,所以 thinking 能在两端都不丢。
        """
        text, reasoning, calls = [], [], []
        for block in msg.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text.append(block.get("text") or "")
            elif kind == "thinking":
                reasoning.append(block.get("thinking") or "")
            elif kind == "tool_use":
                calls.append({"id": block.get("id") or "", "type": "function",
                              "function": {
                                  "name": block.get("name") or "",
                                  "arguments": json.dumps(block.get("input") or {},
                                                          ensure_ascii=False)}})
        message = {"role": "assistant",
                   "content": "".join(text) or (None if calls else "")}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = calls
        stop = msg.get("stop_reason")
        finish = {"tool_use": "tool_calls", "max_tokens": "length"}.get(stop, "stop")
        return {"id": msg.get("id") or "", "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": ClaudeAdapter._usage_of(msg.get("usage") or {})}

    @staticmethod
    def _usage_of(u):
        """Anthropic usage → OpenAI usage。缓存读写分开给,计费层要用。"""
        cache_read = u.get("cache_read_input_tokens") or 0
        cache_write = u.get("cache_creation_input_tokens") or 0
        in_tok = (u.get("input_tokens") or 0) + cache_read + cache_write
        out_tok = u.get("output_tokens") or 0
        return {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
                "prompt_tokens_details": {"cached_tokens": cache_read},
                "cache_creation_input_tokens": cache_write}

    def _payload(self, acct, messages, model, stream, body=None):
        system, turns, tools = self._to_anthropic(messages, body)
        payload = {"model": model, "messages": turns, "system": system,
                   "stream": bool(stream),
                   "max_tokens": int((body or {}).get("max_tokens") or 8192)}
        if tools:
            payload["tools"] = tools
        for k in ("temperature", "top_p", "stop_sequences", "thinking",
                  "context_management"):
            if body and body.get(k) is not None:
                payload[k] = body[k]
        # metadata.user_id 是上游认「同一个会话」的锚点。account_uuid 必须来自
        # 本次实际发请求的那个号 —— 放进实例属性就是并发串号:两个请求同时在跑,
        # 后写的那个会把前一个的 uuid 覆盖掉,于是请求被记到别人的账号名下。
        meta = dict((body or {}).get("metadata") or {})
        if not meta.get("user_id"):
            uuid = str(self._secret(acct).get("account_uuid") or "")
            meta["user_id"] = json.dumps({"device_id": uuid or "bit-api",
                                          "account_uuid": uuid,
                                          "session_id": secrets.token_hex(16)})
        payload["metadata"] = meta
        return payload

    def _request(self, acct, model, messages, stream, body=None):
        payload = self._payload(acct, messages, model, stream, body)
        return urllib.request.Request(
            self._url(acct), data=json.dumps(payload).encode(), method="POST",
            headers=self._headers(acct, stream))

    # ---- 用量窗口 ----

    def window_meta(self, headers):
        """anthropic-ratelimit-unified-{5h,7d}-* → 两个窗口。

        上游给的是**分数**(0..1)而不是百分数,而且重置时间是一个时间戳字符串;
        两者都与 OpenAI 那边不同,归一都在这里做完,面板只认一种形状。
        `-surpassed-threshold: true` 表示这一档已经打穿 —— 用它把 pct 顶到 1,
        否则可能出现「显示 97% 但已经调不动」。
        """
        now = int(time.time())
        out = {"usage_updated_at": now}
        found = False
        for label, key in (("5h", "usage_5h"), ("7d", "usage_7d")):
            raw = header_get(headers, f"anthropic-ratelimit-unified-{label}-utilization")
            exceeded = str(header_get(
                headers, f"anthropic-ratelimit-unified-{label}-surpassed-threshold"
            ) or "").strip().lower() == "true"
            if raw is None and not exceeded:
                continue
            try:
                pct = max(0.0, min(1.0, float(raw)))
            except (TypeError, ValueError):
                pct = 0.0
            if exceeded:
                pct = 1.0
            win = {"pct": pct}
            reset = to_unix(header_get(
                headers, f"anthropic-ratelimit-unified-{label}-reset"))
            if reset:
                win["reset_at"] = reset
            out[key] = win
            found = True
        return out if found else {}

    # ---- 网关对话 ----

    def chat(self, acct, messages, stream=False, model=None, on_token=None,
             body=None):
        """非流式 → {"raw": OpenAI chat.completion}(含 tool_calls 与 usage)。"""
        model = model or (self.models or ["claude-sonnet-4-6"])[0]
        req = self._request(acct, model, messages, stream=False, body=body)
        with self._open(req, config.CLAUDE_TIMEOUT) as resp:
            # 窗口数据在响应头上,读 body 之前先取走
            meta = self.window_meta(resp.headers)
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        chat = self._to_chat(raw, model)
        return {"raw": chat,
                "content": (chat["choices"][0]["message"].get("content") or ""),
                "usage": chat.get("usage"), "meta": meta}

    def stream_chat(self, acct, model, body, on_sse):
        """流式:上游 Anthropic SSE → **OpenAI SSE** 逐帧转出去。

        网关按 OpenAI 帧嗅探 usage 与首字延迟,并靠 finish_reason / [DONE] 判
        流是否正常收尾。上游的事件名(content_block_delta 那一套)下游一个都不认得,
        所以这里是逐事件的翻译而不是透传。
        """
        model = model or (self.models or ["claude-sonnet-4-6"])[0]
        messages = body.get("messages") or []
        req = self._request(acct, model, messages, stream=True, body=body)
        cid = "chatcmpl-" + secrets.token_hex(12)
        created = int(time.time())
        # 上游按 content block 逐个发,块序号 ≠ 工具序号 —— 工具调用要单独编号,
        # 否则一轮里并行调两个工具时下游会把第二把的参数拼到第一把上
        tool_index = {}
        next_tool = 0
        finish = None
        sent_role = False

        def frame(delta=None, finish_reason=None, usage=None):
            o = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": model,
                 "choices": [{"index": 0, "delta": delta or {},
                              "finish_reason": finish_reason}]}
            if usage is not None:
                o["usage"] = usage
            on_sse("data: " + json.dumps(o, ensure_ascii=False) + "\n\n")

        with self._open(req, config.CLAUDE_TIMEOUT) as resp:
            meta = self.window_meta(resp.headers)
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    evt = json.loads(chunk)
                except Exception:
                    continue
                kind = evt.get("type")
                if kind == "message_start":
                    sent_role = True
                    frame({"role": "assistant"})
                elif kind == "content_block_start":
                    block = evt.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        idx = next_tool
                        next_tool += 1
                        tool_index[evt.get("index")] = idx
                        frame({"tool_calls": [{
                            "index": idx, "id": block.get("id") or "",
                            "type": "function",
                            "function": {"name": block.get("name") or "",
                                         "arguments": ""}}]})
                elif kind == "content_block_delta":
                    d = evt.get("delta") or {}
                    dtype = d.get("type")
                    if not sent_role:
                        sent_role = True
                        frame({"role": "assistant"})
                    if dtype == "text_delta" and d.get("text"):
                        frame({"content": d["text"]})
                    elif dtype == "thinking_delta" and d.get("thinking"):
                        frame({"reasoning_content": d["thinking"]})
                    elif dtype == "input_json_delta":
                        idx = tool_index.get(evt.get("index"), 0)
                        frame({"tool_calls": [{
                            "index": idx,
                            "function": {"arguments": d.get("partial_json") or ""}}]})
                elif kind == "message_delta":
                    d = evt.get("delta") or {}
                    if d.get("stop_reason"):
                        finish = {"tool_use": "tool_calls",
                                  "max_tokens": "length"}.get(d["stop_reason"], "stop")
                    if evt.get("usage"):
                        frame({}, finish_reason=finish or "stop",
                              usage=self._usage_of(evt["usage"]))
                elif kind == "error":
                    err = (evt.get("error") or {}).get("message") or "stream error"
                    raise ClaudeError(502, str(err)[:300])
        if finish is None:
            finish = "tool_calls" if tool_index else "stop"
        frame({}, finish_reason=finish)
        on_sse("data: [DONE]\n\n")
        return {"meta": meta} if meta else None

    # ---- 续期与导入 ----

    def refresh_token(self, acct):
        """用 refresh_token 换新 access_token。

        上游刷新时会**轮换 refresh_token**,新的那份必须回传(见
        core.pool.refresh_fields 的说明)。请求体是 JSON 而不是表单 ——
        这个端点与 OpenAI 那边不一样,发表单会 400。
        """
        sec = self._secret(acct)
        rt = str(sec.get("refresh_token") or "").strip()
        if not rt:
            raise ClaudeError(0, "no refresh_token in secret")
        payload = {"grant_type": "refresh_token", "refresh_token": rt,
                   "client_id": sec.get("client_id") or config.CLAUDE_CLIENT_ID}
        req = urllib.request.Request(
            config.CLAUDE_TOKEN_ENDPOINT, data=json.dumps(payload).encode(),
            method="POST", headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                # 上游的 OAuth 端点按这个 UA 认 claude.ai 控制台那条路
                "User-Agent": "axios/1.13.6",
            })
        with self._open(req, 60) as resp:
            got = json.loads(resp.read().decode("utf-8", "replace"))
        token = got.get("access_token")
        if not token:
            raise ClaudeError(0, "refresh returned no access_token")
        new_secret = dict(sec)
        if got.get("refresh_token"):
            new_secret["refresh_token"] = got["refresh_token"]
        account = got.get("account") or {}
        if account.get("uuid"):
            new_secret["account_uuid"] = account["uuid"]
        exp = int(time.time()) + int(got.get("expires_in") or 21600)
        return {"token": token, "token_exp": exp, "secret": new_secret}

    def import_account(self, db, item):
        """导入一个号 → (action, identity),action ∈ {"imported", "skipped"}。

        接受 Claude Code 的 OAuth 凭据:
          {"access_token", "refresh_token", "expires_at",
           "account_uuid"|"account.uuid", "email"|"organization_uuid"}

        **refresh_token 是必须的**:access_token 几小时就过期,这个渠道没有别的
        续期途径。凭据存 accounts.secret,库是唯一真源。
        """
        if not isinstance(item, dict):
            raise ValueError("account must be a JSON object")
        sec = item.get("secret") if isinstance(item.get("secret"), dict) else item
        refresh = str(sec.get("refresh_token") or "").strip()
        if not refresh:
            raise ValueError("refresh_token is required")
        account = sec.get("account") if isinstance(sec.get("account"), dict) else {}
        account_uuid = str(sec.get("account_uuid") or account.get("uuid") or "").strip()
        identity = str(item.get("identity") or sec.get("email")
                       or account.get("email_address") or account_uuid).strip()
        if not identity:
            raise ValueError("email / account_uuid is required")

        secret = {"refresh_token": refresh}
        if account_uuid:
            secret["account_uuid"] = account_uuid
        for k in ("client_id", "base_url"):
            if sec.get(k):
                secret[k] = sec[k]

        meta = {}
        org = sec.get("organization") if isinstance(sec.get("organization"), dict) else {}
        org_uuid = str(sec.get("organization_uuid") or org.get("uuid") or "").strip()
        if org_uuid:
            meta["organization_uuid"] = org_uuid
        if account_uuid:
            meta["account_uuid"] = account_uuid

        from core import db as dbmod
        existed = db.get_by_identity(self.name, identity) is not None
        acct_id = db.upsert_account(self.name, identity, secret=secret,
                                    meta=meta or None, status=dbmod.ST_ACTIVE)
        token = str(sec.get("access_token") or sec.get("token") or "").strip()
        # expires_at 可能是 unix 秒(数字或数字串),也可能是 ISO 串 —— 后者这里
        # 不解析,交给首次取号时的 refresh 去刷,比猜错时间安全。
        exp = sec.get("expires_at") or sec.get("token_exp") or 0
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            exp = 0
        upd = {}
        if token:
            upd["token"] = token
        if exp:
            upd["token_exp"] = exp
        if upd:
            db.update_account(acct_id, **upd)
        return ("skipped" if existed else "imported"), identity


register_adapter(ClaudeAdapter())
