#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端计费闭环:真实 uvicorn 子进程 + mock 支付 + 返佣插件。

  注册管理员 → 建 balance 组 → 注册被邀请人 → 下单 → 支付回调 → 余额到账
  → 邀请人收到返佣 → 建 key 调 /v1/chat/completions 扣费 → 流水可查
  → 回调重放 10 次只到账一次 → 余额清零后 402

不自动跑:占用 127.0.0.1:8123 且约 40 秒,跟单元测试放一起会拖慢每次改动。
tests/conftest.py 把本目录从默认收集里排除,显式点名才跑:

    python -m pytest tests/e2e -q      # 断言式
    python tests/e2e/test_billing_flow.py   # 逐步打印
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
MASTER = "sk-e2e-master"
ADMIN = "adm-e2e"
EPAY_PID = "20001"
EPAY_KEY = "e2e-epay-key-7c1f"

_fails = []


def check(label, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        _fails.append(label)


def req(method, path, body=None, token=None, key=None, raw=False):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    else:
        data = None
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if key:
        r.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(r, data, timeout=30) as resp:
            txt = resp.read().decode("utf-8", "replace")
            return resp.status, (txt if raw else json.loads(txt or "{}"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt or "{}")
        except Exception:
            return e.code, {"_text": txt}


def main():
    tmp = tempfile.mkdtemp(prefix="bitapi_e2e_")
    env = dict(os.environ)
    env.update({
        "BITAPI_DB": os.path.join(tmp, "e2e.db"),
        "BITAPI_GROK_AUTH_DIR": os.path.join(tmp, "nx"),
        "BITAPI_PORT": str(PORT),
        "BITAPI_HOST": "127.0.0.1",
        "BITAPI_API_KEY": MASTER,
        "BITAPI_ADMIN_KEY": ADMIN,
        "BITAPI_JWT_SECRET": "e2e-secret",
        # 本用例验证返佣关系；个人返佣码只在开放注册时用于归因。
        "BITAPI_REQUIRE_INVITE": "0",
        "BITAPI_PAYMENT_PROVIDERS": "mock,epay",
        "BITAPI_PLUGINS": "affiliate_percent",
        "BITAPI_SITE_URL": BASE,
        # epay 的测试凭据。不发外网:测试自己用同一把 key 算签名去打本站
        # /pay/notify/epay,这是把「签名对不上」纳入 CI 的唯一办法 ——
        # 那类错误在本机永远看不到,因为本机没有上游会给你发回调。
        "BITAPI_EPAY_URL": "https://pay.invalid",
        "BITAPI_EPAY_PID": EPAY_PID,
        "BITAPI_EPAY_KEY": EPAY_KEY,
        "BITAPI_EPAY_USD_RATE": "7.2",
        "BITAPI_PRICING_URL": "",                 # 不联网拉价目表
        "BITAPI_PRICING_PATH": os.path.join(tmp, "nopricing.json"),
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(os.path.join(tmp, "server.log"), "w+", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                s, _b = req("GET", "/health")
                if s == 200:
                    break
            except Exception:
                continue
        else:
            log.seek(0)
            print(log.read()[-3000:])
            raise SystemExit("server did not start")
        run()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
    print()
    if _fails:
        # 失败时留下 tmp:里面的 server.log 是唯一的上游线索
        print(f"FAILED {len(_fails)}: " + "; ".join(_fails))
        print(f"日志与库留在 {tmp}")
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL E2E CHECKS PASSED")
    return 0


def test_billing_flow():
    """pytest 入口:跑完整闭环,任何一步 FAIL 就把失败项列出来。

    main() 里每步都用 check() 记账而不是当场抛,这样一次跑完能看到全部问题;
    这里把账本翻译成一条断言,pytest 才收得到。
    """
    code = main()
    assert code == 0, "e2e 失败项: " + "; ".join(_fails)


def run():
    print("\n[1] 注册管理员(首个用户免邀请码)")
    s, b = req("POST", "/api/register",
               {"email": "admin@e2e.example.com", "password": "secret123"})
    check("首个用户注册成为 admin", s == 200 and b.get("role") == "admin", f"{s} {b}")
    admin_tok = b["token"]
    admin_aff = b["aff_code"]

    print("\n[2] 建 balance 计费组 + 定价($1/1M in, $2/1M out)")
    s, b = req("POST", "/api/admin/groups",
               {"name": "paid", "billing_policy": "balance",
                "supported_models": ["*"], "rpm_limit": 0,
                "rate_multiplier": 1.0}, token=admin_tok)
    check("建 balance 组", s == 200, f"{s} {b}")
    gid = b.get("id")
    s, b = req("POST", "/api/admin/pricing",
               {"model_pattern": "demo-*", "billing_mode": "token",
                "input_price": 1.0, "output_price": 2.0}, token=admin_tok)
    check("配定价 demo-*", s == 200, f"{s} {b}")

    print("\n[3] 开放注册下使用管理员返佣码")
    s, b = req("POST", "/api/register",
               {"email": "buyer@e2e.example.com", "password": "secret123",
                "invite_code": admin_aff})
    check("带返佣码注册成功", s == 200, f"{s} {b}")
    buyer_tok = b["token"]
    buyer_id = b["user_id"]
    s, b = req("PATCH", f"/api/admin/users/{buyer_id}", {"group_id": gid},
               token=admin_tok)
    check("被邀请人划入 paid 组", s == 200, f"{s} {b}")

    print("\n[4] 下单 $20 + mock 支付回调")
    s, b = req("POST", "/api/orders", {"amount": 20.0, "provider": "mock"},
               token=buyer_tok)
    check("下单成功", s == 200 and b.get("order", {}).get("status") == "pending",
          f"{s} {b}")
    pay_url = b["payment"]["pay_url"]
    order_id = b["order"]["id"]
    notify_path = pay_url[len(BASE):]
    s, txt = req("GET", notify_path, raw=True)
    check("支付回调返回 success", s == 200 and "success" in str(txt), f"{s} {txt}")

    s, b = req("GET", "/api/billing", token=buyer_tok)
    s2, led = req("GET", "/api/ledger", token=buyer_tok)
    bal = b.get("balance")
    check("余额到账 $20", abs((bal or 0) - 20.0) < 1e-6, f"balance={bal}")
    check("流水首条 reason=recharge",
          led.get("entries") and led["entries"][0]["reason"] == "recharge",
          str(led.get("entries", [])[:1]))
    s, o = req("GET", "/api/orders", token=buyer_tok)
    check("订单转 completed",
          o.get("orders") and o["orders"][0]["status"] == "completed",
          str(o.get("orders", [])[:1]))

    print("\n[5] 返佣落到邀请人(affiliate_percent 20%)")
    s, al = req("GET", f"/api/admin/ledger?user_id=1", token=admin_tok)
    aff = [e for e in al.get("entries", []) if e["reason"] == "affiliate"]
    check("邀请人收到 1 笔返佣 $4", len(aff) == 1 and abs(aff[0]["amount"] - 4.0) < 1e-6,
          str(aff))

    print("\n[6] 回调重放 10 次 → 只到账一次、只返佣一次")
    for _ in range(10):
        req("GET", notify_path, raw=True)
    s, b = req("GET", "/api/billing", token=buyer_tok)
    check("重放后余额仍 $20", abs((b.get("balance") or 0) - 20.0) < 1e-6,
          f"balance={b.get('balance')}")
    s, al = req("GET", "/api/admin/ledger?user_id=1", token=admin_tok)
    aff = [e for e in al.get("entries", []) if e["reason"] == "affiliate"]
    check("重放后返佣仍 1 笔", len(aff) == 1, f"{len(aff)} 笔")

    print("\n[7] 兑换码:管理员生成 → 被邀请人兑付 $5(返佣再 +$1)")
    s, b = req("POST", "/api/admin/codes", {"count": 2, "value": 5.0},
               token=admin_tok)
    check("生成 2 个兑换码", s == 200 and len(b.get("codes", [])) == 2, f"{s} {b}")
    code = b["codes"][0]
    s, b = req("POST", "/api/redeem", {"code": code}, token=buyer_tok)
    check("兑付成功", s == 200, f"{s} {b}")
    s, b = req("POST", "/api/redeem", {"code": code}, token=buyer_tok)
    check("同码二次兑付被拒", s >= 400, f"{s} {b}")
    s, b = req("GET", "/api/billing", token=buyer_tok)
    check("余额 $25", abs((b.get("balance") or 0) - 25.0) < 1e-6,
          f"balance={b.get('balance')}")
    s, al = req("GET", "/api/admin/ledger?user_id=1", token=admin_tok)
    aff = [e for e in al.get("entries", []) if e["reason"] == "affiliate"]
    check("兑码也返佣(共 2 笔 = $5)",
          len(aff) == 2 and abs(sum(e["amount"] for e in aff) - 5.0) < 1e-6, str(aff))

    print("\n[8] 建 API key → 扣费(管理员调额把余额压到可预测值)")
    s, b = req("POST", "/api/keys", {"name": "e2e"}, token=buyer_tok)
    check("创建 API key", s == 200 and str(b.get("key", "")).startswith("sk-"),
          f"{s} {list(b)}")
    user_key = b["key"]
    # 把余额调成 -25 + 10 = 10 便于算账? 直接记录当前值,调用后比对差额
    s, b = req("GET", "/api/billing", token=buyer_tok)
    before = b["balance"]
    s, b = req("POST", "/v1/chat/completions",
               {"model": "demo-nonexistent-model",
                "messages": [{"role": "user", "content": "hi"}]}, key=user_key)
    # 模型不存在 → 网关 4xx/5xx,但鉴权+预检必须先通过(不能 401/402)
    check("有余额时鉴权与预检通过(非 401/402)", s not in (401, 402), f"{s} {b}")

    print("\n[9] 余额清零 → 402 INSUFFICIENT_BALANCE")
    s, b = req("POST", f"/api/admin/users/{buyer_id}/balance",
               {"amount": -before, "notes": "e2e drain"}, token=admin_tok)
    check("管理员调额清零", s == 200 and abs(b.get("balance", 1)) < 1e-6, f"{s} {b}")
    time.sleep(0.3)  # 等鉴权缓存失效
    s, b = req("POST", "/v1/chat/completions",
               {"model": "demo-nonexistent-model",
                "messages": [{"role": "user", "content": "hi"}]}, key=user_key)
    detail = b.get("detail") if isinstance(b, dict) else {}
    check("余额为 0 时返回 402", s == 402, f"{s} {b}")
    check("402 带 INSUFFICIENT_BALANCE 码",
          isinstance(detail, dict) and detail.get("code") == "INSUFFICIENT_BALANCE",
          str(detail))

    print("\n[10] 管理员调额也进流水")
    s, led = req("GET", "/api/ledger", token=buyer_tok)
    reasons = [e["reason"] for e in led.get("entries", [])]
    check("流水含 admin 调额", "admin" in reasons, str(reasons))
    check("流水含 recharge 与 redeem",
          "recharge" in reasons and "redeem" in reasons, str(reasons))

    _epay_phase(buyer_tok, buyer_id, admin_tok)


def _epay_sign(params):
    """与 payments/epay.py 同口径:排除 sign/sign_type 与空值,按 key 升序,末尾接 key。"""
    items = sorted((k, v) for k, v in params.items()
                   if k not in ("sign", "sign_type") and v not in (None, ""))
    raw = "&".join(f"{k}={v}" for k, v in items) + EPAY_KEY
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _epay_notify(otn, money, status="TRADE_SUCCESS", pid=EPAY_PID):
    p = {"pid": pid, "out_trade_no": otn, "trade_no": "E-" + otn[-6:],
         "type": "alipay", "name": "topup-" + otn, "money": money,
         "trade_status": status}
    p["sign"] = _epay_sign(p)
    p["sign_type"] = "MD5"
    return "/pay/notify/epay?" + urllib.parse.urlencode(p)


def _epay_phase(buyer_tok, buyer_id, admin_tok):
    """真渠道那条路:签名、金额闸门、失败通知的响应契约。

    不发外网 —— 本地用同一把 key 算签名打自己的 notify 端点。生产上真正会出问题的
    三件事(签名对不上、少付、失败通知被无限重试)只有这一段守着。
    """
    print("\n[11] epay 下单:应收人民币冻进订单行")
    s, b = req("POST", "/api/orders", {"amount": 10.0, "provider": "epay"},
               token=buyer_tok)
    otn = (b.get("order") or {}).get("out_trade_no")
    check("epay 下单成功", s == 200 and otn, f"{s} {b}")
    check("pay_url 指向配置的易支付站点",
          "pay.invalid/submit.php" in ((b.get("payment") or {}).get("pay_url") or ""),
          str((b.get("payment") or {}).get("pay_url"))[:120])
    check("冻结应收人民币 = 10 × 7.2",
          abs(((b.get("order") or {}).get("pay_amount_cny") or 0) - 72.0) < 1e-6,
          str((b.get("order") or {}).get("pay_amount_cny")))
    if not otn:
        return

    print("\n[12] 篡改签名 → 不到账")
    bad = _epay_notify(otn, "72.00")
    bad = bad.replace("sign=" + bad.split("sign=")[1][:32], "sign=" + "0" * 32)
    s, txt = req("GET", bad, raw=True)
    check("坏签名不返回 success", "success" not in str(txt).lower(), f"{s} {txt}")
    s, b = req("GET", "/api/orders/" + otn, token=buyer_tok)
    check("订单仍未支付", (b.get("order") or {}).get("status") == "pending",
          str((b.get("order") or {}).get("status")))

    print("\n[13] 少付 → 金额闸门拦住")
    s, txt = req("GET", _epay_notify(otn, "0.01"), raw=True)
    check("少付不返回 success", "success" not in str(txt).lower(), f"{s} {txt}")
    s, b = req("GET", "/api/orders/" + otn, token=buyer_tok)
    check("少付后订单仍未支付", (b.get("order") or {}).get("status") == "pending",
          str((b.get("order") or {}).get("status")))

    print("\n[14] 别的商户号 → 拒绝")
    s, txt = req("GET", _epay_notify(otn, "72.00", pid="99999"), raw=True)
    check("外来 pid 不返回 success", "success" not in str(txt).lower(), f"{s} {txt}")

    print("\n[15] 支付失败通知 → 必须 ack(否则渠道无限重试)")
    s, txt = req("GET", _epay_notify(otn, "72.00", status="TRADE_CLOSED"), raw=True)
    check("失败通知回 success", "success" in str(txt).lower(), f"{s} {txt}")
    s, b = req("GET", "/api/orders/" + otn, token=buyer_tok)
    check("失败通知不改状态", (b.get("order") or {}).get("status") == "pending",
          str((b.get("order") or {}).get("status")))

    print("\n[16] 足额 + 正确签名 → 到账一次")
    s, before = req("GET", "/api/billing", token=buyer_tok)
    bal0 = before.get("balance") or 0
    s, txt = req("GET", _epay_notify(otn, "72.00"), raw=True)
    check("回调返回 success", "success" in str(txt).lower(), f"{s} {txt}")
    s, b = req("GET", "/api/orders/" + otn, token=buyer_tok)
    check("订单转 completed", (b.get("order") or {}).get("status") == "completed",
          str((b.get("order") or {}).get("status")))
    s, after = req("GET", "/api/billing", token=buyer_tok)
    check("余额 +$10", abs((after.get("balance") or 0) - bal0 - 10.0) < 1e-6,
          f"{bal0} → {after.get('balance')}")

    print("\n[17] 重放 5 次仍只到账一次")
    for _ in range(5):
        req("GET", _epay_notify(otn, "72.00"), raw=True)
    s, again = req("GET", "/api/billing", token=buyer_tok)
    check("重放后余额不变", abs((again.get("balance") or 0)
                          - (after.get("balance") or 0)) < 1e-6,
          str(again.get("balance")))

    print("\n[18] 跨渠道:拿 epay 单号去打 mock 的 notify")
    s, b = req("POST", "/api/orders", {"amount": 10.0, "provider": "epay"},
               token=buyer_tok)
    otn2 = (b.get("order") or {}).get("out_trade_no")
    s, txt = req("GET", f"/pay/notify/mock?out_trade_no={otn2}&trade_no=X&amount=10",
                 raw=True)
    check("跨渠道不返回 success", "success" not in str(txt).lower(), f"{s} {txt}")
    s, b = req("GET", "/api/orders/" + otn2, token=buyer_tok)
    check("跨渠道后订单仍未支付", (b.get("order") or {}).get("status") == "pending",
          str((b.get("order") or {}).get("status")))

    print("\n[19] 单笔最低充值:服务端硬限")
    s, b = req("POST", "/api/orders", {"amount": 0.01, "provider": "epay"},
               token=buyer_tok)
    detail = b.get("detail") if isinstance(b, dict) else {}
    check("低于最低额被拒 400", s == 400, f"{s} {b}")
    check("错误码 BELOW_MIN_TOPUP",
          isinstance(detail, dict) and detail.get("code") == "BELOW_MIN_TOPUP",
          str(detail))

    print("\n[20] 订单归属:别人的单查不到")
    s, b = req("GET", "/api/orders/" + otn, token=admin_tok)
    check("管理员用用户端接口读他人订单 → 404", s == 404, f"{s} {b}")


if __name__ == "__main__":
    sys.exit(main())
