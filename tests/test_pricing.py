import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "p.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

from core.pricing import (MODE_FREE, MODE_PER_REQUEST, MODE_TOKEN,  # noqa: E402
                          TIER_LONG, TIER_OFFICIAL, Pricing)
from core import pricing_catalog  # noqa: E402
from core.pricing_catalog import Catalog  # noqa: E402
from core.user_db import UserDB  # noqa: E402


def _db(name):
    return UserDB(os.path.join(_TMP, name))


class ResolveChainTest(unittest.TestCase):
    def setUp(self):
        self.db = _db("resolve.db")
        self.gid = self.db.create_group(name="g" + str(id(self)), supported_models=["*"])
        self.p = Pricing(self.db, catalog=None, cache_ttl=0)

    def test_exact_beats_prefix(self):
        self.db.upsert_pricing("demo-*", None, input_price=1, output_price=1)
        self.db.upsert_pricing("demo-sol", None, input_price=9, output_price=9)
        self.assertEqual(self.p.resolve("demo-sol")["input_price"], 9)
        self.assertEqual(self.p.resolve("demo-other")["input_price"], 1)

    def test_longest_prefix_wins(self):
        self.db.upsert_pricing("*", None, input_price=1)
        self.db.upsert_pricing("demo-*", None, input_price=2)
        self.db.upsert_pricing("demo-gpt-*", None, input_price=3)
        self.assertEqual(self.p.resolve("demo-gpt-5")["input_price"], 3)
        self.assertEqual(self.p.resolve("demo-abc")["input_price"], 2)
        self.assertEqual(self.p.resolve("zzz")["input_price"], 1)

    def test_group_overrides_global(self):
        self.db.upsert_pricing("m1", None, input_price=5)
        self.db.upsert_pricing("m1", self.gid, input_price=1)
        self.assertEqual(self.p.resolve("m1")["input_price"], 5)
        self.assertEqual(self.p.resolve("m1", self.gid)["input_price"], 1)
        self.assertEqual(self.p.resolve("m1", self.gid)["source"], "group")

    def test_catalog_fallback_then_free(self):
        class Cat:
            @staticmethod
            def lookup(model):
                if model == "gpt-x":
                    return {"billing_mode": MODE_TOKEN, "input_price": 7,
                            "output_price": 7, "cache_read_price": 0,
                            "cache_write_price": 0, "long_threshold": 0}
                return None

        p = Pricing(self.db, catalog=Cat(), cache_ttl=0)
        self.assertEqual(p.resolve("gpt-x")["source"], "litellm")
        free = p.resolve("never-heard-of-it")
        self.assertEqual(free["billing_mode"], MODE_FREE)
        self.assertEqual(free["source"], "fallback")


