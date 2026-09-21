#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""易支付签名与回调解析的门禁 —— 全程不发网络。

覆盖的边界不是凭想象列的,每一条对应一个真实会静默出错的地方:
签名前忘了排序、忘了排除空值、忘了排除 sign 自身、大小写、money 被篡改、
以及查单响应里 trade_status 与数字 status 冲突时该信谁。

接真渠道之前这些必须全绿 —— 验签错了的表现是「用户付了钱但一直不到账」,
而这种错在本机永远看不到,因为本机没有上游会给你发回调。
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "epay.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

import config  # noqa: E402
from core.payments import NOT_PAID  # noqa: E402
from payments.epay import EpayProvider, _sign, map_query_status  # noqa: E402

KEY = "test-merchant-key-9f3a"


def _cfg():
    config.EPAY_API_URL = "https://pay.example.com"
    config.EPAY_PID = "1001"
    config.EPAY_KEY = KEY
    config.EPAY_USD_RATE = 7.2
    config.SITE_URL = "https://site.example.com"


class SignTest(unittest.TestCase):
    def setUp(self):
        _cfg()

    def test_deterministic(self):
        p = {"pid": "1001", "money": "72.00", "out_trade_no": "PG1"}
        self.assertEqual(_sign(p, KEY), _sign(p, KEY))

    def test_key_order_does_not_matter(self):
        """锁的是「拼串前必须排序」,不是 dict 有序这个实现细节。"""
        a = {"pid": "1001", "money": "72.00", "out_trade_no": "PG1"}
        b = {"out_trade_no": "PG1", "money": "72.00", "pid": "1001"}
        self.assertEqual(_sign(a, KEY), _sign(b, KEY))

    def test_sign_and_sign_type_excluded(self):
        base = {"pid": "1001", "money": "72.00"}
        withsig = dict(base, sign="deadbeef" * 4, sign_type="MD5")
        self.assertEqual(_sign(base, KEY), _sign(withsig, KEY))

    def test_empty_values_excluded(self):
        """可选字段「不传」与「传空串」必须同签。

        这条在本仓比在别处更要紧:两条入参路径行为不一致 ——
        server.py 的 dict(request.query_params) 保留空串,
        payments/epay.py 里的 parse_qs 默认丢弃空值。喂两种形态都得同签,
        否则移动端探测参数一开一关就大面积验签失败。
        """
        bare = {"pid": "1001", "money": "72.00"}
        blank = dict(bare, device="", clientip="", cid="")
        self.assertEqual(_sign(bare, KEY), _sign(blank, KEY))

    def test_none_values_excluded(self):
        bare = {"pid": "1001", "money": "72.00"}
        self.assertEqual(_sign(bare, KEY), _sign(dict(bare, device=None), KEY))

    def test_output_is_32_lowercase_hex(self):
        s = _sign({"a": "1"}, KEY)
        self.assertEqual(len(s), 32)
        self.assertEqual(s, s.lower())
        self.assertTrue(all(c in "0123456789abcdef" for c in s))

    def test_empty_params_still_hashes(self):
        self.assertEqual(len(_sign({}, KEY)), 32)

    def test_different_key_differs(self):
        p = {"pid": "1001", "money": "72.00"}
        self.assertNotEqual(_sign(p, KEY), _sign(p, KEY + "x"))


def _notify(**over):
    data = {"pid": "1001", "out_trade_no": "PG1", "trade_no": "E-1",
            "type": "alipay", "name": "topup-PG1", "money": "72.00",
            "trade_status": "TRADE_SUCCESS"}
    data.update(over)
    data["sign"] = _sign(data, KEY)
    data["sign_type"] = "MD5"
    return data


