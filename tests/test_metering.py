import os
import tempfile
import unittest

os.environ.setdefault("BITAPI_DB", os.path.join(tempfile.mkdtemp(), "m.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", "/tmp/noexist_m")

from core import metering
from core.adapter import BILL_UPSTREAM, BILL_ESTIMATE, BILL_PER_REQUEST


class MeteringTest(unittest.TestCase):
    def test_upstream_usage_takes_priority(self):
        # 声明 estimate,但上游给了真实 usage → 采信 upstream
        it, ot, src = metering.resolve_usage(
            BILL_ESTIMATE,
            {"prompt_tokens": 12, "completion_tokens": 34},
            [{"role": "user", "content": "ignored for estimate"}],
            "ignored")
        self.assertEqual((it, ot), (12, 34))
        self.assertEqual(src, metering.SRC_UPSTREAM)

    def test_total_only_usage_goes_to_output(self):
        it, ot, src = metering.resolve_usage(
            BILL_UPSTREAM, {"total_tokens": 50}, [], "")
        self.assertEqual((it, ot), (0, 50))
        self.assertEqual(src, metering.SRC_UPSTREAM)

    def test_estimate_when_no_upstream_usage(self):
        it, ot, src = metering.resolve_usage(
            BILL_ESTIMATE, None,
            [{"role": "user", "content": "hello world how are you"}],
            "I am fine thanks for asking")
        self.assertGreater(it, 0)
        self.assertGreater(ot, 0)
        self.assertEqual(src, metering.SRC_ESTIMATE)

    def test_estimate_fallback_when_upstream_all_zero(self):
        # upstream usage 全 0 视为无效 → 回退估算
        it, ot, src = metering.resolve_usage(
            BILL_ESTIMATE,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            [{"role": "user", "content": "estimate me please"}],
            "sure thing")
        self.assertEqual(src, metering.SRC_ESTIMATE)
        self.assertGreater(it + ot, 0)

    def test_per_request_zero_tokens(self):
        it, ot, src = metering.resolve_usage(
            BILL_PER_REQUEST, None,
            [{"role": "user", "content": "draw a cat"}], "")
        self.assertEqual((it, ot), (0, 0))
        self.assertEqual(src, metering.SRC_PER_REQUEST)

    def test_per_request_still_honors_real_upstream_usage(self):
        # 即便 per_request,若上游真给了 usage 也采信(极少见但不丢数据)
        it, ot, src = metering.resolve_usage(
            BILL_PER_REQUEST, {"prompt_tokens": 5, "completion_tokens": 7},
            [], "")
        self.assertEqual((it, ot), (5, 7))
        self.assertEqual(src, metering.SRC_UPSTREAM)

    def test_count_tokens_nonempty(self):
        self.assertEqual(metering.count_tokens(""), 0)
        self.assertGreater(metering.count_tokens("the quick brown fox"), 0)

    def test_multimodal_content_text_extracted(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        ]}]
        it, ot, src = metering.resolve_usage(BILL_ESTIMATE, None, msgs, "a photo")
        self.assertGreater(it, 0)
        self.assertEqual(src, metering.SRC_ESTIMATE)


if __name__ == "__main__":
    unittest.main()
