#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openai adapter —— ChatGPT 订阅号池(Codex 那条 Responses 端点)。

自持型:凭据存 accounts.secret,续期自己打 auth.openai.com,对话自己转协议。

凭据形状(导入什么就存什么,库是唯一真源):
  {"refresh_token": "...",            # 必须
   "chatgpt_account_id": "...",       # 可选,有就带 x-chatgpt-account-id
   "client_id": "...",                # 可选,默认 Codex CLI 的公开 client id
   "base_url": "..."}                 # 可选,白名单校验

上游是 Responses API(不是 chat/completions),实测的对应关系:
  system 消息      → instructions 字段
  assistant 的工具调用 → 平铺成 function_call 项
  role=tool        → function_call_output 项
  usage            → 上游给真实值(含 cached/reasoning tokens),所以 billing_mode=upstream

刷新会**轮换 refresh_token**,新拿到的那份由 core.pool.refresh_fields 写回 secret ——
漏了下次刷新就用旧的,直接 invalid_grant,而且报错看上去像「上游挂了」。
"""
import base64
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from core.adapter import (BILL_UPSTREAM, CAP_CHAT, CAP_REFRESH, Adapter,
                          header_get, register_adapter, to_unix)

# 上游只认这两个域名。base_url 存在凭据里(每个号可能不同),用白名单挡住被改写的
# 凭据把 Bearer token 发到别处 —— 那是把号直接送人。
_ALLOWED_HOSTS = {"chatgpt.com", "api.openai.com"}
_AUTH_HOST = "auth.openai.com"
_ERROR_BODY_LIMIT = 600


def _models():
    return [m.strip() for m in str(config.OPENAI_MODELS or "").split(",") if m.strip()]


def _jwt_claims(token):
    """只解 JWT 的 payload 段读几个字段(不验签)。

    导入的凭据里 id_token 带着 chatgpt_account_id 和 email,那是 identity 与
    x-chatgpt-account-id 的唯一来源 —— 让人手工去别处抄一遍没有意义,而且抄错
    就变成「号在库里但每次请求 401」。验签不做:我们不是拿它做鉴权,只是读自己的
    那份凭据里本来就有的公开字段。
    """
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        return {}


def _iso_to_unix(text):
    """凭据里的过期时间可能是 ISO8601(可能带 Z),也可能是 unix 秒。"""
    return to_unix(text)


class OpenAIError(RuntimeError):
    """带 HTTP 状态码的上游错误,供 core.pool.classify_failure 按 code 分类。

    headers 也带出来:窗口打满时上游回 429,而最新的用量百分比就在 429 的
    响应头里 —— 不从这里取,面板会一直停在旧数上,而那个号其实已经调不动了。
    """

    def __init__(self, code, detail, headers=None):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail
        self.headers = headers


class OpenAIAdapter(Adapter):
    name = "openai"
    capabilities = [CAP_CHAT, CAP_REFRESH]
    models = _models()
    columns = [
        {"key": "meta.usage_5h", "label": "5h 窗口", "type": "window"},
        {"key": "meta.usage_7d", "label": "7d 窗口", "type": "window"},
        {"key": "token_exp", "label": "Token到期", "type": "time"},
        {"key": "meta.plan", "label": "套餐", "type": "text"},
    ]
    # access_token 实测几小时。刷新的周期取「有效期 - 提前量」,由巡检提前换掉。
    refresh_interval = 21600 - config.OPENAI_REFRESH_MARGIN
    billing_mode = BILL_UPSTREAM   # 上游返回真实 usage,不必 tiktoken 估算
    credentials_hint = ('ChatGPT 订阅号凭据:{"refresh_token":"...",'
                        '"id_token":"..."(可选,能从中读出账号 id 与套餐),'
                        '"email":"..."}')

    # streaming=True:走 stream_chat,把上游 Responses SSE 翻成 OpenAI SSE。
    # 走普通型(chat + on_token)的话工具调用没有位置 —— agent 客户端
    # (Codex CLI 全都在流式 + 工具)拿到的是没有 tool_calls 的回答。
    #
    # 不声明 stateless_keys:这个号有明确的本地状态可判 —— refresh_token 会失效、
    # 会被上游吊销。上游抖一下该轮换就轮换,号真坏了就该退场。
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
    def _base(cls, acct):
        base = cls._secret(acct).get("base_url") or config.OPENAI_BASE_URL
        parts = urllib.parse.urlsplit(str(base))
        if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
            raise OpenAIError(0, f"untrusted base_url: {parts.hostname}")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc,
                                        parts.path.rstrip("/"), "", ""))

    @staticmethod
    def _headers(acct, accept):
        sec = OpenAIAdapter._secret(acct)
        version = config.OPENAI_CLI_VERSION
        h = {
            "Content-Type": "application/json",
            "Accept": accept,
            "Authorization": "Bearer " + str(acct.get("token") or ""),
            # 上游按这几个头认「这是 Codex CLI」。少了会被当成第三方客户端拒掉。
            "originator": "codex_cli_rs",
            "version": version,
            "User-Agent": f"codex_cli_rs/{version} (Ubuntu 22.4.0; x86_64) xterm-256color",
        }
        acct_id = str(sec.get("chatgpt_account_id") or "").strip()
        if acct_id:
            h["chatgpt-account-id"] = acct_id
        return h

    @staticmethod
    def _open(req, timeout):
        """统一出口:把 HTTPError 翻成带 code 的 OpenAIError。

        code 必须留给 core.pool.classify_failure —— 它按 401/402/403 判 terminal,
        其余(超时、5xx、连接重置)判 transient。丢了 code 就只能靠字符串猜。
        """
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace")
            except Exception:
                pass
            raise OpenAIError(e.code, body[:400], headers=e.headers) from e

    # ---- OpenAI ↔ Responses 协议转换 ----

    @staticmethod
    def _to_input(messages):
        """OpenAI messages → Responses 的 (instructions, input)。

        system 走 instructions 而不是塞进 input:实测 input 里带 role=system
        不报错但不生效,提示词会被当普通用户消息。
        工具轮次要平铺成 function_call / function_call_output 两种独立项 ——
        Responses 里它们不是 message 的一种 role。
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
            elif role == "tool":
                items.append({"type": "function_call_output",
                              "call_id": m.get("tool_call_id") or "",
                              "output": content})
            elif role == "assistant":
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    items.append({"type": "function_call",
                                  "call_id": call.get("id") or "",
                                  "name": fn.get("name") or "",
                                  "arguments": fn.get("arguments") or "{}"})
            elif role == "user":
                items.append({"role": "user", "content": content})
        return instructions, items

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

    @staticmethod
    def _tool_calls_of(resp):
        calls = []
        for item in resp.get("output") or []:
            if item.get("type") != "function_call":
                continue
            calls.append({"id": item.get("call_id") or item.get("id") or "",
                          "type": "function",
                          "function": {"name": item.get("name") or "",
                                       "arguments": item.get("arguments") or "{}"}})
        return calls

    @staticmethod
    def _text_of(resp):
        out = []
        for item in resp.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    out.append(part.get("text") or "")
        return "".join(out)

    @staticmethod
    def _to_chat(resp, model):
        """Responses 响应 → OpenAI chat.completion。带 tool_calls 才是完整的。"""
        calls = OpenAIAdapter._tool_calls_of(resp)
        text = OpenAIAdapter._text_of(resp)
        message = {"role": "assistant", "content": text or (None if calls else "")}
        if calls:
            message["tool_calls"] = calls
        if calls:
            finish = "tool_calls"
        elif resp.get("status") == "incomplete":
            finish = "length"
        else:
            finish = "stop"
        return {"id": resp.get("id") or "", "object": "chat.completion",
                "created": int(resp.get("created_at") or time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": OpenAIAdapter._usage_of(resp)}

    # ---- 用量窗口 ----

    # 一次请求的响应头里带回来的窗口百分比。% 转分数;没有就是 0。
    @staticmethod
    def _pct(headers, name):
        raw = header_get(headers, name)
        if raw is None:
            return None
        try:
            return max(0.0, float(raw) / 100.0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _window(headers, prefix, now):
        """一组 x-codex-<prefix>-* 头 → {"pct","reset_at"}。没有百分比就不算有。"""
        pct = OpenAIAdapter._pct(headers, f"x-codex-{prefix}-used-percent")
        if pct is None:
            return None
        out = {"pct": pct}
        secs = header_get(headers, f"x-codex-{prefix}-reset-after-seconds")
        try:
            if secs is not None:
                out["reset_at"] = now + int(float(secs))
        except (TypeError, ValueError):
            pass
        return out

    def window_meta(self, headers):
        """x-codex-primary/secondary-* → 5h / 7d 两个窗口。

        上游给的是 primary/secondary 两个槽位,哪个是 5 小时是**按窗口时长判**的
        (`-window-minutes`,5h=300、7d=10080),不是固定 primary=5h —— 实测两种
        分配都出现过。两边都拿不到 window-minutes 时按 primary=5h 退化:
        这是有依据的猜测,而且猜错的后果只是面板上两条进度条对调,
        不会影响取号(窗口数据只用于展示)。
        """
        now = int(time.time())
        primary = self._window(headers, "primary", now)
        secondary = self._window(headers, "secondary", now)
        if primary is None and secondary is None:
            return {}

        def minutes(prefix):
            raw = header_get(headers, f"x-codex-{prefix}-window-minutes")
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return None

        pm, sm = minutes("primary"), minutes("secondary")
        if pm is not None and sm is not None:
            short, long_ = (("primary", "secondary") if pm < sm
                            else ("secondary", "primary"))
        else:
            short, long_ = "primary", "secondary"
        wins = {"primary": primary, "secondary": secondary}
        out = {"usage_updated_at": now}
        if wins.get(short):
            out["usage_5h"] = wins[short]
        if wins.get(long_):
            out["usage_7d"] = wins[long_]
        return out

    # ---- 网关对话 ----

    def _payload(self, messages, model, stream, body=None):
        instructions, items = self._to_input(messages)
        payload = {"model": model, "input": items, "stream": bool(stream),
                   # 订阅号这条路上游自己不存对话,不声明会被拒
                   "store": False}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        for k in ("tools", "tool_choice", "parallel_tool_calls", "reasoning",
                  "text", "temperature", "top_p", "max_output_tokens"):
            if body and body.get(k) is not None:
                payload[k] = body[k]
        return payload

    def _request(self, acct, model, messages, stream, body=None):
        payload = self._payload(messages, model, stream, body)
        accept = "text/event-stream" if stream else "application/json"
        return urllib.request.Request(
            self._base(acct) + "/responses", data=json.dumps(payload).encode(),
            method="POST", headers=self._headers(acct, accept))

    def chat(self, acct, messages, stream=False, model=None, on_token=None,
             body=None):
        """非流式 → {"raw": OpenAI chat.completion}(含 tool_calls 与 usage)。

        网关优先用 raw 原样返回给下游,所以这里必须转全 —— 少一个 tool_calls
        就是 agent 客户端收到一条「说了但没做」的回答。
        """
        model = model or (self.models or ["gpt-5.4"])[0]
        req = self._request(acct, model, messages, stream=False, body=body)
        with self._open(req, config.OPENAI_TIMEOUT) as resp:
            # 窗口数据在响应头上,读 body 之前先取走 —— body 读完之后头还在,
            # 但那一步已经跨进了解析逻辑,把两件事分开更不容易漏
            meta = self.window_meta(resp.headers)
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        chat = self._to_chat(raw, model)
        return {"raw": chat, "content": OpenAIAdapter._text_of(raw),
                "usage": chat.get("usage"), "meta": meta}

    def stream_chat(self, acct, model, body, on_sse):
        """流式:上游 Responses SSE → **OpenAI SSE** 逐帧转出去。

        网关(server._stream_sse)按 OpenAI 帧嗅探 usage 与首字延迟,并按
        stream_options.include_usage 的约定在末尾补一条带 usage 的帧。
        不能把上游帧原样透传 —— Responses 的事件名下游客户端一个都不认得。
        """
        model = model or (self.models or ["gpt-5.4"])[0]
        messages = body.get("messages") or []
        req = self._request(acct, model, messages, stream=True, body=body)
        # 一条流里所有帧同一个 id,与上游无关 —— 下游只要求帧内自洽
        cid = "chatcmpl-" + secrets.token_hex(12)
        created = int(time.time())
        # 工具调用的增量要按 index 归位:同一轮可能并行调多个工具,
        # 上游给的是 output_index,OpenAI 那边要的是 choices[].delta.tool_calls[].index
        call_index = {}
        usage = None
        finish = None

        def frame(delta=None, finish_reason=None, extra=None):
            o = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": model,
                 "choices": [{"index": 0, "delta": delta or {},
                              "finish_reason": finish_reason}]}
            if extra:
                o.update(extra)
            on_sse("data: " + json.dumps(o, ensure_ascii=False) + "\n\n")

        frame({"role": "assistant"})
        with self._open(req, config.OPENAI_TIMEOUT) as resp:
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
                kind = evt.get("type") or ""
                if kind == "response.output_text.delta":
                    if evt.get("delta"):
                        frame({"content": evt["delta"]})
                elif kind in ("response.reasoning_summary_text.delta",
                              "response.reasoning_text.delta"):
                    if evt.get("delta"):
                        frame({"reasoning_content": evt["delta"]})
                elif kind == "response.output_item.added":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = len(call_index)
                        call_index[evt.get("output_index", idx)] = idx
                        frame({"tool_calls": [{
                            "index": idx, "id": item.get("call_id") or "",
                            "type": "function",
                            "function": {"name": item.get("name") or "",
                                         "arguments": ""}}]})
                elif kind == "response.function_call_arguments.delta":
                    idx = call_index.get(evt.get("output_index"), 0)
                    frame({"tool_calls": [{"index": idx,
                                           "function": {"arguments": evt.get("delta") or ""}}]})
                elif kind == "response.completed":
                    done = evt.get("response") or {}
                    usage = self._usage_of(done)
                    finish = "tool_calls" if self._tool_calls_of(done) else "stop"
                elif kind in ("response.failed", "response.incomplete"):
                    done = evt.get("response") or {}
                    err = (done.get("error") or {}).get("message") or kind
                    raise OpenAIError(502, str(err)[:300])
        frame({}, finish_reason=finish or "stop",
              extra={"usage": usage} if usage else None)
        on_sse("data: [DONE]\n\n")
        return {"meta": meta} if meta else None

    # ---- 续期与导入 ----

    def refresh_token(self, acct):
        """用 refresh_token 换新 access_token。

        上游刷新时会**轮换 refresh_token**,新的那份必须回传(见 core.pool.refresh_fields
        的说明)—— 漏了下次刷新就用旧的,直接 invalid_grant。
        """
        sec = self._secret(acct)
        rt = str(sec.get("refresh_token") or "").strip()
        if not rt:
            raise OpenAIError(0, "no refresh_token in secret")
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": sec.get("client_id") or config.OPENAI_CLIENT_ID,
            "scope": "openid profile email",
        }).encode()
        req = urllib.request.Request(
            config.OPENAI_TOKEN_ENDPOINT, data=data, method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": f"codex-cli/{config.OPENAI_CLI_VERSION}",
            })
        with self._open(req, 60) as resp:
            got = json.loads(resp.read().decode("utf-8", "replace"))
        token = got.get("access_token")
        if not token:
            raise OpenAIError(0, "refresh returned no access_token")
        new_secret = dict(sec)
        if got.get("refresh_token"):
            new_secret["refresh_token"] = got["refresh_token"]
        # account_id 只在 id_token 里,刷新时顺手补齐 —— 缺了它上游不认这个号
        claims = _jwt_claims(got.get("id_token"))
        auth = claims.get("https://api.openai.com/auth") or {}
        if auth.get("chatgpt_account_id"):
            new_secret["chatgpt_account_id"] = auth["chatgpt_account_id"]
        exp = int(time.time()) + int(got.get("expires_in") or 21600)
        return {"token": token, "token_exp": exp, "secret": new_secret}

    def import_account(self, db, item):
        """导入一个号 → (action, identity),action ∈ {"imported", "skipped"}。

        接受 Codex CLI 的会话凭据(以及 sub2api / CPA 那种同形状的导出):
          {"access_token", "refresh_token", "id_token", "email"|"chatgpt_account_id",
           "expired"|"expires_at"}

        **refresh_token 是必须的**:只有 access_token 的号几小时后就失效,而本渠道
        没有别的续期途径,号会一直卡在取号失败上。凭据存 accounts.secret,
        库是唯一真源 —— 导入之后不再读任何外部文件。
        """
        if not isinstance(item, dict):
            raise ValueError("account must be a JSON object")
        sec = item.get("secret") if isinstance(item.get("secret"), dict) else item
        refresh = str(sec.get("refresh_token") or "").strip()
        if not refresh:
            raise ValueError("refresh_token is required")

        claims = _jwt_claims(sec.get("id_token"))
        auth = claims.get("https://api.openai.com/auth") or {}
        account_id = str(sec.get("chatgpt_account_id")
                         or auth.get("chatgpt_account_id") or "").strip()
        email = str(item.get("identity") or sec.get("email")
                    or claims.get("email") or "").strip()
        identity = email or account_id
        if not identity:
            raise ValueError("email / chatgpt_account_id is required")

        secret = {"refresh_token": refresh}
        if account_id:
            secret["chatgpt_account_id"] = account_id
        for k in ("client_id", "base_url"):
            if sec.get(k):
                secret[k] = sec[k]

        meta = {}
        plan = auth.get("chatgpt_plan_type") or sec.get("plan")
        if plan:
            meta["plan"] = str(plan)
        if account_id:
            meta["chatgpt_account_id"] = account_id

        from core import db as dbmod
        existed = db.get_by_identity(self.name, identity) is not None
        acct_id = db.upsert_account(self.name, identity, secret=secret,
                                    meta=meta or None, status=dbmod.ST_ACTIVE)
        token = str(sec.get("access_token") or sec.get("token") or "").strip()
        exp = _iso_to_unix(sec.get("expired") or sec.get("expires_at")
                           or sec.get("token_exp"))
        upd = {}
        if token:
            upd["token"] = token
        if exp:
            upd["token_exp"] = exp
        if upd:
            db.update_account(acct_id, **upd)
        return ("skipped" if existed else "imported"), identity


register_adapter(OpenAIAdapter())
