#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维告警的门禁:core 在该发事实的地方发了事实;卡住的已付订单会被对账救回;
webhook 插件按 (事件,键) 去重、按渠道形状组包、不在请求线程里阻塞。

这里守的是「站长不必从用户嘴里知道出事」这一条。
"""
import os
import smtplib
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "alerts.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import hooks  # noqa: E402
from core import mailer  # noqa: E402
from core import orders as orders_mod  # noqa: E402
from core import throttle  # noqa: E402
from core.adapter import CAP_CHAT, Adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.credit import credit  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from plugins import alert_webhook as plugin  # noqa: E402


class _Capture:
    """把某事件的 payload 收进列表;用完 detach。"""

    def __init__(self, *events):
        self.got = []
        self.events = events

    def __enter__(self):
        for ev in self.events:
            hooks.on(ev)(self._make(ev))
        return self

    def _make(self, ev):
        def handler(**payload):
            self.got.append((ev, payload))
        handler.__name__ = f"capture_{ev}"
        return handler

    def __exit__(self, *exc):
        for ev in self.events:
            handlers = hooks.handlers(ev)
            hooks.clear(ev)
            for fn in handlers:
                if not fn.__name__.startswith("capture_"):
                    hooks.on(ev)(fn)


class FailingAdapter(Adapter):
    name = "failadapter"
    capabilities = [CAP_CHAT]
    models = ["fail-1"]

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        raise RuntimeError("upstream exploded")


class CoreEventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "alerts-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@example.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.uid = r.json()["user_id"]

    def setUp(self):
        throttle.reset_all()

    def test_pool_empty_emitted_when_channel_has_no_accounts(self):
        with _Capture("pool.empty") as cap:
            self.assertIsNone(server.POOL.get_valid_account("ghost-channel", FailingAdapter()))
        self.assertEqual(cap.got, [("pool.empty", {"channel": "ghost-channel",
                                                    "reason": "no_active"})])

    def test_upstream_failed_emitted_after_all_retries(self):
        ad = FailingAdapter()
        server.DB.upsert_account("failadapter", "acct-1", secret={"k": "v"}, status="active")
        server.DB.upsert_account("failadapter", "acct-2", secret={"k": "v"}, status="active")
        # max_switch 与账号数相同:两把号各失败一次就把重试用完 → 502 + upstream.failed。
        # 默认的 6 次会先把两把号都冷却掉,第三次取号落空走的是 503 + pool.empty。
        with _Capture("upstream.failed", "pool.empty") as cap:
            with self.assertRaises(HTTPException) as ctx:
                server._sync(ad, "failadapter", [{"role": "user", "content": "hi"}],
                             "fail-1", max_switch=2)
        self.assertEqual(ctx.exception.status_code, 502,
                         f"{ctx.exception.detail} / events={cap.got} / "
                         f"accounts={server.DB.list_accounts(channel='failadapter')}")
        failed = [p for ev, p in cap.got if ev == "upstream.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["channel"], "failadapter")
        self.assertEqual(failed[0]["model"], "fail-1")
        self.assertEqual(failed[0]["kind"], "error")
        self.assertIn("exploded", failed[0]["error"])

    def test_auth_throttled_emitted_on_429(self):
        saved = throttle.LOGIN_EMAIL.limit
        throttle.LOGIN_EMAIL.limit = 1
        try:
            with _Capture("auth.throttled") as cap:
                self.client.post("/api/login", json={"email": "root@example.com", "password": "x"})
                r = self.client.post("/api/login", json={"email": "root@example.com", "password": "x"})
                self.assertEqual(r.status_code, 429)
            self.assertEqual(len(cap.got), 1)
            self.assertEqual(cap.got[0][1]["scope"], "login")
            self.assertEqual(cap.got[0][1]["key"], "root@example.com")
        finally:
            throttle.LOGIN_EMAIL.limit = saved

    def test_mail_failed_emitted_on_smtp_error(self):
        db = portal_state.USER_DB
        db.set_setting("smtp_host", "smtp.example.com")
        db.set_setting("smtp_from", "no-reply@example.com")
        db.set_setting("smtp_user", "u")
        db.set_setting("smtp_pass", "p")

        class Boom:
            def __init__(self, *a, **k):
                pass

            def login(self, u, p):
                raise smtplib.SMTPAuthenticationError(535, b"bad creds")

        try:
            with mock.patch.object(mailer.smtplib, "SMTP_SSL", Boom):
                with _Capture("mail.failed") as cap:
                    with self.assertRaises(mailer.MailError):
                        mailer.send("x@example.com", "s", "t")
            self.assertEqual(len(cap.got), 1)
            self.assertEqual(cap.got[0][1]["to"], "x@example.com")
            self.assertIn("认证失败", cap.got[0][1]["error"])
        finally:
            for k in ("smtp_host", "smtp_from", "smtp_user", "smtp_pass"):
                db.delete_setting(k)


class StuckOrderTest(unittest.TestCase):
    """已付订单卡在 paid / recharging 时对账要把它救回来;救不回来要发 order.stuck。"""

    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, f"stuck-{id(self)}.db"))
        portal_state.rebind(self.db)
        self.db.create_group("free", supported_models=["*"], billing_policy="balance", is_default=1)
        self.uid = self.db.create_user("pay@example.com", "x", "AFFCODE1", group_id=1)

    def tearDown(self):
        portal_state.rebind(portal_state.USER_DB)

    def _paid_order(self, otn, amount=10.0, paid_ago=0):
        self.db.create_order(self.uid, otn, amount, "epay")
        self.db.mark_order_paid(otn, "TN", 72.0)
        if paid_ago:
            with self.db._conn() as c:
                c.execute("UPDATE orders SET paid_at=? WHERE out_trade_no=?",
                          (int(time.time()) - paid_ago, otn))

    def test_paid_but_unfulfilled_gets_credited_by_reconcile(self):
        """mark_paid 之后进程死了 → 状态 paid、余额没到。原先没有任何路径再碰它。"""
        self._paid_order("STUCK-1", paid_ago=900)
        self.assertEqual(self.db.get_user(self.uid)["balance"], 0)
        with _Capture("order.stuck", "order.paid") as cap:
            credited, _ = orders_mod.reconcile_orders(self.db)
        self.assertEqual(credited, 1)
        self.assertEqual(self.db.get_order_by_trade_no("STUCK-1")["status"], "completed")
        self.assertAlmostEqual(self.db.get_user(self.uid)["balance"], 10.0)
        self.assertEqual([ev for ev, _ in cap.got], ["order.paid"])   # 救回来就不算卡

    def test_stale_recharging_lease_is_released_and_fulfilled(self):
        """fulfil 跑到一半死了:状态 recharging,租约悬着。超过 STUCK_AFTER 要能翻回 paid 重做。"""
        self._paid_order("STUCK-2", paid_ago=900)
        self.assertIsNotNone(self.db.acquire_order_lease("STUCK-2"))
        self.assertEqual(self.db.get_order_by_trade_no("STUCK-2")["status"], "recharging")
        credited, _ = orders_mod.reconcile_orders(self.db)
        self.assertEqual(credited, 1)
        self.assertEqual(self.db.get_order_by_trade_no("STUCK-2")["status"], "completed")
        self.assertAlmostEqual(self.db.get_user(self.uid)["balance"], 10.0)

    def test_fresh_recharging_is_left_alone(self):
        """刚付的单正在被回调线程处理,对账不能抢它的租约。"""
        self._paid_order("FRESH-1")
        lease = self.db.acquire_order_lease("FRESH-1")
        with _Capture("order.stuck") as cap:
            credited, _ = orders_mod.reconcile_orders(self.db)
        self.assertEqual(credited, 0)
        self.assertEqual(self.db.get_order_by_trade_no("FRESH-1")["status"], "recharging")
        self.assertEqual(self.db.get_order_by_trade_no("FRESH-1")["lease_version"], lease)
        self.assertEqual(cap.got, [])

    def test_still_stuck_emits_order_stuck(self):
        self._paid_order("STUCK-3", paid_ago=900)
        with mock.patch.object(orders_mod, "fulfil", side_effect=RuntimeError("db locked")):
            with _Capture("order.stuck") as cap:
                credited, _ = orders_mod.reconcile_orders(self.db)
        self.assertEqual(credited, 0)
        self.assertEqual(len(cap.got), 1)
        payload = cap.got[0][1]
        self.assertEqual(payload["order"]["out_trade_no"], "STUCK-3")
        self.assertGreaterEqual(payload["age"], 900)
        self.assertEqual(self.db.get_order_by_trade_no("STUCK-3")["status"], "paid")

    def test_idempotent_credit_if_reconcile_races_notify(self):
        """对账救单与回调 fulfil 同一笔:余额只加一次。"""
        self._paid_order("RACE-1", paid_ago=900)
        orders_mod.fulfil(self.db, "RACE-1")
        credited, _ = orders_mod.reconcile_orders(self.db)
        self.assertEqual(credited, 0)
        self.assertAlmostEqual(self.db.get_user(self.uid)["balance"], 10.0)
        # 第二笔无关的 credit 不受影响
        credit(self.uid, 1.0, "admin", "admin:race")
        self.assertAlmostEqual(self.db.get_user(self.uid)["balance"], 11.0)


class _SyncThread:
    """让插件的后台投递在当前线程同步跑,测试才能断言。"""

    def __init__(self, target=None, daemon=None, name=None):
        self._t = target

    def start(self):
        self._t()


class WebhookPluginTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        plugin._last.clear()
        self._saved = dict(plugin.CONFIG)
        plugin.CONFIG["url"] = "https://hooks.example.com/abc"
        plugin.CONFIG["cooldown"] = 600
        self._p1 = mock.patch.object(plugin, "_post", lambda url, body: self.posts.append((url, body)))
        self._p2 = mock.patch.object(plugin.threading, "Thread", _SyncThread)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        plugin.CONFIG.clear()
        plugin.CONFIG.update(self._saved)
        plugin._last.clear()

    def test_no_url_means_silent(self):
        plugin.CONFIG["url"] = ""
        self.assertFalse(plugin.notify("pool.empty", "grok", "t", "x"))
        self.assertEqual(self.posts, [])

    def test_dedup_per_event_and_key_within_cooldown(self):
        self.assertTrue(plugin.notify("pool.empty", "grok", "t", "x"))
        self.assertFalse(plugin.notify("pool.empty", "grok", "t", "x"))     # 冷却中
        self.assertTrue(plugin.notify("pool.empty", "xai", "t", "x"))       # 别的渠道
        self.assertTrue(plugin.notify("upstream.failed", "grok", "t", "x"))  # 别的事件
        self.assertEqual(len(self.posts), 3)
        # 冷却过了再发
        plugin._last[("pool.empty", "grok")] -= 601
        self.assertTrue(plugin.notify("pool.empty", "grok", "t", "x"))

    def test_payload_shapes(self):
        plugin.CONFIG["url"] = "https://api.telegram.org/bot123:abc/sendMessage?chat_id=42"
        plugin.notify("e", "k1", "标题", "正文")
        plugin.CONFIG["url"] = "https://api.day.app/KEY"
        plugin.notify("e", "k2", "标题", "正文")
        plugin.CONFIG["url"] = "https://hooks.slack.com/services/x"
        plugin.notify("e", "k3", "标题", "正文", {"a": 1})
        tg, bark, generic = (b for _, b in self.posts)
        self.assertEqual(tg["chat_id"], "42")
        self.assertIn("标题", tg["text"])
        self.assertIn("正文", tg["text"])
        self.assertEqual(bark["body"], "正文")
        self.assertTrue(bark["title"].startswith("[bit-api] "))
        self.assertEqual(generic["event"], "e")
        self.assertEqual(generic["payload"], {"a": 1})
        self.assertEqual(generic["text"], generic["content"])

    def test_handlers_route_events(self):
        plugin.on_pool_empty(channel="grok", reason="no_active")
        plugin.on_upstream_failed(channel="xai", model="xai-grok-4.6", error="502 bad", kind="error")
        plugin.on_order_stuck(order={"out_trade_no": "OT1", "user_id": 3, "amount": 10.0,
                                     "status": "paid"}, age=1200)
        plugin.on_backup_failed(error="disk full")
        plugin.on_mail_failed(to="a@b.c", error="SMTP 认证失败")
        plugin.on_auth_throttled(scope="login", key="1.2.3.4", retry_after=60)
        self.assertEqual(len(self.posts), 6)
        titles = [b["title"] for _, b in self.posts]
        self.assertTrue(any("grok" in t for t in titles))
        self.assertTrue(any("OT1" in t for t in titles))
        # 大额消费:阈值 0 不报;设了阈值只报超过的
        plugin.on_usage_recorded(user={"id": 1, "email": "a@b.c"}, model="m", channel="c",
                                 actual_cost=9.0, request_id="req_1")
        self.assertEqual(len(self.posts), 6)
        plugin.CONFIG["expensive_request"] = 5.0
        plugin.on_usage_recorded(user={"id": 1, "email": "a@b.c"}, model="m", channel="c",
                                 actual_cost=4.0, request_id="req_2")
        self.assertEqual(len(self.posts), 6)
        plugin.on_usage_recorded(user={"id": 1, "email": "a@b.c"}, model="m", channel="c",
                                 actual_cost=9.0, request_id="req_3")
        self.assertEqual(len(self.posts), 7)
        self.assertIn("9.0000", self.posts[-1][1]["text"])

    def test_delivery_failure_never_raises(self):
        self._p1.stop()
        with mock.patch.object(plugin, "_post", side_effect=OSError("connection refused")):
            self.assertTrue(plugin.notify("pool.empty", "grok", "t", "x"))   # 没抛出来
        self._p1.start()


if __name__ == "__main__":
    unittest.main()
