#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兑换码 —— 生成与兑付。

并发安全就一句 SQL:`UPDATE ... WHERE code=? AND status='unused'` 判 rowcount,
SQLite 单写者天然够用,不需要 Redis 锁。顺序:先占用,后发放。

支付到账也复用这条路径(订单造一张内部码再兑付),一套并发安全代码服务两条路径。

码分两种用途,同一张表同一套占用逻辑:balance 是余额兑换码,invitation 是注册
邀请码 —— 后者不入账、面额恒 0,只在 /api/register 消耗一次(见 routers/portal.py)。
"""
import secrets
import string
import time

from core.credit import REASON_REDEEM, credit
from core.hooks import emit

_ALPHABET = string.ascii_uppercase + string.digits
MAX_BATCH = 1000

TYPE_BALANCE = "balance"
TYPE_INVITATION = "invitation"


class RedeemError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def gen_code(groups=4, size=4):
    """XXXX-XXXX-XXXX-XXXX 形式。"""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(size))
                    for _ in range(groups))


def generate_codes(db, count, value, expires_at=0, notes=None, type="balance"):
    """批量生成。返回码列表。value 允许负数(扣款/纠错)。

    零面额只对邀请码成立:一张 0 元的余额码兑出来什么也没发生,而两边都不报错。
    """
    count = int(count)
    if count < 1 or count > MAX_BATCH:
        raise RedeemError("INVALID_COUNT", f"count must be 1..{MAX_BATCH}")
    if type == TYPE_BALANCE and value == 0:
        raise RedeemError("INVALID_VALUE", "value must not be zero")
    out = []
    for _ in range(count):
        for _attempt in range(5):  # 撞码重试
            code = gen_code()
            if db.get_code(code) is None:
                db.create_code(code, value, type=type,
                               expires_at=expires_at, notes=notes)
                out.append(code)
                break
        else:
            raise RedeemError("CODE_ALLOC_FAILED", "failed to allocate unique code")
    return out


def redeem(db, code, user, reason=REASON_REDEEM, idem_key=None, meta=None,
           notify=True):
    """兑付一张码给用户。原子占用成功后走 credit 入账,再 emit code.redeemed。

    返回 (ledger_row, code_row)。失败抛 RedeemError。
    idem_key 默认 `redeem:<code>` —— 同一张码只可能入账一次(占用已保证),
    这里再加一道幂等是为了应对"占用成功但入账时进程崩溃"后的重试。

    notify=False 用于内部到账(支付订单的 recharge_code):此时对外的事实是
    "order.paid" 而非用户兑了一张码,发 code.redeemed 会让返佣插件重复发放。
    """
    code = (code or "").strip().upper()
    if not code:
        raise RedeemError("CODE_REQUIRED", "code is required")
    rec = db.get_code(code)
    if rec is None:
        raise RedeemError("CODE_NOT_FOUND", "invalid code")
    if rec["type"] == TYPE_INVITATION:
        # 邀请码只在注册时消耗。放它进兑换口,任何登录用户都能把待发的邀请码逐张
        # 换成 0 元:码没了、账上一分没多,两边都不报错,站长只会看到码莫名其妙用光。
        raise RedeemError("CODE_NOT_REDEEMABLE",
                          "invitation code is only usable at registration")
    if rec["status"] == "disabled":
        raise RedeemError("CODE_DISABLED", "code is disabled")
    if rec["status"] == "used":
        raise RedeemError("CODE_USED", "code already used")
    if rec.get("expires_at") and rec["expires_at"] < int(time.time()):
        raise RedeemError("CODE_EXPIRED", "code expired")

    if not db.claim_code(code, user["id"]):
        # 并发下被别人抢走(或刚好过期被禁用)
        raise RedeemError("CODE_USED", "code already used")

    rec = db.get_code(code)
    ledger, _created = credit(
        user["id"], rec["value"], reason, idem_key or f"redeem:{code}",
        meta=dict(meta or {}, code=code))
    if notify:
        emit("code.redeemed", code=rec, user=user,
             inviter=_inviter_of(db, user), ledger=ledger)
    return ledger, rec


def _inviter_of(db, user):
    inviter_id = (user or {}).get("inviter_id")
    return db.get_user(inviter_id) if inviter_id else None
