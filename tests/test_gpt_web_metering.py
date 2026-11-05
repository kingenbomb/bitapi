"""Image estimates, referenced prices and real ledger settlement on isolated DBs."""
import io
import json
import unittest

from PIL import Image

from core import credit
from core.adapter import Adapter, BILL_ESTIMATE
from core.gpt_web.metering import estimate_image_usage
from core.metering import resolve_usage
from tests.test_billing_policy import _setup


def image_bytes(width, height, format="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (width, height)).save(out, format=format)
    return out.getvalue()


class WebImageMeteringTest(unittest.TestCase):
    def test_output_estimate_uses_actual_dimensions_and_rounds_partial_patches(self):
        for width, height, expected, format in [(1024, 1024, 1056, "PNG"), (1312, 1199, 1607, "PNG"),
                                                 (1025, 1024, 1089, "JPEG"), (1536, 1024, 1584, "WEBP")]:
            with self.subTest(width=width, height=height, format=format):
                usage = estimate_image_usage(image_bytes(width, height, format), "cat")
                self.assertEqual(usage["output_tokens"], expected)
                self.assertEqual(usage["input_tokens"], 1)
                self.assertEqual(usage["total_tokens"], expected + 1)
                self.assertEqual(usage["token_source"], "estimate")
                self.assertEqual(usage["image_token_estimate"]["width"], width)
                self.assertEqual(usage["image_token_estimate"]["height"], height)

    def test_invalid_image_is_not_assigned_a_default_billable_size(self):
        with self.assertRaises(Exception):
            estimate_image_usage(b"not an image", "cat")

    def test_measured_usage_takes_priority_over_explicit_estimate(self):
        self.assertEqual(resolve_usage(BILL_ESTIMATE, {"prompt_tokens": 10, "completion_tokens": 20}, None, None,
            estimated_usage={"prompt_tokens": 100, "completion_tokens": 200}), (10, 20, "upstream"))

    def test_flare_price_estimate_discount_idempotency_and_failed_request(self):
        db, billing, user, group = _setup("web-image-estimate.db", rate_multiplier=0.5)
        db.upsert_pricing("gpt-image-2.5-flare", None, billing_mode="token", input_price=5, output_price=30)
        credit.credit(user["id"], 10, "recharge", "web-image-test-credit")
        usage = estimate_image_usage(image_bytes(1024, 1024), "cat")
        estimated = {**usage, "prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"]}
        adapter = Adapter()
        adapter.billing_mode = BILL_ESTIMATE
        def record(request_id, reason):
            return billing.record_usage(user, group, "gpt", "gpt-image-2-web", adapter=adapter,
                estimated_usage=estimated, request_id=request_id, end_reason=reason)
        first = record("web-image-paid", "done")
        expected = (5 + 1056 * 30) / 1_000_000
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10 - expected * 0.5)
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertEqual(log["token_source"], "estimate")
        self.assertEqual(log["input_tokens"], 1)
        self.assertEqual(log["output_tokens"], 1056)
        self.assertAlmostEqual(log["cost"], expected)
        self.assertAlmostEqual(log["actual_cost"], expected * 0.5)
        snapshot = json.loads(log["pricing_snapshot"]) if isinstance(log["pricing_snapshot"], str) else log["pricing_snapshot"]
        self.assertEqual(snapshot["pricing_model"], "gpt-image-2.5-flare")
        self.assertEqual(snapshot["image_token_estimate"]["width"], 1024)
        self.assertIsNone(snapshot["cache_read_tokens"])
        self.assertEqual(record("web-image-paid", "done"), first)
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10 - expected * 0.5)
        record("web-image-failed", "error")
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10 - expected * 0.5)
        self.assertEqual(db.recent_usage(user["id"], limit=1)[0]["actual_cost"], 0)