class ComputeTest(unittest.TestCase):
    """long_* 参数直接以 dict 形式喂给 compute,与 DB 解耦。"""

    SOL = {  # 长上下文档:输入 ×2 输出 ×1.5,倍数不一致
        "billing_mode": MODE_TOKEN, "source": "global",
        "input_price": 5, "output_price": 30,
        "cache_read_price": 0.5, "cache_write_price": 6.25,
        "long_threshold": 272000,
        "long_input_price": 10, "long_output_price": 45,
        "long_cache_read_price": 1, "long_cache_write_price": 12.5,
    }
    PLAIN = {"billing_mode": MODE_TOKEN, "source": "global",
             "input_price": 3, "output_price": 15,
             "cache_read_price": 0.3, "cache_write_price": 3.75,
             "long_threshold": 0}

    def test_no_threshold_uses_base(self):
        cost, actual, snap = Pricing.compute(self.PLAIN, in_tok=1_000_000,
                                             out_tok=1_000_000)
        self.assertAlmostEqual(cost, 3 + 15)
        self.assertAlmostEqual(actual, 18)
        self.assertEqual(snap["tier"], TIER_OFFICIAL)

    def test_exactly_at_threshold_uses_base(self):
        # total_ctx == 272000 → 严格大于不成立 → 普通价
        cost, _, snap = Pricing.compute(self.SOL, in_tok=272000, out_tok=1000)
        self.assertEqual(snap["tier"], TIER_OFFICIAL)
        self.assertAlmostEqual(cost, (272000 * 5 + 1000 * 30) / 1e6)

    def test_one_over_threshold_uses_long_with_independent_multiples(self):
        # total_ctx == 272001 → 长档。输入用 10(=2×5),输出用 45(=1.5×30)
        cost, _, snap = Pricing.compute(self.SOL, in_tok=272001, out_tok=1000)
        self.assertEqual(snap["tier"], TIER_LONG)
        self.assertEqual(snap["input_price"], 10)
        self.assertEqual(snap["output_price"], 45)
        self.assertAlmostEqual(cost, (272001 * 10 + 1000 * 45) / 1e6)

    def test_total_ctx_includes_cache_excludes_output(self):
        # input 200000 + cw 40000 + cr 40000 = 280000 > 272000 → 长档
        cost, _, snap = Pricing.compute(self.SOL, in_tok=200000, out_tok=5,
                                        cache_write=40000, cache_read=40000)
        self.assertEqual(snap["total_ctx"], 280000)
        self.assertEqual(snap["tier"], TIER_LONG)
        # output 极大但 ctx 不到阈值 → 仍普通档
        _, _, snap2 = Pricing.compute(self.SOL, in_tok=1000, out_tok=9_000_000)
        self.assertEqual(snap2["tier"], TIER_OFFICIAL)

    def test_missing_long_price_falls_back_per_item_not_zero(self):
        # 只配了阈值,长档单价全缺 → 各项回落普通价,绝不为 0
        p = dict(self.SOL, long_input_price=None, long_output_price=None,
                 long_cache_read_price=None, long_cache_write_price=None)
        cost, _, snap = Pricing.compute(p, in_tok=300000, out_tok=1000)
        self.assertEqual(snap["tier"], TIER_LONG)      # 档位仍标记为 long
        self.assertEqual(snap["input_price"], 5)       # 但价格回落普通
        self.assertEqual(snap["output_price"], 30)
        self.assertAlmostEqual(cost, (300000 * 5 + 1000 * 30) / 1e6)
        self.assertGreater(cost, 0)

    def test_partial_long_price_only_cache_falls_back(self):
        # 配了 in/out 长档但缺缓存长档 → in/out 走长档,缓存走普通档
        p = dict(self.SOL, long_cache_read_price=None, long_cache_write_price=None)
        _, _, snap = Pricing.compute(p, in_tok=300000, out_tok=1000,
                                     cache_read=10, cache_write=10)
        self.assertEqual(snap["input_price"], 10)
        self.assertEqual(snap["output_price"], 45)
        self.assertEqual(snap["cache_read_price"], 0.5)
        self.assertEqual(snap["cache_write_price"], 6.25)

    def test_rate_multiplier_applies_to_actual_only(self):
        cost, actual, snap = Pricing.compute(self.PLAIN, in_tok=1_000_000,
                                             rate_multiplier=0.5)
        self.assertAlmostEqual(cost, 3)
        self.assertAlmostEqual(actual, 1.5)
        self.assertEqual(snap["rate_multiplier"], 0.5)

    def test_per_request_and_free(self):
        pr = {"billing_mode": MODE_PER_REQUEST, "per_request_price": 0.04}
        cost, actual, snap = Pricing.compute(pr, units=3, rate_multiplier=2)
        self.assertAlmostEqual(cost, 0.12)
        self.assertAlmostEqual(actual, 0.24)
        self.assertEqual(snap["units"], 3)

        cost, actual, snap = Pricing.compute({"billing_mode": MODE_FREE},
                                             in_tok=999999, rate_multiplier=5)
        self.assertEqual((cost, actual), (0.0, 0.0))
        self.assertEqual(snap["mode"], MODE_FREE)

    def test_snapshot_enables_recompute_after_price_change(self):
        cost, _, snap = Pricing.compute(self.SOL, in_tok=300000, out_tok=1000,
                                        cache_read=100, cache_write=200)
        # 用 snapshot 里冻结的单价手工复算,应与当时的 cost 一致
        recomputed = (300000 * snap["input_price"] + 1000 * snap["output_price"]
                      + 200 * snap["cache_write_price"]
                      + 100 * snap["cache_read_price"]) / 1e6
        self.assertAlmostEqual(cost, recomputed)


