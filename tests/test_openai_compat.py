#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 OpenAI 兼容渠道的门禁。

守的是这条渠道形态的承诺:
  1. 流式原样透传 —— tool_calls 增量、usage、finish_reason 一个不丢,[DONE] 照传
  2. 只改 model 字段(上游名 → 对外名),改不了的帧原样放行
  3. 模型映射只在出站请求里发生;非流式 raw 的 model 也改回对外名
  4. 上游非 2xx 带状态码与响应体抛出,失败分类靠 .code
上游全部 mock,不出网。
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "oc.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

from adapters.openai_compat import OpenAICompatAdapter, UpstreamError  # noqa: E402
from core.pool import classify_failure  # noqa: E402


def _resp(payload):
    class _R:
        def read(self):
            return json.dumps(payload).encode()

        def close(self):
            pass
    return _R()


def _sse(lines):
    body = "".join(l + "\n\n" for l in lines).encode()

    class _R:
        def __init__(self):
            self.buf = io.BytesIO(body)
            self.closed = False

        def read1(self, n):
            return self.buf.read(n)

        def close(self):
            self.closed = True
    return _R()


def _adapter(**over):
    kw = dict(name="acme", base_url="https://api.acme.test/v1",
              models=["acme-fast", "acme-pro"], model_map={"acme-pro": "gpt-9-pro"},
              headers={"X-Org": "team-a"}, source="data")
    kw.update(over)
    return OpenAICompatAdapter(**kw)


class ConfigTest(unittest.TestCase):
    def test_declares_key_pool_shape(self):
        ad = _adapter()
        self.assertEqual(ad.name, "acme")
        self.assertTrue(ad.has("chat"))
        self.assertTrue(ad.stateless_keys)
        self.assertTrue(ad.streaming)
        self.assertEqual(ad.billing_mode, "upstream")
        self.assertEqual(ad.models, ["acme-fast", "acme-pro"])
        self.assertEqual(ad.source, "data")

    def test_url_and_headers(self):
        ad = _adapter(base_url="https://api.acme.test/v1/", chat_path="chat/completions")
        self.assertEqual(ad._url(), "https://api.acme.test/v1/chat/completions")
        h = ad._headers("K")
        self.assertEqual(h["Authorization"], "Bearer K")
        self.assertEqual(h["X-Org"], "team-a")

    def test_key_identity_prefixed_and_masked(self):
        ad = _adapter()
        i = ad.key_identity("sk-verysecret")
        self.assertTrue(i.startswith("acme-"))
        self.assertNotIn("secret", i)
        self.assertEqual(i, ad.key_identity("sk-verysecret"))
        self.assertNotEqual(i, _adapter(name="other").key_identity("sk-verysecret"))

    def test_model_map_applies_only_outbound(self):
        ad = _adapter()
        self.assertEqual(ad.upstream_model("acme-pro"), "gpt-9-pro")
        self.assertEqual(ad.upstream_model("acme-fast"), "acme-fast")
        p = ad._payload([{"role": "user", "content": "x"}], "acme-pro",
                        {"model": "acme-pro", "stream": True, "temperature": 0.2}, True)
        self.assertEqual(p["model"], "gpt-9-pro")
        self.assertTrue(p["stream_options"]["include_usage"])
        self.assertEqual(p["temperature"], 0.2)
        p2 = ad._payload([], "acme-pro", {"stream": True, "stream_options": {"include_usage": True}}, False)
        self.assertNotIn("stream", p2)          # 非流式把 stream 字段摘掉
        self.assertNotIn("stream_options", p2)


