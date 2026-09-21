#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据渠道(管理台建的 OpenAI 兼容上游)的门禁。

  1. 校验:名字/模型不能撞现有渠道,地址与请求头的坏值进不了库
  2. 存库即注册:建完模型立刻在 /v1/models 与路由里;删了立刻消失,号池一起清
  3. 网关端到端:用户 sk- key 打 /v1/chat/completions,非流式与流式都保留 tool_calls,
     流式 usage 被采信(token_source=upstream),model 字段是对外名
  4. 导 key 与测试渠道端点;非管理员 403
上游用 class 级 mock 顶掉 OpenAICompatAdapter._post,不出网。
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ch.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter, UpstreamError  # noqa: E402
from core import adapter as adapter_mod  # noqa: E402
from core import channels as channels_mod  # noqa: E402
from core import pool_state  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

STREAM_LINES = [
    'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}',
    'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}',
    'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"SH\\"}"}}]}}]}',
    'data: {"id":"c","model":"gpt-9-pro","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"id":"c","model":"gpt-9-pro","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}',
    "data: [DONE]",
]
NONSTREAM = {"id": "x", "model": "gpt-9-pro",
             "choices": [{"index": 0, "finish_reason": "tool_calls",
                          "message": {"role": "assistant", "content": None,
                                      "tool_calls": [{"id": "call_9", "type": "function",
                                                      "function": {"name": "get_weather",
                                                                   "arguments": "{}"}}]}}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}


class _Json:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def close(self):
        pass


class _Sse:
    def __init__(self, lines):
        self.buf = io.BytesIO("".join(l + "\n\n" for l in lines).encode())

    def read1(self, n):
        return self.buf.read(n)

    def close(self):
        pass


SENT = []


def fake_post(self, body, api_key, timeout):
    SENT.append({"url": self._url(), "key": api_key, "body": body,
                 "headers": self._headers(api_key)})
    if body.get("stream"):
        return _Sse(STREAM_LINES)
    return _Json(dict(NONSTREAM))


def _db():
    return portal_state.USER_DB


class ChannelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "ch-ep.db")
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
        r = cls.client.post("/api/register", json={"email": "u@example.com",
                                                   "password": "secret123"})
        cls.user_h = {"Authorization": "Bearer " + r.json()["token"]}
        cls.uid = r.json()["user_id"]
        cls.sk = cls.client.post("/api/keys", json={"name": "t"}, headers=cls.user_h).json()["key"]
        cls._patch = mock.patch.object(OpenAICompatAdapter, "_post", fake_post)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()

    def setUp(self):
        SENT.clear()
        # 每个用例从干净的数据渠道表开始
        for row in _db().list_channels():
            pool_state.DB.delete_channel(row["name"])
            _db().delete_channel_config(row["name"])
        channels_mod.reload(_db())
        _db().delete_setting("disabled_channels")
        portal_state.apply_channel_switches()

    def _create(self, **over):
        body = {"name": "acme", "base_url": "https://api.acme.test/v1",
                "models": ["acme-fast", "acme-pro"], "model_map": {"acme-pro": "gpt-9-pro"},
                "headers": {"X-Org": "team-a"}}
        body.update(over)
        return self.client.post("/api/admin/channels", json=body, headers=self.admin_h)

    def _import(self, name="acme", keys=("sk-k1", "sk-k2")):
        return self.client.post(f"/api/admin/channels/{name}/keys", json={"keys": list(keys)},
                                headers=self.admin_h)

    # ---- 校验 ----

    def test_validation_rejects_bad_input(self):
        cases = [
            ({"name": "Bad Name"}, "渠道名"),
            ({"name": "grok"}, "代码里定义"),
            # 模型名撞渠道名不行(渠道名本身可作 model 路由);与别的渠道同模型是允许的
            ({"models": ["acme-x", "grok"]}, "与渠道名冲突"),
            ({"base_url": "api.acme.test/v1"}, "base_url"),
            ({"base_url": "https://api.acme.test/v1?x=1"}, "base_url"),
            ({"headers": {"Authorization": "Bearer leak"}}, "Authorization"),
            ({"model_map": {"ghost": "x"}}, "不在模型清单"),
            ({"models": []}, "至少填一个"),
            ({"timeout": 99999}, "超时"),
            ({"type": "gemini"}, "类型"),
        ]
        for over, needle in cases:
            with self.subTest(over=over):
                r = self._create(**over)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(needle, r.json()["detail"])
        self.assertIsNone(adapter_mod.get_adapter("acme"))

    def test_text_forms_accepted(self):
        """管理台文本框里的多行模型清单与 a=b 映射也能进。"""
        r = self._create(models="acme-fast\nacme-pro, acme-mini", model_map="acme-pro=gpt-9-pro")
        self.assertEqual(r.status_code, 200, r.text)
        ad = adapter_mod.get_adapter("acme")
        self.assertEqual(ad.models, ["acme-fast", "acme-pro", "acme-mini"])
        self.assertEqual(ad.model_map, {"acme-pro": "gpt-9-pro"})

    def test_duplicate_name_rejected(self):
        self.assertEqual(self._create().status_code, 200)
        r = self._create()
        self.assertEqual(r.status_code, 400)
        self.assertIn("已存在", r.json()["detail"])

    # ---- 存库即注册 ----

    def test_create_registers_and_models_appear(self):
        r = self._create()
        self.assertEqual(r.status_code, 200, r.text)
        ad = adapter_mod.get_adapter("acme")
        self.assertIsInstance(ad, OpenAICompatAdapter)
        self.assertEqual(ad.source, "data")
        self.assertTrue(channels_mod.is_data_channel("acme"))
        models = self.client.get("/v1/models", headers={"Authorization": "Bearer " + self.sk}).json()
        ids = {m["id"]: m["owned_by"] for m in models["data"]}
        self.assertEqual(ids.get("acme-pro"), "acme")
        listed = {c["name"]: c for c in self.client.get("/api/admin/channels",
                                                          headers=self.admin_h).json()["channels"]}
        self.assertEqual(listed["acme"]["source"], "data")
        self.assertEqual(listed["acme"]["config"]["model_map"], {"acme-pro": "gpt-9-pro"})
        self.assertEqual(listed["acme"]["pool"]["total"], 0)
        # 代码渠道在目录里要标出来,且模型清单不为空 —— 面板靠这个区分来源
        self.assertEqual(listed["grok"]["source"], "code")
        self.assertTrue(listed["grok"]["models"])

    def test_update_reloads_registry(self):
        self._create()
        r = self.client.patch("/api/admin/channels/acme", json={
            "base_url": "https://api2.acme.test/v1", "models": ["acme-pro"],
            "model_map": {"acme-pro": "gpt-10"}}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        ad = adapter_mod.get_adapter("acme")
        self.assertEqual(ad.api_base, "https://api2.acme.test/v1")
        self.assertEqual(ad.models, ["acme-pro"])
        self.assertEqual(ad.upstream_model("acme-pro"), "gpt-10")
        self.assertNotIn("acme-fast", adapter_mod.model_to_channel())

    def test_delete_removes_adapter_and_pool(self):
        self._create()
        self.assertEqual(self._import().json()["imported"], 2)
        r = self.client.delete("/api/admin/channels/acme", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["accounts_deleted"], 2)
        self.assertIsNone(adapter_mod.get_adapter("acme"))
        self.assertEqual(pool_state.DB.count_accounts(channel="acme"), 0)
        self.assertNotIn("acme-pro", adapter_mod.model_to_channel())

    def test_code_channels_are_read_only_here(self):
        self.assertEqual(self.client.delete("/api/admin/channels/grok", headers=self.admin_h).status_code, 404)
        r = self.client.patch("/api/admin/channels/grok", json={"base_url": "https://x.test"},
                              headers=self.admin_h)
        self.assertEqual(r.status_code, 404)

    def test_non_admin_forbidden(self):
        for method, path in (("post", "/api/admin/channels"), ("get", "/api/admin/channels"),
                             ("post", "/api/admin/channels/grok/keys"),
                             ("post", "/api/admin/channels/grok/test")):
            r = getattr(self.client, method)(path, json={"base_url": "https://x.test", "keys": ["k"]},
                                             headers=self.user_h) if method == "post" \
                else self.client.get(path, headers=self.user_h)
            self.assertEqual(r.status_code, 403, path)

    # ---- 导 key / 测试 ----

    def test_import_keys_dedups_and_reports_pool(self):
        self._create()
        r = self._import(keys=("sk-k1", "sk-k2", "sk-k1"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["imported"], r.json()["skipped"]), (2, 1))
        self.assertEqual(r.json()["pool"]["active"], 2)
        r = self.client.post("/api/admin/channels/acme/keys", json={"key": "sk-k3\nsk-k4,sk-k2"},
                             headers=self.admin_h)
        self.assertEqual((r.json()["imported"], r.json()["skipped"]), (2, 1))
        accts = pool_state.DB.list_accounts(channel="acme")
        self.assertEqual(len(accts), 4)
        self.assertTrue(all(a["identity"].startswith("acme-") for a in accts))
        self.assertTrue(all(a["secret"]["api_key"].startswith("sk-k") for a in accts))
        self.assertEqual(self._import(name="ghost").status_code, 404)

    def test_probe_reports_success_and_upstream_error(self):
        self._create()
        self.assertEqual(self.client.post("/api/admin/channels/acme/test", json={},
                                          headers=self.admin_h).status_code, 503)   # 没 key
        self._import(keys=("sk-k1",))
        r = self.client.post("/api/admin/channels/acme/test", json={"model": "acme-pro"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["usage"]["total_tokens"], 8)
        self.assertEqual(SENT[-1]["body"]["model"], "gpt-9-pro")
        self.assertEqual(SENT[-1]["headers"]["X-Org"], "team-a")
        self.assertEqual(SENT[-1]["key"], "sk-k1")
        # 上游 401:回 ok=False 带原话,并且这把 key 按终态退场
        with mock.patch.object(OpenAICompatAdapter, "_post",
                               side_effect=UpstreamError(401, '{"error":"invalid api key"}')):
            r = self.client.post("/api/admin/channels/acme/test", json={}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("invalid api key", r.json()["error"])
        self.assertEqual(pool_state.DB.list_accounts(channel="acme")[0]["status"], "exhausted")
        self.assertEqual(self.client.post("/api/admin/channels/acme/test", json={"model": "nope"},
                                          headers=self.admin_h).status_code, 400)

    # ---- 网关端到端 ----

    def test_gateway_nonstream_keeps_tool_calls_and_records_upstream_usage(self):
        self._create()
        self._import()
        r = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + self.sk},
                             json={"model": "acme-pro", "messages": [{"role": "user", "content": "weather"}],
                                   "tools": [{"type": "function", "function": {"name": "get_weather"}}]})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["model"], "acme-pro")
        self.assertEqual(body["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(SENT[-1]["body"]["model"], "gpt-9-pro")
        self.assertEqual(SENT[-1]["url"], "https://api.acme.test/v1/chat/completions")
        log = _db().recent_usage(self.uid, limit=1)[0]
        self.assertEqual((log["channel"], log["model"]), ("acme", "acme-pro"))
        self.assertEqual((log["input_tokens"], log["output_tokens"]), (5, 3))
        self.assertEqual(log["token_source"], "upstream")

    def test_gateway_stream_passes_tool_calls_and_bills_upstream_usage(self):
        self._create()
        self._import()
        r = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + self.sk},
                             json={"model": "acme-pro", "stream": True,
                                   "messages": [{"role": "user", "content": "weather"}],
                                   "tools": [{"type": "function", "function": {"name": "get_weather"}}]})
        self.assertEqual(r.status_code, 200, r.text)
        frames = [json.loads(ln[6:]) for ln in r.text.split("\n")
                  if ln.startswith("data: ") and ln != "data: [DONE]"]
        self.assertTrue(r.text.rstrip().endswith("data: [DONE]"))
        self.assertTrue(all(f["model"] == "acme-pro" for f in frames))
        calls = [c for f in frames for ch in f.get("choices", [])
                 for c in (ch.get("delta") or {}).get("tool_calls", [])]
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(calls[1]["function"]["arguments"], '{"city":"SH"}')
        self.assertEqual([f for f in frames if f.get("usage")][0]["usage"]["total_tokens"], 18)
        self.assertTrue(SENT[-1]["body"]["stream_options"]["include_usage"])
        log = _db().recent_usage(self.uid, limit=1)[0]
        self.assertEqual((log["input_tokens"], log["output_tokens"]), (11, 7))
        self.assertEqual(log["token_source"], "upstream")
        self.assertEqual(log["pricing_snapshot"]["end_reason"], "done")
        self.assertEqual(log["stream"], 1)

    def test_disabled_channel_hides_models_and_routes(self):
        self._create()
        self._import()
        r = self.client.patch("/api/admin/settings", json={"disabled_channels": ["acme"]},
                              headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        ids = {m["id"] for m in self.client.get("/v1/models", headers={
            "Authorization": "Bearer " + self.sk}).json()["data"]}
        self.assertNotIn("acme-pro", ids)
        r = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + self.sk},
                             json={"model": "acme-pro", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 404)

    def test_gateway_503_without_keys(self):
        self._create()
        r = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + self.sk},
                             json={"model": "acme-fast", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
