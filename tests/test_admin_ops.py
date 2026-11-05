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
from datetime import datetime
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ops.db"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.credit import credit  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient  # noqa: E402


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

        cls.admin_id, cls.admin_h = reg("root@gmail.com")
        cls.alice_id, cls.alice_h = reg("alice@gmail.com", "小艾")
        cls.bob_id, cls.bob_h = reg("bob@gmail.com", "Bob_B")
        cls.now = int(time.time())

        # 用量:alice 3 条(两个模型、一条 client_gone),bob 1 条,其中一条是 3 天前的
        def log(uid, model, channel, cost, when, end="done", key=None):
            fresh.add_usage(user_id=uid, api_key_id=key, channel=channel, model=model,
                            input_tokens=100, output_tokens=50, cost=cost,
                            actual_cost=cost, billing_mode="token",
                            pricing_snapshot={"end_reason": end, "request_id": f"req_{when}",
                                              "frt_ms": 120, "tier": "official"},
                            stream=1, duration_ms=800, token_source="upstream")
        log(cls.alice_id, "kg-deepseek-v4-pro", "kogo", 0.5, 1)
        log(cls.alice_id, "kg-gpt-5.6-sol", "kogo", 1.5, 2, end="client_gone")
        log(cls.alice_id, "xai-grok-4.6", "xai", 0.25, 3)
        log(cls.bob_id, "kg-deepseek-v4-pro", "kogo", 0.75, 4)
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
        r = g(q="alice@gmail.com")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["users"][0]["email"], "alice@gmail.com")
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
        for path in ("/api/admin/users", "/api/admin/usage", "/api/admin/usage/live", "/api/admin/stats",
                     "/api/admin/usage/export.csv", "/api/admin/usage/cache", "/api/admin/usage/cache/trend", "/api/admin/backup"):
            self.assertEqual(self.client.get(path, headers=self.alice_h).status_code, 403, path)

    # ---- 全站用量日志 ----

    def test_admin_usage_lists_all_with_email(self):
        r = self.client.get("/api/admin/usage", headers=self.admin_h).json()
        self.assertEqual(r["total"], 4)
        emails = {row["email"] for row in r["logs"]}
        self.assertEqual(emails, {"alice@gmail.com", "bob@gmail.com"})
        self.assertEqual(r["logs"][0]["model"], "kg-deepseek-v4-pro")   # bob 那条 id 最大但时间最老;按 id 倒序

    def test_admin_usage_filters(self):
        g = lambda **p: self.client.get("/api/admin/usage", params=p, headers=self.admin_h).json()  # noqa: E731
        self.assertEqual(g(email="alice@gmail.com")["total"], 3)
        self.assertEqual(g(user_id=self.bob_id)["total"], 1)
        self.assertEqual(g(email="nobody@gmail.com")["total"], 0)
        self.assertEqual(g(model="kg-*")["total"], 3)
        self.assertEqual(g(model="kg-gpt-5.6-sol")["total"], 1)
        self.assertEqual(g(channel="xai")["total"], 1)
        self.assertEqual(g(end_reason="client_gone")["total"], 1)
        self.assertEqual(g(since=self.now - 86400)["total"], 3)     # bob 的在 3 天前
        self.assertEqual(g(until=self.now - 86400)["total"], 1)
        r = g(limit=2, offset=0)
        self.assertEqual(r["total"], 4)
        self.assertEqual(len(r["logs"]), 2)

    def test_admin_usage_csv_has_email_column_and_all_rows(self):
        r = self.client.get("/api/admin/usage/export.csv", params={"channel": "kogo"},
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
        self.assertEqual(bad["recent"][0]["model"], "kg-gpt-5.6-sol")
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

    def test_usage_csv_separates_cache_and_keeps_unknown_blank(self):
        import csv
        import io

        rows = [
            {"input_tokens": 100, "pricing_snapshot": {
                "total_ctx": 150, "cache_read_tokens": 40, "cache_write_tokens": 10,
                "client_ip": "203.0.113.7"}},
            {"input_tokens": 100, "pricing_snapshot": {
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "client_ip": "2001:db8::1"}},
            {"input_tokens": 100, "pricing_snapshot": {"total_ctx": 150}},
            {"input_tokens": 100, "pricing_snapshot": {}},
        ]
        for with_user in (False, True):
            with self.subTest(with_user=with_user):
                response = portal_routes._usage_csv(rows, with_user=with_user)
                exported = list(csv.DictReader(io.StringIO(response.body.decode())))
                self.assertEqual(
                    [(r["cache_read_tokens"], r["cache_write_tokens"], r["cache_tokens"])
                     for r in exported],
                    [("40", "10", "50"), ("0", "0", "0"), ("", "", "50"), ("", "", "")])
                self.assertEqual([r["client_ip"] for r in exported],
                                 ["203.0.113.7", "2001:db8::1", "", ""])

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

    def test_live_usage_windows_and_user_isolation(self):
        now = int(datetime(2001, 1, 2, 0, 0, 30, tzinfo=portal_routes._CHECKIN_TZ).timestamp())
        day_start = now - 30
        ids = []

        def read(**params):
            with mock.patch.object(portal_routes.time, "time", return_value=now):
                response = self.client.get("/api/admin/usage/live", params=params, headers=self.admin_h)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        empty = read()
        self.assertEqual((empty["today_cost"], empty["rpm"], empty["tpm"]), (0, 0, 0))
        try:
            for uid, when, cost, input_tok, output_tok, total_ctx in (
                    (self.alice_id, now - 45, 9, 100, 50, 150),  # 昨日，仍在最近一分钟
                    (self.bob_id, now - 10, 2, 10, 20, 50),
                    (self.alice_id, day_start + 1, 3, 30, 10, 30),
                    (self.bob_id, now - 60, 90, 1000, 1000, 1000),  # 刚好过期
                    (self.alice_id, now + 1, 90, 1000, 1000, 1000)):  # 未来记录
                log_id = _db().add_usage(user_id=uid, channel="live-test", model="live-test",
                    cost=cost * 2, actual_cost=cost, input_tokens=input_tok,
                    output_tokens=output_tok, pricing_snapshot={"total_ctx": total_ctx})
                ids.append(log_id)
                with _db()._conn() as c:
                    c.execute("UPDATE usage_logs SET created_at=? WHERE id=?", (when, log_id))
            for params in ({}, {"email": "alice@gmail.com", "model": "missing-model"}):
                result = read(**params)
                self.assertEqual((result["today_cost"], result["rpm"], result["tpm"]), (5, 3, 310))
                self.assertEqual(result["as_of"], now)
                self.assertEqual(result["day_start"], day_start)
                self.assertEqual(result["timezone"], "Asia/Shanghai")
                self.assertEqual(result["window_seconds"], 60)
            for headers, expected in ((self.alice_h, (3, 2, 240)),
                                      (self.bob_h, (2, 1, 70)),
                                      (self.admin_h, (0, 0, 0))):
                with mock.patch.object(portal_routes.time, "time", return_value=now):
                    response = self.client.get("/api/usage/live", headers=headers,
                        params={"user_id": self.bob_id, "email": "bob@gmail.com",
                                "range": "month", "model": "missing-model"})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual((result["today_cost"], result["rpm"], result["tpm"]), expected)
                self.assertEqual(result["day_start"], day_start)
                self.assertEqual(result["window_seconds"], 60)
            self.assertEqual(self.client.get("/api/usage/live").status_code, 401)
        finally:
            with _db()._conn() as c:
                c.executemany("DELETE FROM usage_logs WHERE id=?", [(i,) for i in ids])

    def test_model_cache_statistics_preserve_scope_windows_and_missing_fields(self):
        now = int(datetime(2001, 1, 20, 12, tzinfo=portal_routes._CHECKIN_TZ).timestamp())
        ids = []

        def read(path, headers, **params):
            with mock.patch.object(portal_routes.time, "time", return_value=now):
                response = self.client.get(path, headers=headers, params=params)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        def add(uid, model, input_tok, snapshot, when=None):
            log_id = _db().add_usage(user_id=uid, channel="cache-stats", model=model,
                input_tokens=input_tok, output_tokens=10, pricing_snapshot=snapshot)
            ids.append(log_id)
            with _db()._conn() as c:
                c.execute("UPDATE usage_logs SET created_at=? WHERE id=?", (when or now - 10, log_id))

        empty = read("/api/usage/cache", self.alice_h)
        self.assertEqual(empty["models"], [])
        self.assertIsNone(empty["summary"]["cache_rate"])
        try:
            for i in range(15):
                add(self.alice_id, f"cache-model-{i}", 100,
                    {"total_ctx": 250 if i == 0 else 100, "cache_read_tokens": 100 if i == 0 else 0,
                     "cache_write_tokens": 50 if i == 0 else 0, "cache_usage_version": 2})
            add(self.alice_id, "cache-model-0", 20, {})
            add(self.bob_id, "cache-model-0", 500,
                {"total_ctx": 1500, "cache_read_tokens": 1000, "cache_write_tokens": 0, "cache_usage_version": 2})
            add(self.alice_id, "legacy-write", 40, {"total_ctx": 80, "cache_read_tokens": 40, "cache_write_tokens": 0})
            add(self.alice_id, "unknown-cache", 30, {})
            add(self.alice_id, "empty-input", 0, {"total_ctx": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cache_usage_version": 2})
            add(self.alice_id, "expired-model", 10, {"total_ctx": 20}, now - 8 * 86400)
            add(self.alice_id, "future-model", 10, {"total_ctx": 20}, now + 2)
            add(self.alice_id, None, 10, {})
            add(self.alice_id, "", 10, {})
            personal = read("/api/usage/cache", self.alice_h, user_id=self.bob_id)
            models = {row["model"]: row for row in personal["models"]}
            self.assertEqual(len(models), 20)  # 模型集合不受原概览的 top 6 / top 12 限制
            row = models["cache-model-0"]
            self.assertEqual((row["requests"], row["input_tokens"], row["cache_tokens"]), (2, 120, 150))
            self.assertEqual((row["cache_read_tokens"], row["cache_write_tokens"], row["cache_rows"]), (100, 50, 1))
            self.assertAlmostEqual(row["cache_rate"], 150 / 270 * 100)
            self.assertEqual(models["cache-model-1"]["cache_rate"], 0)
            self.assertEqual(models["cache-model-1"]["cache_write_tokens"], 0)
            self.assertIsNone(models["legacy-write"]["cache_write_tokens"])
            self.assertIsNone(models["unknown-cache"]["cache_tokens"])
            self.assertIsNone(models["unknown-cache"]["cache_rate"])
            self.assertIsNone(models["empty-input"]["cache_rate"])
            self.assertEqual(models["未记录模型"]["requests"], 2)
            self.assertNotIn("future-model", models)
            summary = personal["summary"]
            self.assertEqual(summary["requests"], sum(row["requests"] for row in models.values()))
            self.assertAlmostEqual(summary["cache_rate"], summary["cache_tokens"] / summary["total_input_tokens"] * 100)
            for window in ("day", "week"):
                recent = read("/api/usage/cache", self.alice_h, range=window)
                self.assertNotIn("expired-model", {row["model"] for row in recent["models"]})
            month = read("/api/usage/cache", self.alice_h, range="month")
            self.assertIn("expired-model", {row["model"] for row in month["models"]})
            bob = read("/api/usage/cache", self.bob_h, user_id=self.alice_id)
            self.assertEqual(len(bob["models"]), 1)
            self.assertEqual(bob["summary"]["cache_tokens"], 1000)
            all_site = read("/api/admin/usage/cache", self.admin_h, range="all")
            self.assertEqual(all_site["summary"]["cache_tokens"], summary["cache_tokens"] + 1000)
            self.assertEqual(self.client.get("/api/usage/cache").status_code, 401)
            self.assertEqual(self.client.get("/api/usage/cache?range=invalid", headers=self.alice_h).status_code, 400)
        finally:
            with _db()._conn() as c:
                c.executemany("DELETE FROM usage_logs WHERE id=?", [(i,) for i in ids])

    def test_cache_trends_preserve_daily_weighting_gaps_scope_and_snapshot(self):
        now = int(datetime(2001, 1, 20, 12, tzinfo=portal_routes._CHECKIN_TZ).timestamp())
        ids = []

        def stamp(day, hour=0):
            return int(datetime(2001, 1, day, hour, tzinfo=portal_routes._CHECKIN_TZ).timestamp())

        def add(uid, model, when, input_tok, ctx):
            snapshot = {} if ctx is None else {"total_ctx": ctx, "cache_read_tokens": ctx-input_tok,
                                               "cache_write_tokens": 0, "cache_usage_version": 2}
            log_id = _db().add_usage(user_id=uid, channel="cache-trend", model=model,
                input_tokens=input_tok, output_tokens=10, pricing_snapshot=snapshot, cost=.5, actual_cost=.05)
            ids.append(log_id)
            with _db()._conn() as c:
                c.execute("UPDATE usage_logs SET created_at=? WHERE id=?", (when, log_id))

        def read(headers, endpoint="/api/usage/cache/trend", clock=None, **params):
            with mock.patch.object(portal_routes.time, "time", return_value=now if clock is None else clock):
                response = self.client.get(endpoint, headers=headers, params=params)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        try:
            add(self.alice_id, "trend-model", stamp(18, 10), 100, 200)
            add(self.alice_id, "trend-model", stamp(18, 18), 900, 1000)
            add(self.bob_id, "trend-model", stamp(18, 11), 100, 1100)
            add(self.alice_id, "trend-model", stamp(20), 50, 100)  # 北京零点，UTC 仍在前一天
            add(self.alice_id, "trend-model", stamp(10), 50, 50)  # 真实 0% 与无数据不同
            add(self.alice_id, "trend-model", now, 50, 550)       # 当前未完整秒不计入快照
            add(self.alice_id, "trend-model", stamp(21), 50, 1000)
            add(self.alice_id, "trend-model-extra", stamp(19), 10, 20)
            add(self.alice_id, "unknown-trend", stamp(18), 20, None)
            add(self.alice_id, "boundary", now - 7*86400, 25, 50)
            add(self.alice_id, "boundary", now - 7*86400 - 1, 25, 50)

            personal = read(self.alice_h, range="week", model="trend-model", user_id=self.bob_id)
            daily = {row["date"]: row for row in personal["series"]}
            self.assertEqual(len(daily), 8)  # 滚动 7 天跨首尾两个部分自然日
            self.assertAlmostEqual(daily["2001-01-18"]["cache_rate"], 200/1200*100)
            self.assertAlmostEqual(daily["2001-01-18"]["cost"], 1)
            self.assertAlmostEqual(daily["2001-01-18"]["actual_cost"], .1)
            self.assertEqual(daily["2001-01-19"]["requests"], 0)
            self.assertIsNone(daily["2001-01-19"]["cache_rate"])
            self.assertEqual(daily["2001-01-20"]["requests"], 1)
            self.assertEqual(daily["2001-01-20"]["cache_rate"], 50)
            self.assertEqual(personal["summary"]["requests"], 3)
            self.assertAlmostEqual(personal["summary"]["cost"], 1.5)
            self.assertAlmostEqual(personal["summary"]["actual_cost"], .15)
            self.assertAlmostEqual(personal["summary"]["cache_rate"], 250/1300*100)
            self.assertEqual(personal["timezone"], "Asia/Shanghai")

            listed = read(self.alice_h, endpoint="/api/usage/cache", range="week")
            listed_model = next(row for row in listed["models"] if row["model"] == "trend-model")
            self.assertEqual(listed_model["requests"], personal["summary"]["requests"])
            self.assertEqual(listed_model["cache_tokens"], personal["summary"]["cache_tokens"])
            frozen = read(self.alice_h, clock=now+86400, range="week", model="trend-model", as_of=listed["as_of"])
            self.assertEqual(frozen, personal)
            all_days = read(self.alice_h, range="all", model="trend-model")
            self.assertEqual(all_days["series"][0]["date"], "2001-01-10")
            self.assertEqual(all_days["series"][0]["cache_rate"], 0)
            self.assertEqual(all_days["summary"]["requests"], 4)
            unknown = read(self.alice_h, model="unknown-trend")
            self.assertTrue(all(row["cache_rate"] is None for row in unknown["series"]))
            literal = read(self.alice_h, model="trend-model*")
            self.assertEqual(literal["series"], [])
            self.assertEqual(literal["summary"]["requests"], 0)
            boundary = read(self.alice_h, range="week", model="boundary")
            self.assertEqual(boundary["summary"]["requests"], 1)
            bob = read(self.bob_h, range="week", model="trend-model", user_id=self.alice_id)
            self.assertEqual(bob["summary"]["requests"], 1)
            self.assertEqual(bob["summary"]["cache_tokens"], 1000)
            site = read(self.admin_h, endpoint="/api/admin/usage/cache/trend", range="week", model="trend-model")
            self.assertEqual(site["summary"]["requests"], 4)
            self.assertAlmostEqual(site["summary"]["cache_rate"], 1250/2400*100)
            add(self.bob_id, "only-bob", stamp(9), 10, 100)
            selected = ["trend-model-extra", "trend-model", "unknown-trend", "only-bob", "trend-model*"]
            comparison = read(self.alice_h, range="week", models=selected + ["trend-model"], user_id=self.bob_id)
            self.assertEqual(comparison["models"], selected)  # 去重并保留选择顺序
            compared = {item["model"]: item for item in comparison["model_series"]}
            dates = [row["date"] for row in comparison["series"]]
            self.assertEqual(len(dates), 8)
            for item in compared.values():
                self.assertEqual([row["date"] for row in item["series"]], dates)
                self.assertEqual(sum(row["requests"] for row in item["series"]), item["summary"]["requests"])
                for row in item["series"]:
                    if not row["requests"]:
                        self.assertIsNone(row["cache_rate"])
            self.assertEqual(compared["trend-model"]["summary"], personal["summary"])
            self.assertEqual(compared["only-bob"]["summary"]["requests"], 0)
            self.assertEqual(compared["trend-model*"]["summary"]["requests"], 0)  # 精确匹配，星号不是通配符
            self.assertIsNone(compared["unknown-trend"]["summary"]["cache_rate"])
            self.assertEqual(comparison["summary"]["requests"], 5)
            self.assertAlmostEqual(comparison["summary"]["cache_rate"], 260/1340*100)
            self.assertEqual(comparison["summary"]["cache_rows"], 4)
            self.assertEqual(sum(row["requests"] for row in comparison["series"]), 5)
            self.assertEqual(compared["trend-model-extra"]["series"][-2]["cache_rate"], 50)
            self.assertIsNone(compared["trend-model"]["series"][-2]["cache_rate"])
            self.assertEqual(read(self.alice_h, clock=now+86400, range="week", models=selected,
                                  as_of=comparison["as_of"]), comparison)
            all_compared = read(self.alice_h, range="all", models=selected)
            self.assertEqual(all_compared["series"][0]["date"], "2001-01-10")  # 不泄露其他用户最早日期
            self.assertEqual(all_compared["model_series"][1]["series"][0]["cache_rate"], 0)
            self.assertIsNone(all_compared["model_series"][0]["series"][0]["cache_rate"])
            no_matches = read(self.alice_h, models=["only-bob", "trend-model*"])
            self.assertEqual(no_matches["series"], [])
            self.assertEqual(no_matches["summary"]["requests"], 0)
            self.assertTrue(all(not item["series"] for item in no_matches["model_series"]))
            global_compared = read(self.admin_h, endpoint="/api/admin/usage/cache/trend",
                                   range="week", models=selected)
            self.assertEqual(global_compared["summary"]["requests"], 6)
            self.assertEqual(global_compared["model_series"][1]["summary"], site["summary"])
            self.assertEqual(self.client.get("/api/admin/usage/cache/trend", headers=self.alice_h,
                                            params={"models": selected}).status_code, 403)
            self.assertEqual(self.client.get("/api/usage/cache/trend", headers=self.alice_h,
                                            params={"model": "trend-model", "models": selected}).status_code, 400)
            for invalid in ("", "x"*257):
                self.assertEqual(self.client.get("/api/usage/cache/trend", headers=self.alice_h,
                                                params={"models": [invalid]}).status_code, 422)
            future = read(self.alice_h, range="week", model="trend-model", as_of=now+86400)
            self.assertEqual(future["as_of"], now)
            self.assertEqual(self.client.get("/api/usage/cache/trend").status_code, 401)
            self.assertEqual(self.client.get("/api/usage/cache/trend?model=", headers=self.alice_h).status_code, 422)
            self.assertEqual(self.client.get("/api/usage/cache/trend?as_of=-1", headers=self.alice_h).status_code, 422)
            self.assertEqual(self.client.get("/api/usage/cache/trend?range=invalid", headers=self.alice_h).status_code, 400)
        finally:
            with _db()._conn() as c:
                c.executemany("DELETE FROM usage_logs WHERE id=?", [(i,) for i in ids])

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
        self.assertAlmostEqual(chans["kogo"]["actual_cost"], 2.75)
        self.assertEqual(s["top_users"][0]["email"], "alice@gmail.com")
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