class NonStreamTest(unittest.TestCase):
    def test_raw_returned_with_model_relabelled(self):
        ad = _adapter()
        fake = {"id": "x", "model": "gpt-9-pro",
                "choices": [{"message": {"role": "assistant", "content": "hi",
                                         "tool_calls": [{"id": "c1", "type": "function",
                                                         "function": {"name": "f", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}}
        sent = {}

        def fake_post(body, key, timeout):
            sent.update(body)
            return _resp(fake)

        with mock.patch.object(ad, "_post", side_effect=fake_post):
            out = ad.chat({"secret": {"api_key": "K"}}, [{"role": "user", "content": "hi"}],
                          stream=False, model="acme-pro", body={"tools": [{"type": "function"}]})
        self.assertEqual(sent["model"], "gpt-9-pro")
        self.assertEqual(sent["tools"], [{"type": "function"}])     # tools 原样带过去
        self.assertEqual(out["raw"]["model"], "acme-pro")             # 回来改成对外名
        self.assertEqual(out["raw"]["choices"][0]["message"]["tool_calls"][0]["id"], "c1")
        self.assertEqual(out["raw"]["usage"]["prompt_tokens"], 3)

    def test_http_error_carries_code_and_body(self):
        ad = _adapter()
        err = urllib.error.HTTPError("https://api.acme.test/v1/chat/completions", 401, "Unauthorized",
                                     {}, io.BytesIO(b'{"error":{"message":"invalid api key"}}'))
        with mock.patch("adapters.openai_compat.urllib.request.urlopen", side_effect=err):
            with self.assertRaises(UpstreamError) as ctx:
                ad._post({"model": "m"}, "K", 5)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("invalid api key", str(ctx.exception))
        # 失败分类靠 .code:401 是 key 坏了(terminal),不是上游抖动
        self.assertEqual(classify_failure(ctx.exception), "terminal")
        err5 = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"upstream down"))
        with mock.patch("adapters.openai_compat.urllib.request.urlopen", side_effect=err5):
            with self.assertRaises(UpstreamError) as ctx:
                ad._post({"model": "m"}, "K", 5)
        self.assertEqual(classify_failure(ctx.exception), "transient")


class StreamTest(unittest.TestCase):
    LINES = [
        'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}',
        'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}',
        'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"SH\\"}"}}]}}]}',
        'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"id":"c","model":"gpt-9-pro","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}',
        "data: [DONE]",
    ]

    def test_stream_chat_passes_tool_calls_usage_and_done(self):
        ad = _adapter()
        got = []
        sent = {}

        def fake_post(body, key, timeout):
            sent.update(body)
            return _sse(self.LINES)

        with mock.patch.object(ad, "_post", side_effect=fake_post):
            ad.stream_chat({"secret": {"api_key": "K"}}, "acme-pro",
                           {"model": "acme-pro", "messages": [{"role": "user", "content": "w"}],
                            "stream": True, "tools": [{"type": "function", "function": {"name": "get_weather"}}]},
                           on_sse=got.append)
        self.assertEqual(sent["model"], "gpt-9-pro")
        self.assertTrue(sent["stream_options"]["include_usage"])
        self.assertEqual(len(got), 6)
        self.assertTrue(all(x.startswith("data: ") and x.endswith("\n\n") for x in got))
        self.assertEqual(got[-1], "data: [DONE]\n\n")
        frames = [json.loads(x[6:]) for x in got[:-1]]
        # model 改回对外名,其余不动
        self.assertTrue(all(f["model"] == "acme-pro" for f in frames))
        self.assertEqual(frames[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(frames[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '{"city":"SH"}')
        self.assertEqual(frames[3]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(frames[4]["usage"]["total_tokens"], 18)

    def test_stream_chat_leaves_unparseable_frames_alone(self):
        ad = _adapter()
        got = []
        with mock.patch.object(ad, "_post", return_value=_sse(["data: not-json {", ": comment", "data: [DONE]"])):
            ad.stream_chat({"secret": {"api_key": "K"}}, "acme-fast",
                           {"model": "acme-fast", "messages": []}, on_sse=got.append)
        self.assertEqual(got, ["data: not-json {\n\n", "data: [DONE]\n\n"])

    def test_text_channel_still_works_for_callers_that_want_text(self):
        ad = _adapter()
        seen = []
        lines = ['data: {"choices":[{"delta":{"reasoning_content":"think"}}]}',
                 'data: {"choices":[{"delta":{"content":"hi"}}]}',
                 'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}',
                 "data: [DONE]"]
        with mock.patch.object(ad, "_post", return_value=_sse(lines)):
            out = ad.chat({"secret": {"api_key": "K"}}, [], stream=True, model="acme-fast",
                          on_token=lambda t, k: seen.append((k, t)))
        self.assertIsNone(out)
        self.assertEqual(seen, [("reasoning", "think"), ("content", "hi")])

    def test_response_closed_even_when_reader_raises(self):
        ad = _adapter()

        class _Boom:
            closed = False

            def read1(self, n):
                raise OSError("reset")

            def close(self):
                self.closed = True

        r = _Boom()
        with mock.patch.object(ad, "_post", return_value=r):
            with self.assertRaises(OSError):
                ad.stream_chat({"secret": {"api_key": "K"}}, "acme-fast",
                               {"model": "acme-fast", "messages": []}, on_sse=lambda x: None)
        self.assertTrue(r.closed)


if __name__ == "__main__":
    unittest.main()
