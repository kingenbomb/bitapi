import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "p.db"))

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
        self.db.upsert_pricing("fixture-*", None, input_price=1, output_price=1)
        self.db.upsert_pricing("fixture-sol", None, input_price=9, output_price=9)
        self.assertEqual(self.p.resolve("fixture-sol")["input_price"], 9)
        self.assertEqual(self.p.resolve("fixture-other")["input_price"], 1)

    def test_longest_prefix_wins(self):
        self.db.upsert_pricing("*", None, input_price=1)
        self.db.upsert_pricing("fixture-*", None, input_price=2)
        self.db.upsert_pricing("fixture-gpt-*", None, input_price=3)
        self.assertEqual(self.p.resolve("fixture-gpt-5")["input_price"], 3)
        self.assertEqual(self.p.resolve("fixture-abc")["input_price"], 2)
        self.assertEqual(self.p.resolve("zzz")["input_price"], 1)

    def test_group_overrides_global(self):
        self.db.upsert_pricing("m1", None, input_price=5)
        self.db.upsert_pricing("m1", self.gid, input_price=1)
        self.assertEqual(self.p.resolve("m1")["input_price"], 5)
        self.assertEqual(self.p.resolve("m1", self.gid)["input_price"], 1)
        self.assertEqual(self.p.resolve("m1", self.gid)["source"], "group")

    def test_catalog_fallback_then_reject_missing_price(self):
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
        with self.assertRaisesRegex(ValueError, "pricing not configured"):
            p.resolve("never-heard-of-it")

    def test_web_image_references_flare_in_the_same_group(self):
        self.db = _db("web-price-reference.db")
        self.gid = self.db.create_group(name="web-price-reference", supported_models=["*"])
        self.p = Pricing(self.db, cache_ttl=0)
        self.db.upsert_pricing("gpt-image-2.5-flare", None, billing_mode=MODE_TOKEN, input_price=5, output_price=30)
        self.db.upsert_pricing("gpt-image-2.5-flare", self.gid, billing_mode=MODE_TOKEN, input_price=2, output_price=12)
        self.assertEqual(self.p.resolve("gpt-image-2-web")["output_price"], 30)
        priced = self.p.resolve("gpt-image-2-web", self.gid)
        self.assertEqual(priced["output_price"], 12)
        self.assertEqual(priced["pricing_model"], "gpt-image-2.5-flare")
        _, _, snapshot = Pricing.compute(priced, in_tok=10, out_tok=1000)
        self.assertEqual(snapshot["pricing_model"], "gpt-image-2.5-flare")
        self.db.upsert_pricing("gpt-image-2.5-flare", self.gid, billing_mode=MODE_TOKEN, output_price=15)
        self.assertEqual(self.p.resolve("gpt-image-2-web", self.gid)["output_price"], 15)

    def test_explicit_web_image_price_retains_existing_priority(self):
        self.db = _db("web-price-override.db")
        self.p = Pricing(self.db, cache_ttl=0)
        self.db.upsert_pricing("gpt-image-2.5-flare", None, billing_mode=MODE_TOKEN, output_price=30)
        self.db.upsert_pricing("gpt-image-2-web", None, billing_mode=MODE_PER_REQUEST, per_request_price=0.02)
        self.assertEqual(self.p.resolve("gpt-image-2-web")["billing_mode"], MODE_PER_REQUEST)
        self.assertEqual(self.p.resolve("gpt-image-2-web")["per_request_price"], 0.02)


