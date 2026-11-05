"""号池管理接口门禁：列表/筛选/脱敏/启停/批量/鉴权。

这条页面的风险是「管理台把凭据明文吐给浏览器」和「停用了下一轮又被巡检放回」，
所以两类断言都写在这里，不是靠人工点一遍。
"""
import json
import os
import tempfile
import unittest
from unittest import mock
import time
import sqlite3
import base64
import io
from urllib.parse import urlsplit, parse_qs

_TMP = tempfile.mkdtemp()
# 这套用例会注册管理员，不能被套件里先跑的模块改过的环境变量影响。
os.environ["BITAPI_DB"] = os.path.join(_TMP, "pool.db")
os.environ["BITAPI_JWT_SECRET"] = "test-secret"
os.environ["BITAPI_ADMIN_KEY"] = "adm-test"
os.environ["BITAPI_REQUIRE_INVITE"] = "0"

# 库路径在 import 时就定了，所以必须在 import server 之前把核心单例重新绑定到本文件的库。
import config  # noqa: E402
config.DB_PATH = os.environ["BITAPI_DB"]
config.REQUIRE_INVITE = False

import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core.billing import Billing  # noqa: E402
from core import pool_state  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient  # noqa: E402


def _jwt(claims):
    """造一个结构合法的 JWT(gpt 账号凭据要能从里面解析出到期时间)。"""
    def enc(value):
        return base64.urlsafe_b64encode(
            json.dumps(value).encode()).decode().rstrip("=")
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".fixture"


IMPORT_ID = "import-test@pool.test"


def _gpt_token(email, exp_delta=3600):
    """gpt 账号的 access_token。identity 从 profile email 推导,不是 sub ——
    两条导入要用不同 email,否则会被当成同一个号去重。"""
    return _jwt({"exp": int(time.time()) + exp_delta,
                 "https://api.openai.com/profile": {"email": email}})


class PoolApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只换「用户库」，号池沿用进程内共享的 pool_state.DB —— 换掉它会让后面
        # 所有走 server.DB / POOL 的用例对着两个库各写一半。
        cls.fresh = UserDB(os.path.join(_TMP, "pool-users.db"))
        billing = Billing(cls.fresh, pricing=portal_state.PRICING)
        # routers.portal 自己存了一份 USER_DB，漏掉它的表现正是「注册写进旧库、
        # 分组建在新库，外键直接失败」。
        targets = (portal_state, server, portal_routes)
        cls._saved = {mod: (getattr(mod, "USER_DB", None), getattr(mod, "BILLING", None))
                      for mod in targets}
        for mod in targets:
            mod.USER_DB = cls.fresh
            mod.BILLING = billing
        portal_state.rebind(cls.fresh)
        cls.addClassCleanup(cls._restore)
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@gmail.com", "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.admin = {"Authorization": "Bearer " + r.json()["token"]}
        r = cls.client.post("/api/register", json={"email": "u@gmail.com", "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.user = {"Authorization": "Bearer " + r.json()["token"]}

    @classmethod
    def _restore(cls):
        for mod, (user_db, billing) in cls._saved.items():
            mod.USER_DB = user_db
            mod.BILLING = billing
        portal_state.rebind(cls._saved[portal_state][0])

    def setUp(self):
        pool_state.DB.delete_channel("gpt")
        self.ids = []
        for i, status in enumerate(("active", "active", "dead", "cooldown")):
            aid = pool_state.DB.upsert_account(
                "gpt", f"u{i}@pool.test",
                secret={"email": f"u{i}@pool.test", "password": f"secret-{i}"},
                meta={"note": f"note-{i}"}, status=status)
            pool_state.DB.update_account(aid, token=f"token-{i}", token_exp=0)
            self.ids.append(aid)

    def _list(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return self.client.get("/api/admin/pool" + ("?" + qs if qs else ""), headers=self.admin)

    def test_pool_categories_cover_inventory_preview_summary_export_and_bulk(self):
        key_id, account_id = self.ids[:2]
        pool_state.DB.update_account(key_id, account_type="apikey")
        for kind, expected in (("key", [key_id]), ("account", self.ids[1:])):
            listing = self._list(channel="gpt", pool_kind=kind, limit=1).json()
            self.assertEqual(listing["total"], len(expected))
            self.assertTrue(all(r["pool_kind"] == kind for r in listing["accounts"]))
            self.assertEqual(listing["accounts"][0]["today"]["requests"], 0)
            summary = self.client.get("/api/admin/pool/summary", headers=self.admin,
                params={"channel": "gpt", "pool_kind": kind}).json()
            self.assertEqual(summary["totals"]["total"], len(expected))
            self.assertEqual(summary["categories"][kind], len(expected))
            scope = {"filters": {"pool_kind": kind, "channel": "gpt"}}
            preview = self.client.post("/api/admin/pool/preview", headers=self.admin, json=scope).json()
            self.assertEqual({r["id"] for r in preview["targets"]}, set(expected))
            export = self.client.post("/api/admin/pool/export", headers=self.admin, json=scope).json()
            self.assertEqual({r["identity"] for r in export["accounts"]},
                {pool_state.DB.get_account(aid)["identity"] for aid in expected})
        mismatch = {"ids": [key_id, account_id], "filters": {"pool_kind": "key"}}
        for endpoint in ("preview", "export"):
            self.assertEqual(self.client.post("/api/admin/pool/" + endpoint, headers=self.admin, json=mismatch).status_code, 409)
        result = self.client.post("/api/admin/pool/bulk", headers=self.admin,
            json={"ids": [account_id], "action": "delete", "pool_kind": "key"}).json()
        self.assertEqual(result["failed"], 1)
        self.assertIsNotNone(pool_state.DB.get_account(account_id))
        self.assertEqual(self._list(pool_kind="unknown").status_code, 400)
        pool_state.DB.update_account(key_id, account_type="password")
        self.assertEqual(self._list(channel="gpt", pool_kind="account").json()["total"], 4)

    def test_import_category_rejects_wrong_record_before_writing(self):
        content = json.dumps({"accounts": [{"channel": "gpt", "identity": "wrong-pool.test",
            "type": "apikey", "credentials": {"api_key": "test-key"}}]})
        for endpoint in ("import/preview", "import"):
            result = self.client.post("/api/admin/pool/" + endpoint, headers=self.admin,
                json={"content": content, "pool_kind": "account"}).json()
            self.assertEqual(result["failed"], 1, result)
            self.assertIsNone(pool_state.DB.get_by_identity("gpt", "wrong-pool.test"))

    def _gpt_fixture(self):
        from adapters.gpt import GPTAdapter
        from core import adapter as registry
        adapter = GPTAdapter(db=pool_state.DB)
        old = registry.get_adapter("gpt")
        registry.register_adapter(adapter)
        self.addCleanup(registry.register_adapter, old) if old else self.addCleanup(registry.unregister_adapter, "gpt")
        aid = pool_state.DB.upsert_account("gpt", "sync-fixture@example.test",
            secret={"access_token": "at-sync-fixture", "account_id": "workspace-fixture"},
            meta={"admin_paused": True, "note": "keep-note"}, status="active")
        self.addCleanup(pool_state.DB.delete_account, "gpt", "sync-fixture@example.test")
        pool_state.DB.update_account(aid, token="at-sync-fixture", token_exp=int(time.time()) + 3600)
        return aid, adapter

    def test_gpt_sync_caches_real_profile_and_windows_without_chat_or_billing(self):
        aid, adapter = self._gpt_fixture()
        now = int(time.time())
        self.fresh.add_usage(self.fresh.get_user_by_email("root@gmail.com")["id"], channel="gpt",
            upstream_account_id=aid, input_tokens=17, account_cost=.01)
        upstream = {"rate_limit": {"primary_window": {"used_percent": 18, "limit_window_seconds": 604800,
            "reset_at": now + 604000}}, "plan_type": "plus", "account_id": "workspace-fixture"}
        profile = {"user": {"email": "sync-fixture@example.test"}, "accounts": {
            "workspace-fixture": {"account": {"account_id": "workspace-fixture", "plan_type": "plus"},
                "entitlement": {"expires_at": now + 86400}}}}
        before = self.fresh.recent_usage(self.fresh.get_user_by_email("root@gmail.com")["id"])
        resets = {"available_count": 1, "credits": [{"id": "private-credit-id", "status": "available",
            "reset_type": "codex_rate_limits", "expires_at": now + 86400}]}
        with mock.patch.object(adapter, "_account_json", side_effect=[upstream, profile, resets]) as network, \
             mock.patch.object(adapter, "chat", side_effect=AssertionError("sync must not chat")):
            first = self.client.post(f"/api/admin/pool/accounts/{aid}/sync", headers=self.admin, json={}).json()
            cached = self.client.post(f"/api/admin/pool/accounts/{aid}/sync", headers=self.admin, json={}).json()
        self.assertTrue(first["ok"], first)
        self.assertTrue(cached["cached"])
        self.assertEqual(network.call_count, 3)
        row = first["account"]
        self.assertEqual(row["email"], "sync-fixture@example.test")
        self.assertEqual(row["plan_type"], "plus")
        self.assertEqual(row["subscription_expires_at"], now + 86400)
        self.assertIsNone(row["upstream_usage"]["five_hour"])
        self.assertEqual(row["upstream_usage"]["seven_day"]["utilization"], 18)
        self.assertEqual(row["today"]["requests"], 1, "sync must not reset row's existing local usage")
        self.assertEqual(row["usage_windows"]["five_hour"]["tokens"], 17)
        self.assertEqual(row["gpt_reset_credits"]["available_count"], 1)
        self.assertEqual(row["gpt_reset_credits"]["expirations"], [now + 86400])
        self.assertNotIn("private-credit-id", json.dumps(first))
        self.assertEqual(row["note"], "keep-note")
        self.assertTrue(row["paused"])
        self.assertNotIn("at-sync-fixture", json.dumps(first))
        self.assertEqual(len(before), len(self.fresh.recent_usage(self.fresh.get_user_by_email("root@gmail.com")["id"])))

    def test_gpt_partial_sync_preserves_good_snapshot_and_concurrent_admin_edit(self):
        from core import account_management as management
        aid, adapter = self._gpt_fixture()
        good = {"primary": {"used_percent": 18, "limit_window_seconds": 604800}, "fetched_at": int(time.time())}
        stored = pool_state.DB.get_account(aid)
        pool_state.DB.update_account(aid, meta={**stored["meta"], "pool_upstream_usage": good,
            "pool_gpt_profile": {"account_id": "workspace-fixture", "subscription_expires_at": 1999999999}})
        def profile(_):
            management.patch_account(aid, {"note": "edited-during-sync", "paused": False})
            return {"account_id": "workspace-fixture", "plan_type": "pro", "subscription_error": "subscription unavailable"}
        version = management.fingerprint(pool_state.DB.get_account(aid))
        with mock.patch.object(adapter, "account_usage", side_effect=RuntimeError("temporary at-sync-fixture")), \
             mock.patch.object(adapter, "account_profile", side_effect=profile), \
             mock.patch.object(adapter, "account_reset_credits", return_value={"available_count": 1, "expirations": []}):
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/sync", headers=self.admin, json={"force": True}).json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["account"]["upstream_usage"]["seven_day"]["utilization"], 18)
        self.assertEqual(result["account"]["subscription_expires_at"], 1999999999)
        self.assertEqual(result["account"]["note"], "edited-during-sync")
        self.assertFalse(result["account"]["paused"])
        self.assertNotIn("at-sync-fixture", json.dumps(result))
        self.assertNotEqual(version, management.fingerprint(pool_state.DB.get_account(aid)))
        stable = management.fingerprint(pool_state.DB.get_account(aid))
        with mock.patch.object(adapter, "account_usage", side_effect=RuntimeError("temporarily unavailable")), \
             mock.patch.object(adapter, "account_profile", side_effect=RuntimeError("temporarily unavailable")), \
             mock.patch.object(adapter, "account_reset_credits", side_effect=RuntimeError("temporarily unavailable")):
            self.client.post(f"/api/admin/pool/accounts/{aid}/sync", headers=self.admin, json={"force": True})
        self.assertEqual(stable, management.fingerprint(pool_state.DB.get_account(aid)))

    def test_gpt_overlapping_sync_does_not_duplicate_network_calls(self):
        from core import account_management as management
        aid, adapter = self._gpt_fixture()
        def usage(row):
            duplicate = management.sync_gpt_account(row, force=True)
            self.assertTrue(duplicate["syncing"])
            return {"primary": None, "secondary": None}
        with mock.patch.object(adapter, "account_usage", side_effect=usage) as upstream, \
             mock.patch.object(adapter, "account_profile", return_value={"account_id": "workspace-fixture"}), \
             mock.patch.object(adapter, "account_reset_credits", return_value={"available_count": 0, "expirations": []}):
            management.sync_gpt_account(pool_state.DB.get_account(aid), force=True)
        upstream.assert_called_once()

    def test_unlimited_rpm_still_displays_actual_requests(self):
        from core import account_runtime
        row = pool_state.DB.get_account(self.ids[0])
        self.assertEqual(row["base_rpm"], 0)
        with account_runtime.dispatch(pool_state.DB, row):
            self.assertEqual(account_runtime.capacity(row["id"]), 1)
        self.assertEqual(account_runtime.rpm(row["id"]), 1)

    def test_summary_counts_by_channel(self):
        data = self.client.get("/api/admin/pool/summary", headers=self.admin).json()
        pool_ch = next(c for c in data["channels"] if c["channel"] == "gpt")
        self.assertEqual(pool_ch["total"], 4)
        self.assertEqual(pool_ch["by_status"]["active"], 2)
        self.assertEqual(pool_ch["by_status"]["dead"], 1)
        self.assertEqual(data["totals"]["total"], 4)

    def test_list_never_leaks_credentials(self):
        body = self._list()
        self.assertEqual(body.status_code, 200)
        raw = body.text
        for leaked in ("secret-", "token-"):
            self.assertNotIn(leaked, raw, "凭据明文出现在号池响应里")
        for account in body.json()["accounts"]:
            self.assertNotIn("secret", account)
            self.assertNotIn("token", account)
        self.assertTrue(all("has_api_key" in a for a in body.json()["accounts"]))

    def test_filters_and_paging(self):
        self.assertEqual(self._list(status="dead").json()["total"], 1)
        self.assertEqual(self._list(q="u1@").json()["total"], 1)
        self.assertEqual(self._list(channel="gpt").json()["total"], 4)
        page = self._list(limit=2, offset=0).json()
        self.assertEqual(len(page["accounts"]), 2)
        self.assertEqual(page["total"], 4)
        self.assertEqual(self._list(channel="nope").status_code, 404)
        self.assertEqual(self._list(status="nope").status_code, 400)

    def test_pause_hides_account_from_pool_selection(self):
        """停用必须连取号器一起挡住：只翻 status 不够，巡检会把能用的号放回 active。"""
        self.client.patch(f"/api/admin/pool/accounts/{self.ids[0]}", headers=self.admin,
                          json={"paused": True})
        self.assertEqual(self._list(status="paused").json()["total"], 1)
        for _ in range(8):
            picked = pool_state.DB.pick_active("gpt")
            self.assertIsNotNone(picked)
            self.assertNotEqual(picked["identity"], "u0@pool.test",
                                "已停用的号仍被取号器选中")
        self.client.patch(f"/api/admin/pool/accounts/{self.ids[0]}", headers=self.admin,
                          json={"paused": False})
        seen = {pool_state.DB.pick_active("gpt")["identity"] for _ in range(20)}
        self.assertIn("u0@pool.test", seen)

    def test_note_and_status_update(self):
        r = self.client.patch(f"/api/admin/pool/accounts/{self.ids[1]}", headers=self.admin,
                              json={"note": "备用号"})
        self.assertEqual(r.json()["account"]["note"], "备用号")
        r = self.client.patch(f"/api/admin/pool/accounts/{self.ids[1]}", headers=self.admin,
                              json={"status": "exhausted"})
        self.assertEqual(r.json()["account"]["status"], "exhausted")
        self.assertEqual(self.client.patch(f"/api/admin/pool/accounts/{self.ids[1]}",
                                           headers=self.admin,
                                           json={"status": "nope"}).status_code, 400)

    def test_bulk_actions_report_per_account(self):
        r = self.client.post("/api/admin/pool/bulk", headers=self.admin,
                             json={"ids": self.ids, "action": "reset"}).json()
        self.assertEqual((r["success"], r["failed"]), (4, 0))
        self.assertEqual(self._list(status="unchecked").json()["total"], 4)

        r = self.client.post("/api/admin/pool/bulk", headers=self.admin,
                             json={"ids": [self.ids[2], 999999], "action": "delete"}).json()
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(self._list().json()["total"], 3)

        r = self.client.post("/api/admin/pool/bulk", headers=self.admin,
                             json={"ids": self.ids[:2], "action": "pause"}).json()
        self.assertEqual(r["success"], 2)
        self.assertEqual(self._list(status="paused").json()["total"], 2)

    def test_requires_admin(self):
        self.assertEqual(self.client.get("/api/admin/pool", headers=self.user).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/pool/summary", headers=self.user).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/pool/bulk", headers=self.user,
                                          json={"ids": [1], "action": "reset"}).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/pool").status_code, 401)

    def test_usage_is_owned_by_account_and_unattributed_history_is_separate(self):
        uid = self.fresh._conn().execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        for aid, tokens, cost in ((self.ids[0], 17, 2.5), (self.ids[1], 31, 4)):
            self.fresh.add_usage(uid, channel="gpt", model="m", input_tokens=tokens,
                                 upstream_account_id=aid, account_cost=cost)
        self.fresh.add_usage(uid, channel="gpt", model="m", input_tokens=900)
        rows = {r["id"]: r for r in self._list().json()["accounts"]}
        self.assertEqual(rows[self.ids[0]]["today"]["tokens"], 17)
        self.assertEqual(rows[self.ids[1]]["today"]["tokens"], 31)
        self.assertEqual(rows[self.ids[0]]["today"]["cost"], 2.5)
        self.assertEqual(rows[self.ids[2]]["today"]["requests"], 0)
        self.assertEqual(rows[self.ids[3]]["today"]["tokens"], 0)
        stats = self.client.get(f"/api/admin/pool/accounts/{self.ids[0]}/stats", headers=self.admin).json()
        self.assertEqual(stats["summary"]["requests"], 1)
        self.assertEqual(stats["models"][0]["tokens"], 17)
        self.assertGreaterEqual(self.client.get("/api/admin/pool/summary", headers=self.admin).json()["unassigned_today"], 1)

    def test_filtered_preview_and_pagination_include_accounts_without_usage(self):
        self.client.patch(f"/api/admin/pool/accounts/{self.ids[1]}", headers=self.admin,
                          json={"name": "Primary", "note": "find_me"})
        data = self._list(q="find_me", limit=1).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["accounts"][0]["id"], self.ids[1])
        target = self.client.post("/api/admin/pool/preview", headers=self.admin,
                                  json={"filters": {"q": "find_me"}}).json()
        self.assertEqual(target["count"], 1)
        self.assertEqual(target["targets"][0]["id"], self.ids[1])
        summary = self.client.get("/api/admin/pool/summary?q=find_me", headers=self.admin).json()
        self.assertEqual(summary["totals"]["total"], 1)

    def test_bulk_preview_detects_setting_changes_but_not_normal_traffic(self):
        aid = self.ids[0]
        def preview():
            return self.client.post("/api/admin/pool/preview", headers=self.admin, json={"ids": [aid]}).json()["targets"][0]["version"]
        version = preview()
        pool_state.DB.update_account(aid, last_used_at=int(time.time()))
        self.assertEqual(version, preview())
        pool_state.DB.update_account(aid, priority=1)
        result = self.client.post("/api/admin/pool/bulk", headers=self.admin,
            json={"ids": [aid], "action": "delete", "versions": {str(aid): version}}).json()
        self.assertEqual(result["failed"], 1)
        self.assertIsNotNone(pool_state.DB.get_account(aid))

    def test_account_settings_change_real_pool_eligibility(self):
        aid = self.ids[1]
        self.client.patch(f"/api/admin/pool/accounts/{aid}", headers=self.admin, json={"priority": 0})
        self.assertEqual(pool_state.DB.pick_active("gpt")["id"], aid)
        self.client.patch(f"/api/admin/pool/accounts/{aid}", headers=self.admin,
                          json={"account_expires_at": int(time.time()) - 10})
        self.assertEqual(self._list(status="expired").json()["total"], 1)
        self.assertNotEqual(pool_state.DB.pick_active("gpt")["id"], aid)
        self.client.patch(f"/api/admin/pool/accounts/{aid}", headers=self.admin,
                          json={"account_expires_at": 0, "temp_until": int(time.time()) + 300})
        self.assertNotEqual(pool_state.DB.pick_active("gpt")["id"], aid)
        self.assertEqual(self._list(status="temporary").json()["total"], 1)

    def test_capacity_group_binding_and_quota_reset_affect_selection(self):
        from core import account_runtime as runtime, account_management as management
        class Bare:
            def has(self, cap):
                return False
        adapter = Bare()
        aid = self.ids[0]
        pool_state.DB.update_account(aid, priority=0, concurrency=1)
        row = pool_state.DB.get_account(aid)
        with runtime.dispatch(pool_state.DB, row, record=False):
            self.assertEqual(runtime.capacity(aid), 1)
            self.assertNotEqual(pool_state.POOL.get_valid_account("gpt", adapter)["id"], aid)
        self.assertEqual(runtime.capacity(aid), 0)
        self.assertEqual(pool_state.POOL.get_valid_account("gpt", adapter)["id"], aid)
        gid = self.fresh.create_group("pool-group-" + str(aid))
        pool_state.DB.update_account(aid, group_ids=[gid])
        self.assertEqual(pool_state.DB.pick_active("gpt", group_id=gid)["id"], aid)
        self.assertNotEqual(pool_state.DB.pick_active("gpt", group_id=gid + 1000)["id"], aid)
        pool_state.DB.update_account(aid, group_ids=[], quota_daily_limit=1)
        uid = self.fresh._conn().execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        self.fresh.add_usage(uid, channel="gpt", upstream_account_id=aid, account_cost=1)
        self.assertNotEqual(pool_state.POOL.get_valid_account("gpt", adapter)["id"], aid)
        management.operate(pool_state.DB.get_account(aid), "reset_quota")
        self.assertEqual(pool_state.POOL.get_valid_account("gpt", adapter)["id"], aid)
        # A new request in the same second must count after the reset watermark.
        self.fresh.add_usage(uid, channel="gpt", upstream_account_id=aid, account_cost=1)
        self.assertNotEqual(pool_state.POOL.get_valid_account("gpt", adapter)["id"], aid)

    def test_proxy_paste_only_autodetects_formats_generates_name_and_preserves_existing_name(self):
        layouts = ["paste.example.test:8080:fixture:secret", "fixture:secret@paste.example.test:8080",
                   "fixture:secret:paste.example.test:8080", "paste.example.test:8080@fixture:secret",
                   "http://fixture:secret@paste.example.test:8080"]
        ids = set()
        for value in layouts:
            with self.subTest(value=value):
                response = self.client.post("/api/admin/pool/proxies", headers=self.admin, json={"url": value})
                self.assertEqual(response.status_code, 200, response.text)
                row = next(p for p in response.json()["proxies"] if p["address"] == "http://paste.example.test:8080")
                self.assertEqual(row["name"], "代理 " + str(row["id"]))
                self.assertEqual(pool_state.DB.get_pool_proxy(row["id"])["url"], layouts[-1])
                ids.add(row["id"])
        self.assertEqual(len(ids), 1)
        self.client.post("/api/admin/pool/proxies", headers=self.admin, json={"url": layouts[-1], "name": "11"})
        response = self.client.post("/api/admin/pool/proxies", headers=self.admin, json={"url": layouts[0]})
        row = next(p for p in response.json()["proxies"] if p["id"] in ids)
        self.assertEqual(row["name"], "11")
        from core.account_management import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("https://fixture:secret@paste.example.test:8443"),
                         "https://fixture:secret@paste.example.test:8443")
        self.assertEqual(normalize_proxy_url("paste.example.test:8080"), "http://paste.example.test:8080")
        with self.assertRaisesRegex(ValueError, "完整地址"):
            normalize_proxy_url("host:8080:user:1234")

    def test_legacy_proxy_form_accepts_compact_credentials_without_format(self):
        compact = "gate-sg.ipfoxy.io:58688:customer-fixture-cc-US-sessid-1793691991_10000:fixture-password"
        response = self.client.post("/api/admin/pool/proxies", headers=self.admin,
            json={"name": "Legacy compact fixture", "url": compact})
        self.assertEqual(response.status_code, 200, response.text)
        proxy = next(p for p in response.json()["proxies"] if p["name"] == "Legacy compact fixture")
        self.assertEqual(pool_state.DB.get_pool_proxy(proxy["id"])["url"],
            "http://customer-fixture-cc-US-sessid-1793691991_10000:fixture-password@gate-sg.ipfoxy.io:58688")
        self.assertNotIn("fixture-password", response.text)
        self.assertEqual(self.client.post("/api/admin/pool/proxies", headers=self.admin,
            json={"name": "Explicit URL fixture", "url": compact, "format": "url"}).status_code, 400)

    def test_proxy_formats_share_canonical_storage_and_encoded_credentials(self):
        from core import account_runtime as runtime
        password = "pa:ss@word%#/?https://tail"
        layouts = {
            "host_port_user_pass": "proxy.example.test:8080:user+name:" + password,
            "user_pass_at_host_port": "user+name:" + password + "@proxy.example.test:8080",
            "user_pass_host_port": "user+name:" + password + ":proxy.example.test:8080",
            "host_port_at_user_pass": "proxy.example.test:8080@user+name:" + password,
            "url": "http://user%2Bname:pa%3Ass%40word%25%23%2F%3Fhttps%3A%2F%2Ftail@proxy.example.test:8080",
        }
        expected = layouts["url"]
        saved_ids = set()
        for layout, value in layouts.items():
            with self.subTest(layout=layout):
                response = self.client.post("/api/admin/pool/proxies", headers=self.admin,
                    json={"name": "Format fixture", "url": value, "format": layout})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(password, response.text)
                self.assertNotIn("pa%3Ass", response.text)
                record = next(p for p in response.json()["proxies"] if p["name"] == "Format fixture")
                saved_ids.add(record["id"])
                self.assertTrue(record["authenticated"])
                self.assertEqual(pool_state.DB.get_pool_proxy(record["id"])["url"], expected)
        self.assertEqual(len(saved_ids), 1)
        proxy_id = saved_ids.pop()
        row = dict(pool_state.DB.get_account(self.ids[0]), channel="gpt", proxy_id=proxy_id)
        with runtime.network(pool_state.DB, row), mock.patch("core.gpt_transport.open_request") as transport:
            runtime.urlopen("https://upstream.example.test", 30)
        self.assertEqual(transport.call_args.args[2], expected)

    def test_proxy_connectivity_uses_selected_proxy_and_expected_gpt_auth_response(self):
        import urllib.error
        value = self.client.post("/api/admin/pool/proxies", headers=self.admin,
            json={"name": "Probe fixture", "url": "http://fixture:proxy-secret@proxy.example.test:8080", "enabled": False}).json()
        proxy_id = next(p["id"] for p in value["proxies"] if p["name"] == "Probe fixture")
        before = pool_state.DB.get_pool_proxy(proxy_id)
        error = urllib.error.HTTPError("https://chatgpt.com", 401, "Unauthorized",
            {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"unauthorized"}'))
        with mock.patch("core.gpt_transport.open_request", side_effect=error) as network:
            response = self.client.post(f"/api/admin/pool/proxies/{proxy_id}/test", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertGreaterEqual(result["latency_ms"], 0)
        request, timeout, proxy_url = network.call_args.args
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.startswith(config.GPT_BASE_URL + "/models?"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual((timeout, proxy_url), (10, before["url"]))
        self.assertEqual(pool_state.DB.get_pool_proxy(proxy_id), before)
        self.assertNotIn("proxy-secret", response.text)
        with mock.patch("core.gpt_transport.open_request") as network:
            self.assertEqual(self.client.post(f"/api/admin/pool/proxies/{proxy_id}/test", headers=self.user).status_code, 403)
            self.assertEqual(self.client.post("/api/admin/pool/proxies/999999/test", headers=self.admin).status_code, 404)
            network.assert_not_called()

    def test_proxy_connectivity_reports_gpt_block_timeout_and_proxy_auth_without_secrets(self):
        import urllib.error
        from core.account_management import test_proxy
        cases = [
            (urllib.error.HTTPError("https://chatgpt.com", 403, "Forbidden", {}, io.BytesIO(b"blocked")), "403", 403),
            (urllib.error.HTTPError("https://chatgpt.com", 401, "Unauthorized", {"Content-Type": "text/html"}, io.BytesIO(b"challenge")), "非 API", 401),
            (TimeoutError("proxy-secret timeout"), "超时", None),
            (RuntimeError("CONNECT tunnel failed, response 407 http://fixture:proxy-secret@host:8080"), "代理认证失败", None),
            (RuntimeError("proxy-secret: connection refused"), "无法通过此代理", None),
        ]
        for error, message, status in cases:
            with self.subTest(error=type(error).__name__), mock.patch("core.gpt_transport.open_request", side_effect=error):
                result = test_proxy({"url": "http://fixture:proxy-secret@host:8080"})
                self.assertFalse(result["ok"])
                self.assertEqual(result["http_status"], status)
                self.assertIn(message, result["message"])
                self.assertNotIn("proxy-secret", json.dumps(result))
                self.assertEqual(result["latency_ms"] is None, status is None)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        with mock.patch("core.gpt_transport.open_request", return_value=response):
            self.assertTrue(test_proxy({"url": "http://host:8080"})["ok"])
        response.__exit__.assert_called_once()

    def test_proxy_formats_validate_addresses_and_support_https_ipv6_and_legacy_url(self):
        from core.account_management import normalize_proxy_url
        for layout, value in (
            ("host_port_user_pass", "[2001:db8::1]:8443:user:password"),
            ("user_pass_at_host_port", "user:password@[2001:db8::1]:8443"),
            ("user_pass_host_port", "user:password:[2001:db8::1]:8443"),
            ("host_port_at_user_pass", "[2001:db8::1]:8443@user:password"),
        ):
            with self.subTest(layout=layout):
                self.assertEqual(normalize_proxy_url(value, layout, "https"),
                                 "https://user:password@[2001:db8::1]:8443")
        for value in ("http://localhost:8080", "https://localhost"):
            response = self.client.post("/api/admin/pool/proxies", headers=self.admin,
                json={"name": "Legacy fixture", "url": value})
            self.assertEqual(response.status_code, 200, response.text)
        for layout, value in (
            ("host_port_user_pass", "host:8080:user"),
            ("host_port_user_pass", "host:70000:user:password"),
            ("host_port_user_pass", "host:0:user:password"),
            ("user_pass_host_port", "user:password:host:not-a-port"),
            ("user_pass_at_host_port", "user:password@host"),
            ("host_port_at_user_pass", "host:8080@user"),
            ("url", "socks5://host:8080"),
        ):
            with self.subTest(layout=layout, value=value):
                response = self.client.post("/api/admin/pool/proxies", headers=self.admin,
                    json={"name": "Invalid fixture", "url": value, "format": layout})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn("password", response.text)

    def test_sub2api_proxy_import_is_portable_and_credentials_are_redacted(self):
        from adapters.openai_compat import OpenAICompatAdapter
        from core import adapter as registry
        from core import account_runtime as runtime
        adapter = OpenAICompatAdapter(name="pool-transfer-test", base_url="https://example.test/v1", models=["test-model"])
        registry.register_adapter(adapter)
        self.addCleanup(registry.unregister_adapter, adapter.name)
        self.addCleanup(pool_state.DB.delete_channel, adapter.name)
        content = json.dumps({"proxies": [{"proxy_key": "source-id", "name": "Fixture proxy", "protocol": "http",
            "host": "127.0.0.1", "port": 19999, "username": "fixture", "password": "proxy-secret", "status": "active"}],
            "accounts": [{"platform": adapter.name, "type": "apikey", "name": "Portable fixture",
                "credentials": {"api_key": "portable-secret"}, "proxy_key": "source-id", "priority": 4, "concurrency": 2}]})
        response = self.client.post("/api/admin/pool/import", headers=self.admin, json={"content": content})
        self.assertEqual(response.json()["imported"], 1, response.text)
        row = pool_state.DB.list_accounts(channel=adapter.name)[0]
        self.assertIsNotNone(row["proxy_id"])
        ordinary = self.client.get("/api/admin/pool/options", headers=self.admin)
        self.assertNotIn("proxy-secret", ordinary.text)
        self.assertNotIn("portable-secret", self._list(channel=adapter.name).text)
        with runtime.network(pool_state.DB, row), mock.patch("urllib.request.build_opener") as opener:
            runtime.urlopen("https://example.test", timeout=5)
            opener.return_value.open.assert_called_once_with("https://example.test", timeout=5)
        exported = self.client.post("/api/admin/pool/export", headers=self.admin, json={"ids": [row["id"]]}).json()
        self.assertEqual(exported["proxies"][0]["password"], "proxy-secret")
        self.assertNotIn("proxy_id", exported["accounts"][0])
        pool_state.DB.delete_channel(adapter.name)
        self.client.post("/api/admin/pool/import", headers=self.admin, json={"content": json.dumps(exported)})
        self.assertEqual(pool_state.DB.list_accounts(channel=adapter.name)[0]["priority"], 4)

    def test_rpm_gate_skips_saturated_account_and_recovers_after_window(self):
        from core import account_runtime as runtime
        class Bare:
            def has(self, cap):
                return False
        aid = self.ids[0]
        pool_state.DB.update_account(aid, priority=0, base_rpm=1)
        row = pool_state.DB.get_account(aid)
        now = time.time()
        with runtime.dispatch(pool_state.DB, row, record=False):
            pass
        self.assertEqual(runtime.rpm(aid), 1)
        self.assertNotEqual(pool_state.POOL.get_valid_account("gpt", Bare())["id"], aid)
        with mock.patch("core.account_runtime.time.time", return_value=now + 61):
            self.assertEqual(runtime.rpm(aid), 0)
        self.assertEqual(pool_state.POOL.get_valid_account("gpt", Bare())["id"], aid)

    def test_oauth_pkce_is_admin_bound_and_single_use_and_never_returns_tokens(self):
        from core import gpt_oauth
        start = self.client.post("/api/admin/pool/oauth/gpt/start", headers=self.admin, json={})
        self.assertEqual(start.status_code, 200, start.text)
        data = start.json()
        query = parse_qs(urlsplit(data["auth_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", data)
        callback = "http://localhost:1455/auth/callback?code=fixture-code&state=" + query["state"][0]
        admin_id = self.fresh.get_user_by_email("root@gmail.com")["id"]
        with self.assertRaises(ValueError):
            gpt_oauth.finish(admin_id + 1, data["session_id"], callback)
        claims = {"exp": int(time.time()) + 3600, "email": "oauth-fixture@example.test",
                  "https://api.openai.com/auth": {"chatgpt_account_id": "oauth-fixture", "chatgpt_user_id": "oauth-user"}}
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        token = encode({"alg": "RS256"}) + "." + encode(claims) + ".fixture-signature"
        credentials = {"access_token": token, "id_token": token, "refresh_token": "fixture-refresh-token"}
        request = {"session_id": data["session_id"], "callback_url": callback}
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(json.dumps(credentials).encode())) as opener:
            finish = self.client.post("/api/admin/pool/oauth/gpt/finish", headers=self.admin, json=request)
        self.assertEqual(finish.status_code, 200, finish.text)
        self.assertNotIn(token, finish.text)
        self.assertNotIn("fixture-refresh-token", finish.text)
        aid = finish.json()["account"]["id"]
        self.addCleanup(pool_state.DB.delete_account, "gpt", pool_state.DB.get_account(aid)["identity"])
        posted = parse_qs(opener.call_args.args[0].data.decode())
        self.assertIn("code_verifier", posted)
        replay = self.client.post("/api/admin/pool/oauth/gpt/finish", headers=self.admin, json=request)
        self.assertEqual(replay.status_code, 400)

    def test_gpt_upstream_windows_keep_missing_window_unknown(self):
        from adapters.gpt import GPTAdapter
        from core import adapter as registry
        adapter = GPTAdapter(db=pool_state.DB)
        adapter.name = "pool-usage-test"
        registry.register_adapter(adapter)
        self.addCleanup(registry.unregister_adapter, adapter.name)
        self.addCleanup(pool_state.DB.delete_channel, adapter.name)
        aid = pool_state.DB.upsert_account(adapter.name, "usage-fixture", secret={"access_token": "test"}, status="active")
        pool_state.DB.update_account(aid, token="test", token_exp=int(time.time()) + 3600)
        raw = {"rate_limit": {"primary_window": None, "secondary_window": {"used_percent": 25,
            "reset_at": int(time.time()) + 100, "limit_window_seconds": 604800}}, "plan_type": "plus"}
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(json.dumps(raw).encode())):
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/action/usage", headers=self.admin)
        self.assertEqual(result.status_code, 200, result.text)
        windows = result.json()["account"]["upstream_usage"]
        self.assertIsNone(windows["primary"])
        self.assertEqual(windows["secondary"]["used_percent"], 25)
        self.assertEqual(self.client.get(f"/api/admin/pool/accounts/{aid}", headers=self.admin).json()["account"]["today"]["requests"], 0)

    def test_import_preview_is_read_only_and_export_round_trips(self):
        # gpt 只认 OAuth 凭据:access_token 必须是能解析出到期时间的 JWT,
        # 否则 normalize_entry 以「无法解析 accessToken 到期时间」拒收。
        token = _gpt_token(IMPORT_ID)
        content = json.dumps({"accounts": [{"channel": "gpt", "identity": "import-test", "name": "Imported",
                    "credentials": {"access_token": token}, "priority": 3, "concurrency": 2}]})
        request = {"content": content}
        preview = self.client.post("/api/admin/pool/import/preview", headers=self.admin, json=request)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIsNone(pool_state.DB.get_by_identity("gpt", IMPORT_ID))
        result = self.client.post("/api/admin/pool/import", headers=self.admin, json=request).json()
        self.assertEqual(result["imported"], 1, result)
        row = pool_state.DB.get_by_identity("gpt", IMPORT_ID)
        self.assertEqual(row["priority"], 3)
        exported = self.client.post("/api/admin/pool/export", headers=self.admin, json={"ids": [row["id"]]})
        self.assertEqual(exported.headers["cache-control"], "no-store")
        self.assertEqual(exported.json()["accounts"][0]["credentials"]["access_token"], token)
        again = self.client.post("/api/admin/pool/import", headers=self.admin,
                                  json={"content": exported.text}).json()
        self.assertEqual(again["skipped"], 1, again)

    def test_duplicate_copies_static_credentials_without_returning_them(self):
        from adapters.openai_compat import OpenAICompatAdapter
        from core import adapter as registry
        adapter = OpenAICompatAdapter(name="pool-copy-test", base_url="https://example.test/v1", models=["fixture"])
        registry.register_adapter(adapter)
        self.addCleanup(registry.unregister_adapter, adapter.name)
        self.addCleanup(pool_state.DB.delete_channel, adapter.name)
        created = self.client.post("/api/admin/pool/accounts", headers=self.admin, json={"channel": adapter.name,
            "identity": "source", "name": "Source", "account_type": "apikey", "credentials": {"api_key": "copy-secret"}, "priority": 7})
        aid = created.json()["account"]["id"]
        result = self.client.post(f"/api/admin/pool/accounts/{aid}/duplicate", headers=self.admin,
                                 json={"identity": "copy", "name": "Copy"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("copy-secret", result.text)
        row = pool_state.DB.get_by_identity(adapter.name, "copy")
        self.assertEqual(row["secret"]["api_key"], "copy-secret")
        self.assertEqual(row["priority"], 7)
        self.assertEqual(pool_state.DB.get_account(aid)["identity"], "source")

    def test_multiple_import_files_and_gpt_preview_share_real_deduplication(self):
        from core import gpt_accounts
        claims = {"exp": int(time.time()) + 3600, "email": "canonical@example.test",
                  "https://api.openai.com/auth": {"chatgpt_account_id": "preview-account", "chatgpt_user_id": "preview-user"}}
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        token = encode({"alg": "RS256"}) + "." + encode(claims) + ".fixture"
        secret = {"access_token": token, "refresh_token": "preview-refresh"}
        original = gpt_accounts.import_accounts(pool_state.DB, [secret])["items"][0]
        aid = original["account_id"]
        row = pool_state.DB.get_account(aid)
        self.addCleanup(pool_state.DB.delete_account, "gpt", row["identity"])
        pool_state.DB.update_account(aid, meta={**row["meta"], "admin_paused": True, "keep": "retained"})
        incoming = json.dumps({"accounts": [{"identity": "a-different-label", "channel": "gpt", "credentials": secret, "note": "new-note"}]})
        request = {"contents": [incoming]}
        preview = self.client.post("/api/admin/pool/import/preview", headers=self.admin, json=request).json()
        self.assertEqual(preview["skipped"], 1, preview)
        self.assertNotIn("note", pool_state.DB.get_account(aid)["meta"])
        request["update_existing"] = True
        preview = self.client.post("/api/admin/pool/import/preview", headers=self.admin, json=request).json()
        self.assertEqual(preview["updated"], 1, preview)
        actual = self.client.post("/api/admin/pool/import", headers=self.admin, json=request).json()
        self.assertEqual(actual["updated"], 1, actual)
        after = pool_state.DB.get_account(aid)
        self.assertTrue(after["meta"]["admin_paused"])
        self.assertEqual(after["meta"]["keep"], "retained")
        self.assertEqual(after["meta"]["note"], "new-note")
        # gpt 只认 OAuth 凭据(access_token / refresh_token),裸 api_key 一律拒。
        # 这里特意混入一条非 gpt 形状的 key,断言它被拒而不是被当成 access_token。
        mixed = self.client.post("/api/admin/pool/import/preview", headers=self.admin,
            json={"channel": "gpt", "contents": [json.dumps(
                {"accounts": [{"identity": "file-one",
                               "credentials": {"access_token": _gpt_token("mixed-one@pool.test"),
                                               "refresh_token": "mixed-refresh"}}]},
                indent=2)]}).json()
        self.assertEqual(mixed["imported"], 1, mixed)
        rejected = self.client.post("/api/admin/pool/import/preview", headers=self.admin,
            json={"channel": "gpt", "contents": [json.dumps(
                {"accounts": [{"identity": "file-two", "credentials": {"api_key": "one"}}]})]}).json()
        self.assertEqual(rejected["failed"], 1, rejected)

    def test_tests_and_plans_preserve_admin_pause_and_do_not_create_billing_logs(self):
        from core import account_management as management
        aid = self.ids[0]
        self.client.patch(f"/api/admin/pool/accounts/{aid}", headers=self.admin, json={"paused": True})
        adapter = management.adapter_of(pool_state.DB.get_account(aid))
        model = adapter.models[0]
        response = {"raw": {"choices": [{"message": {"content": "pong"}}]}}
        with mock.patch.object(adapter, "chat", return_value=response) as chat, mock.patch.object(
                adapter, "refresh_token", return_value={"token": "test-access", "token_exp": int(time.time()) + 3600}):
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": model})
            self.assertTrue(result.json()["ok"], result.text)
            self.assertEqual(chat.call_args.args[1], [{"role": "user", "content": "hi"}])
            self.assertEqual(result.json()["reply"], "pong")
            self.assertEqual(result.json()["prompt"], "hi")
        self.assertTrue((pool_state.DB.get_account(aid)["meta"])["admin_paused"])
        self.assertEqual(self._list(q="u0@").json()["accounts"][0]["today"]["requests"], 0)
        history = self.client.get(f"/api/admin/pool/accounts/{aid}/tests", headers=self.admin).json()
        self.assertEqual(len(history["history"]), 1)
        plan = self.client.put(f"/api/admin/pool/accounts/{aid}/plan", headers=self.admin,
                               json={"model": model, "interval_seconds": 300, "enabled": True})
        self.assertEqual(plan.status_code, 200, plan.text)

    def test_gpt_catalog_preserves_successful_manual_models_without_inference_on_read(self):
        aid, adapter = self._gpt_fixture()
        with mock.patch.object(adapter, "account_models") as fetch:
            first = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin).json()
        self.assertEqual(first["models"], [])
        self.assertEqual(first["source"], "verified")
        fetch.assert_not_called()  # Opening a picker does not consume upstream quota.
        answer = "Hello! " + "A genuine answer. " * 50
        with mock.patch.object(adapter, "chat", return_value={"raw": {"choices": [{"message": {"content": answer}}]}}) as chat:
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin,
                json={"model": "gpt-manually-entered"}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(chat.call_args.kwargs["model"], "gpt-manually-entered")
        self.assertEqual(chat.call_args.args[1], [{"role": "user", "content": "hi"}])
        self.assertEqual(result["reply"], answer)
        history = self.client.get(f"/api/admin/pool/accounts/{aid}/tests", headers=self.admin).json()
        self.assertEqual(history["history"][0]["reply"], answer)
        catalog = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin).json()
        self.assertEqual([m["id"] for m in catalog["models"]], ["gpt-manually-entered"])
        self.assertGreater(catalog["models"][0]["verified_at"], 0)
        self.assertIn("gpt-manually-entered", adapter.models)
        from core.gpt_models import entries
        self.assertIn("gpt-manually-entered", [m["slug"] for m in entries(pool_state.DB.get_account(aid), [])])
        plan = self.client.put(f"/api/admin/pool/accounts/{aid}/plan", headers=self.admin,
            json={"model": "gpt-manually-entered", "interval_seconds": 300, "enabled": False})
        self.assertEqual(plan.status_code, 200, plan.text)
        invalid = self.client.put(f"/api/admin/pool/accounts/{aid}/plan", headers=self.admin,
            json={"model": "invalid model", "interval_seconds": 300, "enabled": False})
        self.assertEqual(invalid.status_code, 400)

    def _image_events(self, row, model="gpt-6.1-terra", prompt="画一只橘猫"):
        response = self.client.post(f"/api/admin/pool/accounts/{row}/test-image", headers=self.admin,
                                    json={"model": model, "prompt": prompt})
        self.assertEqual(response.status_code, 200, response.text)
        frames = [frame.split("\ndata: ", 1) for frame in response.text.strip().split("\n\n")]
        return [(event.removeprefix("event: "), json.loads(data)) for event, data in frames if event.startswith("event:")]

    def test_image_probe_returns_preview_for_exact_account_without_billing_or_model_check_changes(self):
        from core import account_runtime
        aid, adapter = self._gpt_fixture()
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture-image").decode()
        completed = {"type": "response.completed", "response": {"status": "completed",
                     "output": [{"type": "image_generation_call", "result": encoded}],
                     "usage": {"input_tokens": 11, "output_tokens": 2}}}
        calls = []
        def upstream(row, payload):
            self.assertIsNone(account_runtime.bounded_timeout(), "image generation must outlive the 30-second setup budget")
            calls.append((row["id"], payload))
            return io.BytesIO(("data: " + json.dumps(completed) + "\n\n").encode())
        with mock.patch.object(adapter, "_open_codex", side_effect=upstream), mock.patch.object(server.BILLING, "record_usage") as bill:
            events = self._image_events(aid)
        self.assertEqual(events[0][0], "progress")
        result = events[-1][1]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["images"], [{"b64_json": encoded, "mime_type": "image/png"}])
        self.assertEqual(result["kind"], "image")
        self.assertEqual(calls[0][0], aid)
        self.assertEqual(calls[0][1]["tools"][0]["type"], "image_generation")
        self.assertEqual(calls[0][1]["tool_choice"], {"type": "image_generation"})
        self.assertTrue(calls[0][1]["stream"])
        bill.assert_not_called()
        row = pool_state.DB.get_account(aid)
        self.assertTrue(row["meta"]["admin_paused"])
        self.assertNotIn("pool_model_checks", row["meta"])
        history = self.client.get(f"/api/admin/pool/accounts/{aid}/tests", headers=self.admin).json()["history"]
        self.assertEqual(history[0]["kind"], "image")
        self.assertNotIn("images", history[0])
        self.assertNotIn(encoded, json.dumps(history))

    def test_image_probe_rejects_permissions_policy_and_empty_prompt_before_call(self):
        aid, adapter = self._gpt_fixture()
        path = f"/api/admin/pool/accounts/{aid}/test-image"
        body = {"model": "gpt-6.1-terra", "prompt": "cat"}
        with mock.patch.object(adapter, "_open_codex") as upstream:
            self.assertEqual(self.client.post(path, headers=self.user, json=body).status_code, 403)
            self.assertEqual(self.client.post(path, json=body).status_code, 401)
            self.assertEqual(self.client.post(path, headers=self.admin, json={**body, "prompt": "  "}).status_code, 400)
            # 生图测试只支持 GPT 账号:非 gpt 渠道的账号必须在发请求前就被拒
            # (account_image_test.prepare 第一条即校验渠道)。
            other = pool_state.DB.upsert_account(
                "sharepool-imgtest", "not-gpt@pool.test",
                secret={"api_key": "k"}, meta={}, status="active")
            self.addCleanup(pool_state.DB.delete_account, "sharepool-imgtest",
                            "not-gpt@pool.test")
            self.assertEqual(self.client.post(
                f"/api/admin/pool/accounts/{other}/test-image",
                headers=self.admin, json=body).status_code, 400)
            row = pool_state.DB.get_account(aid)
            for policy in ({"image_tool": "disabled"}, {"model_whitelist": ["other"]}):
                pool_state.DB.update_account(aid, meta={**row["meta"], "gpt_settings": policy})
                self.assertEqual(self.client.post(path, headers=self.admin, json=body).status_code, 400)
            upstream.assert_not_called()

    def test_image_probe_failure_keeps_chat_account_and_history_has_reason(self):
        aid, adapter = self._gpt_fixture()
        wire = 'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"text only"}]}]}}\n\n'
        with mock.patch.object(adapter, "_open_codex", return_value=io.BytesIO(wire.encode())), mock.patch.object(pool_state.POOL, "mark_failure") as penalize:
            result = self._image_events(aid)[-1][1]
        self.assertFalse(result["ok"])
        self.assertIn("未返回生成图片", result["error"])
        penalize.assert_not_called()
        self.assertEqual(pool_state.DB.get_account(aid)["status"], "active")
        self.assertNotIn("pool_model_checks", pool_state.DB.get_account(aid)["meta"])
        history = self.client.get(f"/api/admin/pool/accounts/{aid}/tests", headers=self.admin).json()["history"]
        self.assertFalse(history[0]["ok"])

    def test_image_probe_slot_is_released_on_idle_timeout_and_busy_does_not_call_upstream(self):
        from core import account_runtime, sse
        aid, adapter = self._gpt_fixture()
        for error in (sse.StreamIdleTimeout("no model output"), account_runtime.AccountBusyError("account busy")):
            target = "_open_codex" if isinstance(error, sse.StreamIdleTimeout) else "_acquire_slot"
            with mock.patch.object(adapter, target, side_effect=error), mock.patch.object(adapter, "_release_slot", wraps=adapter._release_slot) as release:
                result = self._image_events(aid)[-1][1]
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], 504 if isinstance(error, sse.StreamIdleTimeout) else 429)
            release.assert_called_once()
            self.assertEqual(account_runtime.capacity(aid), 0)

    def test_gpt_catalog_includes_all_upstream_models_even_hidden_busy_failed_and_untested(self):
        from core import codex, gpt_models, model_status
        from core.gpt_runtime import edit_meta
        from adapters.openai_compat import UpstreamError
        aid, adapter = self._gpt_fixture()
        ids = ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-sol", "gpt-reserve", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "codex-auto-review"]
        manifest = [{"slug": name, "visibility": "hide" if name in ["gpt-reserve", "codex-auto-review"] else "list", "supported_in_api": True} for name in ids]
        edit_meta(pool_state.DB, aid, lambda meta: meta.update(pool_gpt_manifest={"models": manifest, "fetched_at": int(time.time()), "client_version": config.GPT_CLIENT_VERSION}))
        with mock.patch.object(adapter, "chat", return_value={"raw": {"choices": [{"message": {"content": "Hi"}}]}}):
            self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "gpt-6.1-terra"})
        busy = codex.error_from_event({"type": "error", "error": {"code": "server_is_overloaded", "message": "Our servers are currently overloaded"}})
        with mock.patch.object(adapter, "chat", side_effect=busy):
            self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "gpt-6.1-sol"})
        with mock.patch.object(adapter, "chat") as chat, mock.patch.object(adapter, "account_models") as fetch:
            response = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual([model["id"] for model in data["models"]], ids)
        self.assertEqual(data["verified_count"], 1)
        self.assertEqual(data["source"], "upstream")
        by_id = {model["id"]: model for model in data["models"]}
        self.assertEqual(by_id["gpt-6.1-sol"]["test_status"], "busy")
        self.assertEqual(by_id["gpt-6.1-terra"]["test_status"], "verified")
        self.assertTrue(by_id["codex-auto-review"]["hidden"])
        self.assertEqual(by_id["codex-auto-review"]["test_status"], "untested")
        self.assertTrue(by_id["gpt-reserve"]["hidden"])
        chat.assert_not_called()
        fetch.assert_not_called()
        self.assertNotIn("codex-auto-review", adapter.models, "the admin picker must not publish untested hidden models to gateway routing")
        self.assertEqual(model_status.monitored_models(adapter), ["gpt-6.1-terra"])
        with mock.patch.object(adapter, "chat", side_effect=UpstreamError(400, "unsupported fixture")):
            self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "codex-auto-review"})
        models = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin).json()["models"]
        self.assertEqual([model["id"] for model in models], ids)
        self.assertEqual(models[-1]["test_status"], "failed")
        plan = self.client.put(f"/api/admin/pool/accounts/{aid}/plan", headers=self.admin, json={"model": "codex-auto-review", "interval_seconds": 300, "enabled": False})
        self.assertEqual(plan.status_code, 200, plan.text)

    def test_gpt_test_timeout_and_busy_finish_without_penalizing_or_erasing_verified_model(self):
        from core import account_runtime
        aid, adapter = self._gpt_fixture()
        answer = {"raw": {"choices": [{"message": {"content": "Hi!"}}]}}
        with mock.patch.object(adapter, "chat", return_value=answer):
            self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "verified-model"})
        before = pool_state.DB.get_account(aid)
        budget = account_runtime.test_budget
        def slow(*args, **kwargs):
            time.sleep(0.04)
            return answer
        with mock.patch.object(adapter, "chat", side_effect=slow), mock.patch.object(account_runtime, "test_budget", side_effect=lambda: budget(0.02)), mock.patch.object(pool_state.POOL, "mark_failure") as penalize:
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "verified-model"}).json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 408)
        self.assertIn("超时", result["error"])
        penalize.assert_not_called()
        self.assertIsNone(account_runtime._DEADLINE.get())
        with mock.patch.object(account_runtime, "dispatch", side_effect=account_runtime.AccountBusyError("账号并发排队已满")), mock.patch.object(pool_state.POOL, "mark_failure") as penalize:
            busy = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "verified-model"}).json()
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["error_code"], 429)
        self.assertIn("排队", busy["error"])
        penalize.assert_not_called()
        after = pool_state.DB.get_account(aid)
        self.assertEqual((after["status"], after["last_error"]), (before["status"], before["last_error"]))
        self.assertEqual(after["meta"]["pool_model_checks"], before["meta"]["pool_model_checks"])
        history = self.client.get(f"/api/admin/pool/accounts/{aid}/tests", headers=self.admin).json()["history"]
        self.assertEqual([r["error_code"] for r in history[:2]], [429, 408])

    def test_gpt_capacity_error_has_readable_warning_and_success_clears_it(self):
        from core import codex
        aid, adapter = self._gpt_fixture()
        busy = codex.error_from_event({"type": "error", "error": {"code": "server_is_overloaded", "message": "Our servers are currently overloaded. Please try again later."}})
        with mock.patch.object(adapter, "chat", side_effect=busy):
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "gpt-6.1-terra"}).json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 503)
        self.assertEqual(result["error_kind"], "capacity")
        self.assertIn("overloaded", result["upstream_error"])
        view = self.client.get(f"/api/admin/pool/accounts/{aid}", headers=self.admin).json()["account"]
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["last_error_kind"], "capacity")
        with mock.patch.object(adapter, "chat", return_value={"raw": {"choices": [{"message": {"content": "Hi"}}]}}):
            self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={"model": "gpt-6.1-terra"})
        view = self.client.get(f"/api/admin/pool/accounts/{aid}", headers=self.admin).json()["account"]
        self.assertEqual(view["last_error_kind"], "")
        self.assertEqual(view["last_error"], "")

    def test_gpt_detect_rejects_unsupported_without_cooling_account_and_retries_transient(self):
        from adapters.openai_compat import UpstreamError
        from core import account_management as management
        aid, adapter = self._gpt_fixture()
        before = pool_state.DB.get_account(aid)
        answer = {"raw": {"choices": [{"message": {"content": "Hi!"}}]}}
        candidates = ["good", "unsupported", "transient", "still-busy", "empty"]
        attempts = {}
        def chat(row, messages, **kwargs):
            name = kwargs["model"]
            attempts[name] = attempts.get(name, 0) + 1
            self.assertEqual(messages, [{"role": "user", "content": "hi"}])
            if name == "unsupported":
                raise UpstreamError(400, "model is not supported", "fixture")
            if name == "still-busy" or (name == "transient" and attempts[name] == 1):
                raise UpstreamError(502, "overloaded", "fixture")
            if name == "empty":
                return {"raw": {"choices": []}}
            return answer
        with mock.patch.object(adapter, "account_models", return_value=[]), mock.patch.object(adapter, "chat", side_effect=chat), mock.patch(
                "core.gpt_models.test_candidates", return_value=candidates), mock.patch.object(pool_state.POOL, "mark_failure") as penalize:
            started = self.client.post(f"/api/admin/pool/accounts/{aid}/models/detect", headers=self.admin)
        self.assertEqual(started.status_code, 200, started.text)
        penalize.assert_not_called()
        catalog = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin).json()
        self.assertEqual([m["id"] for m in catalog["models"]], ["good", "transient"])
        self.assertFalse(catalog["scan"]["running"])
        self.assertEqual(catalog["scan"]["done"], 5)
        self.assertEqual(attempts["transient"], 2)
        self.assertEqual(attempts["unsupported"], 1)
        after = pool_state.DB.get_account(aid)
        self.assertEqual((after["status"], after["last_error"]), (before["status"], before["last_error"]))
        self.assertEqual(after["meta"].get("admin_paused"), before["meta"].get("admin_paused"))
        self.assertEqual(self.client.post(f"/api/admin/pool/accounts/{aid}/models/detect", headers=self.user).status_code, 403)
        with mock.patch.object(adapter, "chat", side_effect=UpstreamError(404, "unknown model", "fixture")), mock.patch.object(pool_state.POOL, "mark_failure") as penalize:
            failed = management.run_test(after, "good")
        self.assertFalse(failed["ok"])
        penalize.assert_not_called()
        catalog = self.client.get(f"/api/admin/pool/accounts/{aid}/models", headers=self.admin).json()
        self.assertNotIn("good", [m["id"] for m in catalog["models"]])

    def test_gpt_candidates_apply_whitelist_alias_and_keep_shadow_models_separate(self):
        from core import gpt_models as models
        from core.gpt_runtime import edit_meta
        aid, adapter = self._gpt_fixture()
        edit_meta(pool_state.DB, aid, lambda meta: meta.update(gpt_settings={"model_whitelist": ["alias", "gpt-5.4"], "model_mapping": {"alias": "gpt-6.1-sol"}}))
        candidates = models.test_candidates(pool_state.DB.get_account(aid), adapter)
        self.assertEqual(set(candidates), {"alias", "gpt-5.4"})
        edit_meta(pool_state.DB, aid, lambda meta: meta.update(gpt_settings={}))
        child = models.create_shadow(pool_state.DB, pool_state.DB.get_account(aid), ["gpt-5.3-codex-spark"])
        self.addCleanup(pool_state.DB.delete_account, "gpt", child["identity"])
        self.assertNotIn("gpt-5.3-codex-spark", models.test_candidates(pool_state.DB.get_account(aid), adapter))
        self.assertEqual(models.test_candidates(child, adapter), ["gpt-5.3-codex-spark"])
        models.record_test(adapter, pool_state.DB.get_account(aid), {"model": "alias", "ok": True, "elapsed_ms": 10})
        edit_meta(pool_state.DB, aid, lambda meta: meta.update(gpt_settings={"model_mapping": {"alias": "changed-target"}}))
        self.assertNotIn("alias", models.checks(pool_state.DB.get_account(aid), adapter))
        row = pool_state.DB.get_account(aid)
        models.record_test(adapter, row, {"model": "gpt-5.6-sol", "ok": True, "elapsed_ms": 10})
        with mock.patch("config.GPT_CLIENT_VERSION", "0.999.0"):
            self.assertEqual(models.checks(pool_state.DB.get_account(aid), adapter), {})

    def test_empty_assistant_text_is_a_failed_test_not_a_successful_blank_reply(self):
        aid, adapter = self._gpt_fixture()
        with mock.patch.object(adapter, "chat", return_value={"raw": {"choices": [{"message": {"content": ""}}]}}):
            result = self.client.post(f"/api/admin/pool/accounts/{aid}/test", headers=self.admin, json={}).json()
        self.assertFalse(result["ok"])
        self.assertIn("文本回答", result["error"])

    def test_test_endpoint_reports_upstream_error_without_500(self):
        r = self.client.post(f"/api/admin/pool/accounts/{self.ids[2]}/test", headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("ok", body)
        if not body["ok"]:
            self.assertIn("error", body)
            self.assertIn("status_after", body)


class GPTProfileTest(unittest.TestCase):
    def test_model_manifest_honors_visibility_and_api_support_without_duplicates(self):
        from adapters.gpt import GPTAdapter
        adapter = GPTAdapter()
        data = {"models": [{"slug": "gpt-fixture", "display_name": "Fixture", "visibility": "list", "supported_in_api": True},
            {"slug": "hidden", "visibility": "hide", "supported_in_api": True},
            {"slug": "unsupported", "visibility": "list", "supported_in_api": False}, {"slug": "gpt-fixture"}]}
        with mock.patch.object(adapter, "_account_json", return_value=data) as fetch:
            result = adapter.account_models({})
        self.assertEqual([item["id"] for item in result], ["gpt-fixture"])
        self.assertIn("/models?client_version=", fetch.call_args.args[1])

    def test_quota_keeps_credits_and_feature_windows_without_upstream_ids(self):
        from core.gpt_profile import quota_snapshot
        rate = {"allowed": False, "limit_reached": True, "primary_window": {
            "used_percent": 98, "limit_window_seconds": 18000, "reset_after_seconds": 300}}
        data = {"account_id": "private-account", "credits": {"has_credits": True, "unlimited": False,
            "balance": "123456789012345678.00100", "private": "private-credit"},
            "rate_limit": rate, "code_review_rate_limit": rate,
            "additional_rate_limits": [{"limit_name": "Codex Spark", "metered_feature": "codex_bengalfox", "rate_limit": rate}]}
        result = quota_snapshot(data)
        self.assertEqual(result["credits"]["balance"], "123456789012345678.00100")
        self.assertFalse(result["allowed"])
        self.assertTrue(result["limit_reached"])
        self.assertEqual(result["code_review"]["five_hour"]["utilization"], 98)
        self.assertEqual(result["additional_limits"][0]["name"], "Codex Spark")
        self.assertNotIn("private-", json.dumps(result))
        self.assertIsNone(quota_snapshot({})["credits"])
        for value in ("NaN", "Infinity", "-1", "invalid", 5, None):
            with self.subTest(value=value):
                result = quota_snapshot({"credits": {"has_credits": True, "balance": value}})
                self.assertIsNone(result["credits"]["balance"])

    def test_reset_credits_distinguish_unknown_zero_and_expiry_without_private_ids(self):
        from core.gpt_profile import reset_credit_snapshot
        data = {"available_count": 1, "credits": [
            {"id": "private-1", "status": "available", "reset_type": "codex_rate_limits", "expires_at": "2030-01-01T00:00:00Z"},
            {"id": "private-2", "status": "redeemed", "expires_at": "2031-01-01T00:00:00Z"},
            {"id": "private-3", "reset_type": "other", "expires_at": "2032-01-01T00:00:00Z"}]}
        result = reset_credit_snapshot(data)
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["expirations"], [1893456000])
        self.assertNotIn("private-", json.dumps(result))
        self.assertEqual(reset_credit_snapshot({"data": {"credits": []}})["available_count"], 0)
        self.assertEqual(reset_credit_snapshot({"available_count": "0"})["available_count"], 0)
        with self.assertRaises(ValueError):
            reset_credit_snapshot({})

    def test_windows_follow_duration_not_position_and_keep_unknowns(self):
        from core.gpt_profile import normalize_usage
        seven = {"used_percent": 18, "limit_window_seconds": 604800, "reset_at": 1900000000}
        five = {"used_percent": 25, "limit_window_seconds": 18000, "reset_after_seconds": 60}
        for primary, secondary in ((seven, five), (five, seven)):
            result = normalize_usage({"primary": primary, "secondary": secondary, "fetched_at": 1800000000})
            self.assertEqual(result["seven_day"]["utilization"], 18)
            self.assertEqual(result["seven_day"]["reset_at"], 1900000000)
            self.assertEqual(result["five_hour"]["utilization"], 25)
            self.assertEqual(result["five_hour"]["reset_at"], 1800000060)
        single = normalize_usage({"primary": seven, "secondary": None})
        self.assertIsNone(single["five_hour"])
        self.assertEqual(single["seven_day"]["utilization"], 18)
        legacy = normalize_usage({"primary": {"used_percent": None}, "secondary": {"used_percent": 0}})
        self.assertIsNone(legacy["seven_day"]["utilization"])
        self.assertEqual(legacy["five_hour"]["utilization"], 0)
        self.assertIsNone(normalize_usage({"primary": None, "secondary": None})["seven_day"])

    def test_profile_selects_exact_workspace_and_never_falls_back_to_foreign_paid_account(self):
        from core.gpt_profile import select_profile
        data = {"accounts": {
            "chosen": {"account": {"account_id": "chosen", "plan_type": "free"}},
            "other": {"account": {"account_id": "other", "plan_type": "pro", "is_default": True}}}}
        self.assertEqual(select_profile(data, "chosen")["plan_type"], "free")
        self.assertEqual(select_profile(data)["account_id"], "other")
        with self.assertRaises(ValueError):
            select_profile(data, "missing")
        data["accounts"]["other"]["account"].pop("is_default")
        with self.assertRaises(ValueError):
            select_profile(data)

    def test_paid_profile_fetches_subscription_expiry_for_selected_workspace(self):
        from adapters.gpt import GPTAdapter
        adapter = GPTAdapter()
        account = {"identity": "fixture", "secret": {"access_token": "at-fixture", "account_id": "selected id"}}
        data = {"accounts": {"selected id": {"account": {"account_id": "selected id", "plan_type": "plus"}}}}
        with mock.patch.object(adapter, "_account_json", side_effect=[data, {"active_until": "2030-01-01T00:00:00Z"}]) as request:
            result = adapter.account_profile(account)
        self.assertEqual(result["subscription_expires_at"], 1893456000)
        self.assertEqual(parse_qs(urlsplit(request.call_args_list[1].args[1]).query)["account_id"], ["selected id"])


