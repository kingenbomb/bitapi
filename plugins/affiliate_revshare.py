#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
返佣插件 · 消费分成 —— 被邀请人每次调用 API 消费,按比例持续分给邀请人。

与比例返现的区别:返现只在充值那一刻发生一次;消费分成会随对方长期使用持续产生。
注意分成是"额外支出",不是从对方消费里扣 —— 相当于你为拉新支付的持续成本。
"""
from core.credit import REASON_AFFILIATE, credit
from core.hooks import on

CONFIG = {
    "rate": 0.05,          # 按被邀请人实扣金额的比例分成
    "min_payout": 0.000001,  # 低于此金额不记账(避免大量 0 值流水)
}


@on("usage.recorded")
def on_usage(log_id, user, actual_cost, **_):
    if not actual_cost or actual_cost <= 0:
        return
    inviter_id = (user or {}).get("inviter_id")
    if not inviter_id:
        return
    amount = actual_cost * CONFIG["rate"]
    if amount < CONFIG["min_payout"]:
        return
    credit(inviter_id, amount, REASON_AFFILIATE, f"aff:usage:{log_id}",
           meta={"kind": "revshare", "log_id": log_id,
                 "source_user_id": user["id"], "rate": CONFIG["rate"]})
