#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
易支付(彩虹易支付协议)渠道 —— 国内个人站主流聚合支付。

协议要点:
  下单  GET  {API}/submit.php?pid&type&out_trade_no&notify_url&return_url&name&money&sign&sign_type=MD5
  回调  GET/POST 带 pid/trade_no/out_trade_no/type/name/money/trade_status/sign
  验签  把除 sign/sign_type 外的**非空**参数按 key 升序拼 a=1&b=2,末尾接商户 KEY 取 MD5

金额:本项目内部以美元计价,下单时按运行时设置的汇率换算成人民币。
换算**只在这里发生一次**,算出的人民币由 core 冻进订单行,回调比对在人民币域进行。
不要在别处再出现 EPAY_USD_RATE —— 乘一次再除回去,汇率一改两边就永久对不上。

空值必须排除:可选字段(device / clientip / cid)「不传」与「传空串」必须产生同一个
签名,否则移动端探测一开一关就大面积验签失败。这也是本文件唯一的历史坑点。
"""
import hashlib
import urllib.parse

from core import site_settings as S
from core.payments import NOT_PAID, PaymentProvider, register_provider


def _sign(params, key):
    items = sorted((k, v) for k, v in params.items()
                   if k not in ("sign", "sign_type") and v not in (None, ""))
    raw = "&".join(f"{k}={v}" for k, v in items) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def map_query_status(data):
    """查单响应 dict → "paid" | "pending"。纯函数,不发网络,供测试直接喂字典。

    字段优先级:顶层 trade_status → data.trade_status → 顶层数字 status → data.status。
    **trade_status 一旦存在就不再看数字 status** —— 部分易支付克隆站的 status=1 只表示
    「接口调用成功」而不是「订单已付」,只看 status 会给未付订单到账。
    """
    if not isinstance(data, dict):
        return "pending"
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}

    for src in (data, inner):
        if "trade_status" in src:
            ts = str(src.get("trade_status") or "").upper()
            return "paid" if ts in ("TRADE_SUCCESS", "SUCCESS") else "pending"

    for src in (data, inner):
        if "status" in src:
            return "paid" if str(src.get("status")) == "1" else "pending"

    # code=0 常见于「订单不存在」——还没付就查,是正常情况,不是错误
    return "pending"


class EpayProvider(PaymentProvider):
    name = "epay"
    display_name = "易支付(支付宝)"

    def _ready(self):
        return bool(S.epay_api_url() and S.epay_pid() and S.epay_key())

    def create_payment(self, order, pay_type="alipay"):
        if not self._ready():
            raise RuntimeError("epay 未配置(需 BITAPI_EPAY_URL/PID/KEY)")
        site = S.site_url().rstrip("/")
        cny = round(float(order["amount"]) * S.epay_usd_rate(), 2)
        params = {
            "pid": S.epay_pid(),
            "type": pay_type,
            "out_trade_no": order["out_trade_no"],
            "notify_url": f"{site}/pay/notify/epay",
            "return_url": f"{site}/pay/return/epay",
            # 不含空格与浮点小数:name 要经 urlencode 往返再参与验签,
            # 空格会被编成 + ,"10.0" 这种 sqlite REAL 内插也会让两边对不上。
            "name": f"topup-{order['out_trade_no']}",
            "money": f"{cny:.2f}",
        }
        params["sign"] = _sign(params, S.epay_key())
        params["sign_type"] = "MD5"
        url = (S.epay_api_url().rstrip("/") + "/submit.php?"
               + urllib.parse.urlencode(params))
        # amount_cny 由 core 冻进订单行,作为回调金额比对的基准
        return {"pay_url": url, "amount_cny": cny}

    def verify_notify(self, headers, raw_body, params=None):
        """三态返回:

          None      验签失败 / 缺凭据 / 缺单号 —— 端点回失败文本
          NOT_PAID  验签通过但不是「已支付」事件 —— 端点必须回 success,否则渠道无限重试
          dict      验签通过且已支付,含 out_trade_no / trade_no / amount_cny / pid
        """
        if not self._ready():
            # KEY 为空时任何人都能用空 key 算出合法签名。下单侧已经报错,
            # 验签侧不挡的话表面上只是「配置没生效」,实际是敞开的入账口。
            return None
        data = dict(params or {})
        if not data and raw_body:
            data = {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw_body.decode("utf-8", "replace")).items()}
        if not data:
            return None
        got = (data.get("sign") or "").lower()
        if not got or got != _sign(data, S.epay_key()):
            return None
        if str(data.get("pid") or "") != S.epay_pid():
            # 别的商户号的回调。签名能对上只说明对方拿到了同一把 key,商户号不符
            # 仍然不是我们的单。单独打一行:core 那边对 None 一律报「验签失败」,
            # 不留痕的话排查时会盯着签名找半天。
            print(f"[bitapi] epay 回调 pid 不符 out_trade_no="
                  f"{data.get('out_trade_no') or '-'} pid={data.get('pid') or '-'}",
                  flush=True)
            return None
        out_trade_no = data.get("out_trade_no")
        if not out_trade_no:
            return None
        if (data.get("trade_status") or "").upper() not in ("TRADE_SUCCESS", "SUCCESS"):
            # 签名是真的,只是这笔没成功(用户取消、超时关闭)。要 ack,不要让渠道重试。
            return NOT_PAID
        try:
            paid_cny = float(data.get("money"))
        except (TypeError, ValueError):
            paid_cny = None
        return {"out_trade_no": out_trade_no,
                "trade_no": data.get("trade_no") or "",
                "amount_cny": paid_cny,
                "pid": data.get("pid") or ""}

    def query_order(self, out_trade_no):
        if not self._ready():
            return None
        import json
        import urllib.request
        try:
            # 走 POST 表单体:商户 KEY 拼进 GET query string 会落进访问日志和中间代理。
            # 组装也放进 try:key 里若有异常字符,.encode() 会在 try 外抛,
            # 那个异常会一路冒到对账循环里,只留一行「查单失败」。
            body = urllib.parse.urlencode({
                "act": "order", "pid": S.epay_pid(),
                "key": S.epay_key(), "out_trade_no": out_trade_no,
            }).encode()
            url = S.epay_api_url().rstrip("/") + "/api.php"
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=15) as r:
                # 限长:对方返回 HTML 错误页时别把整页读进内存
                data = json.loads(r.read(65536).decode("utf-8", "replace"))
        except Exception as e:
            # 打出来:裸 return None 会把「站点地址填错」伪装成「上游还没付款」,
            # 而那个查单请求带着商户密钥。异常消息本身不含 key(它在 POST 体里)。
            print(f"[bitapi] epay 查单失败 {out_trade_no}: {type(e).__name__}: {e}",
                  flush=True)
            return None
        return {"status": map_query_status(data), "raw": data}


register_provider(EpayProvider())
