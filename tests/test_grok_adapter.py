#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grok adapter 的门禁 —— 协议转换、失败分类、续期轮换。

这里断言的都是「弄错了会静默出事」的点:system 塞进 input 不报错但提示词失效;
refresh_token 轮换没写回下次就 invalid_grant;402 判成 dead 会把还能用的号清掉。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "grok.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

import config  # noqa: E402
from adapters.grok import GrokAdapter, GrokError  # noqa: E402
from core.pool import classify_failure  # noqa: E402


class _Resp:
    """够用的 urlopen 返回值替身:非流式走 read(),流式走迭代。"""

    def __init__(self, payload=None, lines=None):
        self._body = json.dumps(payload).encode() if payload is not None else b""
        self._lines = lines or []

    def read(self, *a):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _acct(**over):
    a = {"id": 1, "channel": "grok", "identity": "a@example.com",
         "token": "tok", "token_exp": 0,
         "secret": {"refresh_token": "rt-old",
                    "base_url": "https://cli-chat-proxy.grok.com/v1"}}
    a.update(over)
    return a


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.ad = GrokAdapter()

    def test_outbound_model_name_matches_upstream_id(self):
        """对外名默认与上游 id 同名(前缀留空),原样发上去。"""
        p = self.ad._to_payload([{"role": "user", "content": "hi"}],
                                "grok-4.6", False)
        self.assertEqual(p["model"], "grok-4.6")

    def test_configured_prefix_is_stripped_for_upstream(self):
        """配了对外前缀时(如 xai-)必须剥掉再发,上游不认前缀,漏剥就是 404。"""
        with mock.patch.object(config, "GROK_MODEL_PREFIX", "xai-"):
            p = self.ad._to_payload([{"role": "user", "content": "hi"}],
                                    "xai-grok-4.6", False)
        self.assertEqual(p["model"], "grok-4.6")

    def test_system_goes_to_instructions_not_input(self):
        """实测:input 里带 role=system 不报错但提示词不生效,静默失效最难查。"""
        p = self.ad._to_payload([
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"}], "grok-4.6", False)
        self.assertEqual(p["instructions"], "be terse")
        self.assertEqual(p["input"], [{"role": "user", "content": "hi"}])

    def test_multiple_system_messages_are_joined(self):
        p = self.ad._to_payload([
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "hi"}], "grok-4.6", False)
        self.assertEqual(p["instructions"], "a\n\nb")

    def test_multimodal_content_blocks_flattened_to_text(self):
        """content 是分块数组时只取文本块 —— 实测分块形式发上去会挂住不返回。"""
        p = self.ad._to_payload([{"role": "user", "content": [
            {"type": "text", "text": "one"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            {"type": "input_text", "text": "two"}]}], "grok-4.6", False)
        self.assertEqual(p["input"], [{"role": "user", "content": "onetwo"}])

    def test_untrusted_base_url_rejected(self):
        """凭据里的 base_url 被改写就等于把 Bearer token 送给别人。"""
        with self.assertRaises(GrokError):
            self.ad._url(_acct(secret={"base_url": "https://evil.example/v1"}),
                         "/responses")


_NONSTREAM = {
    "model": "grok-4.6", "status": "completed",
    "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "hello"}]}],
    "usage": {"input_tokens": 209, "output_tokens": 300, "total_tokens": 509,
              "input_tokens_details": {"cached_tokens": 192},
              "output_tokens_details": {"reasoning_tokens": 299}},
}


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.ad = GrokAdapter()

    def test_nonstream_extracts_text_and_real_usage(self):
        """usage 采信上游(billing_mode=upstream),这样热路径上不必跑 tiktoken。"""
        with mock.patch.object(GrokAdapter, "_open",
                               return_value=_Resp(payload=_NONSTREAM)):
            got = self.ad.chat(_acct(), [{"role": "user", "content": "hi"}],
                               model="grok-4.6")
        self.assertEqual(got["content"], "hello")
        u = got["usage"]
        self.assertEqual((u["prompt_tokens"], u["completion_tokens"],
                          u["total_tokens"]), (209, 300, 509))
        self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 192)
        self.assertEqual(
            u["completion_tokens_details"]["reasoning_tokens"], 299)

    def test_stream_only_reads_output_text_delta(self):
        """状态机事件(created/in_progress/item.*/part.*)不能当内容吐给客户端。"""
        def sse(obj):
            return ("data: " + json.dumps(obj) + "\n").encode()
        lines = [
            sse({"type": "response.created", "response": {"id": "r1"}}),
            sse({"type": "response.in_progress", "response": {"id": "r1"}}),
            sse({"type": "response.output_item.added", "item": {"id": "m1"}}),
            sse({"type": "response.content_part.added",
                 "part": {"type": "output_text", "text": ""}}),
            sse({"type": "response.output_text.delta", "delta": "he"}),
            sse({"type": "response.output_text.delta", "delta": "llo"}),
            sse({"type": "response.output_text.done", "text": "hello"}),
            sse({"type": "response.completed", "response": _NONSTREAM}),
            b"data: [DONE]\n",
        ]
        seen = []
        with mock.patch.object(GrokAdapter, "_open",
                               return_value=_Resp(lines=lines)):
            got = self.ad.chat(_acct(), [{"role": "user", "content": "hi"}],
                               stream=True, model="grok-4.6",
                               on_token=lambda t, k: seen.append((t, k)))
        self.assertEqual(seen, [("he", "content"), ("llo", "content")])
        self.assertEqual(got["content"], "hello")
        self.assertEqual(got["usage"]["total_tokens"], 509)


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.ad = GrokAdapter()

    def test_rotated_refresh_token_is_written_back(self):
        """xAI 刷新时轮换 refresh_token。不写回,下次刷新必然 invalid_grant。"""
        with mock.patch.object(GrokAdapter, "_open", return_value=_Resp(payload={
                "access_token": "tok-new", "refresh_token": "rt-new",
                "expires_in": 21600})):
            got = self.ad.refresh_token(_acct())
        self.assertEqual(got["token"], "tok-new")
        self.assertEqual(got["secret"]["refresh_token"], "rt-new")
        self.assertGreater(got["token_exp"], 0)

    def test_old_refresh_token_kept_when_upstream_omits_it(self):
        with mock.patch.object(GrokAdapter, "_open", return_value=_Resp(payload={
                "access_token": "tok-new", "expires_in": 21600})):
            got = self.ad.refresh_token(_acct())
        self.assertEqual(got["secret"]["refresh_token"], "rt-old")

    def test_missing_refresh_token_raises(self):
        with self.assertRaises(GrokError):
            self.ad.refresh_token(_acct(secret={}))


class FailureClassifyTest(unittest.TestCase):
    """402 spending-limit 必须能被 core.pool 认出来,否则号会被错判。"""

    def test_402_is_terminal_not_transient(self):
        self.assertEqual(
            classify_failure(GrokError(402, "personal-team-blocked:spending-limit")),
            "terminal")

    def test_401_is_terminal(self):
        self.assertEqual(classify_failure(GrokError(401, "invalid token")),
                         "terminal")

    def test_500_is_transient(self):
        """上游 5xx 只是这次不行 —— 判 terminal 会把好号踢出池子。"""
        self.assertEqual(classify_failure(GrokError(500, "internal")), "transient")

    def test_timeout_is_transient(self):
        self.assertEqual(classify_failure(TimeoutError("read timed out")),
                         "transient")


class RegistryTest(unittest.TestCase):
    def test_registered_models_route_to_grok(self):
        """声明出去的模型必须都能路由回本渠道 —— 挂上去却路由不到的模型,
        用户看得见、调不通。"""
        from core.adapter import model_to_channel
        m = model_to_channel()
        self.assertTrue(GrokAdapter.models)
        for name in GrokAdapter.models:
            self.assertEqual(m.get(name), "grok")


if __name__ == "__main__":
    unittest.main()
