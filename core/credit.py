#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
余额变动的唯一入口 —— credit()。

充值到账、兑码、返佣、管理员调额、消费扣费全部走这里,写入 credit_ledger 并
同事务增量更新 users.balance。幂等由 idem_key 唯一索引保证:插件作者调 credit()
即安全,不必自己处理重复回调与并发。

  credit(user_id, amount, reason, idem_key)   amount 正=入账 负=扣费
  → (ledger_row, created)  created=False 表示该 idem_key 已记账过,本次为空操作

写入成功后 emit("balance.changed")。
"""
from core.hooks import emit

# 保留原因(约定,非强制):插件可自定义任意 reason 字符串
REASON_RECHARGE = "recharge"    # 支付充值到账
REASON_REDEEM = "redeem"        # 兑换码
REASON_AFFILIATE = "affiliate"  # 返佣
REASON_ADMIN = "admin"          # 管理员调额
REASON_SIGNUP = "signup"        # 注册赠额(config.SIGNUP_BONUS)
REASON_CHECKIN = "checkin"      # 每日签到随机赠额
REASON_PLAN = "plan"            # 购买/续费订阅套餐(时长卡)
REASON_USAGE = "usage"          # API 调用扣费

_DB = None


def bind(user_db):
    """绑定 UserDB 实例(启动时由 portal_state 调用)。"""
    global _DB
    _DB = user_db


def credit(user_id, amount, reason, idem_key, meta=None):
    """记一笔余额变动。同 idem_key 重复调用只生效一次。

    返回 (ledger_row, created)。amount 为 0 时仍会记录(便于留痕),
    但不发 balance.changed。
    """
    if _DB is None:
        raise RuntimeError("credit not bound to a UserDB; call credit.bind(db) first")
    if not idem_key:
        raise ValueError("idem_key is required for idempotency")
    row, created = _DB.apply_credit(user_id, float(amount), reason, idem_key, meta)
    if created and amount:
        emit("balance.changed", user_id=user_id, delta=float(amount),
             reason=reason, balance_after=row.get("balance_after"), ledger=row)
    return row, created


def balance_of(user_id):
    user = _DB.get_user(user_id) if _DB else None
    return (user or {}).get("balance") or 0.0
