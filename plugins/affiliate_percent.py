#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
返佣插件 · 比例返现 —— 被邀请人充值/兑码时,按比例给邀请人真实余额。

改 CONFIG 即可调整;不想要就从 BITAPI_PLUGINS 里移除。
"""
from core.credit import REASON_AFFILIATE, credit
from core.hooks import on

CONFIG = {
    "rate": 0.20,          # 返佣比例
    "min_amount": 0.0,     # 低于此金额不返
    "max_per_event": 0.0,  # 单笔返佣上限,0=不限
    "on_redeem": True,     # 兑换码入账是否也返佣(防止绕过支付逃返佣)
    "first_only": False,   # 只返首充
}


def _payout(inviter, source_user, base_amount, idem_key, meta):
    if not inviter or base_amount is None:
        return
    if base_amount < CONFIG["min_amount"]:
        return
    amount = base_amount * CONFIG["rate"]
    cap = CONFIG["max_per_event"]
    if cap and amount > cap:
        amount = cap
    if amount <= 0:
        return
    credit(inviter["id"], amount, REASON_AFFILIATE, idem_key,
           meta=dict(meta, rate=CONFIG["rate"],
                     source_user_id=(source_user or {}).get("id")))


def _is_first_recharge(db, user_id, exclude_key):
    """该用户此前是否已有过入账(充值/兑码)。用于 first_only。"""
    rows = db.list_ledger(user_id, limit=1000)
    prior = [r for r in rows
             if r["reason"] in ("recharge", "redeem") and r["idem_key"] != exclude_key]
    return not prior


@on("order.paid")
def on_order_paid(order, user, inviter, **_):
    if CONFIG["first_only"]:
        from core.portal_state import USER_DB
        if not _is_first_recharge(USER_DB, user["id"],
                                  f"recharge:{order['out_trade_no']}"):
            return
    _payout(inviter, user, order.get("amount"),
            idem_key=f"aff:order:{order['id']}",
            meta={"order_id": order["id"], "kind": "recharge"})


@on("code.redeemed")
def on_code_redeemed(code, user, inviter, **_):
    if not CONFIG["on_redeem"]:
        return
    value = code.get("value") or 0
    if value <= 0:          # 负值码是扣款/纠错,不返佣
        return
    _payout(inviter, user, value,
            idem_key=f"aff:code:{code['id']}",
            meta={"code_id": code["id"], "kind": "redeem"})
