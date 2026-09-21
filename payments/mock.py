#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock 支付渠道 —— 本地测试与二次开发用。

create_payment 返回一个"确认支付"页面链接;打开即触发回调路径,视为付款成功。
verify_notify 只校验单号存在(无签名),生产环境不要启用。
"""
import urllib.parse

from core import site_settings as S
from core.payments import PaymentProvider, register_provider


class MockProvider(PaymentProvider):
    name = "mock"
    display_name = "本地测试(mock)"

    def create_payment(self, order):
        # 直接指向本站的 notify 端点,GET 也能触发(仅 mock 如此)
        qs = urllib.parse.urlencode({
            "out_trade_no": order["out_trade_no"],
            "trade_no": "MOCK-" + str(order["id"]),
            "amount": order["amount"],
        })
        return {"pay_url": f"{S.site_url().rstrip('/')}/pay/notify/mock?{qs}",
                "note": "mock 渠道:打开链接即视为付款成功"}

    def verify_notify(self, headers, raw_body, params=None):
        data = dict(params or {})
        if not data and raw_body:
            data = {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw_body.decode("utf-8", "replace")).items()}
        out_trade_no = data.get("out_trade_no")
        if not out_trade_no:
            return None
        amount = data.get("amount")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        return {"out_trade_no": out_trade_no,
                "trade_no": data.get("trade_no") or "",
                "amount": amount}

    def query_order(self, out_trade_no):
        return None


register_provider(MockProvider())
