import contextlib
import io
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "o.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

import config  # noqa: E402
from core import credit as C  # noqa: E402
from core import hooks  # noqa: E402
from core import orders as O  # noqa: E402
from core import payments as P  # noqa: E402
from core.user_db import UserDB  # noqa: E402

import payments.mock  # noqa: E402,F401  (import 即注册 mock provider)

config.PAYMENT_PROVIDERS = ["mock"]


def _fresh(name):
    db = UserDB(os.path.join(_TMP, name))
    C.bind(db)
    gid = db.create_group(name="g-" + name, supported_models=["*"],
                          billing_policy="balance")
    inviter = db.create_user(f"inv-{name}@x.com", "h", "INV" + name[:3].upper(),
                             group_id=gid)
    uid = db.create_user(f"u-{name}@x.com", "h", "AFF" + name[:3].upper(),
                         group_id=gid, inviter_id=inviter)
    return db, uid, inviter


def _notify_params(order):
    return {"out_trade_no": order["out_trade_no"],
            "trade_no": "T" + str(order["id"]),
            "amount": order["amount"]}


class OrderFlowTest(unittest.TestCase):
    def test_create_order_and_pay_credits_balance(self):
        db, uid, _ = _fresh("of1.db")
        order, pay = O.create_order(db, db.get_user(uid), 20.0, "mock")
        self.assertEqual(order["status"], "pending")
        self.assertIn("pay_url", pay)

        ok, msg = O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_order(order["id"])["status"], "completed")
        self.assertAlmostEqual(db.get_user(uid)["balance"], 20.0)
        # 到账走的是内部兑换码 → 流水 reason=recharge
        entries = db.list_ledger(uid)
        self.assertEqual(entries[0]["reason"], "recharge")

    def test_replayed_notify_credits_once(self):
        db, uid, _ = _fresh("of2.db")
        order, _ = O.create_order(db, db.get_user(uid), 15.0, "mock")
        params = _notify_params(order)
        for _ in range(10):
            ok, _msg = O.handle_notify(db, "mock", {}, b"", params)
            self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(uid)["balance"], 15.0)
        self.assertEqual(len([e for e in db.list_ledger(uid)
                              if e["reason"] == "recharge"]), 1)

    def test_bad_signature_rejected(self):
        db, uid, _ = _fresh("of3.db")
        O.create_order(db, db.get_user(uid), 5.0, "mock")
        ok, msg = O.handle_notify(db, "mock", {}, b"", {})  # 无 out_trade_no
        self.assertFalse(ok)
        self.assertIn("signature", msg)
        self.assertEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_unknown_order_returns_ok_to_stop_retry(self):
        db, uid, _ = _fresh("of4.db")
        ok, msg = O.handle_notify(db, "mock", {}, b"",
                                  {"out_trade_no": "PG-NOPE"})
        self.assertTrue(ok)
        self.assertIn("unknown", msg)

    def test_invalid_amount_and_provider(self):
        db, uid, _ = _fresh("of5.db")
        user = db.get_user(uid)
        with self.assertRaises(O.OrderError):
            O.create_order(db, user, 0, "mock")
        with self.assertRaises(O.OrderError):
            O.create_order(db, user, -1, "mock")
        with self.assertRaises(O.OrderError):
            O.create_order(db, user, 10, "nonexistent")

    def test_expire_orders(self):
        db, uid, _ = _fresh("of6.db")
        order, _ = O.create_order(db, db.get_user(uid), 5.0, "mock")
        db.expire_orders(order["expires_at"] + 1)
        self.assertEqual(db.get_order(order["id"])["status"], "expired")
        # 超时后才付款仍应到账(pending/expired/failed → paid)
        ok, _ = O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        self.assertTrue(ok)
        self.assertEqual(db.get_order(order["id"])["status"], "completed")
        self.assertAlmostEqual(db.get_user(uid)["balance"], 5.0)


class _StubProvider:
    """假渠道:query_order 的返回由测试指定,一个网络请求都不发。"""
    name = "stub"
    display_name = "stub"

    def __init__(self, status=None, boom=False):
        self.status, self.boom = status, boom
        self.queried = []

    def create_payment(self, order):
        return {"pay_url": "https://stub.example/pay"}

    def verify_notify(self, headers, raw_body, params=None):
        data = dict(params or {})
        return {"out_trade_no": data.get("out_trade_no"),
                "trade_no": "S1", "amount": data.get("amount")}

    def query_order(self, out_trade_no):
        self.queried.append(out_trade_no)
        if self.boom:
            raise RuntimeError("upstream down")
        return {"status": self.status} if self.status else None


