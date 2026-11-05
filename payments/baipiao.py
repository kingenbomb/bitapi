"""白嫖社区毛支付：固定社区网关，易支付 V1，独立于人民币渠道。"""
import hashlib
import hmac
import re
from decimal import Decimal, ROUND_CEILING
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from core import site_settings as S
from core.payments import NOT_PAID, PaymentProvider, register_provider

GATEWAY = "https://baipiao.org/bbs/payment/"
NOTIFY_FIELDS = frozenset(("pid", "trade_no", "out_trade_no", "type", "name",
                           "money", "trade_status", "param", "sign", "sign_type"))


def sign(params, key):
    pairs = [f"{k}={v}" for k, v in sorted(params.items())
             if k not in ("sign", "sign_type") and v != ""]
    return hashlib.md5(("&".join(pairs) + key).encode("utf-8")).hexdigest()


def money(value):
    # Decimal 拒绝科学计数法、NaN、负数和过多小数；与社区公开金额契约一致。
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        return None
    amount = Decimal(value)
    return amount if amount > 0 else None


def notify_data(raw_body, params):
    if raw_body and params:
        return None
    try:
        if raw_body:
            pairs = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=20)
        elif hasattr(params, "multi_items"):
            pairs = params.multi_items()
        else:
            pairs = list((params or {}).items())
        data = {}
        for key, value in pairs:
            if key not in NOTIFY_FIELDS or key in data or not isinstance(value, str):
                return None
            data[key] = value
        return data
    except (ValueError, UnicodeError):
        return None


class BaipiaoProvider(PaymentProvider):
    name = "baipiao"
    display_name = "白嫖社区毛支付"

    def _ready(self):
        return bool(S.baipiao_pid() and S.baipiao_key())

    def create_payment(self, order):
        if not self._ready():
            raise RuntimeError("请先在支付设置中配置白嫖商户 PID 和 KEY")
        site = S.site_url().rstrip("/")
        parsed = urlsplit(site)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("毛支付需要 HTTPS 公网站点根地址，端口为 443")
        rate = Decimal(str(S.mao_per_usd()))
        if not rate.is_finite() or not Decimal("0.01") <= rate <= Decimal("10000"):
            raise RuntimeError("每美元所需毛数配置无效")
        mao = int((Decimal(str(order["amount"])) * rate).to_integral_value(rounding=ROUND_CEILING))
        if not 1 <= mao <= 9999999:
            raise RuntimeError("订单毛数超出社区支付范围")
        params = {
            "pid": S.baipiao_pid(), "type": "mao",
            "out_trade_no": order["out_trade_no"],
            "notify_url": site + "/pay/notify/baipiao",
            "return_url": site + "/pay/return/baipiao",
            "name": "白嫖公益站额度-" + order["out_trade_no"],
            "money": f"{mao}.00",
        }
        params["sign"] = sign(params, S.baipiao_key())
        params["sign_type"] = "MD5"
        return {"pay_url": GATEWAY + "submit.php?" + urlencode(params),
                "amount_mao": mao, "merchant_id": params["pid"]}

    def _payment(self, data):
        amount = money(data.get("money"))
        if (str(data.get("pid")) != S.baipiao_pid() or data.get("type") != "mao"
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(data.get("out_trade_no") or ""))
                or not data.get("trade_no") or amount is None):
            return None
        return {"out_trade_no": data["out_trade_no"], "trade_no": data["trade_no"],
                "amount_mao": str(amount), "amount": float(amount), "pid": str(data["pid"])}

    def verify_notify(self, headers, raw_body, params=None):
        if not self._ready():
            return None
        data = notify_data(raw_body, params)
        if not data or data.get("sign_type", "MD5") != "MD5":
            return None
        supplied = data.get("sign", "")
        if (not re.fullmatch(r"[0-9a-f]{32}", supplied)
                or not hmac.compare_digest(supplied, sign(data, S.baipiao_key()))):
            return None
        payment = self._payment(data)
        if not payment:
            return None
        if data.get("trade_status") in ("WAIT_BUYER_PAY", "TRADE_CLOSED"):
            return NOT_PAID
        return payment if data.get("trade_status") == "TRADE_SUCCESS" else None

    def query_order(self, out_trade_no):
        if not self._ready():
            return None
        try:
            response = httpx.post(GATEWAY + "api.php", data={
                "act": "order", "pid": S.baipiao_pid(), "key": S.baipiao_key(),
                "out_trade_no": out_trade_no,
            }, timeout=15, follow_redirects=False)
            if response.status_code != 200:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # 不透传响应或请求体，以免商户密钥进入日志/浏览器。
            return None
        if not isinstance(data, dict) or data.get("code") != 1:
            return None
        payment = self._payment(data)
        if not payment or payment["out_trade_no"] != out_trade_no:
            return None
        paid = (data.get("order_status") == "paid" and data.get("status") == 1
                and data.get("trade_status") == "TRADE_SUCCESS" and not data.get("refunded"))
        return dict(payment, status="paid" if paid else "pending")


register_provider(BaipiaoProvider())
