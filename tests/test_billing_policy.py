import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "b.db"))

from core import credit as C  # noqa: E402
from core import hooks  # noqa: E402
from core.adapter import BILL_UPSTREAM, Adapter  # noqa: E402
from core.billing import (Billing, InsufficientBalanceError,  # noqa: E402
                          RateLimitError)
from core.pricing import Pricing  # noqa: E402
from core.user_db import UserDB  # noqa: E402


class _Upstream(Adapter):
    """上游返回真实 usage 的 adapter(如 NVIDIA/inceptionlabs)。"""
    billing_mode = BILL_UPSTREAM


def _setup(name, policy="balance", **group_kw):
    db = UserDB(os.path.join(_TMP, name))
    C.bind(db)
    gid = db.create_group(name="g-" + name, supported_models=["*"],
                          billing_policy=policy, **group_kw)
    uid = db.create_user(f"u-{name}@x.com", "h", "AFF" + name[:4].upper(),
                         group_id=gid)
    pricing = Pricing(db, catalog=None, cache_ttl=0)
    db.upsert_pricing("m1", billing_mode="free")
    billing = Billing(db, pricing=pricing)
    user = dict(db.get_user(uid), _api_key_id=None)
    return db, billing, user, db.get_group(gid)


class BalancePolicyTest(unittest.TestCase):
    def setUp(self):
        hooks.clear()

    def tearDown(self):
        hooks.clear()

    def test_zero_balance_precheck_raises_402(self):
        db, billing, user, group = _setup("bp1.db")
        with self.assertRaises(InsufficientBalanceError):
            billing.precheck(user, group)

    def test_positive_balance_passes_precheck(self):
        db, billing, user, group = _setup("bp2.db")
        C.credit(user["id"], 1.0, "recharge", "r1")
        billing.precheck(user, group)   # 不抛异常即通过

    def test_usage_deducts_balance_by_pricing(self):
        db, billing, user, group = _setup("bp3.db")
        C.credit(user["id"], 100.0, "recharge", "r1")
        # $5/1M input, $30/1M output
        db.upsert_pricing("m1", None, billing_mode="token",
                          input_price=5, output_price=30)
        billing.pricing.invalidate()
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 1_000_000,
                            "completion_tokens": 100_000})
        expected = 5 + 30 * 0.1   # 5 + 3 = 8
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 100 - expected)
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertAlmostEqual(log["cost"], expected)
        self.assertAlmostEqual(log["actual_cost"], expected)
        self.assertEqual(log["billing_mode"], "token")
        self.assertEqual(log["token_source"], "upstream")

    def test_rate_multiplier_discounts_actual_cost(self):
        db, billing, user, group = _setup("bp4.db", rate_multiplier=0.5)
        C.credit(user["id"], 10.0, "recharge", "r1")
        db.upsert_pricing("m1", None, billing_mode="token", input_price=10)
        billing.pricing.invalidate()
        billing.record_usage(user, group, "ch", "m1", adapter=_Upstream(),
                             upstream_usage={"prompt_tokens": 1_000_000})
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertAlmostEqual(log["cost"], 10.0)
        self.assertAlmostEqual(log["actual_cost"], 5.0)
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 5.0)

    def test_long_context_tier_in_snapshot_and_charge(self):
        db, billing, user, group = _setup("bp5.db")
        C.credit(user["id"], 1000.0, "recharge", "r1")
        db.upsert_pricing("sol", None, billing_mode="token",
                          input_price=5, output_price=30,
                          long_threshold=272000,
                          long_input_price=10, long_output_price=45)
        billing.pricing.invalidate()
        # 超阈值:输入按 10、输出按 45
        billing.record_usage(user, group, "ch", "sol", adapter=_Upstream(),
                             upstream_usage={"prompt_tokens": 300000,
                                             "completion_tokens": 1000})
        log = db.recent_usage(user["id"], limit=1)[0]
        snap = log["pricing_snapshot"]
        self.assertEqual(snap["tier"], "long")
        self.assertEqual(snap["input_price"], 10)
        self.assertEqual(snap["output_price"], 45)
        self.assertEqual(snap["total_ctx"], 300000)
        expected = (300000 * 10 + 1000 * 45) / 1e6
        self.assertAlmostEqual(log["actual_cost"], expected)

    def test_cache_tokens_counted_from_upstream_usage(self):
        db, billing, user, group = _setup("bp6.db")
        C.credit(user["id"], 100.0, "recharge", "r1")
        db.upsert_pricing("m1", None, billing_mode="token",
                          input_price=1, output_price=1,
                          cache_read_price=0.1, cache_write_price=2.0)
        billing.pricing.invalidate()
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            # prompt_tokens 按 OpenAI 规范是**总输入**(含缓存);净输入 = 1_201_000 - 1_200_000 = 1000
            upstream_usage={"prompt_tokens": 1_201_000, "completion_tokens": 1000,
                            "cache_read_input_tokens": 1_000_000,
                            "cache_creation_input_tokens": 200_000})
        log = db.recent_usage(user["id"], limit=1)[0]
        snap = log["pricing_snapshot"]
        self.assertEqual(snap["cache_read_tokens"], 1_000_000)
        self.assertEqual(snap["cache_write_tokens"], 200_000)
        # total_ctx = 净输入 + 缓存写 + 缓存读,仍是真实总输入
        self.assertEqual(snap["total_ctx"], 1000 + 1_200_000)
        self.assertEqual(log["input_tokens"], 1000)          # 缓存那部分不能再按输入价收
        expected = (1000 * 1 + 1000 * 1 + 200_000 * 2.0
                    + 1_000_000 * 0.1) / 1e6
        self.assertAlmostEqual(log["actual_cost"], expected)

    def test_cache_snapshot_for_free_per_request_and_unknown_usage(self):
        db, billing, user, group = _setup("cache-split.db", policy="free")
        for mode in ("free", "per_request", "token"):
            db.upsert_pricing(mode, None, billing_mode=mode)
            for usage, expected in (
                ({"prompt_tokens": 100, "completion_tokens": 10,
                  "prompt_tokens_details": {"cached_tokens": 72}}, (72, None)),
                ({"prompt_tokens": 100, "completion_tokens": 10}, (None, None)),
                ({"prompt_tokens": 100, "completion_tokens": 10,
                  "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}, (0, 0)),
                (None, (None, None)),
            ):
                with self.subTest(mode=mode, usage=usage):
                    billing.record_usage(user, group, "ch", mode,
                                         upstream_usage=usage, adapter=_Upstream())
                    snap = db.recent_usage(user["id"], limit=1)[0]["pricing_snapshot"]
                    self.assertEqual((snap["cache_read_tokens"],
                                      snap["cache_write_tokens"]), expected)
                    self.assertEqual(snap["cache_usage_version"], 3)
                    self.assertNotIn("cache_write_estimated", snap)

    def test_openai_cache_reads_and_writes_are_billed_once_across_usage_shapes(self):
        db, billing, user, group = _setup("openai-cache-write.db")
        C.credit(user["id"], 10.0, "recharge", "cache-write-credit")
        db.upsert_pricing("m-cache", None, billing_mode="token", input_price=10, output_price=50,
                          cache_read_price=1, cache_write_price=12.5)
        billing.pricing.invalidate()
        for usage in (
            {"prompt_tokens": 32179, "completion_tokens": 19, "prompt_tokens_details": {"cached_tokens": 32000, "cache_write_tokens": 100}},
            {"input_tokens": 32179, "output_tokens": 19, "input_tokens_details": {"cached_tokens": 32000, "cache_write_tokens": 100}},
            {"input_tokens": 79, "output_tokens": 19, "cache_read_input_tokens": 32000, "cache_creation_input_tokens": 100},
        ):
            with self.subTest(usage=usage):
                before = db.get_user(user["id"])["balance"]
                billing.record_usage(user, group, "ch", "m-cache", adapter=_Upstream(), upstream_usage=usage)
                log = db.recent_usage(user["id"], limit=1)[0]
                snap = log["pricing_snapshot"]
                self.assertEqual((log["input_tokens"], log["output_tokens"]), (79, 19))
                self.assertEqual((snap["cache_read_tokens"], snap["cache_write_tokens"], snap["total_ctx"]), (32000, 100, 32179))
                expected = (79 * 10 + 32000 + 100 * 12.5 + 19 * 50) / 1e6
                self.assertAlmostEqual(log["cost"], expected)
                self.assertAlmostEqual(before - db.get_user(user["id"])["balance"], expected)

    def test_request_ip_snapshot_validates_ipv4_ipv6_and_unknown(self):
        db, billing, user, group = _setup("request-ip.db", policy="free")
        for client_ip, expected in (("203.0.113.7", "203.0.113.7"),
                                    ("2001:0db8::1", "2001:db8::1"),
                                    (None, None), ("=invalid", None)):
            with self.subTest(client_ip=client_ip):
                billing.record_usage(user, group, "ch", "m1", client_ip=client_ip)
                snap = db.recent_usage(user["id"], limit=1)[0]["pricing_snapshot"]
                self.assertEqual(snap.get("client_ip"), expected)

    def test_free_policy_does_not_deduct(self):
        db, billing, user, group = _setup("bp7.db", policy="free")
        C.credit(user["id"], 10.0, "recharge", "r1")
        db.upsert_pricing("m1", None, billing_mode="token", input_price=100)
        billing.pricing.invalidate()
        billing.precheck(user, group)   # free 不检查余额
        billing.record_usage(user, group, "ch", "m1", adapter=_Upstream(),
                             upstream_usage={"prompt_tokens": 1_000_000})
        # 日志仍记价格,但余额不动
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertAlmostEqual(log["actual_cost"], 100.0)
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10.0)

    def test_quota_policy_limits_requests(self):
        db, billing, user, group = _setup("bp8.db", policy="quota",
                                         daily_limit=2, limit_unit="requests")
        billing.precheck(user, group)
        for _ in range(2):
            billing.record_usage(user, group, "ch", "m1", adapter=_Upstream(),
                                 upstream_usage={"prompt_tokens": 1})
        with self.assertRaises(RateLimitError) as ctx:
            billing.precheck(user, group)
        self.assertEqual(ctx.exception.code, "DAILY_LIMIT_EXCEEDED")

    def test_concurrency_limit(self):
        db, billing, user, group = _setup("bp9.db", policy="free", concurrency_limit=2)
        releases = [billing.acquire_concurrency(user, group) for _ in range(2)]
        try:
            with self.assertRaises(RateLimitError) as ctx:
                billing.acquire_concurrency(user, group)
            self.assertEqual(ctx.exception.code, "CONCURRENCY_LIMIT_EXCEEDED")
            releases[0]()
            releases[0]()  # 重复释放不能多放一个名额
            releases.append(billing.acquire_concurrency(user, group))
            with self.assertRaises(RateLimitError):
                billing.acquire_concurrency(user, group)
        finally:
            for release in releases:
                release()

    def test_missing_price_rejects_without_credit_or_usage_write(self):
        db, billing, user, group = _setup("bp10.db")
        C.credit(user["id"], 5.0, "recharge", "r1")
        with self.assertRaisesRegex(ValueError, "pricing not configured"):
            billing.record_usage(user, group, "ch", "never-priced",
                                 adapter=_Upstream(),
                                 upstream_usage={"prompt_tokens": 999999})
        self.assertEqual(db.recent_usage(user["id"], limit=1), [])
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 5.0)

    def test_usage_deduction_is_idempotent_per_request(self):
        db, billing, user, group = _setup("bp11.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        db.upsert_pricing("m1", None, billing_mode="token", input_price=1)
        billing.pricing.invalidate()
        for _ in range(3):
            billing.record_usage(user, group, "ch", "m1", adapter=_Upstream(),
                                 upstream_usage={"prompt_tokens": 1_000_000},
                                 request_id="rid-1")
        # 同 request_id 只扣一次、只记一条日志
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 9.0)
        self.assertEqual(len(db.recent_usage(user["id"], limit=10)), 1)

    def test_emits_usage_recorded(self):
        db, billing, user, group = _setup("bp12.db", policy="free")
        seen = []
        hooks.on("usage.recorded")(lambda **kw: seen.append(kw))
        billing.record_usage(user, group, "ch", "m1", adapter=_Upstream(),
                             upstream_usage={"prompt_tokens": 10})
        self.assertEqual(len(seen), 1)
        self.assertIn("log_id", seen[0])
        self.assertIn("actual_cost", seen[0])


if __name__ == "__main__":
    unittest.main()
