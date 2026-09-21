#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用 OpenAI 兼容渠道 —— key 池型透传。

一个类管两种来源:
  - 数据渠道:管理台「渠道」页建一条(base_url / 模型清单 / 模型映射 / 额外头),
    core/channels.py 把它实例化并注册,存库即生效,不写代码不重启
  - 代码渠道:有固定上游地址与默认模型的,继承本类只填几个类属性

流式走 streaming=True 的 stream_chat:把上游 SSE **原样**透传(只把 model 字段改回
对外名),tool_calls / usage / finish_reason 一个不丢。此前 key 池渠道走的是
chat(stream=True, on_token=...) 那条文本通道,只有 content / reasoning 两种 kind
—— 工具调用的增量在那条路上没有位置,agent 客户端(全都流式 + 工具)拿到的是没有
tool_calls 的回答。非流式返回 {"raw": 上游完整 JSON},本来就完整。

错误带上游状态码与响应体前几百字:core/pool.classify_failure 靠 .code 分「key 坏了」
与「上游此刻不好」,管理台「测试渠道」靠正文告诉站长到底哪里填错了。
"""
import hashlib
import json
import urllib.error
import urllib.request

import config
from core import sse
from core.adapter import BILL_UPSTREAM, CAP_CHAT, Adapter

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")
ERROR_BODY_LIMIT = 600


class UpstreamError(RuntimeError):
    """上游非 2xx。code 给失败分类用,body 给人看。"""

    def __init__(self, code, body, url=""):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code} from upstream: {body[:ERROR_BODY_LIMIT]}"
                         + (f" ({url})" if url else ""))


class OpenAICompatAdapter(Adapter):
    capabilities = [CAP_CHAT]
    billing_mode = BILL_UPSTREAM
    # key 长期有效、无 token / 余额 / 配额 —— 没有本地状态可判,上游抖动只轮换不罚号
    stateless_keys = True
    streaming = True
    columns = [
        {"key": "identity", "label": "Key", "type": "text"},
        {"key": "status", "label": "状态", "type": "text"},
    ]

    # ---- 子类 / 实例可覆盖 ----
    api_base = ""                    # 如 https://api.example.com/v1
    chat_path = "/chat/completions"  # 拼在 api_base 后面
    user_agent = UA
    default_models = []
    key_prefix = None                # identity 前缀,默认用 name
    source = "code"                  # code | data(管理台建的)

    def __init__(self, name=None, base_url=None, models=None, model_map=None,
                 headers=None, timeout=None, key_prefix=None, chat_path=None,
                 source=None, notes=None):
        if name:
            self.name = name
        if base_url is not None:
            self.api_base = base_url.rstrip("/")
        if chat_path:
            self.chat_path = chat_path if chat_path.startswith("/") else "/" + chat_path
        self.models = list(models) if models is not None else list(self.default_models)
        # 对外名 → 上游名。没配的按同名透传。
        self.model_map = dict(model_map or {})
        self.extra_headers = dict(headers or {})
        self.timeout = int(timeout) if timeout else 0
        if key_prefix:
            self.key_prefix = key_prefix
        if source:
            self.source = source
        self.notes = notes or ""

    # ---- 号池 ----

    def key_identity(self, key):
        """由 key 派生稳定、脱敏的 identity。前缀按渠道:换端点实现后同一把 key 不能
        再进一个新账号,否则去重失效。"""
        prefix = self.key_prefix or self.name
        return f"{prefix}-" + hashlib.sha1(key.encode()).hexdigest()[:16]

    def _api_key(self, acct):
        sec = acct.get("secret") or {}
        key = sec.get("api_key") or acct.get("token") or ""
        if not key:
            raise RuntimeError("account missing api_key")
        return key

    def decorate_accounts(self, accts):
        for a in accts:
            a.pop("token", None)  # 面板不显示 key 明文

    # ---- 请求 ----

    def upstream_model(self, model):
        return self.model_map.get(model, model)

    def _url(self):
        return self.api_base + self.chat_path

    def _headers(self, api_key):
        h = {"Authorization": f"Bearer {api_key}",
             "Content-Type": "application/json",
             "Accept": "text/event-stream, application/json",
             "User-Agent": self.user_agent}
        h.update(self.extra_headers)
        return h

    def _timeout(self):
        return self.timeout or config.SEND_TIMEOUT

    def _post(self, body, api_key, timeout):
        req = urllib.request.Request(
            self._url(), data=json.dumps(body).encode(), method="POST",
            headers=self._headers(api_key))
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                text = e.read(ERROR_BODY_LIMIT).decode("utf-8", "replace")
            except Exception:
                text = ""
            raise UpstreamError(e.code, text or e.reason or "", self._url()) from e

    def _payload(self, messages, model, body, stream):
        payload = dict(body or {})
        payload["model"] = self.upstream_model(model or (self.models[0] if self.models else ""))
        payload.setdefault("messages", messages)
        if stream:
            payload["stream"] = True
            # 强开 include_usage:客户端没要也要,否则流式末尾没有真实 token 数
            opts = dict(payload.get("stream_options") or {})
            opts["include_usage"] = True
            payload["stream_options"] = opts
        else:
            payload.pop("stream", None)
            payload.pop("stream_options", None)
        return payload

    # ---- 对话 ----

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        """非流式 → {"raw": 上游完整 OpenAI JSON},model 字段改回对外名。
        流式(文本通道)→ 逐段调 on_token(text, kind);网关不走这条,streaming=True
        让它走 stream_chat。留着是给「只要文字」的调用方(体验页之外的工具、测试)。"""
        api_key = self._api_key(acct)
        payload = self._payload(messages, model, body, stream)
        upstream = self._post(payload, api_key, self._timeout())

        if not stream:
            try:
                raw = json.loads(upstream.read().decode("utf-8", "replace"))
            finally:
                upstream.close()
            if isinstance(raw, dict) and model:
                raw["model"] = model
            return {"raw": raw}

        if on_token is None:
            on_token = lambda t, k="content": None  # noqa: E731
        try:
            for raw in sse.iter_sse_lines(upstream, self._timeout()):
                data = sse.data_of(raw)
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except Exception:
                    continue
                choices = evt.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # 同一渠道里两种字段名都出现过:标准的 reasoning_content 与非标的 reasoning
                for kind, field in (("content", "content"),
                                    ("reasoning", "reasoning_content"),
                                    ("reasoning", "reasoning")):
                    tok = delta.get(field)
                    if tok:
                        on_token(tok, kind)
        finally:
            upstream.close()
        return None

    def stream_chat(self, acct, model, body, on_sse):
        """流式:上游 SSE 原样透传,每个 data 事件一条 "data: ...\\n\\n"。

        只改一处:把 model 字段从上游名改回对外名 —— 对外名是这站的产品口径,上游名
        是供应商的。其余(tool_calls 增量、usage、finish_reason)一字不动。[DONE] 也
        照传,server 靠它判流完整。"""
        api_key = self._api_key(acct)
        payload = self._payload(body.get("messages") or [], model, body, True)
        upstream = self._post(payload, api_key, self._timeout())
        try:
            for raw in sse.iter_sse_lines(upstream, self._timeout()):
                data = sse.data_of(raw)
                if data is None or data == "":
                    continue
                if data == "[DONE]":
                    on_sse("data: [DONE]\n\n")
                    continue
                on_sse("data: " + _relabel(data, model) + "\n\n")
        finally:
            upstream.close()
        return None

    # ---- 向量 ----

    embeddings_path = "/embeddings"

    def embeddings(self, acct, body):
        """POST /embeddings 原样转,model 映射到上游名、回来改成对外名。
        上游不支持会回 404 / 400,带原话抛出 —— 管理台测试与调用方都看得到。"""
        api_key = self._api_key(acct)
        model = body.get("model", "")
        payload = dict(body)
        payload["model"] = self.upstream_model(model)
        req = urllib.request.Request(
            self.api_base + self.embeddings_path, data=json.dumps(payload).encode(),
            method="POST", headers=self._headers(api_key))
        try:
            upstream = urllib.request.urlopen(req, timeout=self._timeout())
        except urllib.error.HTTPError as e:
            try:
                text = e.read(ERROR_BODY_LIMIT).decode("utf-8", "replace")
            except Exception:
                text = ""
            raise UpstreamError(e.code, text or e.reason or "",
                                self.api_base + self.embeddings_path) from e
        try:
            raw = json.loads(upstream.read().decode("utf-8", "replace"))
        finally:
            upstream.close()
        if isinstance(raw, dict):
            _normalize_embedding_usage(raw)
            if model:
                raw["model"] = model
        return raw

    # ---- 管理台 ----

    def describe(self):
        return {"base_url": self.api_base, "chat_path": self.chat_path,
                "model_map": dict(self.model_map), "headers": dict(self.extra_headers),
                "timeout": self.timeout, "notes": self.notes, "source": self.source}


def _normalize_embedding_usage(raw):
    """把「只报 total_tokens」的上游响应补成计量层认得的形状。

    embeddings 没有输出 token —— 上游报的 token 全都是输入量。但计量层
    (core/metering.resolve_usage)那条「只有 total 就整个记到 output」的分支是为
    chat 写的(只报 total 的渠道,total 基本是生成量),照它走 embedding 会全落到
    output 上;而向量模型的 output 价是 0,一次调用就被记成 $0。

    只回 {"total_tokens": N} 的向量上游就是这么踩的。已有 prompt_tokens /
    completion_tokens 的响应一字不动,所以对官方 OpenAI 一类上游无影响。
    """
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        return
    total = usage.get("total_tokens")
    if total:
        usage["prompt_tokens"] = int(total)
        usage["completion_tokens"] = 0


def _relabel(data, model):
    """把 SSE 帧里的 model 改回对外名。不是 JSON 或没有 model 字段就原样返回,
    绝不为了改名把帧弄坏。"""
    if not model or '"model"' not in data:
        return data
    try:
        o = json.loads(data)
    except Exception:
        return data
    if isinstance(o, dict) and o.get("model") != model:
        o["model"] = model
        return json.dumps(o, ensure_ascii=False)
    return data