class VerifyNotifyTest(unittest.TestCase):
    def setUp(self):
        _cfg()
        self.p = EpayProvider()

    def test_valid_notify_parsed(self):
        got = self.p.verify_notify({}, b"", params=_notify())
        self.assertEqual(got["out_trade_no"], "PG1")
        self.assertEqual(got["trade_no"], "E-1")
        self.assertAlmostEqual(got["amount_cny"], 72.00)
        self.assertEqual(got["pid"], "1001")

    def test_sign_survives_untrimmed_callback_table(self):
        """验签函数必须容忍没清洗过的回调原始参数表(sign/sign_type 都在里面)。"""
        data = _notify()
        self.assertIn("sign", data)
        self.assertIsNotNone(self.p.verify_notify({}, b"", params=data))

    def test_tampered_money_fails(self):
        data = _notify()
        data["money"] = "0.01"          # 签名不重算 —— 攻击者唯一有动机改的字段
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_wrong_key_fails(self):
        data = dict(_notify())
        data["sign"] = _sign(data, "not-the-key")
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_all_zero_sign_fails(self):
        """和「换错密钥」分开测:能抓住「只校验长度」或「任意 32 位 hex 都返真」。"""
        data = dict(_notify())
        data["sign"] = "0" * 32
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_uppercase_sign_accepted(self):
        """口径:宽容。上游回大写 hex 也认(docs/payments.md 同向记载)。"""
        data = _notify()
        data["sign"] = data["sign"].upper()
        self.assertIsNotNone(self.p.verify_notify({}, b"", params=data))

    def test_missing_out_trade_no_fails(self):
        data = _notify()
        del data["out_trade_no"]
        data["sign"] = _sign({k: v for k, v in data.items()}, KEY)
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_foreign_pid_rejected(self):
        """签名对得上只说明对方有同一把 key,商户号不符仍然不是我们的单。"""
        data = _notify(pid="9999")
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_unpaid_status_is_not_paid_not_none(self):
        """验签通过但没成功 —— 必须能和「验签失败」区分,否则渠道无限重试。"""
        got = self.p.verify_notify({}, b"", params=_notify(
            trade_status="TRADE_CLOSED"))
        self.assertIs(got, NOT_PAID)

    def test_no_credentials_refuses_even_with_valid_sign(self):
        """KEY 为空时任何人都能用空 key 算出合法签名,所以验签侧必须先挡凭据。"""
        data = {"pid": "1001", "out_trade_no": "PG1", "money": "1.00",
                "trade_status": "TRADE_SUCCESS"}
        data["sign"] = _sign(data, "")
        config.EPAY_KEY = ""
        self.assertIsNone(self.p.verify_notify({}, b"", params=data))

    def test_form_body_path_matches_query_path(self):
        import urllib.parse
        data = _notify()
        body = urllib.parse.urlencode(data).encode()
        self.assertIsNotNone(self.p.verify_notify({}, body, params=None))


class CreatePaymentTest(unittest.TestCase):
    def setUp(self):
        _cfg()
        self.p = EpayProvider()

    def test_name_survives_urlencode_roundtrip(self):
        """下单参数经 urlencode → parse_qs 往返后必须仍同签。

        name 里一旦有空格或 sqlite REAL 内插出来的 "10.0",往返就会变形,
        上游拿变形后的值验签,两边永远对不上。
        """
        import urllib.parse
        order = {"id": 7, "out_trade_no": "PG7", "amount": 10.0}
        url = self.p.create_payment(order)["pay_url"]
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        flat = {k: v[0] for k, v in qs.items()}
        self.assertEqual(flat["sign"], _sign(flat, KEY))

    def test_amount_cny_frozen_by_rate(self):
        order = {"id": 1, "out_trade_no": "PG1", "amount": 10.0}
        self.assertAlmostEqual(self.p.create_payment(order)["amount_cny"], 72.00)

    def test_unconfigured_raises(self):
        config.EPAY_KEY = ""
        with self.assertRaises(RuntimeError):
            self.p.create_payment({"id": 1, "out_trade_no": "PG1", "amount": 10.0})


class QueryStatusMappingTest(unittest.TestCase):
    """查单响应 → 状态。纯函数,喂字典,零网络。"""

    def test_trade_success_is_paid(self):
        self.assertEqual(map_query_status({"trade_status": "TRADE_SUCCESS"}), "paid")

    def test_trade_status_wins_over_numeric_status(self):
        """最容易写错的一条:部分易支付克隆的 status=1 只表示接口调用成功。"""
        self.assertEqual(
            map_query_status({"trade_status": "WAITING", "status": 1}), "pending")

    def test_empty_trade_status_still_wins(self):
        self.assertEqual(
            map_query_status({"trade_status": "", "status": 1}), "pending")

    def test_numeric_status_used_when_no_trade_status(self):
        self.assertEqual(map_query_status({"status": 1}), "paid")
        self.assertEqual(map_query_status({"status": 0}), "pending")
        self.assertEqual(map_query_status({"status": "1"}), "paid")

    def test_nested_data_object(self):
        self.assertEqual(
            map_query_status({"code": 1, "data": {"trade_status": "TRADE_SUCCESS"}}),
            "paid")
        self.assertEqual(
            map_query_status({"code": 1, "data": {"status": 0}}), "pending")

    def test_order_not_found_is_pending_not_error(self):
        self.assertEqual(
            map_query_status({"code": 0, "msg": "订单不存在"}), "pending")

    def test_garbage_is_pending(self):
        for junk in (None, "", [], {"unrelated": 1}):
            with self.subTest(junk=junk):
                self.assertEqual(map_query_status(junk), "pending")


if __name__ == "__main__":
    unittest.main()
