#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端看到的 usage 与账单一致 —— 计费透明度的门禁。

原先合成响应(逆向渠道、虚拟工具)的 usage 是三个 0,Anthropic 响应更是永远 0:
Claude Code 显示 $0 而账单在扣,这是投诉源。现在:
  1. 上游给了真实 usage → 原样给客户端,计量层记 upstream
  2. 上游没给 → 客户端拿到与计量层同一套估算,计量层仍如实记 estimate
  3. Anthropic:message_start 给输入估算,message_delta / 非流 usage 给最终值,
     finish_reason=length → stop_reason=max_tokens
"""
import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ub.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import metering  # noqa: E402
from core import pool_state  # noqa: E402
from core.adapter import CAP_CHAT, Adapter, register_adapter, unregister_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MSGS = [{"role": "user", "content": "please say hello to the world"}]
EST = metering.estimate_usage(MSGS, "hello world")


class EstimateAdapter(Adapter):
    """逆向型:只出文字,没有 usage。"""
    name = "ub-est"
    capabilities = [CAP_CHAT]
    models = ["ub-est-model"]

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        if stream:
            on_token("hello ", "content")
            on_token("world", "content")
            return None
        return {"content": "hello world", "reasoning": None, "raw": None}


class RealUsageAdapter(Adapter):
    """普通型但上游给了 usage(非流走 raw,流式走返回值)。"""
    name = "ub-real"
    capabilities = [CAP_CHAT]
    models = ["ub-real-model"]
    USAGE = {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49,
             "prompt_tokens_details": {"cached_tokens": 12}}

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        if stream:
            on_token("hello world", "content")
            return {"content": "hello world", "reasoning": None, "usage": dict(self.USAGE)}
        return {"raw": {"id": "r", "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"},
                                     "finish_reason": "length"}],
                        "usage": dict(self.USAGE)}}


def _db():
    return portal_state.USER_DB


class UsageBackfillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "ub-ep.db")
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
        for ad in (EstimateAdapter(), RealUsageAdapter()):
            register_adapter(ad)
            pool_state.DB.upsert_account(ad.name, "k1", secret={"api_key": "K"}, status="active")

    @classmethod
    def tearDownClass(cls):
        for n in ("ub-est", "ub-real"):
            unregister_adapter(n)
            pool_state.DB.delete_channel(n)

    def _log(self):
        return _db().recent_usage(self.uid, limit=1)[0]

    @staticmethod
    def _frames(text):
        return [json.loads(ln[6:]) for ln in text.split("\n")
                if ln.startswith("data: ") and ln != "data: [DONE]"]

    # ---- OpenAI ----

    def test_openai_nonstream_estimate_matches_bill(self):
        r = self.client.post("/v1/chat/completions", headers=self.h,
                             json={"model": "ub-est-model", "messages": MSGS})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["usage"], EST)
        self.assertGreater(EST["total_tokens"], 0)
        log = self._log()
        self.assertEqual((log["input_tokens"], log["output_tokens"]),
                         (EST["prompt_tokens"], EST["completion_tokens"]))
        self.assertEqual(log["token_source"], "estimate")     # 估算值没被记成上游真实值

    def test_openai_stream_estimate_in_terminal_frame(self):
        r = self.client.post("/v1/chat/completions", headers=self.h,
                             json={"model": "ub-est-model", "messages": MSGS, "stream": True})
        frames = self._frames(r.text)
        with_usage = [f for f in frames if f.get("usage")]
        self.assertEqual(len(with_usage), 1)
        self.assertEqual(with_usage[0]["usage"], EST)
        self.assertEqual(with_usage[0]["choices"][0]["finish_reason"], "stop")
        log = self._log()
        self.assertEqual(log["token_source"], "estimate")
        self.assertEqual(log["output_tokens"], EST["completion_tokens"])
        self.assertEqual(log["pricing_snapshot"]["end_reason"], "done")

    def test_openai_real_usage_passes_through_and_bills_upstream(self):
        r = self.client.post("/v1/chat/completions", headers=self.h,
                             json={"model": "ub-real-model", "messages": MSGS})
        self.assertEqual(r.json()["usage"]["prompt_tokens"], 40)
        self.assertEqual(self._log()["token_source"], "upstream")
        r = self.client.post("/v1/chat/completions", headers=self.h,
                             json={"model": "ub-real-model", "messages": MSGS, "stream": True})
        usage = [f for f in self._frames(r.text) if f.get("usage")][0]["usage"]
        self.assertEqual(usage["completion_tokens"], 9)
        log = self._log()
        self.assertEqual((log["input_tokens"], log["output_tokens"], log["token_source"]),
                         (40, 9, "upstream"))

    # ---- Anthropic ----

    def _anthropic(self, model, stream=False, max_tokens=64):
        return self.client.post("/v1/messages", headers={"x-api-key": self.sk},
                                json={"model": model, "max_tokens": max_tokens, "stream": stream,
                                      "messages": [{"role": "user",
                                                    "content": "please say hello to the world"}]})

    def test_anthropic_nonstream_usage_not_zero(self):
        r = self._anthropic("ub-est-model")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["usage"], {"input_tokens": EST["prompt_tokens"],
                                         "output_tokens": EST["completion_tokens"]})
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(self._log()["token_source"], "estimate")

    def test_anthropic_nonstream_real_usage_with_cache_and_max_tokens(self):
        r = self._anthropic("ub-real-model")
        body = r.json()
        self.assertEqual(body["usage"], {"input_tokens": 40, "output_tokens": 9,
                                         "cache_read_input_tokens": 12})
        self.assertEqual(body["stop_reason"], "max_tokens")
        self.assertEqual(self._log()["token_source"], "upstream")

    @staticmethod
    def _events(text):
        out = []
        for block in text.strip().split("\n\n"):
            lines = block.split("\n")
            ev = [ln[7:] for ln in lines if ln.startswith("event: ")]
            data = [ln[6:] for ln in lines if ln.startswith("data: ")]
            if ev and data:
                out.append((ev[0], json.loads(data[0])))
        return out

    def test_anthropic_stream_message_start_and_delta_usage(self):
        r = self._anthropic("ub-est-model", stream=True)
        self.assertEqual(r.status_code, 200, r.text)
        events = dict(self._events(r.text))
        self.assertEqual(events["message_start"]["message"]["usage"]["input_tokens"], EST["prompt_tokens"])
        delta = events["message_delta"]
        self.assertEqual(delta["usage"]["output_tokens"], EST["completion_tokens"])
        self.assertEqual(delta["usage"]["input_tokens"], EST["prompt_tokens"])
        self.assertEqual(delta["delta"]["stop_reason"], "end_turn")
        self.assertIn("message_stop", events)
        self.assertEqual(self._log()["token_source"], "estimate")

    def test_anthropic_stream_prefers_real_usage(self):
        r = self._anthropic("ub-real-model", stream=True)
        events = dict(self._events(r.text))
        self.assertEqual(events["message_delta"]["usage"],
                         {"input_tokens": 40, "output_tokens": 9, "cache_read_input_tokens": 12})
        log = self._log()
        self.assertEqual((log["input_tokens"], log["token_source"]), (40, "upstream"))


class UsageHelperTest(unittest.TestCase):
    def test_anthropic_usage_mapping(self):
        self.assertEqual(server._anthropic_usage(None, 3, 4), {"input_tokens": 3, "output_tokens": 4})
        self.assertEqual(server._anthropic_usage({"prompt_tokens": 0, "completion_tokens": 0,
                                                  "total_tokens": 0}, 3, 4),
                         {"input_tokens": 3, "output_tokens": 4})     # 全 0 视同没给
        self.assertEqual(server._anthropic_usage({"prompt_tokens": 10, "completion_tokens": 2,
                                                  "total_tokens": 12,
                                                  "cache_creation_input_tokens": 5}),
                         {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 5})

    def test_stop_reason_mapping(self):
        self.assertEqual(server._anthropic_stop("stop", False), "end_turn")
        self.assertEqual(server._anthropic_stop("length", False), "max_tokens")
        self.assertEqual(server._anthropic_stop("tool_calls", False), "tool_use")
        self.assertEqual(server._anthropic_stop("stop", True), "tool_use")

    def test_estimate_matches_metering_resolve(self):
        est = metering.estimate_usage(MSGS, "hello world")
        it, ot, src = metering.resolve_usage("estimate", None, MSGS, "hello world")
        self.assertEqual((est["prompt_tokens"], est["completion_tokens"]), (it, ot))
        self.assertEqual(src, "estimate")


if __name__ == "__main__":
    unittest.main()
