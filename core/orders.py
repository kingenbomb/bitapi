#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单 —— 创建、回调处理、到账。

状态机: pending → paid → recharging → completed,旁支 expired / failed。

回调安全六层(缺一不可):
  1. provider.verify_notify 内验签(core 不知道各渠道签名规则)
  2. 渠道必须在启用列表里 + 单号的 provider 必须与回调入口一致
     (否则可以用弱渠道建单、拿单号去打强渠道的 notify)
  3. 金额闸门:实付人民币与下单时冻结的应收人民币比对,少付不到账
  4. out_trade_no 唯一索引(建单时)
  5. 状态条件更新:mark_order_paid 只在 pending/expired/failed 时生效,
     rowcount==0 即视为已处理(重放直接返回成功)
  6. 租约乐观锁:paid→recharging 抢占并递增 lease_version,
     completed 时校验版本,防并发重复到账

到账不直接改 balance,而是造一张内部兑换码再兑付 —— 复用兑换码的并发安全。

迟到的合法回调**不设时间上界**:签名伪造不出来,能进来的就是用户真付了钱,
所以 expired → paid 是允许的(tests/test_orders.py 的 test_expire_orders 锁了这条)。
"""
import secrets
import time

import config
from core.hooks import emit
from core.payments import NOT_PAID, get_provider
from core import site_settings as S
from core.redeem import redeem

# 人民币金额比对容差(元)。两位小数的域里,0.01 就是一分钱。
CNY_TOLERANCE = 0.01


def _reject(reason, provider_name, otn=None, extra=""):
    """拒绝一笔回调时留痕。不打回调原文 —— 里面有签名和可能的 PII。"""
    print(f"[bitapi] 支付回调拒绝 provider={provider_name} "
          f"out_trade_no={otn or '-'} reason={reason}"
          + (f" {extra}" if extra else ""), flush=True)


class OrderError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _out_trade_no():
    return f"PG{int(time.time())}{secrets.token_hex(4).upper()}"


def create_order(db, user, amount, provider_name):
    """建单并向渠道发起支付。返回 (order, pay_info)。"""
    try:
        amount = float(amount)
    except (TypeError, ValueError) as e:
        raise OrderError("INVALID_AMOUNT", "amount must be a number") from e
    if amount <= 0:
        raise OrderError("INVALID_AMOUNT", "amount must be positive")
    min_topup = S.min_topup()
    if amount < min_topup:
        raise OrderError("BELOW_MIN_TOPUP",
                         f"minimum topup is {min_topup:g} USD")
    provider = get_provider(provider_name)
    if provider is None or provider_name not in S.payment_providers():
        raise OrderError("PROVIDER_UNAVAILABLE", f"unknown provider: {provider_name}")

    now = int(time.time())
    for _ in range(5):  # out_trade_no 撞号重试
        otn = _out_trade_no()
        if db.get_order_by_trade_no(otn) is None:
            break
    else:
        raise OrderError("ORDER_ALLOC_FAILED", "failed to allocate order no")

    # 到账用的内部兑换码(与订单一一对应,幂等的物理载体)
    recharge_code = f"PAY-{otn}"
    oid = db.create_order(user["id"], otn, amount, provider_name,
                          expires_at=now + config.ORDER_TTL,
                          recharge_code=recharge_code)
    order = db.get_order(oid)
    try:
        pay_info = provider.create_payment(order)
    except Exception as e:
        # 渠道没接上就别留一张无主 pending 单在列表里污染统计
        db.fail_order(otn)
        raise OrderError("PROVIDER_ERROR", str(e)) from e
    # 冻结应收人民币:此后回调金额比对以这个值为准,改汇率不影响在途单
    cny = pay_info.get("amount_cny")
    if cny is not None:
        # 与配置无关的下限:0 元应收不是合法订单,不该有能落库的路径。
        # 冻结值一旦是 0(汇率填 0)或负数,金额闸门的比大小就恒成立,
        # 用户零成本拿全额额度 —— 所以这里拒绝建单,而不是留给闸门去挡。
        if not (float(cny) >= 0.01):
            db.fail_order(otn)
            raise OrderError("BAD_PAYABLE",
                             f"payable amount {cny} is not chargeable"
                             f" (check the exchange rate setting)")
        db.set_order_pay_amount_cny(otn, cny)
        order = db.get_order(oid)
    return order, pay_info


def handle_notify(db, provider_name, headers, raw_body, params=None):
    """处理支付回调。返回 (ok:bool, message:str)。

    ok=True 表示「已受理,别再重试」—— 包括未知订单、已完成、以及一笔正常失败的
    通知。只有验签失败和明确的拒绝(渠道不匹配、金额不符)才返回 False。
    真正的到账只在状态机允许时发生一次。
    """
    provider = get_provider(provider_name)
    if provider is None:
        _reject("unknown provider", provider_name)
        return False, f"unknown provider: {provider_name}"
    if provider_name not in S.payment_providers():
        # 建单侧查了这个,回调侧原先没查 —— 关掉一个渠道之后它的入账口还开着
        _reject("provider disabled", provider_name)
        return False, f"provider disabled: {provider_name}"
    try:
        verified = provider.verify_notify(headers, raw_body, params=params)
    except TypeError:  # provider 未实现 params 形参
        verified = provider.verify_notify(headers, raw_body)

    if verified is NOT_PAID:
        # 验签通过,只是这笔没成功。必须 ack,否则渠道会无限重试一笔正常失败的通知。
        return True, "success"
    if not verified:
        _reject("bad signature", provider_name)
        return False, "signature verification failed"

    otn = verified.get("out_trade_no")
    order = db.get_order_by_trade_no(otn)
    if order is None:
        _reject("unknown order", provider_name, otn)
        return True, "unknown order (ignored)"
    if order["provider"] != provider_name:
        # 用弱渠道建单、拿单号去打强渠道的 notify
        _reject("provider mismatch", provider_name, otn,
                f"order.provider={order['provider']}")
        return False, "provider mismatch"
    if order["status"] == "completed":
        return True, "already completed"

    # 金额闸门。只在两边都有人民币数时比对 —— 老库的在途单没有冻结值,
    # 拿 NULL 当 0 比会把它们全判成少付。
    want = order.get("pay_amount_cny")
    got = verified.get("amount_cny")
    if want is not None:
        # 冻结值存在但不是个能收钱的数(汇率曾被填成 0 / 负数 / nan,
        # 其中 nan 写进 REAL 列会被 SQLite 存成 NULL,所以这里也挡不住它 ——
        # 真正挡 nan 的是写入侧校验与建单时的 >= 0.01)。
        # 与「老库没冻结值」必须分开:那是历史兼容,这是数据写坏了,不能放行。
        if not (float(want) >= 0.01):
            _reject("bad payable", provider_name, otn, f"want={want}")
            return False, "bad payable amount"
        if got is None:
            # 订单有应收基准,渠道却不报实付 —— 无从校验,不放行。
            _reject("amount unverifiable", provider_name, otn,
                    f"want={float(want):.2f}")
            return False, "amount unverifiable"
        if float(got) + CNY_TOLERANCE < float(want):
            _reject("amount mismatch", provider_name, otn,
                    f"want={float(want):.2f} got={float(got):.2f}")
            return False, "amount mismatch"

    # 第 5 层:条件更新。已是 paid/recharging 时 rowcount=0,继续往下走完成流程
    db.mark_order_paid(otn, verified.get("trade_no"), verified.get("amount"))
    fulfil(db, otn)
    return True, "ok"


def fulfil(db, out_trade_no):
    """到账:租约抢占 → 造码兑付 → completed。可安全重试。"""
    order = db.get_order_by_trade_no(out_trade_no)
    if order is None or order["status"] == "completed":
        return False

    lease = db.acquire_order_lease(out_trade_no)
    if lease is None:
        # 别的执行流正在处理(recharging),或状态不是 paid
        return False

    order = db.get_order_by_trade_no(out_trade_no)
    user = db.get_user(order["user_id"])
    code = order.get("recharge_code") or f"PAY-{out_trade_no}"

    # 造码(幂等:已存在则复用)后兑付。兑付本身有 claim + credit 双重幂等。
    if db.get_code(code) is None:
        db.create_code(code, order["amount"], type="balance",
                       notes=f"order {out_trade_no}")
    existing = db.get_code(code)
    if existing["status"] == "unused":
        # notify=False:对外的事实是 order.paid,不再发 code.redeemed,
        # 否则返佣插件会对同一笔充值发放两次。
        redeem(db, code, user, reason="recharge",
               idem_key=f"recharge:{out_trade_no}",
               meta={"order_id": order["id"], "out_trade_no": out_trade_no},
               notify=False)

    db.complete_order(out_trade_no, lease)
    order = db.get_order_by_trade_no(out_trade_no)
    inviter_id = (user or {}).get("inviter_id")
    emit("order.paid", order=order, user=user,
         inviter=db.get_user(inviter_id) if inviter_id else None)
    return True


def poll_pending(db, limit=200):
    """主动查单兜底。返回补到账的笔数。

    扫描范围含 expired,不只是 pending:ORDER_TTL 默认 1800 秒而对账 600 秒一跳,
    用户扫码后去吃饭、四十分钟回来才付款,订单早已被翻成 expired ——
    只扫 pending 的话这种单永久失联,而钱是真付了的。
    """
    done = 0
    enabled = set(S.payment_providers())
    for status in ("pending", "expired"):
        for order in db.list_orders(status=status, limit=limit):
            if order["provider"] not in enabled:
                # 与 handle_notify 同一个开关:关掉渠道就该两条路一起关。
                # 少了这句,「关渠道」关得住回调却关不住查单补账,
                # 同一个开关在两条路上有两种解释。
                continue
            provider = get_provider(order["provider"])
            if provider is None:
                continue
            try:
                res = provider.query_order(order["out_trade_no"])
            except Exception as e:
                print(f"[bitapi] 查单失败 {order['out_trade_no']}: {e}", flush=True)
                continue
            if res and res.get("status") == "paid":
                db.mark_order_paid(order["out_trade_no"])
                if fulfil(db, order["out_trade_no"]):
                    done += 1
                    print(f"[bitapi] 查单补到账 {order['out_trade_no']}", flush=True)
    return done


# paid / recharging 停留超过这么久就算卡住。fulfil 本身是毫秒级的;留这么宽是给
# 「回调线程刚 mark_paid、fulfil 还没跑完」和「对账循环恰好同时扫到」让路。
STUCK_AFTER = 600


def recover_stuck(db, now=None, limit=200):
    """把卡在 paid / recharging 的单重新走完到账。返回 (补到账笔数, 仍卡住的订单列表)。

    poll_pending 只扫 pending/expired:一笔单在 mark_order_paid 之后、fulfil 之前
    进程死掉,状态就是 paid,钱收了、额度没到,而且再也没有任何路径会碰它 ——
    原先只有站长手点「查单」能救。这里不问上游(已经知道付了),直接 fulfil。
    仍完成不了的发 order.stuck 事件,由告警插件通知站长。
    """
    now = int(now if now is not None else time.time())
    db.release_stale_leases(now - STUCK_AFTER)
    recovered, stuck = 0, []
    for order in db.list_orders(status="paid", limit=limit):
        try:
            if fulfil(db, order["out_trade_no"]):
                recovered += 1
                print(f"[bitapi] 卡住的已付订单补到账 {order['out_trade_no']}", flush=True)
                continue
        except Exception as e:
            print(f"[bitapi] 订单 {order['out_trade_no']} 到账失败: {e}", flush=True)
        fresh = db.get_order_by_trade_no(order["out_trade_no"])
        if (fresh and fresh["status"] in ("paid", "recharging")
                and (fresh.get("paid_at") or now) < now - STUCK_AFTER):
            stuck.append(fresh)
    for order in stuck:
        emit("order.stuck", order=order, age=now - (order.get("paid_at") or now))
    return recovered, stuck


def reconcile_orders(db, now=None):
    """对账一跳:**先查上游、再救卡单、后写过期**。返回 (补到账笔数, 新过期笔数)。

    顺序是关键。反过来先过期再查单,同一跳里刚被翻成 expired 的单要等下一跳才被查,
    而 poll_pending 若只扫 pending 就永远等不到。

    这个函数是同步的、可直接调用的 —— 它就是 server.py 那个 async 循环里的订单部分,
    抽出来是为了能测:一个 async while True 里的逻辑没法写断言。
    """
    now = int(now if now is not None else time.time())
    credited = poll_pending(db)
    recovered, _ = recover_stuck(db, now)
    expired = db.expire_orders(now)
    return credited + recovered, expired