class NotifyGateTest(unittest.TestCase):
    """回调闸门。每条对应一个「不挡就会丢钱」的入口。"""

    def tearDown(self):
        config.PAYMENT_PROVIDERS = ["mock"]

    def test_notify_rejects_disabled_provider(self):
        """关掉一个渠道之后,它的入账口必须一起关 —— 建单侧原先查了,回调侧没查。"""
        db, uid, _ = _fresh("ng1.db")
        order, _ = O.create_order(db, db.get_user(uid), 5.0, "mock")
        config.PAYMENT_PROVIDERS = []
        ok, msg = O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        self.assertFalse(ok)
        self.assertIn("disabled", msg)
        self.assertEqual(db.get_order(order["id"])["status"], "pending")
        self.assertAlmostEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_notify_rejects_cross_provider(self):
        """用弱渠道建单、拿单号去打强渠道的 notify。"""
        db, uid, _ = _fresh("ng2.db")
        stub = _StubProvider()
        P.register_provider(stub)
        config.PAYMENT_PROVIDERS = ["mock", "stub"]
        order, _ = O.create_order(db, db.get_user(uid), 5.0, "mock")
        ok, msg = O.handle_notify(db, "stub", {}, b"", _notify_params(order))
        self.assertFalse(ok)
        self.assertIn("mismatch", msg)
        self.assertAlmostEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_notify_acks_payment_failure_without_crediting(self):
        """验签通过但这笔没成功:必须回 ok 让渠道停手,同时一分钱都不能到账。"""
        db, uid, _ = _fresh("ng3.db")

        class _Closed(_StubProvider):
            name = "closed"

            def verify_notify(self, headers, raw_body, params=None):
                return P.NOT_PAID

        P.register_provider(_Closed())
        config.PAYMENT_PROVIDERS = ["closed"]
        order, _ = O.create_order(db, db.get_user(uid), 5.0, "closed")
        ok, _ = O.handle_notify(db, "closed", {}, b"", _notify_params(order))
        self.assertTrue(ok)
        self.assertEqual(db.get_order(order["id"])["status"], "pending")
        self.assertAlmostEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_notify_rejects_underpay(self):
        """签名合法但少付:金额闸门必须挡住,不能按订单面额到账。"""
        db, uid, _ = _fresh("ng4.db")

        class _Cny(_StubProvider):
            name = "cny"

            def create_payment(self, order):
                return {"pay_url": "x", "amount_cny": 72.00}

            def verify_notify(self, headers, raw_body, params=None):
                data = dict(params or {})
                return {"out_trade_no": data.get("out_trade_no"),
                        "trade_no": "C1", "amount_cny": 0.01}

        P.register_provider(_Cny())
        config.PAYMENT_PROVIDERS = ["cny"]
        order, _ = O.create_order(db, db.get_user(uid), 10.0, "cny")
        self.assertAlmostEqual(db.get_order(order["id"])["pay_amount_cny"], 72.00)
        ok, msg = O.handle_notify(db, "cny", {}, b"", _notify_params(order))
        self.assertFalse(ok)
        self.assertIn("amount", msg)
        self.assertAlmostEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_notify_accepts_exact_and_overpay(self):
        db, uid, _ = _fresh("ng5.db")
        paid = {"v": 72.00}

        class _Cny2(_StubProvider):
            name = "cny2"

            def create_payment(self, order):
                return {"pay_url": "x", "amount_cny": 72.00}

            def verify_notify(self, headers, raw_body, params=None):
                data = dict(params or {})
                return {"out_trade_no": data.get("out_trade_no"),
                        "trade_no": "C2", "amount_cny": paid["v"]}

        P.register_provider(_Cny2())
        config.PAYMENT_PROVIDERS = ["cny2"]
        order, _ = O.create_order(db, db.get_user(uid), 10.0, "cny2")
        ok, _ = O.handle_notify(db, "cny2", {}, b"", _notify_params(order))
        self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(uid)["balance"], 10.0)

    def test_notify_rejection_is_logged(self):
        db, uid, _ = _fresh("ng6.db")
        order, _ = O.create_order(db, db.get_user(uid), 5.0, "mock")
        config.PAYMENT_PROVIDERS = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        out = buf.getvalue()
        self.assertIn("拒绝", out)
        self.assertIn("mock", out)


