#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥级管控:额度上限、模型白名单、IP 白名单、最后使用时间回写。

三项都是「收紧」型:模型必须同时过分组与密钥白名单,额度只是本密钥的消费
上限(钱仍从账户余额扣),IP 白名单空=不限。这里逐条把语义钉死,防止后续
改动把「空=不限」写成「空=拒绝」或反过来。
"""
import os
import tempfile
import time
import datetime as _dt
import unittest

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "bitapi.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "noexist_auths")
os.environ["BITAPI_JWT_SECRET"] = "kc-secret"
os.environ["BITAPI_API_KEY"] = "sk-master-kc"
os.environ["BITAPI_DEFAULT_GROUP"] = "free"

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core.billing import Billing, KeyQuotaError  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DB = None
BILL = None


def setUpModule():
    global DB, BILL
    config.API_KEY = "sk-master-kc"
    config.JWT_SECRET = "kc-secret"
    config.DEFAULT_GROUP = "free"
    config.REQUIRE_INVITE = False
    config.TRUST_PROXY_HEADERS = True
    db_path = os.path.join(_TMP, "bitapi.db")
    config.DB_PATH = db_path
    DB = UserDB(db_path)
    BILL = Billing(DB)
    for mod in (portal_state, server, portal_routes):
        mod.USER_DB = DB
        mod.BILLING = BILL


class KeyControlUnitTest(unittest.TestCase):
    """不经 HTTP,直接钉死 Billing 的判定语义。"""

    GROUP = {"supported_models": ["demo-*", "grok-4.5"], "billing_policy": "free"}

    def test_group_whitelist_unchanged_without_key_list(self):
        self.assertTrue(BILL.check_model_allowed(self.GROUP, "demo-a", None))
        self.assertTrue(BILL.check_model_allowed(self.GROUP, "grok-4.5", None))
        self.assertFalse(BILL.check_model_allowed(self.GROUP, "gpt-5", None))
        # 空白名单 == 不额外收紧
        self.assertTrue(BILL.check_model_allowed(
            self.GROUP, "demo-a", {"allowed_models": []}))

    def test_key_models_can_only_narrow(self):
        key = {"allowed_models": ["demo-b"]}
        self.assertTrue(BILL.check_model_allowed(self.GROUP, "demo-b", key))
        # 分组允许但密钥不允许 → 拒
        self.assertFalse(BILL.check_model_allowed(self.GROUP, "demo-a", key))
        # 密钥允许但分组不允许 → 仍拒(不能扩权)
        self.assertFalse(BILL.check_model_allowed(
            self.GROUP, "gpt-5", {"allowed_models": ["gpt-5"]}))
        self.assertFalse(BILL.check_model_allowed(
            self.GROUP, "gpt-5", {"allowed_models": ["*"]}))

    def test_key_models_support_prefix_wildcard(self):
        key = {"allowed_models": ["demo-x*"]}
        self.assertTrue(BILL.check_model_allowed(self.GROUP, "demo-x1", key))
        self.assertFalse(BILL.check_model_allowed(self.GROUP, "demo-y1", key))

    def test_no_group_still_denies(self):
        self.assertFalse(BILL.check_model_allowed(None, "demo-a", None))

    def test_ip_whitelist(self):
        self.assertTrue(BILL.check_key_ip(None, "1.2.3.4"))
        self.assertTrue(BILL.check_key_ip({"allowed_ips": []}, "1.2.3.4"))
        k = {"allowed_ips": ["1.2.3.4", "10.0.0.0/8"]}
        self.assertTrue(BILL.check_key_ip(k, "1.2.3.4"))
        self.assertTrue(BILL.check_key_ip(k, "10.9.9.9"))
        self.assertFalse(BILL.check_key_ip(k, "1.2.3.5"))
        self.assertFalse(BILL.check_key_ip(k, "11.0.0.1"))
        # 配了名单却拿不到/拿到坏 IP → 拒绝(安全默认)
        self.assertFalse(BILL.check_key_ip(k, None))
        self.assertFalse(BILL.check_key_ip(k, ""))
        self.assertFalse(BILL.check_key_ip(k, "not-an-ip"))
        # 名单里有坏条目不应炸,只是那条不匹配
        self.assertTrue(BILL.check_key_ip(
            {"allowed_ips": ["garbage", "1.2.3.4"]}, "1.2.3.4"))

    def test_ipv6_supported(self):
        k = {"allowed_ips": ["2001:db8::/32"]}
        self.assertTrue(BILL.check_key_ip(k, "2001:db8::1"))
        self.assertFalse(BILL.check_key_ip(k, "2001:dbf::1"))

    def test_quota_precheck(self):
        uid = DB.create_user("q@example.com", "x", "AFFQ1")
        # quota=0 不限
        k0 = DB.create_api_key(uid, "sk-q0", quota=0)
        BILL.precheck({"id": uid, "_api_key_id": k0}, None)
        # quota>0 且未用满 → 通过
        k1 = DB.create_api_key(uid, "sk-q1", quota=1.0)
        DB.touch_api_key(k1, spent=0.4)
        BILL.precheck({"id": uid, "_api_key_id": k1}, None)
        # 用满 → 抛
        DB.touch_api_key(k1, spent=0.6)
        with self.assertRaises(KeyQuotaError):
            BILL.precheck({"id": uid, "_api_key_id": k1}, None)
        rec = DB.get_api_key_by_id(k1)
        self.assertAlmostEqual(rec["used_quota"], 1.0, places=6)

    def test_touch_accumulates_and_sets_time(self):
        uid = DB.create_user("t@example.com", "x", "AFFT1")
        kid = DB.create_api_key(uid, "sk-t1")
        self.assertEqual(DB.get_api_key_by_id(kid)["last_used_at"], 0)
        DB.touch_api_key(kid, spent=0.25)
        DB.touch_api_key(kid, spent=0.25)
        rec = DB.get_api_key_by_id(kid)
        self.assertGreater(rec["last_used_at"], 0)
        self.assertAlmostEqual(rec["used_quota"], 0.5, places=6)


class KeyControlApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        cls.client.post("/api/register", json={
            "email": "owner@example.com", "password": "secret123"})
        cls.tok = cls.client.post("/api/login", json={
            "email": "owner@example.com", "password": "secret123"}).json()["token"]
        cls.h = {"Authorization": f"Bearer {cls.tok}"}

    def _create(self, **body):
        return self.client.post("/api/keys", json=body, headers=self.h)

    def test_list_shape_has_every_column(self):
        r = self._create(name="cols", quota=5, allowed_models=["demo-a"],
                         allowed_ips=["10.0.0.0/8"])
        self.assertEqual(r.status_code, 200, r.text)
        kid = r.json()["id"]
        full = r.json()["key"]
        body = self.client.get("/api/keys", headers=self.h).json()
        self.assertIn("group", body)
        self.assertIn("group_models", body)
        self.assertTrue(body["available_models"])
        self.assertNotIn("*", body["available_models"])
        row = [k for k in body["keys"] if k["id"] == kid][0]
        for f in ("name", "status", "quota", "used_quota", "quota_exhausted",
                  "key_masked", "allowed_models", "allowed_ips", "created_at",
                  "last_used_at", "expires_at", "expired"):
            self.assertIn(f, row)
        self.assertEqual(row["quota"], 5)
        self.assertEqual(row["allowed_models"], ["demo-a"])
        self.assertEqual(row["allowed_ips"], ["10.0.0.0/8"])
        # 列表不含完整明文;脱敏串保留首尾便于区分两把密钥
        self.assertNotIn("key", row)
        self.assertTrue(row["key_masked"].startswith("sk-"))
        self.assertTrue(row["key_masked"].endswith(full[-4:]))
        self.assertLess(len(row["key_masked"]), len(full))
        # 完整明文按 id 单取
        rev = self.client.get(f"/api/keys/{kid}/reveal", headers=self.h)
        self.assertEqual(rev.json()["key"], full)

    def test_defaults_are_unlimited(self):
        kid = self._create(name="plain").json()["id"]
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == kid][0]
        self.assertEqual(row["quota"], 0)
        self.assertEqual(row["allowed_models"], [])
        self.assertEqual(row["allowed_ips"], [])
        self.assertFalse(row["quota_exhausted"])

    def test_stats_block(self):
        """概览四卡的数字:总数 / 活跃 / 今日费用 / 累计费用。
        活跃必须排掉禁用、过期、额度耗尽三种失效,不是简单数 status。"""
        import time as _t
        body = self.client.get("/api/keys", headers=self.h).json()
        keys = body["keys"]
        st = body["stats"]
        self.assertEqual(st["total"], len(keys))
        self.assertEqual(st["active"], len(
            [k for k in keys if k["status"] == "active" and not k["expired"]
             and not k["quota_exhausted"]]))
        self.assertAlmostEqual(st["cost_all"],
                               sum(k["usage"]["cost"] for k in keys), places=8)
        self.assertAlmostEqual(st["cost_today"],
                               sum(k["usage_today"]["cost"] for k in keys),
                               places=8)

        # 三种失效各造一把,active 不应把它们算进去
        base = self.client.get("/api/keys", headers=self.h).json()["stats"]["active"]
        dis = self._create(name="st-disabled").json()["id"]
        self.client.patch("/api/keys/%d" % dis, json={"status": "disabled"},
                          headers=self.h)
        exp = self._create(name="st-expired",
                           expires_at=int(_t.time()) + 2).json()["id"]
        self.client.patch("/api/keys/%d" % exp,
                          json={"expires_at": int(_t.time()) - 10}, headers=self.h)
        ex = self._create(name="st-exhausted", quota=1.0).json()["id"]
        DB.touch_api_key(ex, spent=1.0)
        st2 = self.client.get("/api/keys", headers=self.h).json()["stats"]
        self.assertEqual(st2["total"], st["total"] + 3)
        self.assertEqual(st2["active"], base)

    def test_usage_today_split(self):
        """今日与累计是两个窗口:写一条 3 天前的用量只进累计,不进今日。"""
        created = self._create(name="split").json()
        kid = created["id"]
        u = DB.get_user_by_email("owner@example.com")
        now = int(__import__("time").time())
        DB.add_usage(user_id=u["id"], api_key_id=kid, channel="c", model="m",
                     input_tokens=10, output_tokens=5, cost=2.0, actual_cost=2.0)
        with DB._conn() as c:
            c.execute("UPDATE usage_logs SET created_at=? WHERE api_key_id=?",
                      (now - 3 * 86400, kid))
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == kid][0]
        self.assertAlmostEqual(row["usage"]["cost"], 2.0)
        self.assertEqual(row["usage_today"]["cost"], 0)
        DB.add_usage(user_id=u["id"], api_key_id=kid, channel="c", model="m",
                     input_tokens=1, output_tokens=1, cost=0.5, actual_cost=0.5)
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == kid][0]
        self.assertAlmostEqual(row["usage"]["cost"], 2.5)
        self.assertAlmostEqual(row["usage_today"]["cost"], 0.5)

    def test_usage_map_matches_single_query(self):
        """批量聚合与逐把查询结果一致(前者是后者的 N+1 消除版)。"""
        keys = DB.list_api_keys(
            DB.get_user_by_email("owner@example.com")["id"])
        m = DB.api_key_usage_map(
            DB.get_user_by_email("owner@example.com")["id"])
        for k in keys:
            one = DB.api_key_usage(k["id"])
            got = m.get(k["id"], {"requests": 0, "tokens": 0, "cost": 0.0})
            self.assertEqual(one["requests"], got["requests"], k["id"])
            self.assertAlmostEqual(one["cost"], got["cost"], places=8)

    def test_bad_ip_rejected_on_create_and_patch(self):
        r = self._create(name="badip", allowed_ips=["999.1.1.1"])
        self.assertEqual(r.status_code, 400, r.text)
        kid = self._create(name="okip").json()["id"]
        r = self.client.patch(f"/api/keys/{kid}",
                              json={"allowed_ips": ["nonsense"]}, headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)

    def test_patch_can_clear_lists(self):
        kid = self._create(name="clearme", allowed_models=["demo-a"],
                           allowed_ips=["1.2.3.4"]).json()["id"]
        r = self.client.patch(f"/api/keys/{kid}",
                              json={"allowed_models": [], "allowed_ips": []},
                              headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == kid][0]
        self.assertEqual(row["allowed_models"], [])
        self.assertEqual(row["allowed_ips"], [])

    def test_patch_quota_and_negative_rejected(self):
        kid = self._create(name="q").json()["id"]
        self.assertEqual(self.client.patch(
            f"/api/keys/{kid}", json={"quota": 2.5}, headers=self.h).status_code, 200)
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == kid][0]
        self.assertEqual(row["quota"], 2.5)
        self.assertEqual(self.client.patch(
            f"/api/keys/{kid}", json={"quota": -1}, headers=self.h).status_code, 422)

    def test_gateway_402_when_key_quota_exhausted(self):
        created = self._create(name="exhaust", quota=1.0).json()
        DB.touch_api_key(created["id"], spent=1.0)
        BILL.invalidate()
        r = self.client.post("/v1/chat/completions",
                             json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {created['key']}"})
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(r.json()["detail"]["code"], "KEY_QUOTA_EXHAUSTED")
        # 列表把它标成耗尽
        row = [k for k in self.client.get("/api/keys", headers=self.h).json()["keys"]
               if k["id"] == created["id"]][0]
        self.assertTrue(row["quota_exhausted"])

    def test_gateway_403_when_model_not_in_key_list(self):
        """分组允许 * ,密钥只允许 demo-*,请求 grok 应 403(收紧生效)。"""
        gid = DB.create_group(name="kc-all", supported_models=["*"], rpm_limit=0)
        u = DB.get_user_by_email("owner@example.com")
        old = u["group_id"]
        DB.update_user(u["id"], group_id=gid)
        BILL.invalidate()
        try:
            created = self._create(name="narrow", allowed_models=["demo-*"]).json()
            h = {"Authorization": f"Bearer {created['key']}"}
            r = self.client.post("/v1/chat/completions",
                                 json={"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}]},
                                 headers=h)
            self.assertEqual(r.status_code, 403, r.text)
            # demo-* 内的模型不应被这道白名单拦(未知模型走到路由才 404)
            r = self.client.post("/v1/chat/completions",
                                 json={"model": "demo-nope", "messages": [{"role": "user", "content": "hi"}]},
                                 headers=h)
            self.assertNotEqual(r.status_code, 403, r.text)
            # /v1/models 与网关口径一致:只剩 demo-*
            ids = [m["id"] for m in self.client.get("/v1/models", headers=h).json()["data"]]
            self.assertTrue(all(m.startswith("demo-") for m in ids), ids[:5])
        finally:
            DB.update_user(u["id"], group_id=old)
            BILL.invalidate()

    def test_gateway_403_when_ip_not_allowed(self):
        gid = DB.create_group(name="kc-all2", supported_models=["*"], rpm_limit=0)
        u = DB.get_user_by_email("owner@example.com")
        old = u["group_id"]
        DB.update_user(u["id"], group_id=gid)
        BILL.invalidate()
        try:
            created = self._create(name="pinned", allowed_ips=["203.0.113.7"]).json()
            h = {"Authorization": f"Bearer {created['key']}"}
            r = self.client.post("/v1/chat/completions",
                                 json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                                 headers=dict(h, **{"X-Forwarded-For": "198.51.100.9"}))
            self.assertEqual(r.status_code, 403, r.text)
            r = self.client.post("/v1/chat/completions",
                                 json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                                 headers=dict(h, **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}))
            self.assertNotEqual(r.status_code, 403, r.text)
        finally:
            DB.update_user(u["id"], group_id=old)
            BILL.invalidate()

    def test_record_usage_sets_last_used_and_used_quota(self):
        created = self._create(name="settle").json()
        kid = created["id"]
        u = DB.get_user_by_email("owner@example.com")
        group = {"id": u["group_id"], "billing_policy": "free",
                 "rate_multiplier": 1.0, "supported_models": ["*"]}
        BILL.record_usage(dict(u, _api_key_id=kid), group, "kg", "demo-test",
                          upstream_usage={"prompt_tokens": 10,
                                          "completion_tokens": 5},
                          duration_ms=100, request_id="kc-req-1")
        rec = DB.get_api_key_by_id(kid)
        self.assertGreater(rec["last_used_at"], 0)
        self.assertLessEqual(abs(rec["last_used_at"] - int(time.time())), 5)


class OverviewTest(unittest.TestCase):
    """概览八卡的数据源 /api/overview。缓存 token 由 snapshot 反推,
    没有 snapshot 的行必须承认「不知道」而不是当 0 —— 这条最容易写错。

    用独立账号,避免与 KeyControlApiTest 的用量互相污染。"""

    @classmethod
    def setUpClass(cls):
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        cls.client.post("/api/register", json={
            "email": "ov@example.com", "password": "secret123"})
        cls.tok = cls.client.post("/api/login", json={
            "email": "ov@example.com", "password": "secret123"}).json()["token"]
        cls.h = {"Authorization": f"Bearer {cls.tok}"}
        cls.uid = DB.get_user_by_email("ov@example.com")["id"]

    def _get(self):
        r = self.client.get("/api/overview", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_empty_account_is_all_zero(self):
        o = self._get()
        for k in ("requests", "input_tokens", "output_tokens", "cache_tokens",
                  "cost", "actual_cost", "duration_ms"):
            self.assertEqual(o["total"][k], 0, k)
        self.assertEqual(o["total"]["cache_rows"], 0)
        self.assertIsNone(o["total"]["first_at"])
        self.assertEqual(o["perf"]["rpm"], 0)
        self.assertEqual(o["perf"]["tpm"], 0)
        self.assertEqual(o["perf"]["avg_ms"], 0)
        self.assertEqual(o["perf"]["samples"], 0)
        self.assertEqual(o["keys"], {"total": 0, "active": 0})

    def test_b_cache_tokens_derived_from_snapshot(self):
        # 有 snapshot 且 total_ctx > input → 缓存 = 差值
        DB.add_usage(user_id=self.uid, model="m", input_tokens=100,
                     output_tokens=10, cost=1.0, actual_cost=1.0,
                     duration_ms=2000,
                     pricing_snapshot={"mode": "token", "total_ctx": 350})
        # total_ctx < input(理论上不该出现)→ 夹到 0,不能出负数
        DB.add_usage(user_id=self.uid, model="m", input_tokens=200,
                     output_tokens=20, cost=2.0, actual_cost=2.0,
                     duration_ms=0,
                     pricing_snapshot={"mode": "token", "total_ctx": 150})
        # 有 snapshot 但没有 total_ctx → 算不出,两个计数都不参与
        DB.add_usage(user_id=self.uid, model="m", input_tokens=50,
                     output_tokens=5, cost=0.5, actual_cost=0.5,
                     duration_ms=1000, pricing_snapshot={"mode": "free"})
        # 完全没有 snapshot
        DB.add_usage(user_id=self.uid, model="m", input_tokens=10,
                     output_tokens=1, cost=0, actual_cost=0, duration_ms=500)

        t = self._get()["total"]
        self.assertEqual(t["requests"], 4)
        self.assertEqual(t["input_tokens"], 360)
        self.assertEqual(t["output_tokens"], 36)
        self.assertEqual(t["cache_tokens"], 250)   # 250 + 0,后两条不参与
        self.assertEqual(t["cache_rows"], 2)       # 只有两条带 total_ctx
        self.assertAlmostEqual(t["cost"], 3.5)
        self.assertAlmostEqual(t["actual_cost"], 3.5)

    def test_c_avg_ms_only_counts_rows_with_duration(self):
        """平均响应只对有耗时的行取平均:duration_ms=0 的行不该拉低均值。"""
        o = self._get()
        # 上一个用例写了 4 条,其中 3 条有耗时(2000/1000/500)
        self.assertEqual(o["perf"]["samples"], 3)
        self.assertAlmostEqual(o["perf"]["avg_ms"], 3500 / 3, places=6)

    def test_d_rpm_tpm_use_record_span_not_calendar_day(self):
        """速率按首末记录的跨度平摊,而不是固定除以 1440 分钟。"""
        o = self._get()
        span = o["perf"]["span_minutes"]
        self.assertGreaterEqual(span, 1.0)
        self.assertAlmostEqual(o["perf"]["rpm"],
                               o["total"]["requests"] / span, places=6)
        toks = o["total"]["input_tokens"] + o["total"]["output_tokens"]
        self.assertAlmostEqual(o["perf"]["tpm"], toks / span, places=6)

    def test_e_quota_policy_adds_usage_block(self):
        """quota 组才带日/周/月额度块;balance 与 free 组不带。"""
        u = DB.get_user_by_email("ov@example.com")
        old = u["group_id"]
        gid = DB.create_group(name="ov-quota", supported_models=["*"],
                              billing_policy="quota", daily_limit=100,
                              weekly_limit=500, monthly_limit=2000,
                              limit_unit="requests")
        DB.update_user(u["id"], group_id=gid)
        try:
            o = self._get()
            self.assertEqual(o["policy"], "quota")
            self.assertIn("usage", o)
            self.assertEqual(o["usage"]["daily"]["limit"], 100)
        finally:
            DB.update_user(u["id"], group_id=old)
        self.assertNotIn("usage", self._get())

    def test_f_today_window_excludes_old_rows(self):
        """今日窗口是近 24 小时:把全部记录挪到 3 天前,今日归零而累计不变。
        放在本类最后跑(名字带 f),它会改动 created_at。"""
        before = self._get()
        self.assertEqual(before["today"]["requests"], before["total"]["requests"])
        now = int(time.time())
        with DB._conn() as c:
            c.execute("UPDATE usage_logs SET created_at=? WHERE user_id=?",
                      (now - 3 * 86400, self.uid))
        after = self._get()
        self.assertEqual(after["today"]["requests"], 0)
        self.assertEqual(after["today"]["cache_tokens"], 0)
        self.assertEqual(after["total"]["requests"], before["total"]["requests"])
        self.assertEqual(after["total"]["cache_tokens"],
                         before["total"]["cache_tokens"])

    def test_g_series_is_14_padded_days(self):
        """图表序列必须补齐 14 格:没有记录的日子也要有格子,
        否则柱状图会把「那天没调用」直接跳过,时间轴被压缩。"""
        o = self._get()
        s = o["series"]
        self.assertEqual(len(s), 14)
        self.assertEqual(s[-1]["date"], _dt.date.today().isoformat())
        # 日期严格递增且相邻差一天
        for a, b in zip(s, s[1:]):
            da = _dt.date.fromisoformat(a["date"])
            db = _dt.date.fromisoformat(b["date"])
            self.assertEqual((db - da).days, 1)
        # label 是 MM-DD
        self.assertEqual(s[0]["label"], s[0]["date"][5:])
        # 上一个用例把记录挪到 3 天前 → 只有那一天有量,其余为 0
        nonzero = [d for d in s if d["requests"]]
        self.assertEqual(len(nonzero), 1)
        self.assertEqual(nonzero[0]["date"],
                         (_dt.date.today() - _dt.timedelta(days=3)).isoformat())

    def test_h_series_and_by_model_share_token_definition(self):
        """三处 token 口径必须一致(都含反推的缓存),否则图表合计与卡片对不上。"""
        o = self._get()
        s_tok = sum(d["tokens"] for d in o["series"])
        m_tok = sum(m["tokens"] for m in o["by_model"])
        t = o["total"]
        card = t["input_tokens"] + t["output_tokens"] + t["cache_tokens"]
        self.assertEqual(s_tok, card)
        self.assertEqual(m_tok, card)
        # 请求数与消费同样对得上
        self.assertEqual(sum(d["requests"] for d in o["series"]), t["requests"])
        self.assertAlmostEqual(sum(d["cost"] for d in o["series"]),
                               t["actual_cost"], places=8)

    def test_i_by_model_collapses_tail_into_other(self):
        """模型数超过 6 个时,尾部并成一条「其他 N 个」,总量不丢。"""
        for k in range(9):
            DB.add_usage(user_id=self.uid, model="mdl-%d" % k,
                         input_tokens=100 - k * 5, output_tokens=1,
                         cost=0.1, actual_cost=0.1, duration_ms=10)
        o = self._get()
        rows = o["by_model"]
        self.assertLessEqual(len(rows), 7)          # 6 + 1 条「其他」
        other = [r for r in rows if r["model"].startswith("其他")]
        self.assertEqual(len(other), 1, rows)
        self.assertEqual(sum(r["tokens"] for r in rows),
                         o["total"]["input_tokens"] + o["total"]["output_tokens"]
                         + o["total"]["cache_tokens"])
        self.assertEqual(sum(r["requests"] for r in rows),
                         o["total"]["requests"])


class ProfileTest(unittest.TestCase):
    """昵称与头像。头像走 data URI 存 DB,所以尺寸与格式在后端必须硬校验
    —— 前端的压缩可以被绕过。"""

    PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
           "FcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    @classmethod
    def setUpClass(cls):
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        cls.client.post("/api/register", json={
            "email": "pf@example.com", "password": "secret123"})
        cls.tok = cls.client.post("/api/login", json={
            "email": "pf@example.com", "password": "secret123"}).json()["token"]
        cls.h = {"Authorization": f"Bearer {cls.tok}"}

    def _me(self):
        return self.client.get("/api/me", headers=self.h).json()

    def _patch(self, **body):
        return self.client.patch("/api/me", json=body, headers=self.h)

    def test_a_me_exposes_profile_fields(self):
        me = self._me()
        for f in ("display_name", "avatar", "balance", "total_spent",
                  "rpm_limit", "email_verified_at"):
            self.assertIn(f, me)
        self.assertEqual(me["display_name"], "")
        self.assertEqual(me["avatar"], "")

    def test_b_set_and_clear_display_name(self):
        self.assertEqual(self._patch(display_name="阿明").status_code, 200)
        self.assertEqual(self._me()["display_name"], "阿明")
        # 空串 = 清空,回落邮箱前缀由前端做,后端存 NULL
        self.assertEqual(self._patch(display_name="").status_code, 200)
        self.assertEqual(self._me()["display_name"], "")
        # 只传头像不该动昵称
        self._patch(display_name="阿明")
        self._patch(avatar=self.PNG)
        self.assertEqual(self._me()["display_name"], "阿明")

    def test_c_display_name_length_capped(self):
        self.assertEqual(self._patch(display_name="x" * 32).status_code, 200)
        self.assertEqual(self._patch(display_name="x" * 33).status_code, 422)

    def test_d_avatar_must_be_image_data_uri(self):
        for bad in ("https://example.com/a.png", "data:text/html;base64,AAA",
                    "not-a-uri", "data:image/svg+xml;base64,AAA"):
            self.assertEqual(self._patch(avatar=bad).status_code, 400, bad)
        for ok in ("data:image/png;base64,", "data:image/jpeg;base64,",
                   "data:image/webp;base64,", "data:image/gif;base64,"):
            self.assertEqual(self._patch(avatar=ok + "AAAA").status_code, 200, ok)

    def test_e_avatar_size_capped_on_server(self):
        """前端压到 20KB,但前端可绕过,后端要再挡一次。"""
        self.assertEqual(
            self._patch(avatar="data:image/png;base64," + "A" * 27000).status_code,
            200)
        self.assertEqual(
            self._patch(avatar="data:image/png;base64," + "A" * 28100).status_code,
            400)
        # 超过 Pydantic 的 max_length 直接 422,不进业务校验
        self.assertEqual(
            self._patch(avatar="data:image/png;base64," + "A" * 40000).status_code,
            422)

    def test_f_avatar_clear_keeps_other_fields(self):
        self._patch(display_name="留着", avatar=self.PNG)
        self.assertEqual(self._patch(avatar="").status_code, 200)
        me = self._me()
        self.assertEqual(me["avatar"], "")
        self.assertEqual(me["display_name"], "留着")

    def test_g_empty_patch_is_noop(self):
        self._patch(display_name="不变")
        r = self._patch()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["updated"], [])
        self.assertEqual(self._me()["display_name"], "不变")

    def test_h_profile_is_per_user(self):
        """改自己的资料不能影响别人 —— PATCH /me 用的是 JWT 里的 uid。"""
        self.client.post("/api/register", json={
            "email": "pf2@example.com", "password": "secret123"})
        tok2 = self.client.post("/api/login", json={
            "email": "pf2@example.com", "password": "secret123"}).json()["token"]
        h2 = {"Authorization": f"Bearer {tok2}"}
        self._patch(display_name="我")
        self.client.patch("/api/me", json={"display_name": "他"}, headers=h2)
        self.assertEqual(self._me()["display_name"], "我")
        self.assertEqual(
            self.client.get("/api/me", headers=h2).json()["display_name"], "他")


if __name__ == "__main__":
    unittest.main()
