#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一模型多渠道路由的门禁。

守四条:
  1. 候选序列按 priority 从高到低,同档按 weight 随机分流,weight 0 垫底;下线渠道不进
  2. 非流式:首选渠道没号(503)/ 换遍号仍失败(502)/ 上游限流(429)→ 换下一个;
     其它错误(400/403)不换;全败抛最后一个
  3. 流式:一帧没出才换渠道,已开始写的流不换;普通型 role 帧只发一次,
     透传型的 role 帧在上游第一帧里
  4. 计费记实际作答的渠道,不是请求开始时猜的首选
"""
import json
import os
import random
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "rt.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter, UpstreamError  # noqa: E402
from core import adapter as A  # noqa: E402
from core import pool_state  # noqa: E402
from core.adapter import CAP_CHAT, Adapter, register_adapter, unregister_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MODEL = "shared-model"


class TextAdapter(Adapter):
    """普通型:on_token 出字。行为由类属性控制,便于每个用例改口径。"""
    capabilities = [CAP_CHAT]
    fail = None          # None=正常;"raise"=抛;"empty"=空回复
    reply = "hello"

    def __init__(self, name):
        self.name = name
        self.models = [MODEL]

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        if self.fail == "raise":
            raise RuntimeError(f"{self.name} exploded")
        if self.fail == "empty":
            return {"content": "", "reasoning": None, "raw": None} if not stream else None
        if stream:
            on_token(self.reply + "@" + self.name, "content")
            return {"content": self.reply, "reasoning": None,
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        return {"raw": {"id": "x", "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant",
                                                             "content": self.reply + "@" + self.name},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}}


class SseAdapter(OpenAICompatAdapter):
    """透传型。_post 被 mock:fail_code 非 None 就抛 UpstreamError。"""
    fail_code = None

    def _post(self, body, api_key, timeout):
        if self.fail_code:
            raise UpstreamError(self.fail_code, "boom")
        import io
        frames = [
            {"id": "s", "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            {"id": "s", "model": body["model"], "choices": [{"index": 0, "delta": {"content": "sse@" + self.name}}]},
            {"id": "s", "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"id": "s", "model": body["model"], "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}},
        ]
        if not body.get("stream"):
            class _J:
                def read(_self):
                    return json.dumps({"id": "s", "model": body["model"],
                                       "choices": [{"index": 0, "message": {"role": "assistant", "content": "sse@" + self.name}, "finish_reason": "stop"}],
                                       "usage": frames[-1]["usage"]}).encode()

                def close(_self):
                    pass
            return _J()
        buf = io.BytesIO("".join("data: " + json.dumps(f) + "\n\n" for f in frames).encode()
                         + b"data: [DONE]\n\n")

        class _S:
            def read1(_self, n):
                return buf.read(n)

            def close(_self):
                pass
        return _S()


def _db():
    return portal_state.USER_DB


class RouteOrderTest(unittest.TestCase):
    def setUp(self):
        self.names = ["rt-a", "rt-b", "rt-c"]
        for n in self.names:
            register_adapter(TextAdapter(n))
        A.set_channel_routing({})
        A.set_disabled_channels([])

    def tearDown(self):
        for n in self.names:
            unregister_adapter(n)
        A.set_channel_routing({})
        A.set_disabled_channels([])

    def test_default_is_registration_order_per_tier_shuffled_by_weight(self):
        routes = A.model_routes(MODEL, rng=random.Random(1))
        self.assertEqual(sorted(routes), sorted(self.names))
        self.assertEqual(len(routes), 3)

    def test_priority_tiers_then_weight(self):
        A.set_channel_routing({"rt-c": {"priority": 10, "weight": 1},
                               "rt-a": {"priority": 0, "weight": 5},
                               "rt-b": {"priority": 0, "weight": 0}})
        for seed in range(5):
            routes = A.model_routes(MODEL, rng=random.Random(seed))
            self.assertEqual(routes[0], "rt-c")          # 高优先级永远第一
            self.assertEqual(routes[-1], "rt-b")         # weight 0 垫底
        self.assertEqual(A.primary_channel(MODEL), "rt-c")
        self.assertEqual(A.model_to_channel()[MODEL], "rt-c")

    def test_weight_skews_distribution(self):
        A.set_channel_routing({"rt-a": {"priority": 0, "weight": 9},
                               "rt-b": {"priority": 0, "weight": 1}})
        unregister_adapter("rt-c")
        rng = random.Random(42)
        firsts = [A.model_routes(MODEL, rng=rng)[0] for _ in range(400)]
        share_a = firsts.count("rt-a") / 400
        self.assertGreater(share_a, 0.8)
        self.assertLess(share_a, 0.98)
        register_adapter(TextAdapter("rt-c"))

    def test_disabled_channel_excluded(self):
        A.set_disabled_channels(["rt-a"])
        self.assertNotIn("rt-a", A.model_routes(MODEL))
        self.assertEqual(A.model_routes("nope"), [])
        self.assertIsNone(A.primary_channel("nope"))

    def test_channel_name_itself_routes(self):
        self.assertEqual(A.model_routes("rt-b"), ["rt-b"])


class FailoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "rt-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh, pricing=portal_state.PRICING)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@example.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.admin_h = {"Authorization": "Bearer " + r.json()["token"]}
        cls.uid = r.json()["user_id"]
        cls.sk = cls.client.post("/api/keys", json={"name": "t"}, headers=cls.admin_h).json()["key"]
        cls.h = {"Authorization": "Bearer " + cls.sk}

    def setUp(self):
        self.a = TextAdapter("fo-a")
        self.b = TextAdapter("fo-b")
        self.s = SseAdapter(name="fo-s", base_url="https://s.test/v1", models=[MODEL])
        for ad in (self.a, self.b, self.s):
            register_adapter(ad)
            pool_state.DB.delete_channel(ad.name)
        # a 最高优先级,其次 s,最后 b
        A.set_channel_routing({"fo-a": {"priority": 10, "weight": 1},
                               "fo-s": {"priority": 5, "weight": 1},
                               "fo-b": {"priority": 0, "weight": 1}})
        A.set_disabled_channels([])
        server.BILLING.invalidate()

    def tearDown(self):
        for ad in (self.a, self.b, self.s):
            unregister_adapter(ad.name)
            pool_state.DB.delete_channel(ad.name)
        A.set_channel_routing({})

    def _acct(self, name, n=1):
        for k in range(n):
            pool_state.DB.upsert_account(name, f"{name}-{k}", secret={"api_key": "K"}, status="active")

    def _chat(self, stream=False, **extra):
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
        body.update(extra)
        return self.client.post("/v1/chat/completions", json=body, headers=self.h)

    def _last_log(self):
        return _db().recent_usage(self.uid, limit=1)[0]

    # ---- 非流式 ----

    def test_nonstream_falls_through_empty_pool_to_next_channel(self):
        """a 没号 → 503 → s 没号 → b 有号 → b 作答,账记在 b。"""
        self._acct("fo-b")
        r = self._chat()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "hello@fo-b")
        self.assertEqual(self._last_log()["channel"], "fo-b")

    def test_nonstream_falls_through_upstream_failure(self):
        """a 有号但一直抛(502)→ s 上游 429 → b 作答。"""
        self._acct("fo-a", 2)
        self._acct("fo-s")
        self._acct("fo-b")
        self.a.fail = "raise"
        SseAdapter.fail_code = 429
        try:
            r = self._chat()
        finally:
            SseAdapter.fail_code = None
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "hello@fo-b")
        self.assertEqual(self._last_log()["channel"], "fo-b")

    def test_nonstream_all_fail_returns_last_error(self):
        """全败时抛的是最后一个渠道的错:一把号抛一次就进冷却,下一轮取号落空是
        503「fo-b 没号」;号多时才是 502「换遍号仍失败」。两种都得指向 fo-b。"""
        self._acct("fo-b")
        self.b.fail = "raise"
        r = self._chat()
        self.assertIn(r.status_code, (502, 503), r.text)
        self.assertIn("fo-b", r.text)
        self._acct("fo-b", 8)
        for a in pool_state.DB.list_accounts(channel="fo-b"):
            pool_state.DB.set_status(a["id"], "active")
        r = self._chat()
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn("fo-b exploded", r.text)

    def test_nonstream_stays_on_first_channel_when_it_answers(self):
        self._acct("fo-a")
        self._acct("fo-b")
        r = self._chat()
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "hello@fo-a")
        self.assertEqual(self._last_log()["channel"], "fo-a")

    def test_sse_channel_answers_nonstream_with_relabelled_model(self):
        self._acct("fo-s")
        r = self._chat()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], MODEL)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "sse@fo-s")
        log = self._last_log()
        self.assertEqual((log["channel"], log["input_tokens"], log["token_source"]),
                         ("fo-s", 7, "upstream"))

    # ---- 流式 ----

    @staticmethod
    def _frames(text):
        return [json.loads(ln[6:]) for ln in text.split("\n")
                if ln.startswith("data: ") and ln != "data: [DONE]"]

    def test_stream_falls_through_to_text_channel_with_single_role_frame(self):
        """a 没号、s 没号、b 出字:role 帧只有一个,内容来自 b,账记 b。"""
        self._acct("fo-b")
        r = self._chat(stream=True)
        self.assertEqual(r.status_code, 200, r.text)
        frames = self._frames(r.text)
        roles = [f for f in frames if (f["choices"][0]["delta"] or {}).get("role")]
        self.assertEqual(len(roles), 1)
        content = "".join((f["choices"][0]["delta"] or {}).get("content", "") for f in frames)
        self.assertEqual(content, "hello@fo-b")
        self.assertTrue(r.text.rstrip().endswith("data: [DONE]"))
        # 一条流里所有帧同一个 id
        self.assertEqual(len({f["id"] for f in frames}), 1)
        self.assertEqual(self._last_log()["channel"], "fo-b")
        self.assertEqual(self._last_log()["pricing_snapshot"]["end_reason"], "done")

    def test_stream_falls_through_raising_text_channel_to_sse_channel(self):
        """a 有号但抛(一帧没出)→ s 透传作答。a 的失败已经让 role 帧发出去了,
        s 的上游第一帧也带 role —— 两个 role 帧是这种切换的代价,内容不重不漏。"""
        self._acct("fo-a")
        self._acct("fo-s")
        self.a.fail = "raise"
        r = self._chat(stream=True)
        self.assertEqual(r.status_code, 200, r.text)
        frames = self._frames(r.text)
        content = "".join((f["choices"][0]["delta"] or {}).get("content", "")
                          for f in frames if f.get("choices"))
        self.assertEqual(content, "sse@fo-s")
        self.assertTrue(all(f["model"] == MODEL for f in frames))
        log = self._last_log()
        self.assertEqual((log["channel"], log["input_tokens"], log["output_tokens"]), ("fo-s", 7, 1))

    def test_stream_all_fail_gives_parseable_closure(self):
        """全部候选一帧没出:客户端拿到 role + 一句说明 + 终止帧 + [DONE],不是空流。"""
        r = self._chat(stream=True)
        self.assertEqual(r.status_code, 200)
        frames = self._frames(r.text)
        self.assertIn("no available upstream", r.text)
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(r.text.rstrip().endswith("data: [DONE]"))

    def test_stream_does_not_switch_after_partial_output(self):
        """a 出了字再断:流归 a,不去找 b 再拼一段。"""
        class Partial(TextAdapter):
            def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
                on_token("partial@fo-a", "content")
                raise RuntimeError("cut")
        unregister_adapter("fo-a")
        self.a = Partial("fo-a")
        register_adapter(self.a)
        self._acct("fo-a")
        self._acct("fo-b")
        r = self._chat(stream=True)
        content = "".join((f["choices"][0]["delta"] or {}).get("content", "")
                          for f in self._frames(r.text) if f.get("choices"))
        self.assertEqual(content, "partial@fo-a")
        self.assertNotIn("fo-b", content)
        log = self._last_log()
        self.assertEqual(log["channel"], "fo-a")
        self.assertEqual(log["pricing_snapshot"]["end_reason"], "eof")

    # ---- 清单与端点 ----

    def test_models_listed_once_with_primary_owner(self):
        data = self.client.get("/v1/models", headers=self.h).json()["data"]
        mine = [m for m in data if m["id"] == MODEL]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["owned_by"], "fo-a")
        plaza = self.client.get("/api/models", headers=self.admin_h).json()["data"]
        card = [c for c in plaza if c["model"] == MODEL][0]
        self.assertEqual(card["channel"], "fo-a")
        self.assertEqual(sorted(card["channels"]), ["fo-a", "fo-b", "fo-s"])

    def test_routing_endpoint_changes_order_and_persists(self):
        r = self.client.patch("/api/admin/channels/fo-b/routing", json={"priority": 99, "weight": 3},
                              headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(A.routing_of("fo-b"), (99, 3))
        self.assertEqual(A.model_routes(MODEL)[0], "fo-b")
        self.assertEqual(_db().get_setting("channel_routing")["fo-b"], {"priority": 99, "weight": 3})
        listed = {c["name"]: c for c in self.client.get("/api/admin/channels",
                                                          headers=self.admin_h).json()["channels"]}
        self.assertEqual((listed["fo-b"]["priority"], listed["fo-b"]["weight"]), (99, 3))
        self.assertEqual(listed["fo-b"]["shared_models"], [MODEL])
        self.assertEqual(self.client.patch("/api/admin/channels/ghost/routing", json={},
                                           headers=self.admin_h).status_code, 404)
        self.assertEqual(self.client.patch("/api/admin/channels/fo-b/routing", json={"weight": -1},
                                           headers=self.admin_h).status_code, 422)
        # 恢复默认走 DELETE settings
        self.client.delete("/api/admin/settings/channel_routing", headers=self.admin_h)
        self.assertEqual(A.routing_of("fo-b"), (0, 1))


if __name__ == "__main__":
    unittest.main()