class AccountMigrationTest(unittest.TestCase):
    def test_old_account_rows_and_usage_are_preserved_across_restarts(self):
        from core.db import DB, SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            account_path = os.path.join(directory, "legacy-pool.db")
            conn = sqlite3.connect(account_path)
            try:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO accounts(channel,identity,secret,status) VALUES(?,?,?,?)",
                             ("legacy", "kept", json.dumps({"api_key": "kept-secret"}), "active"))
                conn.commit()
            finally:
                conn.close()
            for _ in range(2):
                db = DB(account_path)
                row = db.get_by_identity("legacy", "kept")
                self.assertEqual(row["secret"]["api_key"], "kept-secret")
                self.assertEqual(row["priority"], 50)
                self.assertEqual(row["group_ids"], [])
                self.assertEqual(row["status"], "active")
                db.close()

    def test_old_usage_stays_unattributed_without_fabricated_account_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy-users.db")
            db = UserDB(path)
            uid = db.create_user("fixture@example.test", "hash", "FIXTURE")
            db.add_usage(uid, channel="legacy", input_tokens=17, cost=2)
            db._conns.close_current()
            conn = sqlite3.connect(path)
            try:
                conn.execute("DROP INDEX idx_usage_account_time")
                conn.execute("ALTER TABLE usage_logs DROP COLUMN upstream_account_id")
                conn.execute("ALTER TABLE usage_logs DROP COLUMN account_cost")
                conn.commit()
            finally:
                conn.close()
            for _ in range(2):
                migrated = UserDB(path)
                log = migrated.recent_usage(uid)[0]
                self.assertEqual(log["input_tokens"], 17)
                self.assertEqual(log["cost"], 2)
                self.assertIsNone(log["upstream_account_id"])
                self.assertIsNone(log["account_cost"])
                migrated._conns.close_current()


if __name__ == "__main__":
    unittest.main()
