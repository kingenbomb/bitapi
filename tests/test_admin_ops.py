#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理台运营读数的门禁:用户检索、全站用量日志、看板、CSV 导出、库备份端点。

这些端点没有新的写路径,守的是「读数对不对」:
  - 过滤条件与 total 必须同一口径(分页器靠 total 算页数)
  - 看板的钱要能和流水对上:充值/兑码/消费/赠送各归各
  - 非管理员 403;用户侧只能看自己的
"""
import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ops.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.credit import credit  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _db():
    return portal_state.USER_DB


class AdminOpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "ops-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)

        def reg(email, name=None):
            r = cls.client.post("/api/register", json={"email": email, "password": "secret123"})
            assert r.status_code == 200, r.text
            uid = r.json()["user_id"]
            if name:
                fresh.update_user(uid, display_name=name)
            return uid, {"Authorization": "Bearer " + r.json()["token"]}

        cls.admin_id, cls.admin_h = reg("root@example.com")
        cls.alice_id, cls.alice_h = reg("alice@shop.example", "小艾")
        cls.bob_id, cls.bob_h = reg("bob@example.com", "Bob_B")
        cls.now = int(time.time())

        # 用量:alice 3 条(两个模型、一条 client_gone),bob 1 条,其中一条是 3 天前的
        def log(uid, model, channel, cost, when, end="done", key=None):
            fresh.add_usage(user_id=uid, api_key_id=key, channel=channel, model=model,
                            input_tokens=100, output_tokens=50, cost=cost,
                            actual_cost=cost, billing_mode="token",
                            pricing_snapshot={"end_reason": end, "request_id": f"req_{when}",
                                              "frt_ms": 120, "tier": "official"},
                            stream=1, duration_ms=800, token_source="upstream")
        log(cls.alice_id, "deepseek-ai/deepseek-v4-pro-0813", "grok", 0.5, 1)
        log(cls.alice_id, "deepseek-ai/deepseek-v4-flash-0731", "grok", 1.5, 2, end="client_gone")
        log(cls.alice_id, "xai-grok-4.6", "metered", 0.25, 3)
        log(cls.bob_id, "deepseek-ai/deepseek-v4-pro-0813", "grok", 0.75, 4)
        with fresh._conn() as c:
            # 把 bob 那条改到 3 天前,验时间窗过滤
            c.execute("UPDATE usage_logs SET created_at=? WHERE user_id=?",
                      (cls.now - 3 * 86400, cls.bob_id))

        # 流水:alice 充值 10(走已完成订单),兑码 2,消费 -2.25,签到 0.05;admin 给 bob 加 1
        fresh.create_order(cls.alice_id, "OT-1", 10.0, "epay")
        fresh.set_order_pay_amount_cny("OT-1", 72.0)
        fresh.mark_order_paid("OT-1", "TN-1", 72.0)
        lease = fresh.acquire_order_lease("OT-1")
        fresh.complete_order("OT-1", lease)
        credit(cls.alice_id, 10.0, "recharge", "recharge:OT-1")
        credit(cls.alice_id, 2.0, "redeem", "redeem:c1")
        credit(cls.alice_id, -2.25, "usage", "usage:x1")
        credit(cls.alice_id, 0.05, "checkin", "checkin:a:today")
        credit(cls.bob_id, 1.0, "admin", "admin:1")

    # ---- 用户检索 ----

    def test_user_search_by_email_substring_name_id_and_aff(self):
        g = lambda **p: self.client.get("/api/admin/users", params=p, headers=self.admin_h).json()  # noqa: E731
        r = g(q="shop.example")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["users"][0]["email"], "alice@shop.example")
        self.assertNotIn("password_hash", r["users"][0])
        self.assertEqual(g(q="小艾")["total"], 1)
        self.assertEqual(g(q=str(self.bob_id))["total"], 1)
        aff = _db().get_user(self.bob_id)["aff_code"]
        self.assertEqual(g(q=aff.lower())["users"][0]["id"], self.bob_id)
        # LIKE 通配符要转义:一个 "_" 不能匹配所有人
        self.assertEqual(g(q="_")["total"], 1)     # 只有 Bob_B
        self.assertEqual(g(q="%")["total"], 0)
        self.assertEqual(g()["total"], 3)
        self.assertEqual(g(role="admin")["total"], 1)

    def test_user_search_total_matches_pagination(self):
        r = self.client.get("/api/admin/users", params={"limit": 2, "offset": 0},
                            headers=self.admin_h).json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(r["users"]), 2)
        r2 = self.client.get("/api/admin/users", params={"limit": 2, "offset": 2},
                             headers=self.admin_h).json()
        self.assertEqual(len(r2["users"]), 1)

    def test_non_admin_forbidden(self):
        for path in ("/api/admin/users", "/api/admin/usage", "/api/admin/stats",
                     "/api/admin/usage/export.csv", "/api/admin/backup"):
            self.assertEqual(self.client.get(path, headers=self.alice_h).status_code, 403, path)

    # ---- 全站用量日志 ----

    def test_admin_usage_lists_all_with_email(self):
        r = self.client.get("/api/admin/usage", headers=self.admin_h).json()
        self.assertEqual(r["total"], 4)
        emails = {row["email"] for row in r["logs"]}
        self.assertEqual(emails, {"alice@shop.example", "bob@example.com"})
        self.assertEqual(r["logs"][0]["model"], "deepseek-ai/deepseek-v4-pro-0813")   # bob 那条 id 最大但时间最老;按 id 倒序

    def test_admin_usage_filters(self):
        g = lambda **p: self.client.get("/api/admin/usage", params=p, headers=self.admin_h).json()  # noqa: E731
        self.assertEqual(g(email="alice@shop.example")["total"], 3)
        self.assertEqual(g(user_id=self.bob_id)["total"], 1)
        self.assertEqual(g(email="nobody@example.com")["total"], 0)
        self.assertEqual(g(model="deepseek-ai/*")["total"], 3)
        self.assertEqual(g(model="deepseek-ai/deepseek-v4-flash-0731")["total"], 1)
        self.assertEqual(g(channel="metered")["total"], 1)
        self.assertEqual(g(end_reason="client_gone")["total"], 1)
        self.assertEqual(g(since=self.now - 86400)["total"], 3)     # bob 的在 3 天前
        self.assertEqual(g(until=self.now - 86400)["total"], 1)
        r = g(limit=2, offset=0)
        self.assertEqual(r["total"], 4)
        self.assertEqual(len(r["logs"]), 2)

    def test_admin_usage_csv_has_email_column_and_all_rows(self):
        r = self.client.get("/api/admin/usage/export.csv", params={"channel": "grok"},
                            headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        lines = r.text.strip().splitlines()
        self.assertEqual(len(lines), 1 + 3)
        head = lines[0].split(",")
        self.assertEqual(head[:5], ["id", "time", "email", "key", "channel"])
        self.assertIn("end_reason", head)
        self.assertTrue(any("client_gone" in ln for ln in lines[1:]))

    # ---- 用户侧分页与导出 ----

    def test_user_usage_paginates_and_stays_own(self):
        r = self.client.get("/api/usage", params={"limit": 2}, headers=self.alice_h).json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(r["recent"]), 2)
        self.assertIn("summary", r)
        # totals 与列表同一组条件:3 条、1 条异常、实扣 2.25
        self.assertEqual(r["totals"]["requests"], 3)
        self.assertEqual(r["totals"]["failed"], 1)
        self.assertAlmostEqual(r["totals"]["actual_cost"], 2.25)
        self.assertAlmostEqual(r["totals"]["frt_ms"], 120)
        r2 = self.client.get("/api/usage", params={"limit": 2, "offset": 2},
                             headers=self.alice_h).json()
        self.assertEqual(len(r2["recent"]), 1)
        self.assertEqual(self.client.get("/api/usage", params={"model": "xai-*"},
                                         headers=self.alice_h).json()["total"], 1)
        bad = self.client.get("/api/usage", params={"end_reason": "failed"},
                              headers=self.alice_h).json()
        self.assertEqual(bad["total"], 1)
        self.assertEqual(bad["recent"][0]["model"], "deepseek-ai/deepseek-v4-flash-0731")
        # 默认 range=day:bob 那条在 3 天前,今日窗口看不到,30 天窗口能看到;
        # 无论哪个窗口都看不到 alice 的
        self.assertEqual(self.client.get("/api/usage", headers=self.bob_h).json()["total"], 0)
        self.assertEqual(self.client.get("/api/usage", params={"range": "month"},
                                         headers=self.bob_h).json()["total"], 1)

    def test_user_csv_has_no_email_column(self):
        r = self.client.get("/api/usage/export.csv", headers=self.alice_h)
        self.assertEqual(r.status_code, 200)
        lines = r.text.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertNotIn("email", lines[0].split(","))

    def test_user_csv_honours_same_window_as_list(self):
        """页面在「今日」上点导出,导出的必须也是今日 —— 不是全部历史。"""
        day = self.client.get("/api/usage/export.csv", headers=self.bob_h).text
        self.assertEqual(len(day.strip().splitlines()), 1)      # 只有表头
        month = self.client.get("/api/usage/export.csv", params={"range": "month"},
                                headers=self.bob_h).text
        self.assertEqual(len(month.strip().splitlines()), 2)
        explicit = self.client.get("/api/usage/export.csv",
                                   params={"since": self.now - 5 * 86400},
                                   headers=self.bob_h).text
        self.assertEqual(len(explicit.strip().splitlines()), 2)

    # ---- 看板 ----

    def test_stats_money_matches_ledger(self):
        r = self.client.get("/api/admin/stats", params={"days": 7}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        month = s["windows"]["month"]
        self.assertAlmostEqual(month["recharge"], 10.0)
        self.assertAlmostEqual(month["redeem"], 2.0)
        self.assertAlmostEqual(month["consumed"], 2.25)
        self.assertAlmostEqual(month["giveaway"], 1.05)     # checkin 0.05 + admin 1
        self.assertEqual(month["orders"]["count"], 1)
        self.assertAlmostEqual(month["orders"]["cny"], 72.0)
        self.assertEqual(month["usage"]["requests"], 4)
        self.assertEqual(month["usage"]["users"], 2)
        self.assertEqual(month["usage"]["end_reasons"].get("client_gone"), 1)
        self.assertEqual(month["usage"]["end_reasons"].get("done"), 3)
        # 今日窗口不含 bob 那条 3 天前的
        self.assertEqual(s["windows"]["today"]["usage"]["requests"], 3)
        self.assertEqual(len(s["series"]), 7)
        self.assertEqual(s["series"][-1]["requests"], 3)
        self.assertEqual(s["users"]["total"], 3)
        self.assertEqual(s["users"]["active_month"], 2)
        self.assertAlmostEqual(s["balances"]["owed"], 10 + 2 - 2.25 + 0.05 + 1)
        chans = {x["key"]: x for x in s["by_channel"]}
        self.assertAlmostEqual(chans["grok"]["actual_cost"], 2.75)
        self.assertEqual(s["top_users"][0]["email"], "alice@shop.example")
        self.assertAlmostEqual(s["top_users"][0]["actual_cost"], 2.25)

    def test_stats_days_is_clamped(self):
        s = self.client.get("/api/admin/stats", params={"days": 500}, headers=self.admin_h).json()
        self.assertEqual(len(s["series"]), 90)

    # ---- 库备份端点 ----

    def test_backup_endpoints(self):
        saved = (config.BACKUP_DIR, config.BACKUP_KEEP)
        config.BACKUP_DIR = os.path.join(_TMP, "bk-ep")
        config.BACKUP_KEEP = 3
        try:
            st = self.client.get("/api/admin/backup", headers=self.admin_h).json()
            self.assertEqual(st["count"], 0)
            self.assertIsNone(st["latest"])
            r = self.client.post("/api/admin/backup", headers=self.admin_h)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["name"].startswith("bitapi-"))
            self.assertEqual(body["status"]["count"], 1)
            self.assertTrue(os.path.exists(os.path.join(config.BACKUP_DIR, body["name"])))
            st = self.client.get("/api/admin/backup", headers=self.admin_h).json()
            self.assertEqual(st["latest"]["name"], body["name"])
            self.assertEqual(self.client.post("/api/admin/backup", headers=self.alice_h).status_code, 403)
        finally:
            config.BACKUP_DIR, config.BACKUP_KEEP = saved


if __name__ == "__main__":
    unittest.main()