class SeedDataTest(unittest.TestCase):
    """内置官网价可直接解析、也可导入管理端。"""

    @staticmethod
    def _rows():
        import json
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "data", "pricing_seed.json"), encoding="utf-8") as f:
            return json.load(f)["pricing"]

    def test_seed_file_loads_and_prices_correctly(self):
        rows = self._rows()
        self.assertTrue(rows)
        self.assertEqual(len(rows), len({r["model_pattern"] for r in rows}))
        by_model = {r["model_pattern"]: r for r in rows}

        # grok-4.6:总上下文超过 199999 后输入/输出/缓存都进长上下文档
        g = dict({"billing_mode": MODE_TOKEN}, **by_model["grok-4.6"])
        _, _, s1 = Pricing.compute(g, in_tok=199999, out_tok=10)
        self.assertEqual(s1["tier"], TIER_OFFICIAL)
        self.assertEqual(s1["input_price"], 2)
        _, _, s2 = Pricing.compute(g, in_tok=200000, out_tok=10)
        self.assertEqual(s2["tier"], TIER_LONG)
        self.assertEqual((s2["input_price"], s2["output_price"]), (4, 12))
        self.assertEqual(s2["cache_read_price"], 1)

        # 缓存价两档不同:普通档 0.3,长档 0.6
        g5 = dict({"billing_mode": MODE_TOKEN}, **by_model["grok-4.5"])
        _, _, c1 = Pricing.compute(g5, in_tok=1000, cache_read=1000)
        self.assertEqual(c1["cache_read_price"], 0.3)
        _, _, c2 = Pricing.compute(g5, in_tok=200000, cache_read=1000)
        self.assertEqual(c2["cache_read_price"], 0.6)

        # 都是文本模型,必须按 Token 计费
        for r in rows:
            self.assertEqual(r["billing_mode"], MODE_TOKEN, r["model_pattern"])

        # 任何一条都不允许价回落成 0,长档不得低于普通档
        for r in rows:
            self.assertGreater(r["input_price"] + r["output_price"], 0)
            if r.get("long_threshold"):
                if r.get("long_input_price") is not None:
                    self.assertGreaterEqual(r["long_input_price"], r["input_price"])
                if r.get("long_output_price") is not None:
                    self.assertGreaterEqual(r["long_output_price"], r["output_price"])

    def test_seed_importable_and_roundtrip(self):
        rows = self._rows()
        db = _db("seed.db")
        for r in rows:
            db.upsert_pricing(r["model_pattern"], None,
                              **{k: v for k, v in r.items() if k != "model_pattern"})
        stored = {r["model_pattern"]: r for r in db.list_pricing(None)}
        self.assertEqual(len(stored), len(rows))
        g = stored["grok-4.6"]
        self.assertEqual(g["long_threshold"], 199999)
        self.assertEqual(g["input_price"], 2)
        self.assertEqual(g["long_input_price"], 4)
        p = Pricing(db, catalog=None, cache_ttl=0)
        _, _, snap = Pricing.compute(p.resolve("grok-4.6"), in_tok=300000)
        self.assertEqual(snap["tier"], TIER_LONG)
        self.assertEqual(snap["input_price"], 4)

    def test_builtin_catalog_loads_and_admin_overrides(self):
        pricing_catalog._load_builtin()
        db = _db("official-catalog.db")
        p = Pricing(db, catalog=Catalog(), cache_ttl=0)

        g = p.resolve("grok-4.6")
        self.assertEqual(g["source"], "builtin")
        self.assertEqual((g["input_price"], g["output_price"]), (2, 6))

        # 管理员显式设置仍覆盖内置目录价
        db.upsert_pricing("grok-4.6", None, billing_mode=MODE_TOKEN,
                          input_price=9.9, output_price=9.9)
        self.assertEqual(p.resolve("grok-4.6")["input_price"], 9.9)


if __name__ == "__main__":
    unittest.main()
