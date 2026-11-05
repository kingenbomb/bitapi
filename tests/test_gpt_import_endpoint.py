"""管理员原生 GPT 导入和网关调用：隔离 SQLite + 本地 Codex/OAuth 报文模拟。"""
from contextlib import ExitStack
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "default.db"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config
import server
from core import db as dbmod, pool_state, portal_state
from core.billing import Billing
from core.pool import Pool
from core.user_db import UserDB
from routers import portal
from tests.registration_fixture import TestClient
from tests.test_gpt_import import jwt, wire


def close_connections(*dbs):
    for db in dbs:
        with db._conns._lock:
            for conn in db._conns._live.values():
                conn.close()
            db._conns._live.clear()


class GPTEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        for name, value in {"JWT_SECRET": "test-secret", "REQUIRE_INVITE": False, "ADMIN_KEY": "adm-test"}.items():
            cls.stack.enter_context(mock.patch.object(config, name, value))
        path = os.path.join(cls.tmp.name, "test.db")
        cls.pool = dbmod.DB(path)
        cls.users = UserDB(path)
        cls.addClassCleanup(close_connections, cls.pool, cls.users)
        billing, pool = Billing(cls.users, pricing=portal_state.PRICING), Pool(cls.pool)
        for mod in (server, portal_state, portal):
            cls.stack.enter_context(mock.patch.object(mod, "USER_DB", cls.users, create=True))
            cls.stack.enter_context(mock.patch.object(mod, "BILLING", billing))
        for mod in (server, pool_state):
            cls.stack.enter_context(mock.patch.object(mod, "DB", cls.pool))
            cls.stack.enter_context(mock.patch.object(mod, "POOL", pool))
        old = portal_state.PRICING.db
        cls.addClassCleanup(portal_state.rebind, old)
        portal_state.rebind(cls.users)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        cls.addClassCleanup(cls.client.close)
        r = cls.client.post("/api/register", json={"email": "root@gmail.com", "password": "secret123"})
        cls.admin = {"Authorization": "Bearer " + r.json()["token"]}
        r = cls.client.post("/api/register", json={"email": "user@gmail.com", "password": "secret123"})
        cls.user = {"Authorization": "Bearer " + r.json()["token"]}

    def setUp(self):
        self.pool.delete_channel("gpt")

    def value(self, email="native@gpt.test"):
        return {"access_token": jwt(email=email), "refresh_token": "rt"}

    def test_two_admin_entries_share_native_database_and_dedupe(self):
        value = self.value()
        r = self.client.post("/admin/gpt/import", headers={"X-BitAPI-Admin-Key": "adm-test"}, json=value)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["imported"], 1)
        r = self.client.post("/api/admin/gpt/import", headers=self.admin, json={"content": json.dumps(value)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["skipped"], 1)
        self.assertEqual(self.pool.count_accounts("gpt"), 1)
        self.assertEqual(self.pool.get_by_identity("gpt", "native@gpt.test")["secret"]["refresh_token"], "rt")

    def test_raw_array_file_texts_and_partial_failures(self):
        r = self.client.post("/api/admin/gpt/import", headers=self.admin,
                             json={"contents": [json.dumps(self.value()), json.dumps({"tokens": {"access_token": jwt(email="two@gpt.test", user_id="user-2")}}), "{}"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["imported"], r.json()["failed"]), (2, 1))
        self.assertTrue(r.json()["warnings"])
        r = self.client.post("/api/admin/gpt/import", headers=self.admin, json=[self.value()])
        self.assertEqual(r.json()["skipped"], 1)

    def test_authentication_and_bad_payloads(self):
        self.assertEqual(self.client.post("/api/admin/gpt/import", headers=self.user, json=self.value()).status_code, 403)
        self.assertEqual(self.client.post("/admin/gpt/import", headers=self.user, json=self.value()).status_code, 401)
        for body in ({}, {"accounts": []}, {"contents": [3]}, {"content": "x", "force": "false"}):
            with self.subTest(body=body):
                r = self.client.post("/api/admin/gpt/import", headers=self.admin, json=body)
                self.assertEqual(r.status_code, 400, r.text)

    def test_unusable_opaque_access_only_is_not_reported_as_imported(self):
        r = self.client.post("/api/admin/gpt/import", headers=self.admin,
                             json={"content": "opaque.not-a-jwt"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["imported"], 0)
        self.assertEqual(r.json()["failed"], 1)
        self.assertIn("到期时间", r.json()["errors"][0]["message"])
        self.assertEqual(self.pool.count_accounts("gpt"), 0)

    def test_admin_channel_test_uses_imported_oauth_account_and_official_endpoint(self):
        value = self.value()
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=value)
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire())) as http:
            r = self.client.post("/api/admin/channels/gpt/test", headers=self.admin, json={"model": "gpt-5.6-luna"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(r.json()["reply"], "pong")
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + value["access_token"])

    def test_gateway_chat_responses_anthropic_and_stream_keep_usage_tools(self):
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        headers = {"Authorization": "Bearer " + config.API_KEY}
        for path, body in (("/v1/chat/completions", {"messages": [{"role": "user", "content": "ping"}]}),
                           ("/v1/responses", {"input": "ping"}),
                           ("/v1/messages", {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 32})):
            with self.subTest(path=path), mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire())):
                r = self.client.post(path, headers=headers, json={"model": "gpt-5.6-luna", **body})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn("pong", r.text)
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire(tool=True))):
            r = self.client.post("/v1/chat/completions", headers=headers,
                                 json={"model": "gpt-5.6-luna", "stream": True, "messages": [{"role": "user", "content": "ping"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("call_weather", r.text)
        self.assertIn('"cached_tokens": 6', r.text)
        self.assertIn("data: [DONE]", r.text)

    def test_listing_redacts_credentials_and_normal_delete_clears_pool(self):
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        r = self.client.get("/admin/accounts?channel=gpt", headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        for account in r.json()["accounts"]:
            self.assertNotIn("secret", account)
            self.assertNotIn("token", account)
        r = self.client.post("/admin/channel/delete?channel=gpt", headers=self.admin)
        self.assertEqual(r.json()["deleted"], 1)
        self.assertEqual(self.pool.count_accounts("gpt"), 0)

    def test_user_key_stream_records_native_usage_and_cache(self):
        from core.credit import credit
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        user = self.users.get_user_by_email("user@gmail.com")
        self.users.update_group(user["group_id"], billing_policy="balance")
        credit(user["id"], 1, "admin", "native-gpt-credit")
        self.users.upsert_pricing("gpt-5.6-luna", None, billing_mode="token", input_price=1,
                                  output_price=2, cache_read_price=0.1)
        key = self.client.post("/api/keys", headers=self.user, json={"name": "native-test"}).json()["key"]
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire())):
            r = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                                 json={"model": "gpt-5.6-luna", "stream": True,
                                       "messages": [{"role": "user", "content": "ping"}]})
        self.assertEqual(r.status_code, 200, r.text)
        logs = self.users.recent_usage(user["id"])
        self.assertTrue(logs)
        row = logs[0]
        self.assertEqual(row["token_source"], "upstream")
        # 数据库 input_tokens 记未缓存部分，缓存单独进入计价快照。
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (5, 7))
        self.assertEqual(row["pricing_snapshot"]["cache_read_tokens"], 6)
        self.assertGreater(row["cost"], 0)

    def test_cache_write_survives_all_gateway_protocols_and_billing(self):
        from core.credit import credit
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        user = self.users.get_user_by_email("user@gmail.com")
        self.users.update_group(user["group_id"], billing_policy="balance")
        credit(user["id"], 1, "admin", "native-cache-write-credit")
        self.users.upsert_pricing("gpt-5.6-luna", None, billing_mode="token", input_price=1,
                                 output_price=2, cache_read_price=.1, cache_write_price=1.25)
        key = self.client.post("/api/keys", headers=self.user, json={"name": "cache-write"}).json()["key"]
        upstream = wire().replace(b'"cached_tokens": 6', b'"cached_tokens": 6, "cache_write_tokens": 3')
        for path, body, field in (
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, "cache_write_tokens"),
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}, "cache_write_tokens"),
            ("/v1/responses", {"input": "hi"}, "cache_write_tokens"),
            ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}, "cache_creation_input_tokens"),
            ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "stream": True}, "cache_creation_input_tokens"),
        ):
            with self.subTest(path=path, stream=body.get("stream")), mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(upstream)):
                response = self.client.post(path, headers={"Authorization": "Bearer " + key}, json={"model": "gpt-5.6-luna", **body})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('"' + field + '":3', response.text.replace(" ", ""))
                log = self.users.recent_usage(user["id"], limit=1)[0]
                self.assertEqual((log["input_tokens"], log["output_tokens"]), (2, 7))
                self.assertEqual((log["pricing_snapshot"]["cache_read_tokens"], log["pricing_snapshot"]["cache_write_tokens"]), (6, 3))
                self.assertAlmostEqual(log["cost"], (2 + 14 + .6 + 3.75) / 1e6)

    def test_capacity_retry_settles_once_across_gateway_protocols(self):
        from core.credit import credit
        from core import gpt_request_policy as policy
        from types import SimpleNamespace
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        user = self.users.get_user_by_email("user@gmail.com")
        self.users.update_group(user["group_id"], billing_policy="balance")
        credit(user["id"], 1, "admin", "capacity-retry-credit")
        self.users.upsert_pricing("gpt-5.6-luna", None, billing_mode="token", input_price=1,
                                 output_price=2, cache_read_price=.1, cache_write_price=1.25)
        key = self.client.post("/api/keys", headers=self.user, json={"name": "capacity-retry"}).json()["key"]
        failed = b'data: {"type":"response.failed","response":{"error":{"code":"server_is_overloaded"}}}\n\n'
        for path, body in (
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            ("/v1/responses", {"input": "hi"}),
            ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "stream": True}),
        ):
            before = self.users.get_user_by_email("user@gmail.com")["balance"]
            old_logs = len(self.users.recent_usage(user["id"], limit=1000))
            rejected, completed = io.BytesIO(failed), io.BytesIO(wire())
            with self.subTest(path=path), mock.patch("core.account_runtime.urlopen", side_effect=[rejected, completed]) as upstream, mock.patch.object(policy, "time", SimpleNamespace(sleep=mock.Mock())):
                response = self.client.post(path, headers={"Authorization": "Bearer " + key}, json={"model": "gpt-5.6-luna", **body})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("pong", response.text)
            self.assertEqual(upstream.call_count, 2)
            self.assertTrue(rejected.closed and completed.closed)
            logs = self.users.recent_usage(user["id"], limit=1000)
            self.assertEqual(len(logs), old_logs + 1)
            self.assertEqual(logs[0]["pricing_snapshot"]["end_reason"], "done")
            self.assertAlmostEqual(before - self.users.get_user_by_email("user@gmail.com")["balance"], logs[0]["cost"])
            self.assertAlmostEqual(logs[0]["cost"], (5 + 14 + .6) / 1e6)
            self.assertEqual(self.pool.list_accounts("gpt")[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