class ReconcileTest(unittest.TestCase):
    """查单兜底。回调丢了之后,钱能不能自己找回来。"""

    def tearDown(self):
        config.PAYMENT_PROVIDERS = ["mock"]

    def _with_stub(self, name, dbname, status=None, boom=False):
        stub = _StubProvider(status=status, boom=boom)
        stub.name = name
        P.register_provider(stub)
        config.PAYMENT_PROVIDERS = [name]
        db, uid, _ = _fresh(dbname)
        order, _ = O.create_order(db, db.get_user(uid), 5.0, name)
        return db, uid, order, stub

    def test_poll_credits_when_upstream_says_paid(self):
        db, uid, order, _ = self._with_stub("rc1", "rc1.db", status="paid")
        self.assertEqual(O.poll_pending(db), 1)
        self.assertEqual(db.get_order(order["id"])["status"], "completed")
        self.assertAlmostEqual(db.get_user(uid)["balance"], 5.0)
        reasons = [e["reason"] for e in db.list_ledger(uid, limit=10)]
        self.assertEqual(reasons.count("recharge"), 1)

    def test_poll_leaves_unpaid_alone(self):
        db, uid, order, _ = self._with_stub("rc2", "rc2.db", status="pending")
        self.assertEqual(O.poll_pending(db), 0)
        self.assertEqual(db.get_order(order["id"])["status"], "pending")
        self.assertAlmostEqual(db.get_user(uid)["balance"] or 0, 0)

    def test_poll_survives_upstream_exception(self):
        db, uid, order, _ = self._with_stub("rc3", "rc3.db", boom=True)
        self.assertEqual(O.poll_pending(db), 0)
        self.assertEqual(db.get_order(order["id"])["status"], "pending")

    def test_poll_also_covers_expired_orders(self):
        """ORDER_TTL 1800 而对账 600 一跳:扫码后去吃饭再回来付款的单已经 expired,
        只扫 pending 的话这种单永久失联,而钱是真付了的。"""
        db, uid, order, _ = self._with_stub("rc4", "rc4.db", status="paid")
        db.expire_orders(order["expires_at"] + 1)
        self.assertEqual(db.get_order(order["id"])["status"], "expired")
        self.assertEqual(O.poll_pending(db), 1)
        self.assertEqual(db.get_order(order["id"])["status"], "completed")
        self.assertAlmostEqual(db.get_user(uid)["balance"], 5.0)

    def test_reconcile_queries_upstream_before_expiring(self):
        """顺序是关键:先过期再查单的话,刚翻成 expired 的单要等下一跳。"""
        db, uid, order, stub = self._with_stub("rc5", "rc5.db", status="paid")
        credited, _ = O.reconcile_orders(db, now=order["expires_at"] + 1)
        self.assertEqual(credited, 1)
        self.assertIn(order["out_trade_no"], stub.queried)
        self.assertEqual(db.get_order(order["id"])["status"], "completed")

    def test_reconcile_still_expires_unpaid(self):
        db, uid, order, _ = self._with_stub("rc6", "rc6.db", status="pending")
        credited, expired = O.reconcile_orders(db, now=order["expires_at"] + 1)
        self.assertEqual((credited, expired), (0, 1))
        self.assertEqual(db.get_order(order["id"])["status"], "expired")

    def test_reconcile_has_a_caller_in_housekeeping(self):
        """poll_pending 从写好那天起零调用点,而死面扫描钉在 min-confidence 80,
        抓不到未被调用的函数 —— 这条断言是那个盲区里唯一的守卫。"""
        import inspect

        import server
        src = inspect.getsource(server._housekeeping_loop)
        self.assertIn("reconcile_orders", src)


class TopupGuardTest(unittest.TestCase):
    def test_default_providers_excludes_mock(self):
        """mock 把「打开链接」当付款成功,默认启用等于开一个免费充值口。"""
        import importlib

        import config as cfg
        saved = dict(os.environ)
        os.environ.pop("BITAPI_PAYMENT_PROVIDERS", None)
        try:
            importlib.reload(cfg)
            self.assertNotIn("mock", cfg.PAYMENT_PROVIDERS)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            importlib.reload(cfg)
            cfg.PAYMENT_PROVIDERS = ["mock"]

    def test_order_rejects_below_min_topup(self):
        """前端那个 min 是 UI 软约束,直接打接口能绕过,服务端必须硬限。"""
        db, uid, _ = _fresh("mt1.db")
        saved = config.MIN_TOPUP
        config.MIN_TOPUP = 1.0
        try:
            with self.assertRaises(O.OrderError) as cm:
                O.create_order(db, db.get_user(uid), 0.01, "mock")
            self.assertEqual(cm.exception.code, "BELOW_MIN_TOPUP")
        finally:
            config.MIN_TOPUP = saved

    def test_create_order_marks_failed_on_provider_error(self):
        db, uid, _ = _fresh("mt2.db")

        class _Broken(_StubProvider):
            name = "broken"

            def create_payment(self, order):
                raise RuntimeError("gateway unreachable")

        P.register_provider(_Broken())
        config.PAYMENT_PROVIDERS = ["broken"]
        try:
            with self.assertRaises(O.OrderError):
                O.create_order(db, db.get_user(uid), 5.0, "broken")
            rows = db.list_orders(status="failed", limit=10)
            self.assertEqual(len(rows), 1)
        finally:
            config.PAYMENT_PROVIDERS = ["mock"]


