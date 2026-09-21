import os
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "c.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

from core import credit as C  # noqa: E402
from core import hooks  # noqa: E402
from core.redeem import RedeemError, generate_codes, redeem  # noqa: E402
from core.user_db import UserDB  # noqa: E402


def _fresh(name):
    db = UserDB(os.path.join(_TMP, name))
    C.bind(db)
    gid = db.create_group(name="g-" + name, supported_models=["*"],
                          billing_policy="balance")
    uid = db.create_user(f"u-{name}@x.com", "h", "AFF" + name[:4].upper(),
                         group_id=gid)
    return db, gid, uid


class CreditIdempotencyTest(unittest.TestCase):
    def test_same_idem_key_applies_once(self):
        db, _, uid = _fresh("idem1.db")
        for _ in range(100):
            C.credit(uid, 10.0, "recharge", "order:1")
        self.assertEqual(db.get_user(uid)["balance"], 10.0)
        self.assertEqual(len(db.list_ledger(uid)), 1)

    def test_created_flag(self):
        db, _, uid = _fresh("idem2.db")
        _, first = C.credit(uid, 5.0, "redeem", "k1")
        _, second = C.credit(uid, 5.0, "redeem", "k1")
        self.assertTrue(first)
        self.assertFalse(second)

    def test_concurrent_same_key_applies_once(self):
        db, _, uid = _fresh("idem3.db")
        errors = []
        created = []

        def worker():
            try:
                _, won = C.credit(uid, 3.0, "recharge", "concurrent:1")
                created.append(won)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(created), 1)
        self.assertEqual(db.get_user(uid)["balance"], 3.0)
        self.assertEqual(len(db.list_ledger(uid)), 1)

    def test_negative_amount_tracks_total_spent(self):
        db, _, uid = _fresh("idem4.db")
        C.credit(uid, 20.0, "recharge", "r1")
        C.credit(uid, -7.5, "usage", "u1")
        u = db.get_user(uid)
        self.assertAlmostEqual(u["balance"], 12.5)
        self.assertAlmostEqual(u["total_spent"], 7.5)

    def test_requires_idem_key(self):
        db, _, uid = _fresh("idem5.db")
        with self.assertRaises(ValueError):
            C.credit(uid, 1.0, "recharge", "")

    def test_emits_balance_changed(self):
        db, _, uid = _fresh("idem6.db")
        seen = []
        hooks.clear("balance.changed")
        hooks.on("balance.changed")(lambda **kw: seen.append(kw))
        C.credit(uid, 4.0, "recharge", "e1")
        C.credit(uid, 4.0, "recharge", "e1")   # 重复,不应再发
        hooks.clear("balance.changed")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["delta"], 4.0)
        self.assertEqual(seen[0]["balance_after"], 4.0)

    def test_ledger_sum_by_reason(self):
        db, _, uid = _fresh("idem7.db")
        C.credit(uid, 1.0, "affiliate", "a1")
        C.credit(uid, 2.0, "affiliate", "a2")
        C.credit(uid, 9.0, "recharge", "r1")
        self.assertAlmostEqual(db.ledger_sum(uid, reason="affiliate"), 3.0)


class RedeemTest(unittest.TestCase):
    def test_generate_and_redeem(self):
        db, _, uid = _fresh("rd1.db")
        codes = generate_codes(db, 3, 5.0)
        self.assertEqual(len(codes), 3)
        self.assertEqual(len(set(codes)), 3)
        user = db.get_user(uid)
        ledger, code_row = redeem(db, codes[0], user)
        self.assertEqual(db.get_user(uid)["balance"], 5.0)
        self.assertEqual(code_row["status"], "used")
        self.assertEqual(code_row["used_by"], uid)

    def test_double_redeem_rejected(self):
        db, _, uid = _fresh("rd2.db")
        code = generate_codes(db, 1, 5.0)[0]
        user = db.get_user(uid)
        redeem(db, code, user)
        with self.assertRaises(RedeemError) as ctx:
            redeem(db, code, user)
        self.assertEqual(ctx.exception.code, "CODE_USED")
        self.assertEqual(db.get_user(uid)["balance"], 5.0)

    def test_concurrent_redeem_only_one_wins(self):
        db, gid, _ = _fresh("rd3.db")
        code = generate_codes(db, 1, 100.0)[0]
        users = [db.create_user(f"c{i}@x.com", "h", f"AFFC{i}", group_id=gid)
                 for i in range(8)]
        wins, fails = [], []

        def worker(uid):
            try:
                redeem(db, code, db.get_user(uid))
                wins.append(uid)
            except Exception:
                fails.append(uid)

        threads = [threading.Thread(target=worker, args=(u,)) for u in users]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1, f"wins={wins} fails={len(fails)}")
        # 只有中标者拿到钱
        balances = [db.get_user(u)["balance"] or 0 for u in users]
        self.assertEqual(sorted(balances)[-1], 100.0)
        self.assertEqual(sum(1 for b in balances if b > 0), 1)

    def test_unknown_and_disabled_and_expired(self):
        db, _, uid = _fresh("rd4.db")
        user = db.get_user(uid)
        with self.assertRaises(RedeemError) as c1:
            redeem(db, "NOPE-NOPE-NOPE-NOPE", user)
        self.assertEqual(c1.exception.code, "CODE_NOT_FOUND")

        dis = generate_codes(db, 1, 5.0)[0]
        db.disable_code(dis)
        with self.assertRaises(RedeemError) as c2:
            redeem(db, dis, user)
        self.assertEqual(c2.exception.code, "CODE_DISABLED")

        exp = generate_codes(db, 1, 5.0, expires_at=1)[0]
        with self.assertRaises(RedeemError) as c3:
            redeem(db, exp, user)
        self.assertEqual(c3.exception.code, "CODE_EXPIRED")

    def test_zero_value_rejected(self):
        db, _, _ = _fresh("rd5.db")
        with self.assertRaises(RedeemError):
            generate_codes(db, 1, 0)

    def test_negative_value_code_deducts(self):
        db, _, uid = _fresh("rd6.db")
        C.credit(uid, 10.0, "recharge", "seed")
        code = generate_codes(db, 1, -4.0)[0]
        redeem(db, code, db.get_user(uid))
        self.assertAlmostEqual(db.get_user(uid)["balance"], 6.0)

    def test_invitation_code_allows_zero_value(self):
        """邀请码不入账,零面额是它的常态 —— 那道限制只管余额码。"""
        db, _, _ = _fresh("rd7.db")
        code = generate_codes(db, 1, 0, type="invitation")[0]
        self.assertEqual(db.get_code(code)["type"], "invitation")

    def test_invitation_code_not_redeemable(self):
        db, _, uid = _fresh("rd8.db")
        code = generate_codes(db, 1, 0, type="invitation")[0]
        with self.assertRaises(RedeemError) as ctx:
            redeem(db, code, db.get_user(uid))
        self.assertEqual(ctx.exception.code, "CODE_NOT_REDEEMABLE")
        # 拒得干净:码没被这次尝试占掉,还能拿去注册
        self.assertEqual(db.get_code(code)["status"], "unused")


if __name__ == "__main__":
    unittest.main()
