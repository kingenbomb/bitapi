#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
返佣插件 · 固定送额度 —— 每成功邀请一人,给邀请人固定额度(不看对方是否花钱)。

注意:这种形态容易被小号刷,建议配合邮箱验证或人工审核使用。
可选也给被邀请人一笔见面礼(双边奖励)。
"""
from core.credit import REASON_AFFILIATE, credit
from core.hooks import on

CONFIG = {
    "inviter_bonus": 1.0,       # 邀请人每邀一人得(美元)
    "invitee_bonus": 0.0,       # 被邀请人见面礼,0=不送
    "require_verified_email": False,  # True=仅在邮箱已验证时发放
    "max_invitees": 0,          # 每人最多计几次返佣,0=不限
}


@on("user.registered")
def on_registered(user, inviter, **_):
    if not inviter:
        return
    if CONFIG["require_verified_email"] and not user.get("email_verified_at"):
        return
    if CONFIG["max_invitees"]:
        from core.portal_state import USER_DB
        if USER_DB.count_invitees(inviter["id"]) > CONFIG["max_invitees"]:
            return
    if CONFIG["inviter_bonus"] > 0:
        credit(inviter["id"], CONFIG["inviter_bonus"], REASON_AFFILIATE,
               f"aff:invite:{user['id']}",
               meta={"kind": "fixed_invite", "source_user_id": user["id"]})
    if CONFIG["invitee_bonus"] > 0:
        credit(user["id"], CONFIG["invitee_bonus"], REASON_AFFILIATE,
               f"aff:welcome:{user['id']}",
               meta={"kind": "welcome", "inviter_id": inviter["id"]})