if __name__ == "__main__":
    unittest.main()


class PluginTest(unittest.TestCase):
    def setUp(self):
        hooks.clear()

    def tearDown(self):
        hooks.clear()

    def test_percent_plugin_rebates_once_on_replayed_notify(self):
        db, uid, inviter = _fresh("pl1.db")
        import plugins.affiliate_percent as ap
        hooks.clear()
        hooks.on("order.paid")(ap.on_order_paid)
        hooks.on("code.redeemed")(ap.on_code_redeemed)
        ap.CONFIG["rate"] = 0.2
        ap.CONFIG["first_only"] = False

        order, _ = O.create_order(db, db.get_user(uid), 50.0, "mock")
        params = _notify_params(order)
        for _ in range(5):
            O.handle_notify(db, "mock", {}, b"", params)
        # 邀请人拿到 50*0.2=10,且只发一次
        self.assertAlmostEqual(db.get_user(inviter)["balance"], 10.0)
        affs = [e for e in db.list_ledger(inviter) if e["reason"] == "affiliate"]
        self.assertEqual(len(affs), 1)

    def test_fixed_plugin_on_registration(self):
        db, uid, inviter = _fresh("pl2.db")
        import plugins.affiliate_fixed as af
        hooks.clear()
        hooks.on("user.registered")(af.on_registered)
        af.CONFIG["inviter_bonus"] = 2.0
        af.CONFIG["invitee_bonus"] = 0.5
        af.CONFIG["require_verified_email"] = False
        af.CONFIG["max_invitees"] = 0

        hooks.emit("user.registered", user=db.get_user(uid),
                   inviter=db.get_user(inviter))
        hooks.emit("user.registered", user=db.get_user(uid),
                   inviter=db.get_user(inviter))   # 重复不再发
        self.assertAlmostEqual(db.get_user(inviter)["balance"], 2.0)
        self.assertAlmostEqual(db.get_user(uid)["balance"], 0.5)

    def test_revshare_plugin_on_usage(self):
        db, uid, inviter = _fresh("pl3.db")
        import plugins.affiliate_revshare as ar
        hooks.clear()
        hooks.on("usage.recorded")(ar.on_usage)
        ar.CONFIG["rate"] = 0.1

        hooks.emit("usage.recorded", log_id=1, user=db.get_user(uid),
                   actual_cost=2.0)
        hooks.emit("usage.recorded", log_id=1, user=db.get_user(uid),
                   actual_cost=2.0)   # 同 log_id 幂等
        hooks.emit("usage.recorded", log_id=2, user=db.get_user(uid),
                   actual_cost=3.0)
        self.assertAlmostEqual(db.get_user(inviter)["balance"], 0.5)  # 0.2 + 0.3

    def test_plugin_exception_does_not_break_main_flow(self):
        db, uid, _ = _fresh("pl4.db")

        @hooks.on("order.paid")
        def boom(**_):
            raise RuntimeError("plugin exploded")

        order, _ = O.create_order(db, db.get_user(uid), 8.0, "mock")
        ok, _msg = O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        self.assertTrue(ok)
        # 插件炸了,但订单完成、余额照样到账
        self.assertEqual(db.get_order(order["id"])["status"], "completed")
        self.assertAlmostEqual(db.get_user(uid)["balance"], 8.0)

    def test_no_plugins_system_still_works(self):
        db, uid, inviter = _fresh("pl5.db")
        hooks.clear()
        order, _ = O.create_order(db, db.get_user(uid), 12.0, "mock")
        O.handle_notify(db, "mock", {}, b"", _notify_params(order))
        self.assertAlmostEqual(db.get_user(uid)["balance"], 12.0)
        self.assertEqual(db.get_user(inviter)["balance"] or 0, 0)  # 无插件=无返佣


if __name__ == "__main__":
    unittest.main()