class ComputeTest(unittest.TestCase):
    """long_* 参数直接以 dict 形式喂给 compute,与 DB 解耦。"""

    SOL = {  # 虚构阶梯价格：输入 ×2、输出 ×1.5，用于验证独立跳档
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
    """当前内置价格可直接解析、导入管理端，且不含已退役别名。"""

    @staticmethod
    def _rows():
        import json
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "data", "pricing_seed.json"), encoding="utf-8") as f:
            return json.load(f)["pricing"]

    def test_seed_file_loads_and_prices_correctly(self):
        rows = self._rows()
        self.assertEqual(len(rows), len({r["model_pattern"] for r in rows}))
        by_model = {r["model_pattern"]: r for r in rows}
        required = {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
                    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}
        self.assertFalse(required - by_model.keys())
        self.assertFalse({name for name in by_model if name == "kg" or name.startswith("kg-")})

        # GPT 原生名称仍保留长上下文阶梯价格。
        g = by_model["gpt-5.6-sol"]
        _, _, s1 = Pricing.compute(g, in_tok=272000, out_tok=10)
        self.assertEqual(s1["tier"], TIER_OFFICIAL)
        self.assertEqual(s1["input_price"], 4)
        _, _, s2 = Pricing.compute(g, in_tok=272001, out_tok=10)
        self.assertEqual(s2["tier"], TIER_LONG)
        self.assertEqual((s2["input_price"], s2["output_price"]), (8, 30))
        self.assertEqual(s2["cache_read_price"], 0.8)

        terra = by_model["gpt-5.6-terra"]
        _, _, s3 = Pricing.compute(terra, in_tok=272001)
        self.assertEqual(s3["tier"], TIER_LONG)
        self.assertEqual((s3["input_price"], s3["output_price"]), (4, 18))
        self.assertEqual((s3["cache_read_price"], s3["cache_write_price"]), (0.4, 5))

        # 保留不同渠道公开型号各自的价格。
        self.assertEqual(by_model["DeepSeek-V4-Flash"]["input_price"], 0.14)
        self.assertEqual(by_model["DeepSeek-V4-Flash"]["output_price"], 0.28)
        self.assertEqual(by_model["deepseek-ai/deepseek-v4.1-flash"]["input_price"], 0.3)
        self.assertEqual(by_model["deepseek-ai/deepseek-v4.1-flash"]["output_price"], 1.2)

        for name in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            image = by_model[name]
            self.assertEqual(image["billing_mode"], MODE_TOKEN)
            self.assertEqual((image["input_price"], image["output_price"], image["cache_read_price"]), (5, 30, 1.25))

        # 这三个都是文本模型，必须按 Token 计费
        gemma = by_model["google/gemma-4-31b-it"]
        self.assertEqual(gemma["billing_mode"], MODE_TOKEN)
        self.assertEqual((gemma["input_price"], gemma["output_price"]), (0.09, 0.34))
        minimax = by_model["minimaxai/minimax-m3.1-flash"]
        self.assertEqual(minimax["billing_mode"], MODE_TOKEN)
        self.assertEqual((minimax["input_price"], minimax["output_price"]), (0.15, 0.6))

        for r in rows:
            if r["billing_mode"] == MODE_PER_REQUEST:
                self.assertGreater(r["per_request_price"], 0)
            else:
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
        for row in rows:
            for field, value in row.items():
                with self.subTest(model=row["model_pattern"], field=field):
                    self.assertEqual(stored[row["model_pattern"]][field], value)
        sol = stored["gpt-5.6-sol"]
        self.assertEqual(sol["long_threshold"], 272000)
        self.assertEqual(sol["input_price"], 4)
        self.assertEqual(sol["long_input_price"], 8)
        self.assertEqual(sol["long_cache_write_price"], 10)
        p = Pricing(db, catalog=None, cache_ttl=0)
        _, _, snap = Pricing.compute(p.resolve("gpt-5.6-sol"), in_tok=300000)
        self.assertEqual(snap["tier"], TIER_LONG)
        self.assertEqual(snap["input_price"], 8)

    def test_builtin_catalog_prices_current_models_and_honors_overrides(self):
        pricing_catalog._load_builtin()
        db = _db("official-catalog.db")
        p = Pricing(db, catalog=Catalog(), cache_ttl=0)

        sol = p.resolve("gpt-5.6-sol")
        self.assertEqual(sol["source"], "builtin")
        self.assertEqual((sol["input_price"], sol["output_price"]), (4, 20))

        web = p.resolve("gpt-image-2-web")
        self.assertEqual(web["source"], "builtin")
        self.assertEqual(web["pricing_model"], "gpt-image-2.5-flare")
        self.assertEqual((web["input_price"], web["output_price"]), (5, 30))

        flash = p.resolve("minimaxai/minimax-m3.1-flash")
        self.assertEqual(flash["source"], "builtin")
        self.assertEqual((flash["input_price"], flash["output_price"]), (0.15, 0.6))

        # 管理员显式设置仍覆盖内置目录价
        db.upsert_pricing("minimaxai/minimax-m3.1-flash", None, billing_mode=MODE_TOKEN,
                          input_price=0.2, output_price=0.6)
        self.assertEqual(p.resolve("minimaxai/minimax-m3.1-flash")["input_price"], 0.2)


if __name__ == "__main__":
    unittest.main()
