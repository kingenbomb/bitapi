#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池面板的门禁 —— 端点契约 + 「摘出号池」真的摘得出去。

这个仓栽过一次:端点调的方法被换实现后消失了,端点照样注册、界面照样渲染,
只有点下去才 500。所以面板用的每个端点都有一条直接打上去的用例,
断言的是**行为**(号有没有真的不再被取到、导出能不能导回去),不是状态码。
"""
import json
import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "panel.db")
os.environ["BITAPI_ADMIN_KEY"] = "adm-test"

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from core.adapter import get_adapter  # noqa: E402
from core.pool_state import DB, POOL  # noqa: E402

H = {"Authorization": "Bearer adm-test"}
# token 直接给到远期 —— 号池取号时会惰性刷新,而测试不该真的出网。
_FAR = int(time.time()) + 86400


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(server.app)
        # 种一批号:unittest 按方法名字母序跑,不在这里种的话
        # 「导出全部」会跑在任何一条导入用例前面,读到一个空库。
        cls._seed()

    @classmethod
    def _seed(cls):
        cls.c.post("/admin/accounts/import", headers=H, json={
            "channel": "openai", "accounts": [
                {"refresh_token": "rt-p1", "email": "p1@x.com",
                 "access_token": "at", "expires_at": _FAR},
                {"refresh_token": "rt-p2", "email": "p2@x.com",
                 "access_token": "at", "expires_at": _FAR}]})
        cls.c.post("/admin/accounts/import", headers=H, json={
            "channel": "claude", "accounts": [
                {"refresh_token": "rt-c1", "account_uuid": "uuid-aaa",
                 "access_token": "at", "expires_at": _FAR},
                {"refresh_token": "rt-c2", "account_uuid": "uuid-bbb",
                 "access_token": "at", "expires_at": _FAR}]})

    def _import(self, channel, accounts):
        r = self.c.post("/admin/accounts/import", headers=H,
                        json={"channel": channel, "accounts": accounts})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _ids(self, channel):
        return [a["id"] for a in DB.list_accounts(channel=channel, limit=999)]

    def _fresh(self, channel):
        """把本渠道所有号恢复成「一个健康的号该有的样子」:在池、active、token 未过期。

        用例之间会互相影响(有的把号置成待检、有的只导了 refresh_token),
        而这条用例的前提是「池子里有健康号」,所以先把前提铺平 ——
        顺带也保证取号不会真的出网:token 没过期就不会去刷。
        """
        ids = self._ids(channel)
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids, "action": "enable"})
        for i in ids:
            DB.update_account(i, status="active", token="at", token_exp=_FAR)

    # ---- 鉴权 ----

    def test_admin_endpoints_reject_without_key(self):
        for method, path in (("get", "/admin/accounts"),
                             ("get", "/admin/accounts/export"),
                             ("get", "/admin/accounts/stats"),
                             ("post", "/admin/accounts/bulk"),
                             ("post", "/admin/accounts/import"),
                             ("post", "/admin/accounts/test")):
            r = (self.c.get(path) if method == "get"
                 else self.c.post(path, json={}))
            self.assertEqual(r.status_code, 401, f"{path} 没鉴权")

    # ---- 导入 ----

    def test_import_creates_accounts(self):
        j = self._import("openai", [
            {"refresh_token": "rt-n1", "email": "n1@x.com"},
            {"refresh_token": "rt-n2", "email": "n2@x.com"}])
        self.assertEqual(j["count"], 2)
        self.assertEqual(j["errors"], [])

    def test_import_reports_per_item_errors(self):
        """一条坏的不该让整批失败,而且要说清是哪条坏的。"""
        j = self._import("openai", [
            {"refresh_token": "rt-ok", "email": "n3@x.com"},
            {"email": "n4@x.com"}])            # 缺 refresh_token
        self.assertEqual(j["count"], 1)
        self.assertEqual(len(j["errors"]), 1)
        self.assertIn("n4@x.com", j["errors"][0])

    def test_import_empty_list_is_400(self):
        r = self.c.post("/admin/accounts/import", headers=H,
                        json={"channel": "openai", "accounts": []})
        self.assertEqual(r.status_code, 400)

    def test_import_unknown_channel_is_400(self):
        r = self.c.post("/admin/accounts/import", headers=H,
                        json={"channel": "nope", "accounts": [{"refresh_token": "x"}]})
        self.assertEqual(r.status_code, 400)

    # ---- 列表:筛选 / 排序 / 分页 ----

    def test_filter_by_keyword_hits_identity(self):
        j = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "claude", "q": "aaa"}).json()
        self.assertEqual(j["total"], 1)
        self.assertEqual(j["accounts"][0]["identity"], "uuid-aaa")

    def test_filter_by_keyword_hits_meta(self):
        """号的关键信息一半在 identity(email)一半在 meta(套餐),
        只搜 identity 会让「按套餐筛」这类用法落空。"""
        DB.update_account(self._ids("claude")[0], meta={"plan": "k12-edu"})
        j = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "claude", "q": "k12-edu"}).json()
        self.assertEqual(j["total"], 1)

    def test_sort_and_total_are_server_side(self):
        j = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "openai", "sort": "identity",
                               "order": "asc", "limit": 1}).json()
        self.assertEqual(len(j["accounts"]), 1)
        self.assertGreaterEqual(j["total"], 2)
        self.assertEqual(j["sort"], "identity")

    def test_unknown_sort_key_does_not_inject_sql(self):
        """排序键来自 query string。不白名单就是一条注入口子。"""
        j = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "openai",
                               "sort": "id; DROP TABLE accounts"}).json()
        self.assertIn("accounts", j)
        self.assertGreaterEqual(j["total"], 2)   # 表还在

    def test_list_never_leaks_secret_or_full_token(self):
        DB.update_account(self._ids("openai")[0], token="sk-secret-value-1234567890")
        a = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "openai"}).json()["accounts"][0]
        self.assertNotIn("secret", a)
        self.assertNotIn("secret-value", a.get("token") or "")

    # ---- 可调度开关 ----

    def test_parked_account_is_not_picked_and_is_counted(self):
        """摘出 = 不再被取号,但状态还是 active(它本身没坏)。
        面板上「可用 N」和「其中 M 个已摘出」要能同时看见,否则账对不上。"""
        self._fresh("openai")
        ids = self._ids("openai")
        r = self.c.post("/admin/accounts/bulk", headers=H,
                        json={"ids": ids, "action": "disable"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(POOL.get_valid_account(
            "openai", get_adapter("openai"), max_tries=2),
            "摘出之后不该还能取到号")
        self.assertEqual(DB.stats()["openai"]["parked"], len(ids))
        # 放回去就又能取到
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids, "action": "enable"})
        self.assertIsNotNone(POOL.get_valid_account(
            "openai", get_adapter("openai"), max_tries=2))
        self.assertEqual(DB.stats()["openai"]["parked"], 0)

    def test_filter_by_schedulable(self):
        self._fresh("openai")
        ids = self._ids("openai")
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids[:1], "action": "disable"})
        j = self.c.get("/admin/accounts", headers=H,
                       params={"channel": "openai", "schedulable": 0}).json()
        self.assertEqual(j["total"], len(ids[:1]))
        self.assertTrue(all(a["schedulable"] == 0 for a in j["accounts"]))

    def test_bulk_reset_parks_to_unchecked_not_active(self):
        """重置状态是「回到待检」,不是替巡检下结论说它可用。"""
        ids = self._ids("openai")
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids, "action": "reset"})
        self.assertTrue(all(a["status"] == "unchecked"
                            for a in DB.list_accounts(channel="openai", limit=99)))

    def test_bulk_unknown_action_is_400(self):
        r = self.c.post("/admin/accounts/bulk", headers=H,
                        json={"ids": [1], "action": "drop-everything"})
        self.assertEqual(r.status_code, 400)

    def test_bulk_requires_ids(self):
        r = self.c.post("/admin/accounts/bulk", headers=H,
                        json={"ids": [], "action": "enable"})
        self.assertEqual(r.status_code, 400)

    # ---- 导出 / 导入往返 ----

    def test_export_selected_contains_credentials(self):
        """导出是唯一会吐 secret 明文的读端点 —— 备份要的就是能导回去的那份。"""
        acct_id = self._ids("openai")[0]
        j = self.c.get("/admin/accounts/export", headers=H,
                       params={"ids": str(acct_id)}).json()
        self.assertEqual(j["count"], 1)
        self.assertIn("refresh_token", j["accounts"][0]["secret"])

    def test_export_import_round_trip_keeps_schedulable(self):
        """导回去不能把「谁被摘出了」这个决定悄悄抹掉。"""
        self._fresh("openai")
        ids = self._ids("openai")
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids, "action": "disable"})
        dump = self.c.get("/admin/accounts/export", headers=H,
                          params={"channel": "openai"}).json()
        self.c.post("/admin/accounts/bulk", headers=H,
                    json={"ids": ids, "action": "enable"})
        self._import("openai", dump["accounts"])
        self.assertEqual(DB.stats()["openai"]["parked"], len(ids))

    def test_export_all_when_no_ids(self):
        j = self.c.get("/admin/accounts/export", headers=H).json()
        self.assertGreaterEqual(j["count"], 2)

    # ---- 账号统计 ----

    def test_stats_reports_zero_when_no_usage_yet(self):
        # 单独导一个号来断言「没用量就是 0」—— 共用种子号的话会被别的用例
        # 写进去的用量污染,而 unittest 是按方法名字母序跑的。
        self._import("claude", [{"refresh_token": "rt-z", "account_uuid": "uuid-zero",
                                 "access_token": "at", "expires_at": _FAR}])
        acct = DB.get_by_identity("claude", "uuid-zero")
        j = self.c.get("/admin/accounts/stats", headers=H,
                       params={"ids": str(acct["id"]), "days": 7}).json()
        self.assertTrue(j["stats"])
        self.assertEqual(j["stats"][0]["requests"], 0)

    def test_stats_counts_usage_attributed_to_the_account(self):
        """按号统计的锚点是 usage_logs.account_id —— 没有它就只能给渠道级汇总,
        一个渠道几十个号时那个数对站长没有意义。"""
        from core.portal_state import USER_DB
        self._import("claude", [{"refresh_token": "rt-u", "account_uuid": "uuid-used",
                                 "access_token": "at", "expires_at": _FAR}])
        acct = DB.get_by_identity("claude", "uuid-used")
        USER_DB.add_usage(user_id=1, channel="claude", model="claude-sonnet-4-6",
                          input_tokens=10, output_tokens=5, actual_cost=0.02,
                          account_id=acct["id"])
        j = self.c.get("/admin/accounts/stats", headers=H,
                       params={"ids": str(acct["id"])}).json()
        s = j["stats"][0]
        self.assertEqual(s["requests"], 1)
        self.assertEqual(s["input_tokens"], 10)
        self.assertAlmostEqual(s["cost"], 0.02)

    def test_stats_only_counts_within_the_window(self):
        from core.portal_state import USER_DB
        self._import("claude", [{"refresh_token": "rt-o", "account_uuid": "uuid-old",
                                 "access_token": "at", "expires_at": _FAR}])
        acct = DB.get_by_identity("claude", "uuid-old")
        USER_DB.add_usage(user_id=1, channel="claude", model="claude-sonnet-4-6",
                          input_tokens=99, account_id=acct["id"])
        # usage_logs 在用户层那个库里(USER_DB),跟 accounts 不是同一份 ——
        # 直接照 BITAPI_DB 连会连到另一个文件上,报「no such table」。
        import sqlite3
        with sqlite3.connect(USER_DB.path) as c:
            c.execute("UPDATE usage_logs SET created_at=? WHERE account_id=?",
                      (int(time.time()) - 30 * 86400, acct["id"]))
        j = self.c.get("/admin/accounts/stats", headers=H,
                       params={"ids": str(acct["id"]), "days": 7}).json()
        self.assertEqual(j["stats"][0]["input_tokens"], 0)

    # ---- 测试端点 ----

    def test_test_endpoint_calls_adapter_and_echoes_upstream(self):
        """端点调的是 adapter.test_account —— 与方法消失那次同一种断法。
        这里换成一个假实现,验证「真的调到了」而不是只回了 200。"""
        acct = DB.list_accounts(channel="claude", limit=1)[0]
        ad = get_adapter("claude")
        seen = {}

        def fake(acct_, model=None, prompt=None):
            seen["identity"] = acct_["identity"]
            return {"ok": True, "model": model or "m", "ms": 12,
                    "text": "上游原话", "reasoning": None,
                    "usage": {"total_tokens": 3}}

        original = ad.test_account
        ad.test_account = fake
        try:
            j = self.c.post("/admin/accounts/test", headers=H,
                            json={"id": acct["id"]}).json()
        finally:
            ad.test_account = original
        self.assertTrue(j["ok"])
        self.assertEqual(j["text"], "上游原话")
        self.assertEqual(seen["identity"], acct["identity"])

    def test_test_endpoint_surfaces_upstream_error_verbatim(self):
        """401(号坏了)和 503(上游此刻不好)站长要自己看得见,不能只给一句「失败」。"""
        from adapters.claude import ClaudeError
        acct = DB.list_accounts(channel="claude", limit=1)[0]
        ad = get_adapter("claude")

        def boom(*a, **k):
            raise ClaudeError(401, "upstream said: invalid_grant")

        original = ad.test_account
        ad.test_account = boom
        try:
            j = self.c.post("/admin/accounts/test", headers=H,
                            json={"id": acct["id"]}).json()
        finally:
            ad.test_account = original
        self.assertFalse(j["ok"])
        self.assertEqual(j["code"], 401)
        self.assertIn("invalid_grant", j["error"])

    def test_test_endpoint_404_on_unknown_account(self):
        r = self.c.post("/admin/accounts/test", headers=H, json={"id": 999999})
        self.assertEqual(r.status_code, 404)

    # ---- 用量窗口:一次真请求之后要落到库上 ----

    def test_gateway_request_persists_window_meta(self):
        """窗口数据从上游响应头来,经 adapter → holder → 结算收尾写进 accounts.meta。

        这条链路跨了四层,任何一层断掉都是静默的:面板上那两条进度条永远空着,
        而没人会因此报错。所以直接打一次网关,回头查库。
        """
        import config
        self._fresh("openai")
        acct = DB.list_accounts(channel="openai")[0]
        DB.update_account(acct["id"], meta={})
        ad = get_adapter("openai")
        patch = {"usage_5h": {"pct": 0.42},
                 "usage_7d": {"pct": 0.81},
                 "usage_updated_at": int(time.time())}

        def fake_chat(acct_, messages, stream=False, model=None, on_token=None,
                      body=None):
            return {"raw": {"id": "x", "object": "chat.completion",
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant",
                                                     "content": "hi"}}],
                            "usage": {"total_tokens": 3}},
                    "content": "hi", "meta": patch}

        original = ad.chat
        ad.chat = fake_chat
        try:
            r = self.c.post("/v1/chat/completions",
                            headers={"Authorization": "Bearer " + config.API_KEY},
                            json={"model": "gpt-5.4",
                                  "messages": [{"role": "user", "content": "hi"}]})
        finally:
            ad.chat = original
        self.assertEqual(r.status_code, 200, r.text)
        meta = DB.get_account(acct["id"])["meta"] or {}
        self.assertAlmostEqual(meta["usage_5h"]["pct"], 0.42)
        self.assertAlmostEqual(meta["usage_7d"]["pct"], 0.81)

    def test_window_survives_a_429_from_upstream(self):
        """窗口打满时上游回 429,最新百分比就在 429 的响应头里 —— 不从失败
        响应里取,面板会一直停在旧数上,而那个号其实已经调不动了。"""
        import email.message

        import config
        from adapters.openai import OpenAIError
        self._fresh("openai")
        acct = DB.list_accounts(channel="openai")[0]
        DB.update_account(acct["id"], meta={})
        ad = get_adapter("openai")
        hdrs = email.message.Message()
        hdrs["x-codex-primary-used-percent"] = "100"
        hdrs["x-codex-primary-window-minutes"] = "300"

        def boom(*a, **k):
            raise OpenAIError(429, "rate limited", headers=hdrs)

        original = ad.chat
        ad.chat = boom
        try:
            # 网关会把 429 原样回给客户端(客户端 SDK 见 429 自己退避),
            # 但窗口数据必须已经落盘 —— 这正是「面板显示的和实际对不上」的那个点。
            r = self.c.post("/v1/chat/completions",
                            headers={"Authorization": "Bearer " + config.API_KEY},
                            json={"model": "gpt-5.4",
                                  "messages": [{"role": "user", "content": "hi"}]})
        finally:
            ad.chat = original
        self.assertEqual(r.status_code, 429)
        meta = DB.get_account(acct["id"])["meta"] or {}
        self.assertAlmostEqual(meta.get("usage_5h", {}).get("pct", -1), 1.0)

    def test_test_endpoint_persists_window_meta(self):
        """「测试」是站长发现调不动时第一个点的按钮,它也该刷新窗口数据。"""
        self._fresh("claude")
        acct = DB.list_accounts(channel="claude")[0]
        DB.update_account(acct["id"], meta={})
        ad = get_adapter("claude")

        def fake(acct_, model=None, prompt=None):
            return {"ok": True, "model": "m", "ms": 1, "text": "ok",
                    "reasoning": None, "usage": None,
                    "meta": {"usage_5h": {"pct": 0.77},
                             "usage_updated_at": int(time.time())}}

        original = ad.test_account
        ad.test_account = fake
        try:
            self.c.post("/admin/accounts/test", headers=H, json={"id": acct["id"]})
        finally:
            ad.test_account = original
        meta = DB.get_account(acct["id"])["meta"] or {}
        self.assertAlmostEqual(meta["usage_5h"]["pct"], 0.77)

    def test_stats_endpoint_declares_window_columns(self):
        j = self.c.get("/admin/stats", headers=H).json()
        for name in ("openai", "claude"):
            types = [c.get("type") for c in j["channels"][name]["columns"]]
            self.assertIn("window", types, f"{name} 没声明窗口列")

    # ---- 面板读得到 adapter 声明的列 ----

    def test_stats_endpoint_exposes_columns_for_every_channel(self):
        """面板按这个渲染每个渠道的列与能力。渠道没有 columns 就渲染不出表头。"""
        j = self.c.get("/admin/stats", headers=H).json()
        self.assertIn("openai", j["channels"])
        self.assertIn("claude", j["channels"])
        for name in ("openai", "claude"):
            self.assertTrue(j["channels"][name]["columns"], f"{name} 没声明列")
            self.assertIn("models", j["channels"][name])
            # 导入弹窗那句提示也由渠道声明 —— 面板里写死渠道名的话,
            # 加一个渠道就得回去改 dashboard.html
            self.assertTrue(j["channels"][name]["credentials_hint"],
                            f"{name} 没声明凭据提示")
        # 面板的「已摘出」计数读这里
        self.assertIn("parked", j["pool"].get("openai", {}))


if __name__ == "__main__":
    unittest.main()
