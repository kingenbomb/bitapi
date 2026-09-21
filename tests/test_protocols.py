#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端协议覆盖面的门禁:count_tokens、Anthropic 图片 block、/v1/embeddings、/v1/responses。

每一条都对应一种真实客户端的调用方式:Claude Code 每轮打 count_tokens 并发图;
RAG 用户打 embeddings;Codex CLI 与新 SDK 默认走 responses(流式 + function tools)。
上游用 mock 顶掉,不出网。
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "pr.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from core import metering  # noqa: E402
from core import pool_state  # noqa: E402
from core.adapter import register_adapter, unregister_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SENT = []
TOOL_STREAM = [
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {"content": "Let me check."}}]},
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_7", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]}}]},
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"city\":"}}]}}]},
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"SH\"}"}}]}}]},
    {"id": "c", "model": "up-x", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "c", "model": "up-x", "choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}},
]
NONSTREAM = {"id": "n", "model": "up-x",
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "Sunny in SH."}}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}


def fake_embed_urlopen(req, timeout=None):
    """顶掉 urllib.urlopen:记录发到上游的 embeddings 请求体,按 FakeUp.embed_fail 决定回什么。
    走 HTTP 层而不是覆盖 embeddings():模型映射与错误包装在真方法里,得让它跑到。"""
    body = json.loads(req.data.decode())
    SENT.append(("embed", body, req.full_url, dict(req.header_items())))
    if FakeUp.embed_fail:
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, FakeUp.embed_fail, "boom", {},
                                     io.BytesIO(b'{"error":"embed boom"}'))
    n = len(body["input"]) if isinstance(body["input"], list) else 1
    out = {"object": "list", "model": body["model"],
           "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2]} for i in range(n)],
           "usage": {"prompt_tokens": 8 * n, "total_tokens": 8 * n}}

    class _R:
        def read(self):
            return json.dumps(out).encode()

        def close(self):
            pass
    return _R()


class FakeUp(OpenAICompatAdapter):
    embed_fail = None

    def _post(self, body, api_key, timeout):
        SENT.append(("chat", body))
        if body.get("stream"):
            buf = io.BytesIO("".join("data: " + json.dumps(f) + "\n\n" for f in TOOL_STREAM).encode()
                             + b"data: [DONE]\n\n")

            class _S:
                def read1(_self, n):
                    return buf.read(n)

                def close(_self):
                    pass
            return _S()

        class _J:
            def read(_self):
                return json.dumps(NONSTREAM).encode()

            def close(_self):
                pass
        return _J()


def _db():
    return portal_state.USER_DB


class ProtocolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "pr-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh, pricing=portal_state.PRICING)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@example.com", "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.uid = r.json()["user_id"]
        h = {"Authorization": "Bearer " + r.json()["token"]}
        cls.sk = cls.client.post("/api/keys", json={"name": "t"}, headers=h).json()["key"]
        cls.h = {"Authorization": "Bearer " + cls.sk}
        cls.ad = FakeUp(name="pr-up", base_url="https://up.test/v1",
                        models=["pr-chat", "pr-embed"], model_map={"pr-chat": "up-x", "pr-embed": "up-e"})
        register_adapter(cls.ad)
        pool_state.DB.upsert_account("pr-up", "k1", secret={"api_key": "K"}, status="active")

    @classmethod
    def tearDownClass(cls):
        unregister_adapter("pr-up")
        pool_state.DB.delete_channel("pr-up")

    def setUp(self):
        SENT.clear()
        FakeUp.embed_fail = None
        self._p = mock.patch("adapters.openai_compat.urllib.request.urlopen", fake_embed_urlopen)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _log(self):
        return _db().recent_usage(self.uid, limit=1)[0]

    # ---- count_tokens ----

    def test_count_tokens_matches_estimator_and_needs_auth(self):
        body = {"model": "pr-chat", "system": "be brief",
                "messages": [{"role": "user", "content": "hello there"}],
                "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]}
        r = self.client.post("/v1/messages/count_tokens", json=body, headers={"x-api-key": self.sk})
        self.assertEqual(r.status_code, 200, r.text)
        n = r.json()["input_tokens"]
        expect = (metering.estimate_usage([{"role": "system", "content": "be brief"},
                                           {"role": "user", "content": "hello there"}], "")["prompt_tokens"]
                  + metering.count_tokens(json.dumps(body["tools"], ensure_ascii=False)))
        self.assertEqual(n, expect)
        self.assertGreater(n, 0)
        self.assertEqual(self.client.post("/v1/messages/count_tokens", json=body,
                                          headers={"x-api-key": "sk-bad"}).status_code, 401)
        self.assertEqual(SENT, [])                                    # 不问上游
        self.assertEqual(_db().recent_usage(self.uid, limit=1), [])   # 不计费

    # ---- 图片 block ----

    def test_anthropic_image_blocks_become_image_url_parts(self):
        body = {"model": "pr-chat", "max_tokens": 10,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                    {"type": "image", "source": {"type": "url", "url": "https://img.test/a.jpg"}},
                    {"type": "image", "source": {"type": "weird"}}]}]}
        out = server._anthropic_to_openai(body)
        content = out["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "what is this"})
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,AAAA")
        self.assertEqual(content[2]["image_url"]["url"], "https://img.test/a.jpg")
        self.assertEqual(len(content), 3)                 # 认不出的 source 跳过,不造坏 part
        # 纯文字仍是字符串:逆向渠道只认字符串
        plain = server._anthropic_to_openai({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]})
        self.assertEqual(plain["messages"][0]["content"], "ab")

    # ---- embeddings ----

    def test_embeddings_routes_maps_and_bills(self):
        r = self.client.post("/v1/embeddings", headers=self.h,
                             json={"model": "pr-embed", "input": ["hello", "world"]})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["model"], "pr-embed")
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(SENT[-1][0], "embed")
        self.assertEqual(SENT[-1][1]["model"], "up-e")                       # 出站映射到上游名
        self.assertEqual(SENT[-1][2], "https://up.test/v1/embeddings")
        self.assertEqual(SENT[-1][3].get("Authorization"), "Bearer K")
        log = self._log()
        self.assertEqual((log["channel"], log["model"], log["input_tokens"], log["output_tokens"],
                          log["token_source"]), ("pr-up", "pr-embed", 16, 0, "upstream"))

    def test_embeddings_errors(self):
        self.assertEqual(self.client.post("/v1/embeddings", headers=self.h,
                                          json={"model": "pr-embed"}).status_code, 400)
        self.assertEqual(self.client.post("/v1/embeddings", headers=self.h,
                                          json={"model": "ghost", "input": "x"}).status_code, 404)
        FakeUp.embed_fail = 429
        r = self.client.post("/v1/embeddings", headers=self.h, json={"model": "pr-embed", "input": "x"})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("embed boom", r.text)          # 上游原话带出来
        # 限流不罚号:key 还在 active
        self.assertEqual(pool_state.DB.list_accounts(channel="pr-up")[0]["status"], "active")

    def test_embeddings_unsupported_channel(self):
        from core.adapter import CAP_CHAT, Adapter

        class NoEmbed(Adapter):
            name = "pr-noembed"
            capabilities = [CAP_CHAT]
            models = ["pr-noembed-model"]

        register_adapter(NoEmbed())
        pool_state.DB.upsert_account("pr-noembed", "k", secret={"api_key": "K"}, status="active")
        try:
            r = self.client.post("/v1/embeddings", headers=self.h,
                                 json={"model": "pr-noembed-model", "input": "x"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("does not support embeddings", r.text)
        finally:
            unregister_adapter("pr-noembed")
            pool_state.DB.delete_channel("pr-noembed")

    # ---- responses ----

    def test_responses_request_translation(self):
        body = {"model": "pr-chat", "instructions": "You are terse.",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "weather?"},
                                                 {"type": "input_image", "image_url": "https://i.test/x.png"}]},
                    {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{\"city\":\"SH\"}"},
                    {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
                    {"type": "reasoning", "summary": []},
                    {"role": "developer", "content": "dev note"},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
                "tools": [{"type": "function", "name": "get_weather", "description": "d",
                           "parameters": {"type": "object"}, "strict": True}],
                "tool_choice": {"type": "function", "name": "get_weather"},
                "max_output_tokens": 77, "temperature": 0.3,
                "reasoning": {"effort": "low"}, "text": {"format": {"type": "json_object"}}}
        out = server._responses_to_openai(body)
        m = out["messages"]
        self.assertEqual(m[0], {"role": "system", "content": "You are terse."})
        self.assertEqual(m[1]["role"], "user")
        self.assertEqual(m[1]["content"][1]["image_url"]["url"], "https://i.test/x.png")
        self.assertEqual(m[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(m[3], {"role": "tool", "tool_call_id": "call_1", "content": "sunny"})
        self.assertEqual(m[4], {"role": "system", "content": "dev note"})
        self.assertEqual(m[5], {"role": "assistant", "content": "ok"})
        self.assertEqual(out["tools"][0]["function"]["name"], "get_weather")
        self.assertTrue(out["tools"][0]["function"]["strict"])
        self.assertEqual(out["tool_choice"]["function"]["name"], "get_weather")
        self.assertEqual((out["max_tokens"], out["temperature"], out["reasoning_effort"]), (77, 0.3, "low"))
        self.assertEqual(out["response_format"], {"type": "json_object"})
        self.assertEqual(server._responses_to_openai({"model": "m", "input": "hi"})["messages"],
                         [{"role": "user", "content": "hi"}])

    def test_responses_rejects_state_and_builtin_tools(self):
        r = self.client.post("/v1/responses", headers=self.h,
                             json={"model": "pr-chat", "input": "hi", "previous_response_id": "resp_1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "stateless")
        r = self.client.post("/v1/responses", headers=self.h,
                             json={"model": "pr-chat", "input": "hi", "tools": [{"type": "web_search_preview"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "unsupported_tool")

    def test_responses_nonstream(self):
        r = self.client.post("/v1/responses", headers=self.h,
                             json={"model": "pr-chat", "input": "weather in SH?"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["object"], "response")
        self.assertTrue(body["id"].startswith("resp_"))
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["model"], "pr-chat")
        self.assertEqual(body["output"][0]["type"], "message")
        self.assertEqual(body["output"][0]["content"][0], {"type": "output_text", "text": "Sunny in SH.",
                                                          "annotations": []})
        self.assertEqual(body["usage"]["input_tokens"], 12)
        self.assertEqual(body["usage"]["total_tokens"], 16)
        self.assertEqual(SENT[-1][1]["model"], "up-x")
        log = self._log()
        self.assertEqual((log["input_tokens"], log["output_tokens"], log["token_source"]), (12, 4, "upstream"))

    def test_responses_stream_with_function_call(self):
        r = self.client.post("/v1/responses", headers=self.h,
                             json={"model": "pr-chat", "stream": True, "input": "weather in SH?",
                                   "tools": [{"type": "function", "name": "get_weather",
                                              "parameters": {"type": "object"}}]})
        self.assertEqual(r.status_code, 200, r.text)
        events = []
        for block in r.text.strip().split("\n\n"):
            lines = block.split("\n")
            ev = [ln[7:] for ln in lines if ln.startswith("event: ")]
            data = [ln[6:] for ln in lines if ln.startswith("data: ")]
            if ev and data:
                events.append((ev[0], json.loads(data[0])))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[:2], ["response.created", "response.in_progress"])
        self.assertIn("response.output_text.delta", kinds)
        self.assertIn("response.function_call_arguments.delta", kinds)
        self.assertEqual(kinds[-1], "response.completed")
        # sequence_number 单调递增,类型字段在 data 里
        seqs = [d["sequence_number"] for _, d in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertTrue(all(d["type"] == k for k, d in events))
        final = events[-1][1]["response"]
        self.assertEqual(final["status"], "completed")
        msg = [o for o in final["output"] if o["type"] == "message"][0]
        self.assertEqual(msg["content"][0]["text"], "Let me check.")
        fc = [o for o in final["output"] if o["type"] == "function_call"][0]
        self.assertEqual((fc["call_id"], fc["name"], fc["arguments"], fc["status"]),
                         ("call_7", "get_weather", '{"city":"SH"}', "completed"))
        done = [d for k, d in events if k == "response.function_call_arguments.done"][0]
        self.assertEqual(done["arguments"], '{"city":"SH"}')
        self.assertEqual(final["usage"]["input_tokens"], 21)
        self.assertEqual(final["usage"]["output_tokens"], 9)
        # 出站带了 tools,上游模型名映射了
        self.assertEqual(SENT[-1][1]["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(SENT[-1][1]["model"], "up-x")
        log = self._log()
        self.assertEqual((log["input_tokens"], log["output_tokens"], log["token_source"],
                          log["pricing_snapshot"]["end_reason"]), (21, 9, "upstream", "done"))

    def test_responses_stream_empty_upstream_gives_no_events(self):
        out = list(server._responses_stream(iter([]), {"model": "m"}, "m"))
        self.assertEqual(out, [])

    def test_responses_incomplete_on_length(self):
        chat = {"choices": [{"message": {"role": "assistant", "content": "cut"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        resp = server._openai_to_response(chat, {"model": "m", "max_output_tokens": 2}, "m")
        self.assertEqual(resp["status"], "incomplete")
        self.assertEqual(resp["incomplete_details"], {"reason": "max_output_tokens"})
        self.assertEqual(resp["max_output_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
