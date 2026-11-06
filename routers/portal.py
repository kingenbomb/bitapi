#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户接入层 REST API(/api/*)—— 注册/登录/资料/密钥/用量/计费/钱包/兑码/订单/管理端。

社区专用注册按后台最低社区等级校验，关闭邮箱建号。独立部署的邮箱注册模式
强制邀请码(settings.require_invite 覆盖 config.REQUIRE_INVITE)时,invite_code
只接受管理台发的一次性注册邀请码(invitation 型兑换码)。老用户的 aff_code 仅在
开放注册时用于绑定返佣关系,不能绕过邀请制。首个用户(库空)免码成为 admin。
浏览器用 HttpOnly cookie 会话与 CSRF token；脚本继续用 JWT Bearer。
与网关 /v1/* 的 sk- key 鉴权分离。
"""
import asyncio
import hashlib
import ipaddress
import os
import secrets
import threading
import time
from datetime import date as _date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from core.credit import publish_balance_change

import config
from core import auth
from core.clock import SITE_TZ, day_start as site_day_start
from core import adapter as adapter_mod
from core import channels as channels_mod
from core import community_auth
from core import mailer
from core import model_status
from core import pool_state
from core import share as share_mod
from core import orders as orders_mod
from core import pricing_catalog
from core.registration import primary_email
from core import throttle
from core import totp
from core.credit import (REASON_ADMIN, REASON_CHECKIN, REASON_PLAN,
                         REASON_SIGNUP, credit)
from core.hooks import emit
from core import site_settings as S
from core.payments import all_providers, get_provider
from core.portal_state import (BILLING, PRICING, USER_DB,
                               apply_channel_switches, ensure_default_group)
from core.redeem import (TYPE_BALANCE, TYPE_INVITATION, RedeemError,
                         generate_codes, redeem)
from core.user_db import (CommunityAffCodeConflict, CommunityEmailConflict,
                          CommunityFlowError, CommunityIdentityConflict,
                          CommunityInviteError, CommunityLevelError, CommunityUserDisabled,
                          RegistrationCodeCooldown, RegistrationCodeError)

from core.session import SessionRoute, clear_session

# router 与共用依赖/模型都在 portal_shared —— 那里建一次 /api 实例,本文件和管理端
# 子模块往同一个实例上注册。这里**不要**再新建 APIRouter。
from routers.portal_shared import (AdminResetPwReq, GroupReq, GroupUpdateReq,
                                   OTHER_SETTING_KEYS, PAY_KEYS, SettingsReq,
                                   UserAdminReq, _ANN_KEY, _ANN_LEVELS, _ANN_MODES,
                                   _CSV_MAX, _RANGE_SPAN, _ann_all, _ann_find,
                                   _ann_lock, _ann_sorted, _ann_visible,
                                   _channel_names, _live_usage_snapshot,
                                   _usage_csv, _usage_filters, current_user,
                                   optional_user, require_admin, router)
# 管理端路由挂在同一个 router 上。用 import 语句(而不是 from ... import name)
# 触发注册:后者要等目标模块执行完才能取属性,而在本模块导入途中触发时,
# Python 会把它当循环导入,导致 portal_admin 的装饰器注册被跳过。
from routers import portal_admin as _portal_admin  # noqa: E402,F401


@router.post("/logout")
def logout(response: Response, request: Request):
    clear_session(response, request)
    return {"ok": True}


# ---- 依赖:JWT 当前用户 ----





@router.get("/admin/audit")
def admin_audit(limit: int = 100, offset: int = 0, user=Depends(require_admin)):
    return {"rows": USER_DB.list_admin_audit(limit, offset)}




# ---- 请求体 ----

class RegisterReq(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    invite_code: str = ""
    verification_code: str = Field(pattern=r"^[0-9]{6}$")


class RegistrationCodeReq(BaseModel):
    email: EmailStr


class LoginReq(BaseModel):
    email: EmailStr
    password: str


class CommunityStartReq(BaseModel):
    purpose: str = Field(pattern="^(login|bind)$")
    referral_code: str = Field(default="", max_length=64)


class KeyCreateReq(BaseModel):
    name: str = Field(default="", max_length=64)
    count: int = Field(default=1, ge=1, le=50)
    expires_at: int = 0          # unix 秒,0=永不过期
    quota: float = Field(default=0, ge=0, allow_inf_nan=False)     # 本密钥消费上限(美元),0=不限
    allowed_models: list[str] | None = None   # 空/None=跟随分组
    allowed_ips: list[str] | None = None      # 空/None=不限


class KeyUpdateReq(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    status: str | None = None
    expires_at: int | None = None
    quota: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allowed_models: list[str] | None = None
    allowed_ips: list[str] | None = None


class PasswordChangeReq(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class ProfileReq(BaseModel):
    """昵称与头像。avatar 传空串表示删除,传 None 表示不改这一项。"""
    display_name: str | None = Field(default=None, max_length=32)
    # data URI 上限 ~28KB base64,对应约 20KB 原图;再大就退回 400。
    avatar: str | None = Field(default=None, max_length=30000)


class EmailBindReq(BaseModel):
    email: EmailStr


class EmailVerifyReq(BaseModel):
    code: str = Field(max_length=64)


# ---- 认证 ----

@router.get("/registration-policy")
def registration_policy():
    return {"community_only": config.COMMUNITY_REGISTRATION_ONLY,
            "community_min_level": S.community_min_level(),
            "closed": _registration_closed()}


def _registration_closed():
    """运行时优先:settings.registration_closed 覆盖 config.REGISTRATION_CLOSED。"""
    v = USER_DB.get_setting("registration_closed")
    return config.REGISTRATION_CLOSED if v is None else bool(v)


def _reject_closed_registration():
    """站点已关闭注册时拒绝建号。

    只拦「新建账号」,不拦「已有账号登录 / 绑定社区」—— 关注册是把门关小,
    不是把老用户一起关在门外。
    """
    if _registration_closed():
        raise HTTPException(
            status_code=403,
            detail="站点已关闭注册，如需账号请联系站长")


def _require_email_registration():
    _reject_closed_registration()
    if config.COMMUNITY_REGISTRATION_ONLY:
        raise HTTPException(
            status_code=403,
            detail=f"请使用白嫖社区账号注册，需达到 Lv{S.community_min_level()} 及以上，无需邀请码")


def _require_invite():
    """运行时优先:settings.require_invite 覆盖 config.REQUIRE_INVITE。"""
    v = USER_DB.get_setting("require_invite")
    return config.REQUIRE_INVITE if v is None else bool(v)


def _alloc_aff_code():
    for _ in range(10):
        aff = auth.generate_aff_code()
        if not USER_DB.get_user_by_aff_code(aff):
            return aff
    raise HTTPException(status_code=500, detail="failed to allocate invite code")


def _publish_registered_invite_reward(user_id):
    row = USER_DB.get_ledger_by_idem(f"aff:invite:{user_id}")
    if row and row["amount"]:
        publish_balance_change(row)


def _throttled(e):
    """AuthThrottled → 429。带 Retry-After 头,前端据此显示「N 秒后再试」。
    同时发 auth.throttled:有人在撞库是站长该知道的事,告警插件按 key 去重后推。"""
    emit("auth.throttled", scope=e.scope, key=e.key, retry_after=e.retry_after)
    return HTTPException(
        status_code=429,
        detail={"code": "AUTH_RATE_LIMITED", "scope": e.scope,
                "retry_after": e.retry_after,
                "message": f"操作过于频繁,请 {e.retry_after} 秒后再试"},
        headers={"Retry-After": str(e.retry_after)})


def _registration_email(value):
    try:
        return primary_email(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/register/code")
def registration_code(req: RegistrationCodeReq, request: Request):
    _require_email_registration()
    email = _registration_email(req.email)
    if USER_DB.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="站点未配置邮件服务，请联系站长")
    ip = throttle.client_ip(request)
    for limiter, key, scope in ((throttle.REGISTER_MAIL_IP, ip, "registration_mail_ip"),
                                (throttle.REGISTER_MAIL_EMAIL, email, "registration_mail_email")):
        if key and limiter.hit(key):
            raise _throttled(throttle.AuthThrottled(scope, limiter.retry_after(key), key))
    code = auth.generate_email_code()
    code_hash = auth.hash_password(code)
    try:
        USER_DB.reserve_registration_code(email, code_hash)
    except RegistrationCodeCooldown as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after)}) from e
    subject, text, html = mailer.verify_email_mail(S.site_name(), code, 15)
    try:
        mailer.send(email, subject, text, html)
    except mailer.MailError as e:
        USER_DB.finish_registration_mail(email, code_hash, False)
        raise HTTPException(status_code=502, detail="验证码邮件发送失败，请稍后重试") from e
    USER_DB.finish_registration_mail(email, code_hash, True)
    return {"ok": True, "sent": True, "retry_after": 60, "expires_in": 900}


@router.post("/register")
def register(req: RegisterReq, request: Request = None):
    _require_email_registration()
    email = _registration_email(req.email)
    ip = throttle.client_ip(request)
    try:
        throttle.check((throttle.REGISTER_IP, ip, "register"))
    except throttle.AuthThrottled as e:
        raise _throttled(e) from e
    if USER_DB.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="email already registered")

    group = ensure_default_group()
    aff = _alloc_aff_code()
    try:
        uid, role, inviter_id = USER_DB.register_verified_user(
            email, req.verification_code, auth.hash_password(req.password), aff,
            group["id"] if group else None, auth.normalize_aff_code(req.invite_code),
            _require_invite())
    except RegistrationCodeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except CommunityInviteError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except CommunityEmailConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    # 注册赠额。放在 emit 之前:插件可能在 user.registered 里读余额,
    # 先记账再广播,插件看到的就是最终值。
    if config.SIGNUP_BONUS:
        credit(uid, config.SIGNUP_BONUS, REASON_SIGNUP, f"signup:{uid}",
               meta={"source": "register"})
    _publish_registered_invite_reward(uid)
    emit("user.registered", user=USER_DB.get_user(uid),
         inviter=USER_DB.get_user(inviter_id) if inviter_id else None)
    # 注册成功才计数:被邀请码挡回去的请求不该消耗额度,否则填错一次码就少一次机会
    throttle.REGISTER_IP.hit(ip)
    token = auth.issue_token(uid, role=role)
    return {"ok": True, "user_id": uid, "token": token, "role": role,
            "aff_code": aff}


@router.post("/login")
def login(req: LoginReq, request: Request = None):
    email = req.email.lower().strip()
    ip = throttle.client_ip(request)
    try:
        throttle.check((throttle.LOGIN_IP, ip, "login"),
                       (throttle.LOGIN_EMAIL, email, "login"))
    except throttle.AuthThrottled as e:
        raise _throttled(e) from e
    user = USER_DB.get_user_by_email(email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        # 失败才记账,IP 与邮箱各记一笔。不存在的邮箱也记:否则攻击者能靠
        # 「不限速 = 邮箱不存在」的差异枚举出哪些邮箱注册过。
        throttle.LOGIN_IP.hit(ip)
        throttle.LOGIN_EMAIL.hit(email)
        raise HTTPException(status_code=401, detail="invalid credentials")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="account disabled")
    throttle.LOGIN_EMAIL.reset(email)
    if user.get("totp_secret"):
        # 开了 TOTP:密码对了只换一张 5 分钟的受限票,不是会话。第二步拿票 + 验证码换会话。
        ticket = auth.issue_token(user["id"], role=user["role"],
                                  ttl=auth.TOTP_TICKET_TTL, scope="totp")
        return {"ok": False, "needs_totp": True, "ticket": ticket}
    token = auth.issue_token(user["id"], role=user["role"])
    return {"ok": True, "token": token, "role": user["role"]}


class TotpLoginReq(BaseModel):
    ticket: str
    code: str = Field(min_length=6, max_length=8)


@router.post("/login/totp")
def login_totp(req: TotpLoginReq, request: Request = None):
    """TOTP 第二步。失败与登录失败同一套限速记账:6 位码一百万种,没有限速就是可以
    穷举的;同一时间步的码只能用一次,防抓包重放。"""
    payload = auth.decode_token(req.ticket)
    if not payload or payload.get("scope") != "totp":
        raise HTTPException(status_code=401, detail="ticket invalid or expired, log in again")
    user = USER_DB.get_user(int(payload["sub"]))
    if not user or user["status"] != "active" or not user.get("totp_secret"):
        raise HTTPException(status_code=401, detail="ticket invalid or expired, log in again")
    email = user["email"]
    ip = throttle.client_ip(request)
    try:
        throttle.check((throttle.LOGIN_IP, ip, "login"), (throttle.LOGIN_EMAIL, email, "login"))
    except throttle.AuthThrottled as e:
        raise _throttled(e) from e
    step = totp.verify(user["totp_secret"], req.code, last_step=user.get("totp_last_step") or 0)
    if step is None:
        throttle.LOGIN_IP.hit(ip)
        throttle.LOGIN_EMAIL.hit(email)
        raise HTTPException(status_code=401, detail="验证码不正确")
    USER_DB.update_user(user["id"], totp_last_step=step)
    throttle.LOGIN_EMAIL.reset(email)
    return {"ok": True, "token": auth.issue_token(user["id"], role=user["role"]),
            "role": user["role"]}


# ---- 白嫖社区登录 / 绑定 ----

def _set_community_flow_cookie(response, nonce):
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        community_auth.FLOW_COOKIE, nonce,
        max_age=community_auth.FLOW_TTL, path="/", httponly=True,
        secure=community_auth.cookie_secure(), samesite="lax")


def _clear_community_flow_cookie(response):
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        community_auth.FLOW_COOKIE, path="/", httponly=True,
        secure=community_auth.cookie_secure(), samesite="lax")


def _public_community(rec):
    return {"username": rec.get("username") or "",
            "name": rec.get("name") or rec.get("username") or "",
            "email": rec.get("email") or ""}


@router.post("/community/auth/start")
def community_auth_start(req: CommunityStartReq, response: Response,
                         user=Depends(optional_user)):
    if req.purpose == "bind" and not user:
        raise HTTPException(status_code=401, detail="login required for binding")
    state = community_auth.new_secret()
    nonce = community_auth.new_secret()
    try:
        # 私有 HttpOnly flow cookie 同时持有 PKCE verifier，数据库仅保存其摘要。
        authorize_url = community_auth.authorize_url(state, nonce)
    except community_auth.CommunityAuthError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    now = int(time.time())
    USER_DB.create_community_flow(
        community_auth.digest(state), community_auth.digest(nonce),
        req.purpose, user["id"] if user else None,
        now + community_auth.FLOW_TTL,
        referral_code=auth.normalize_aff_code(req.referral_code) if req.purpose == "login" else "")
    _set_community_flow_cookie(response, nonce)
    return {"ok": True, "authorize_url": authorize_url}


def community_oauth_callback(request: Request, code="", state="", error=""):
    """生产登记的无 `/api` callback；由 server.py 的公开路由调用。"""
    nonce = request.cookies.get(community_auth.FLOW_COOKIE, "")
    if (not code and not error) or not state or not nonce:
        raise HTTPException(status_code=400,
                            detail="community authorization state is missing")
    now = int(time.time())
    flow = USER_DB.claim_community_flow(
        community_auth.digest(state), community_auth.digest(nonce), now)
    if not flow:
        raise HTTPException(status_code=400,
                            detail="community authorization state is invalid or expired")
    if error:
        # 不透传上游错误正文；拒绝授权仍校验并消费 state，返回可重试的页面。
        reason = "access_denied" if error == "access_denied" else "failed"
        response = RedirectResponse("/portal#/community-callback?error=" + reason, status_code=302)
        _clear_community_flow_cookie(response)
        return response
    try:
        upstream_token = community_auth.exchange_code(code, nonce)
        profile = community_auth.get_userinfo(upstream_token)
    except community_auth.CommunityAuthError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not USER_DB.ready_community_flow(
            flow["id"], profile, now + community_auth.FLOW_TTL):
        raise HTTPException(status_code=400,
                            detail="community authorization flow was already used")
    response = RedirectResponse("/portal#/community-callback", status_code=302)
    # callback 到 finish 再给完整五分钟；原 nonce 仍只存在 HttpOnly cookie。
    _set_community_flow_cookie(response, nonce)
    return response


@router.post("/community/auth/finish")
def community_auth_finish(request: Request,
                          response: Response,
                          user=Depends(optional_user)):
    nonce = request.cookies.get(community_auth.FLOW_COOKIE, "")
    if not nonce:
        raise HTTPException(status_code=400,
                            detail="community authorization flow is missing")
    flow = USER_DB.get_ready_community_flow(community_auth.digest(nonce))
    if not flow:
        raise HTTPException(status_code=400,
                            detail="community authorization flow is invalid or expired")

    if flow["purpose"] == "bind":
        if not user:
            raise HTTPException(status_code=401,
                                detail="login required for binding")
        if flow.get("initiator_user_id") != user["id"]:
            raise HTTPException(status_code=403,
                                detail="community binding belongs to another session")
        try:
            identity = USER_DB.bind_community_identity(flow["id"], user["id"])
        except CommunityFlowError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except CommunityIdentityConflict as e:
            raise HTTPException(
                status_code=409,
                detail="community account or local account is already bound") from e
        _clear_community_flow_cookie(response)
        return {"ok": True, "bound": True,
                "community": _public_community(identity)}

    try:
        logged_in = USER_DB.finish_community_login(flow["id"])
    except CommunityFlowError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except CommunityUserDisabled as e:
        raise HTTPException(status_code=403,
                            detail="account disabled") from e
    if logged_in:
        _clear_community_flow_cookie(response)
        return {"ok": True, "registered": False,
                "token": auth.issue_token(logged_in["id"],
                                          role=logged_in["role"]),
                "role": logged_in["role"]}

    _registration_email(flow["email"])
    # 走到这里才是「要新建一个本地账号」,所以关闭注册拦在这里:
    # 上面的 bind 分支是给老用户绑社区身份用的,不能一起封掉;
    # 下面的 finish_community_login 命中老账号时已经提前 return 了。
    _reject_closed_registration()
    group = ensure_default_group()
    created = None
    for _ in range(10):
        aff = _alloc_aff_code()
        try:
            created = USER_DB.register_community_user(
                flow["id"], aff,
                group_id=group["id"] if group else None)
            break
        except CommunityAffCodeConflict:
            continue
        except CommunityLevelError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except CommunityInviteError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except CommunityEmailConflict as e:
            raise HTTPException(
                status_code=409,
                detail="该邮箱已有账号，请先使用邮箱登录后在个人资料绑定社区") from e
        except CommunityIdentityConflict as e:
            raise HTTPException(
                status_code=409,
                detail="community account or local account is already bound") from e
        except CommunityFlowError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if created is None:
        raise HTTPException(status_code=500,
                            detail="failed to allocate invite code")

    uid = created["user_id"]
    inviter_id = created["inviter_id"]
    if config.SIGNUP_BONUS:
        credit(uid, config.SIGNUP_BONUS, REASON_SIGNUP, f"signup:{uid}",
               meta={"source": "community"})
    _publish_registered_invite_reward(uid)
    emit("user.registered", user=USER_DB.get_user(uid),
         inviter=USER_DB.get_user(inviter_id) if inviter_id else None)
    _clear_community_flow_cookie(response)
    return {"ok": True, "registered": True,
            "token": auth.issue_token(uid, role="user"), "role": "user",
            "aff_code": created["aff_code"]}


# ---- 个人资料(简化) ----

@router.get("/me")
def me(user=Depends(current_user)):
    group = USER_DB.effective_group(user)
    fresh = USER_DB.get_user(user["id"]) or user
    community = USER_DB.get_community_identity_by_user(user["id"])
    return {
        "id": user["id"], "email": user["email"], "role": user["role"],
        "status": user["status"],
        "email_verified": bool(user.get("email_verified_at")),
        "email_verified_at": user.get("email_verified_at") or 0,
        "group": group["name"] if group else None,
        "aff_code": user["aff_code"],
        "created_at": user["created_at"],
        "display_name": fresh.get("display_name") or "",
        "avatar": fresh.get("avatar") or "",
        "balance": fresh.get("balance") or 0.0,
        "total_spent": fresh.get("total_spent") or 0.0,
        "concurrency_limit": (group or {}).get("concurrency_limit") or 0,
        "has_password": str(fresh.get("password_hash") or "").startswith(
            "pbkdf2$"),
        "totp_enabled": bool(fresh.get("totp_secret")),
        "community": ({**_public_community(community),
                       "bound_at": community.get("bound_at") or 0}
                      if community else None),
    }


_AVATAR_OK = ("data:image/png;base64,", "data:image/jpeg;base64,",
              "data:image/webp;base64,", "data:image/gif;base64,")


@router.patch("/me")
def update_profile(req: ProfileReq, user=Depends(current_user)):
    """改昵称与头像。头像存 data URI 而不是走文件上传:

    单租户自托管场景下没有对象存储也没有静态目录写权限的前提,一张 20KB 的
    data URI 放 DB 里最省事;换成文件上传要同时解决存储路径、静态服务、
    清理孤儿文件三件事。代价是 /api/me 的响应会变大,所以尺寸在两侧都卡死:
    前端压缩到 20KB,后端再硬校验一次(前端可绕过)。"""
    fields = {}
    if req.display_name is not None:
        fields["display_name"] = req.display_name.strip() or None
    if req.avatar is not None:
        av = req.avatar.strip()
        if not av:
            fields["avatar"] = None            # 空串 = 删除头像
        else:
            if not av.startswith(_AVATAR_OK):
                raise HTTPException(status_code=400,
                                    detail="avatar must be a png/jpeg/webp/gif data URI")
            # base64 每 4 字符 3 字节;20KB 原图 → 约 27.4KB 文本
            if len(av) > 28000:
                raise HTTPException(status_code=400,
                                    detail="avatar too large, keep it under 20KB")
            fields["avatar"] = av
    if fields:
        USER_DB.update_user(user["id"], **fields)
    return {"ok": True, "updated": list(fields)}


@router.post("/me/password")
def change_password(req: PasswordChangeReq, user=Depends(current_user)):
    if not auth.verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=403, detail="old password incorrect")
    USER_DB.set_password(user["id"], auth.hash_password(req.new_password))
    # 改密让旧会话全部作废(含当前这个),所以把新会话一并回给前端换上,
    # 否则用户改完密码下一次点击就被弹回登录页。
    return {"ok": True, "token": auth.issue_token(user["id"], role=user["role"])}


# ---- 二次验证(TOTP) ----

class TotpCodeReq(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TotpDisableReq(TotpCodeReq):
    password: str


@router.post("/2fa/setup")
def totp_setup(user=Depends(current_user)):
    """生成候选密钥,等用户在 authenticator 里录好、回一个码来确认(/2fa/enable)才生效。
    已开启的要先关再开 —— 否则一次 setup 请求就能把别人的 authenticator 顶掉。"""
    if user.get("totp_secret"):
        raise HTTPException(status_code=409, detail="已开启二次验证,先关闭再重新绑定")
    secret = totp.generate_secret()
    USER_DB.update_user(user["id"], totp_pending=secret)
    issuer = S.site_name()
    return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, user["email"], issuer),
            "issuer": issuer, "account": user["email"]}


@router.post("/2fa/enable")
def totp_enable(req: TotpCodeReq, user=Depends(current_user)):
    fresh = USER_DB.get_user(user["id"])
    pending = fresh.get("totp_pending")
    if not pending:
        raise HTTPException(status_code=400, detail="先调用 setup 生成密钥")
    step = totp.verify(pending, req.code)
    if step is None:
        raise HTTPException(status_code=400, detail="验证码不正确,检查手机时间是否准确")
    USER_DB.update_user(user["id"], totp_secret=pending, totp_pending=None, totp_last_step=step)
    return {"ok": True}


@router.post("/2fa/disable")
def totp_disable(req: TotpDisableReq, user=Depends(current_user)):
    """关闭要密码 + 当前验证码两样都对:偷到会话的人不该能把锁拆了。"""
    fresh = USER_DB.get_user(user["id"])
    if not fresh.get("totp_secret"):
        raise HTTPException(status_code=400, detail="未开启二次验证")
    if not auth.verify_password(req.password, fresh["password_hash"]):
        raise HTTPException(status_code=403, detail="密码不正确")
    if totp.verify(fresh["totp_secret"], req.code, last_step=fresh.get("totp_last_step") or 0) is None:
        raise HTTPException(status_code=400, detail="验证码不正确")
    USER_DB.update_user(user["id"], totp_secret=None, totp_pending=None, totp_last_step=0)
    return {"ok": True}


# ---- 找回密码 ----

class ForgotReq(BaseModel):
    email: EmailStr


class ResetReq(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=6, max_length=128)


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/password/forgot")
def password_forgot(req: ForgotReq, request: Request = None):
    """发找回链接。邮箱不存在也回 ok —— 否则响应差异能枚举出哪些邮箱注册过。
    邮件服务没配时明说,不假装发了:那种「发了但永远收不到」比报错更伤。"""
    email = req.email.lower().strip()
    ip = throttle.client_ip(request)
    try:
        throttle.check((throttle.RESET_IP, ip, "reset"),
                       (throttle.RESET_EMAIL, email, "reset"))
    except throttle.AuthThrottled as e:
        raise _throttled(e) from e
    if not mailer.configured():
        raise HTTPException(status_code=503,
                            detail="站点未配置邮件服务,请联系站长重置密码")
    throttle.RESET_IP.hit(ip)
    throttle.RESET_EMAIL.hit(email)
    user = USER_DB.get_user_by_email(email)
    if user and user["status"] == "active":
        token = secrets.token_urlsafe(32)
        USER_DB.create_password_reset(user["id"], _hash_token(token),
                                      config.PASSWORD_RESET_TTL)
        link = f"{S.site_url().rstrip('/')}/portal#/reset?token={token}"
        subject, text, html = mailer.reset_password_mail(
            S.site_name(), link, max(1, config.PASSWORD_RESET_TTL // 60))
        try:
            mailer.send(email, subject, text, html)
        except mailer.MailError as e:
            print(f"[portal] 找回密码邮件发送失败 user={user['id']}: {e}", flush=True)
            raise HTTPException(status_code=502,
                                detail="邮件发送失败,请稍后再试或联系站长") from e
    return {"ok": True, "sent": True, "ttl": config.PASSWORD_RESET_TTL}


@router.post("/password/reset")
def password_reset(req: ResetReq):
    uid = USER_DB.consume_password_reset(_hash_token(req.token.strip()))
    if uid is None:
        raise HTTPException(status_code=400, detail="链接无效或已过期,请重新申请")
    user = USER_DB.get_user(uid)
    if not user or user["status"] != "active":
        raise HTTPException(status_code=403, detail="account disabled")
    USER_DB.set_password(uid, auth.hash_password(req.new_password))
    # 找回成功,顺手解掉该邮箱的登录失败锁:忘密码的人多半刚试错过几次
    throttle.LOGIN_EMAIL.reset(user["email"])
    return {"ok": True, "email": user["email"]}


# ---- 邮箱绑定 ----

@router.post("/email/bind")
def email_bind(req: EmailBindReq, request: Request, user=Depends(current_user)):
    """在用户、IP 和收件邮箱限速内发码；未配置 SMTP 时明确拒绝，不向日志写码。"""
    email = req.email.lower().strip()
    owner = USER_DB.get_user_by_email(email)
    if owner and owner["id"] != user["id"]:
        raise HTTPException(409, "该邮箱已被其他账号绑定")
    if not mailer.configured():
        raise HTTPException(503, "站点未配置邮件服务，请联系站长")
    for limiter, key, scope in (
            (throttle.BIND_MAIL_IP, throttle.client_ip(request), "bind_mail_ip"),
            (throttle.BIND_MAIL_EMAIL, email, "bind_mail_email"),
            (throttle.BIND_MAIL_USER, str(user["id"]), "bind_mail_user")):
        if key and limiter.hit(key):
            raise _throttled(throttle.AuthThrottled(scope, limiter.retry_after(key), key))
    code = auth.generate_email_code()
    USER_DB.add_email_verification(user["id"], email, code)
    subject, text, html = mailer.verify_email_mail(S.site_name(), code, 15)
    try:
        mailer.send(email, subject, text, html)
    except mailer.MailError as e:
        raise HTTPException(status_code=502, detail=f"邮件发送失败:{e}") from e
    return {"ok": True, "sent": True}


@router.post("/email/verify")
def email_verify(req: EmailVerifyReq, user=Depends(current_user)):
    try:
        USER_DB.verify_and_bind_email(user["id"], req.code.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    BILLING.invalidate()
    return {"ok": True}


# ---- API 密钥 ----

def _mask_key(full):
    """sk- 前缀 + 头 7 位 + 尾 4 位。留尾是为了两把密钥能区分开:
    只截头的话同前缀的密钥在列表里长得一模一样。"""
    return full[:10] + "…" + full[-4:] if full else ""


def _clean_ips(items):
    """校验 IP/CIDR 写法并去重。写错一条就整体 400 —— IP 白名单写错等于把
    自己锁在门外,静默丢弃比报错更难排查。"""
    out = []
    for raw in items or []:
        s = str(raw).strip()
        if not s:
            continue
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError as e:
            raise HTTPException(status_code=400,
                                detail=f"invalid IP or CIDR: {s}") from e
        if s not in out:
            out.append(s)
    return out


def _clean_models(items):
    out = []
    for raw in items or []:
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return out


@router.get("/keys")
def list_keys(user=Depends(current_user)):
    """列表只给脱敏串,完整明文按 id 单取(/keys/{id}/reveal)。

    不返全文的理由是表格这个场景本身:它是用户截图、投屏、演示时露得最多的
    界面,一次泄露的是所有行。列表响应还会进浏览器缓存与前端错误上报。
    前端要复制时点一下按钮再取,泄露面收窄到「用户主动要的那一把」。

    另附每把 key 的用量汇总、账户有效分组与展开后的可用模型,让密钥页一次
    请求出全表;顶部 stats 是页面概览卡要的四个数,一并算好省一轮请求。"""
    from core.adapter import enabled_adapters

    keys = USER_DB.list_api_keys(user["id"])
    group = USER_DB.effective_group(user)
    available_models = sorted({
        model
        for adapter in enabled_adapters().values()
        for model in adapter.models
        if BILLING.check_model_allowed(group, model)
    })
    now = int(time.time())
    all_usage = USER_DB.api_key_usage_map(user["id"])
    today_usage = USER_DB.api_key_usage_map(user["id"], since=site_day_start(now))
    empty = {"requests": 0, "tokens": 0, "cost": 0.0}
    out = []
    active = 0
    for k in keys:
        quota = k.get("quota") or 0
        used = k.get("used_quota") or 0
        expired = bool(k.get("expires_at")) and k["expires_at"] < now
        exhausted = bool(quota) and used >= quota
        if k["status"] == "active" and not expired and not exhausted:
            active += 1
        out.append({
            "id": k["id"], "name": k["name"], "status": k["status"],
            "key_masked": _mask_key(k["key"]),
            "expires_at": k.get("expires_at") or 0,
            "expired": expired,
            "last_used_at": k.get("last_used_at") or 0,
            "created_at": k["created_at"],
            "quota": quota, "used_quota": used,
            "quota_exhausted": exhausted,
            "allowed_models": k.get("allowed_models") or [],
            "allowed_ips": k.get("allowed_ips") or [],
            "usage": all_usage.get(k["id"], empty),
            "usage_today": today_usage.get(k["id"], empty),
        })
    return {"keys": out,
            "group": (group or {}).get("name"),
            "group_models": (group or {}).get("supported_models") or [],
            "available_models": available_models,
            # 费用按 actual_cost(实扣)合计,与余额流水口径一致
            "stats": {
                "total": len(out),
                "active": active,
                "cost_today": sum(v["cost"] for v in today_usage.values()),
                "cost_all": sum(v["cost"] for v in all_usage.values()),
            }}


@router.get("/keys/{key_id}/reveal")
def reveal_key(key_id: int, user=Depends(current_user)):
    """按需取回完整密钥(懒加载,前端点开才请求)。"""
    rec = USER_DB.get_api_key_by_id(key_id, user["id"])
    if not rec:
        raise HTTPException(status_code=404, detail="key not found")
    return {"id": rec["id"], "key": rec["key"]}


@router.post("/keys")
def create_key(req: KeyCreateReq, user=Depends(current_user)):
    """支持一次创建多把(count>1 时名称自动加随机后缀)。

    额度/模型/IP 三项都是「收紧」型限制,用户自助设置不会扩权:
    模型仍要过分组白名单,额度只是本密钥的消费上限,钱照旧从账户余额扣。"""
    count = max(1, min(req.count or 1, 50))
    expires_at = req.expires_at or 0
    if expires_at and expires_at < int(time.time()):
        raise HTTPException(status_code=400, detail="expires_at is in the past")
    models = _clean_models(req.allowed_models)
    ips = _clean_ips(req.allowed_ips)
    base = (req.name or "").strip()
    created = []
    for i in range(count):
        name = base or None
        if base and count > 1 and i > 0:
            name = f"{base}-{secrets.token_hex(3)}"
        key = auth.generate_api_key()
        kid = USER_DB.create_api_key(user["id"], key, name=name,
                                     expires_at=expires_at,
                                     quota=req.quota or 0,
                                     allowed_models=models,
                                     allowed_ips=ips)
        created.append({"id": kid, "name": name, "key": key})
    return {"ok": True, "count": len(created), "keys": created,
            "key": created[0]["key"], "id": created[0]["id"]}


@router.patch("/keys/{key_id}")
def update_key(key_id: int, req: KeyUpdateReq, user=Depends(current_user)):
    rec = USER_DB.get_api_key_by_id(key_id, user["id"])
    if not rec:
        raise HTTPException(status_code=404, detail="key not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if fields.get("status") not in (None, "active", "disabled"):
        raise HTTPException(status_code=400, detail="status must be active|disabled")
    # 空列表是有意义的取值(清空白名单),不能被上面的 None 过滤混淆,
    # 所以这里显式规整而不是 if fields.get(...)
    if "allowed_models" in fields:
        fields["allowed_models"] = _clean_models(fields["allowed_models"])
    if "allowed_ips" in fields:
        fields["allowed_ips"] = _clean_ips(fields["allowed_ips"])
    if not fields:
        return {"ok": True}
    USER_DB.update_api_key(key_id, user["id"], **fields)
    BILLING.invalidate(rec["key"])   # 状态/有效期/白名单变更需失效鉴权缓存
    return {"ok": True}


@router.delete("/keys/{key_id}")
def delete_key(key_id: int, user=Depends(current_user)):
    rec = USER_DB.get_api_key_by_id(key_id, user["id"])
    deleted = USER_DB.delete_api_key(key_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="key not found")
    if rec:
        BILLING.invalidate(rec["key"])   # 该 key 可能正被网关缓存
    return {"ok": True}


# ---- 使用记录 ----




def _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason):
    """用户侧列表 / 合计 / 导出共用的条件。时间窗:显式传 since/until 就用它们,
    否则按 range 取窗口 —— 三处必须同一口径,否则「今日」页面上导出的却是全部历史。"""
    if not since and not until:
        since = site_day_start() if range == "day" else int(time.time()) - _RANGE_SPAN.get(range, 86400)
    return _usage_filters(model, channel, since, until, end_reason, api_key_id)


@router.get("/usage")
def usage(range: str = "day", limit: int = 50, offset: int = 0,
          model: str = None, channel: str = None, api_key_id: int = None,
          since: int = None, until: int = None, end_reason: str = None,
          user=Depends(current_user)):
    """用量三件:summary 是 range 窗口的按模型分布(概览页用,老口径不动);
    recent 是按条件分页的明细;totals 是与 recent **同一组条件**下的合计,
    给「使用记录」页的统计卡 —— 卡片与列表必须是同一个集合。

    分页与筛选是后加的:原先只回最近 50 条,用户查「上周三那次为什么扣了 2 美元」
    翻不到。"""
    summary = USER_DB.usage_summary(
        user["id"], site_day_start() if range == "day" else int(time.time()) - _RANGE_SPAN.get(range, 86400))
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    f = _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason)
    rows, total = USER_DB.query_usage(limit=limit, offset=offset,
                                      user_id=user["id"], **f)
    for row in rows:
        row.pop("upstream_account_id", None)
        row.pop("account_cost", None)
    totals = USER_DB.usage_totals(user_id=user["id"], **f)
    return {"range": range, "summary": summary, "recent": rows, "totals": totals,
            "total": total, "limit": limit, "offset": offset,
            "since": f["since"], "until": f["until"]}




@router.get("/usage/live")
def user_live_usage(user=Depends(current_user)):
    """当前用户所有密钥及在线体验的当日用量与最近一分钟读数。"""
    return _live_usage_snapshot(user["id"])


def _cache_period(range, as_of=None):
    if range not in (*_RANGE_SPAN, "all"):
        raise HTTPException(400, "请选择 day、week、month 或 all")
    now = int(time.time())
    cutoff = min(now, as_of) if as_of is not None else now
    return cutoff, (site_day_start(cutoff) if range == "day" else
                    cutoff - _RANGE_SPAN[range] if range != "all" else 0)


def _model_cache_snapshot(range, user_id=None):
    now, since = _cache_period(range)
    # Completed seconds keep model totals and a later trend request on the same snapshot.
    return {**USER_DB.model_cache_usage(user_id=user_id, since=since, until=now),
            "range": range, "since": since, "until": now, "as_of": now}


@router.get("/usage/cache")
def user_model_cache(range: str = "all", user=Depends(current_user)):
    return _model_cache_snapshot(range, user["id"])


@router.get("/admin/usage/cache")
def admin_model_cache(range: str = "week", user=Depends(require_admin)):
    return _model_cache_snapshot(range)


def _model_cache_trend_snapshot(range, model=None, user_id=None, as_of=None, models=None):
    if model is not None and models:
        raise HTTPException(400, "model 和 models 不能同时指定")
    if models and any(not name or len(name) > 256 for name in models):
        raise HTTPException(422, "模型名称长度须为 1–256 个字符")
    selected = list(dict.fromkeys(models or []))
    cutoff, since = _cache_period(range, as_of)
    result = USER_DB.model_cache_usage(user_id=user_id, since=since, until=cutoff,
                                      model=model, models=selected, daily=True)
    rows_by_day = {}
    rows_by_model = {name: {} for name in selected}
    for row in result["series"]:
        rows_by_day.setdefault(row["date"], []).append(row)
        if selected:
            rows_by_model[row["model"]][row["date"]] = row
    last_day = datetime.fromtimestamp(cutoff, _CHECKIN_TZ).date()
    first_day = (min(rows_by_day) if rows_by_day else None) if range == "all" else datetime.fromtimestamp(since, _CHECKIN_TZ).date().isoformat()
    dates = []
    if first_day is not None:
        day = _date.fromisoformat(first_day)
        while day <= last_day:
            dates.append(day.isoformat())
            day += timedelta(days=1)
    empty = USER_DB.cache_usage_summary([])
    series = [{**USER_DB.cache_usage_summary(rows_by_day.get(stamp, [])), "date": stamp} for stamp in dates]
    response = {**result, "series": series, "range": range, "model": model, "since": since,
                "until": cutoff, "as_of": cutoff, "timezone": "Asia/Shanghai"}
    if selected:
        response["models"] = selected
        response["model_series"] = [
            {"model": name, "summary": USER_DB.cache_usage_summary(list(rows_by_model[name].values())),
             "series": [rows_by_model[name].get(stamp, {**empty, "date": stamp}) for stamp in dates]}
            for name in selected]
    return response


@router.get("/usage/cache/trend")
def user_model_cache_trend(range: str = "all", model: str = Query(None, min_length=1, max_length=256),
                           as_of: int = Query(None, ge=1), models: list[str] = Query(None),
                           user=Depends(current_user)):
    return _model_cache_trend_snapshot(range, model, user["id"], as_of, models)


@router.get("/admin/usage/cache/trend")
def admin_model_cache_trend(range: str = "week", model: str = Query(None, min_length=1, max_length=256),
                            as_of: int = Query(None, ge=1), models: list[str] = Query(None),
                            user=Depends(require_admin)):
    return _model_cache_trend_snapshot(range, model, as_of=as_of, models=models)


@router.get("/usage/export.csv")
def usage_export(range: str = "day", model: str = None, channel: str = None,
                 api_key_id: int = None, since: int = None, until: int = None,
                 end_reason: str = None, user=Depends(current_user)):
    f = _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason)
    rows, _ = USER_DB.query_usage(limit=_CSV_MAX, offset=0, user_id=user["id"], **f)
    return _usage_csv(rows, with_user=False,
                      filename=f"usage-{_date.today().isoformat()}.csv")


_CHECKIN_TZ = SITE_TZ
_CHECKIN_SCALE = 10_000


def _checkin_date(now=None):
    """签到按 UTC+8 自然日切换，不跟着服务器系统时区漂移。"""
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, _CHECKIN_TZ).date().isoformat()


def _checkin_range():
    """返回 (最低额,最高额,最低整数档,最高整数档)。"""
    low = int(round(S.checkin_min() * _CHECKIN_SCALE))
    high = int(round(S.checkin_max() * _CHECKIN_SCALE))
    if low < 1 or high < low or high > 100 * _CHECKIN_SCALE:
        # 管理台会在写入时挡住；这里还要挡环境变量或人工改库造成的坏值，
        # 不能在配置倒置时继续随机发钱。
        raise HTTPException(status_code=503, detail="checkin reward range is invalid")
    return (low / _CHECKIN_SCALE, high / _CHECKIN_SCALE, low, high)


def _checkin_status(user_id, now=None):
    day = _checkin_date(now)
    low, high, _, _ = _checkin_range()
    row = USER_DB.get_ledger_by_idem(f"checkin:{user_id}:{day}")
    return {"date": day, "checked_in": bool(row),
            "reward": row.get("amount") if row else None,
            "range": {"min": low, "max": high}}


@router.get("/overview")
def overview(user=Depends(current_user)):
    """概览页的一次性数据源:今日 + 累计两个窗口 + 密钥计数 + 性能指标。

    原先概览要打 /billing + /usage?range=day + /usage?range=month 三次,
    且 month 只是近 30 天不是「累计」。这里今日走北京时间自然日、累计走全量,
    另附 RPM/TPM(按有记录的时间跨度平摊)与平均响应时长。
    """
    now = int(time.time())
    today = USER_DB.usage_overview(user["id"], since=site_day_start(now))
    total = USER_DB.usage_overview(user["id"])
    keys = USER_DB.list_api_keys(user["id"])
    active = 0
    for k in keys:
        quota = k.get("quota") or 0
        expired = bool(k.get("expires_at")) and k["expires_at"] < now
        exhausted = bool(quota) and (k.get("used_quota") or 0) >= quota
        if k["status"] == "active" and not expired and not exhausted:
            active += 1

    # 平均 RPM/TPM 按「首末记录的时间跨度」平摊,而不是按自然日:
    # 只用了 5 分钟的新账号除以 1440 分钟会得出 0,看着像没在用。
    span_min = 0.0
    if total["first_at"] and total["last_at"]:
        span_min = max((total["last_at"] - total["first_at"]) / 60.0, 1.0)
    tok_total = total["input_tokens"] + total["output_tokens"]
    perf = {
        "rpm": (total["requests"] / span_min) if span_min else 0.0,
        "tpm": (tok_total / span_min) if span_min else 0.0,
        "span_minutes": span_min,
        # 平均响应只对有耗时记录的行取平均,非流式/未记录的行不该拉低均值
        "avg_ms": (total["duration_ms"] / total["duration_rows"]
                   if total["duration_rows"] else 0),
        "samples": total["duration_rows"],
    }
    fresh = USER_DB.get_user(user["id"]) or user
    group = USER_DB.effective_group(user)
    policy = BILLING.policy_of(group)

    # 近 14 天按本地日历日补齐:没有记录的日子也要有格子,否则折线会把
    # 「那天没调用」画成直连到下一个有数据的日子,看着像一直在跑。
    raw = {r["date"]: r for r in USER_DB.usage_daily(user["id"], days=14)}
    series = []
    for k in range(13, -1, -1):
        d = datetime.fromtimestamp(now - k * 86400, SITE_TZ).date().isoformat()
        r = raw.get(d) or {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                           "cache_tokens": 0, "tokens": 0, "cost": 0.0}
        series.append({"date": d, "label": d[5:],
                       "requests": r["requests"], "tokens": r["tokens"],
                       "input_tokens": r["input_tokens"],
                       "output_tokens": r["output_tokens"],
                       "cache_tokens": r["cache_tokens"], "cost": r["cost"]})
    top, rest = USER_DB.usage_by_model(user["id"], limit=6)
    if rest:
        top.append({"model": "其他 " + str(len(rest)) + " 个",
                    "requests": sum(x["requests"] for x in rest),
                    "tokens": sum(x["tokens"] for x in rest),
                    "cost": sum(x["cost"] for x in rest)})

    out = {
        "balance": fresh.get("balance") or 0.0,
        "total_spent": fresh.get("total_spent") or 0.0,
        "policy": policy,
        "group": (group or {}).get("name"),
        "rate_multiplier": (group or {}).get("rate_multiplier"),
        "concurrency_limit": (group or {}).get("concurrency_limit") or 0,
        "limit_unit": (group or {}).get("limit_unit") or "requests",
        "supported_models": (group or {}).get("supported_models") or [],
        "group_status": (group or {}).get("status"),
        "keys": {"total": len(keys), "active": active},
        "today": today,
        "total": total,
        "perf": perf,
        "series": series,
        "by_model": top,
        "checkin": _checkin_status(user["id"], now),
    }
    if policy == "quota" and group:
        unit = out["limit_unit"]
        measure = USER_DB.usage_tokens if unit == "tokens" else USER_DB.usage_count
        out["usage"] = {
            "daily": {"used": measure(user["id"], now - 86400),
                      "limit": group["daily_limit"]},
            "weekly": {"used": measure(user["id"], now - 7 * 86400),
                       "limit": group["weekly_limit"]},
            "monthly": {"used": measure(user["id"], now - 30 * 86400),
                        "limit": group["monthly_limit"]},
        }
    return out


@router.post("/checkin")
def daily_checkin(user=Depends(current_user)):
    """每日签到一次，奖励按管理端配置的万分之一美元整数档随机。"""
    day = _checkin_date()
    low, high, low_units, high_units = _checkin_range()
    reward_units = low_units + secrets.randbelow(high_units - low_units + 1)
    reward = reward_units / _CHECKIN_SCALE
    row, created = credit(
        user["id"], reward, REASON_CHECKIN, f"checkin:{user['id']}:{day}",
        meta={"date": day, "timezone": "UTC+8",
              "range": {"min": low, "max": high}})
    if created:
        # 网关鉴权缓存里带着用户余额；签到到账后立即失效，下一次调用才能
        # 立刻用上这笔额度，而不是再等 AUTH_CACHE_TTL。
        BILLING.invalidate()
    fresh = USER_DB.get_user(user["id"]) or user
    return {"ok": True, "date": day, "checked_in": True,
            "claimed": created, "reward": row.get("amount"),
            "balance": fresh.get("balance") or 0.0,
            "range": {"min": low, "max": high}}


# ---- 计费(套餐 + 额度用量) ----

@router.get("/billing")
def billing(user=Depends(current_user)):
    group = USER_DB.effective_group(user)
    if not group:
        return {"group": None, "policy": "free"}
    policy = BILLING.policy_of(group)
    now = int(time.time())
    unit = group.get("limit_unit") or "requests"
    measure = (USER_DB.usage_tokens if unit == "tokens" else USER_DB.usage_count)
    fresh = USER_DB.get_user(user["id"]) or user
    out = {
        "group": group["name"],
        "policy": policy,
        "rate_multiplier": group["rate_multiplier"],
        "supported_models": group.get("supported_models") or [],
        "concurrency_limit": group["concurrency_limit"],
        "limit_unit": unit,
        "balance": fresh.get("balance") or 0.0,
        "total_spent": fresh.get("total_spent") or 0.0,
    }
    if policy == "quota":
        out["usage"] = {
            "daily": {"used": measure(user["id"], now - 86400),
                      "limit": group["daily_limit"]},
            "weekly": {"used": measure(user["id"], now - 7 * 86400),
                       "limit": group["weekly_limit"]},
            "monthly": {"used": measure(user["id"], now - 30 * 86400),
                        "limit": group["monthly_limit"]},
        }
    if policy == "balance":
        # 该组可见模型的生效单价(解析链结果),供用户自查
        prices = []
        seen = set()
        for row in (USER_DB.list_pricing(group["id"])
                    + USER_DB.list_pricing(None)):
            pat = row["model_pattern"]
            if pat in seen:
                continue
            seen.add(pat)
            if not BILLING.check_model_allowed(group, pat.rstrip("*")):
                continue
            prices.append({k: row.get(k) for k in (
                "model_pattern", "billing_mode", "input_price", "output_price",
                "cache_read_price", "cache_write_price", "per_request_price",
                "long_threshold", "long_input_price", "long_output_price",
                "long_cache_read_price", "long_cache_write_price")})
        out["pricing"] = prices
    return out


# ---- 模型广场 ----

# 图/视频模型把规格编进模型名第 3 段起(oreate-vid/kling-3-0/1080p/10s),
# 同一模型能展开出十几条。广场按前两段折叠成一张卡,规格进 variants。
#
# 只对**生成型**渠道折叠:对话型渠道的模型名里斜杠是命名空间
# (corvex/deepseek-ai/DeepSeek-V4-Flash-0731),前两段不是模型、折出来的
# 卡名调不通 —— 在线体验直接把卡名发出去就是 404 unknown model。
_SPEC_SEGMENTS = 3


def _collapse_key(model, fold=True):
    """返回 (卡片名, 规格后缀)。段数不足 3 或 fold=False 时不折叠,后缀为空。"""
    if not fold:
        return model, ""
    parts = model.split("/")
    if len(parts) < _SPEC_SEGMENTS:
        return model, ""
    return "/".join(parts[:2]), "/".join(parts[2:])


_PRICE_KEYS = ("input_price", "output_price", "cache_read_price",
               "cache_write_price", "per_request_price", "long_threshold",
               "long_input_price", "long_output_price",
               "long_cache_read_price", "long_cache_write_price")


@router.get("/models")
def list_plaza_models(user=Depends(optional_user)):
    """模型广场:可用模型 × 生效单价。价格走 PRICING 解析链,与网关同一套。

    未登录也能看(站点对外的模型清单),此时按默认分组的视角出价与可见范围 ——
    游客注册后落的就是这个组,所见即所得。登录用户看自己组。
    可见范围与 /v1/models 对齐(分组白名单),否则广场里点得到、调用却 403。
    下线的渠道整条不出现(enabled_adapters),与清单同一口径。
    单价是原价,分组倍率单独给,由前端乘 —— 和计费页同一口径。
    """
    from core.adapter import enabled_adapters, model_to_channel

    if user:
        group = USER_DB.effective_group(user)
    else:
        group = ensure_default_group()
    gid = group["id"] if group else None
    cards = {}
    for channel, ad in enabled_adapters().items():
        # 只有生成型渠道按「前两段」折叠规格;对话型渠道的名字原样当卡名,
        # 否则 corvex/deepseek-ai/... 会被折成 corvex/deepseek-ai,体验即 404。
        fold = getattr(ad, "kind", None) == "generation"
        for m in ad.models:
            can_chat = ad.supports_chat(m)
            if group and not BILLING.check_model_allowed(group, m):
                continue
            name, spec = _collapse_key(m, fold=fold)
            try:
                pr = PRICING.resolve(m, gid)
            except ValueError:
                continue  # 缺价模型不能调用，也不在广场显示成免费。
            row = cards.get(name)
            if row is None:
                # 对外不再暴露渠道归属：模型广场只给模型口径。渠道是内部实现，
                # 备胎数量、首选渠道都不该出现在用户侧响应里。
                row = cards[name] = {
                    "model": name, "chat": can_chat,
                    "billing_mode": pr.get("billing_mode"),
                    "price_source": pr.get("source"),
                    "variants": [],
                }
                row.update({k: pr.get(k) for k in _PRICE_KEYS})
            if spec:
                row["variants"].append(
                    {"spec": spec, "model": m,
                     "billing_mode": pr.get("billing_mode"),
                     "price_source": pr.get("source"),
                     "per_request_price": pr.get("per_request_price")})
    for row in cards.values():
        _fold_variants(row)
    # 卡片状态:对话卡带巡检结果与 7 天状态条;生成类(图/视频)不巡检,不带。
    # 渠道名与上游原话照例不出网关 —— 卡片只有「能不能用 + 首字多快」。
    stored = {(r["channel"], r["model"]): r
              for r in USER_DB.latest_model_status()}
    since = time.time() - model_status.RETENTION
    hist = {}
    for r in USER_DB.history_model_status(since):
        hist.setdefault((r["channel"], r["model"]), []).append(r)
    smap = model_status.user_status_map(enabled_adapters(), stored, hist)
    for row in cards.values():
        row["status"] = smap.get(row["model"]) if row["chat"] else None
    return {
        "guest": user is None,
        "group": group["name"] if group else None,
        "policy": BILLING.policy_of(group) if group else "free",
        "rate_multiplier": group["rate_multiplier"] if group else 1.0,
        "data": sorted(cards.values(), key=lambda r: r["model"]),
    }


def _fold_variants(row):
    """折叠卡的卡面数据由各规格汇总,不取「第一个碰到的规格」那种任意值。

    规格之间计费模式可能不一致(管理员只给部分规格配了价),这种情况如实标成
    mixed 并给出价格区间,而不是挑一个规格的模式冒充整卡。
    """
    vs = row["variants"]
    if not vs:
        return
    vs.sort(key=lambda v: v["spec"])
    modes = {v["billing_mode"] for v in vs}
    row["billing_mode"] = modes.pop() if len(modes) == 1 else "mixed"
    row["price_source"] = None if len({v["price_source"] for v in vs}) > 1 \
        else vs[0]["price_source"]
    prices = [v["per_request_price"] or 0.0 for v in vs]
    row["per_request_price"] = min(prices)
    row["per_request_price_max"] = max(prices)


# ---- 钱包(余额流水 / 兑换码 / 订单) ----

@router.get("/ledger")
def ledger(limit: int = 50, offset: int = 0, reason: str = None,
           user=Depends(current_user)):
    fresh = USER_DB.get_user(user["id"]) or user
    return {
        "balance": fresh.get("balance") or 0.0,
        "total_spent": fresh.get("total_spent") or 0.0,
        "entries": USER_DB.list_ledger(user["id"], limit=min(limit, 200),
                                       offset=offset, reason=reason),
    }


class RedeemReq(BaseModel):
    code: str


@router.post("/redeem")
def redeem_code(req: RedeemReq, request: Request, user=Depends(current_user)):
    uid, ip = str(user["id"]), throttle.client_ip(request)
    try:
        throttle.check((throttle.REDEEM_USER, uid, "redeem_user"),
                       (throttle.REDEEM_IP, ip, "redeem_ip"))
    except throttle.AuthThrottled as exc:
        raise _throttled(exc) from exc
    try:
        ledger_row, code_row = redeem(USER_DB, req.code, user)
    except RedeemError as e:
        throttle.REDEEM_USER.hit(uid)
        if ip:
            throttle.REDEEM_IP.hit(ip)
        raise HTTPException(status_code=400,
                            detail={"code": e.code, "message": e.detail}) from e
    BILLING.invalidate()
    return {"ok": True, "value": code_row["value"],
            "balance": ledger_row.get("balance_after")}


class OrderReq(BaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    provider: str = "mock"


# ---- 订阅套餐(时长卡) ----

def _plan_view(g):
    """套餐卡片。price/duration 原样给,剩余时长由前端按 current.expires_at 自己算 ——
    卡片本身跟「现在几点」无关,所以这里不收 now。"""
    return {
        "id": g["id"], "name": g["name"], "price": g.get("price") or 0,
        "duration_hours": g.get("duration_hours") or 0,
        "notes": g.get("notes") or "",
        "billing_policy": g.get("billing_policy"),
        "rate_multiplier": g.get("rate_multiplier"),
        "concurrency_limit": g.get("concurrency_limit") or 0,
        "daily_limit": g.get("daily_limit") or 0,
        "weekly_limit": g.get("weekly_limit") or 0,
        "monthly_limit": g.get("monthly_limit") or 0,
        "limit_unit": g.get("limit_unit") or "requests",
        "supported_models": g.get("supported_models") or [],
    }


@router.get("/plans")
def list_plans(user=Depends(current_user)):
    """可购套餐 + 当前订阅状态。

    没买套餐时 current 为 null,用户走默认分组按量计费 —— 那是常态,不是缺省错误。
    """
    now = int(time.time())
    fresh = USER_DB.get_user(user["id"]) or user
    eff = USER_DB.effective_group_id(fresh, now)
    plan_gid = fresh.get("plan_group_id")
    exp = fresh.get("plan_expires_at") or 0
    active = bool(plan_gid) and eff == plan_gid
    base = USER_DB.get_group(fresh["group_id"]) if fresh.get("group_id") else None
    cur = USER_DB.get_group(plan_gid) if plan_gid else None
    return {
        "plans": [_plan_view(g) for g in USER_DB.list_listed_groups()],
        "balance": fresh.get("balance") or 0.0,
        # 按量计费那档(没订阅时生效的分组)
        "base_group": base["name"] if base else None,
        "current": ({"name": cur["name"], "group_id": plan_gid,
                     "expires_at": exp, "active": active} if cur else None),
    }


class PlanBuyReq(BaseModel):
    group_id: int


@router.post("/plans/purchase")
def purchase_plan(req: PlanBuyReq, user=Depends(current_user)):
    """买/续费一张时长卡。

    续费叠加而不是重置:同一个套餐没到期时再买,从原到期时间往后加,不吞掉
    用户已经付过的那段。换成别的套餐则从现在起算(剩余时长不折算、不退款,
    这条写在用户端卡片文案里)。

    扣款走 credit() 记一条 reason=plan 的负数流水 —— 余额的每一分都要有对应
    流水行,否则「钱少了但查不出为什么」。幂等键带秒级时间戳:同一秒内的重复
    提交算一次(防双击),不同秒的两次购买是两次真实续费。
    """
    g = USER_DB.get_group(req.group_id)
    if not g or not g.get("listed") or g.get("status") != "active":
        raise HTTPException(status_code=404, detail="plan not available")
    price = float(g.get("price") or 0)
    fresh = USER_DB.get_user(user["id"]) or user
    if price > 0 and (fresh.get("balance") or 0.0) < price:
        raise HTTPException(status_code=402, detail="insufficient balance")

    now = int(time.time())
    hours = int(g.get("duration_hours") or 0)
    same = fresh.get("plan_group_id") == req.group_id
    old = fresh.get("plan_expires_at") or 0
    if hours <= 0:
        expires_at = 0          # 不限时套餐
    else:
        # 同一张卡续费从原到期时间接着算,换成别的卡从现在起算
        expires_at = (old if (same and old > now) else now) + hours * 3600

    if price > 0:
        _row, created = credit(fresh["id"], -price, REASON_PLAN,
                               f"plan:{fresh['id']}:{req.group_id}:{now}",
                               meta={"group": g["name"], "hours": hours})
        # 幂等键撞上,说明这一秒里已经买过一次。那次已经生效就原样返回(双击):
        # 只挡扣款不挡时长的话,一次点击的钱能买到两段有效期。若那次扣款成了而
        # 生效没落库(两步之间断开),这里照常往下走把卡补上,别让钱白扣。
        if not created and same and (old == 0 or old > now):
            return {"ok": True, "plan": g["name"], "expires_at": old,
                    "balance": fresh.get("balance") or 0.0}
    USER_DB.update_user(fresh["id"], plan_group_id=req.group_id,
                        plan_expires_at=expires_at)
    after = USER_DB.get_user(fresh["id"])
    return {"ok": True, "plan": g["name"], "expires_at": expires_at,
            "balance": after.get("balance") or 0.0}


@router.get("/payment/providers")
def payment_providers(user=Depends(current_user)):
    return {"providers": [
        {"name": p.name, "display_name": getattr(p, "display_name", p.name)}
        for n, p in all_providers().items() if n in S.payment_providers()],
        "min_topup": S.min_topup(), "mao_per_usd": S.mao_per_usd()}


@router.post("/orders")
def create_order(req: OrderReq, user=Depends(current_user)):
    try:
        order, pay_info = orders_mod.create_order(
            USER_DB, user, req.amount, req.provider)
    except orders_mod.OrderError as e:
        raise HTTPException(status_code=400,
                            detail={"code": e.code, "message": e.detail}) from e
    return {"ok": True, "order": order, "payment": pay_info}


@router.get("/orders/{out_trade_no}")
def get_my_order(out_trade_no: str, user=Depends(current_user)):
    """单笔订单状态 —— 前端付款窗口打开后轮询这个,拿到 completed 就停。

    按 user_id 过滤:订单号是可猜的(PG+时间戳+8 位 hex),不校验归属就是越权读。
    """
    order = USER_DB.get_order_by_trade_no(out_trade_no)
    if order is None or order["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order": order}


@router.get("/orders")
def list_my_orders(limit: int = 50, offset: int = 0, user=Depends(current_user)):
    return {"orders": USER_DB.list_orders(user_id=user["id"],
                                          limit=min(limit, 200), offset=offset)}


# ---- Token 消耗榜 ----

@router.get("/leaderboard")
def token_leaderboard(range: str = Query(default="all", pattern="^(all|7d|30d)$"),
                      page: int = Query(default=1, ge=1),
                      page_size: int = Query(default=20, ge=1, le=100),
                      user=Depends(current_user)):
    now = int(time.time())
    days = {"all": 0, "7d": 7, "30d": 30}[range]
    since = now - days * 86400 if days else 0
    result = USER_DB.token_leaderboard(user["id"], since, now, page, page_size)

    def public_row(row):
        if row is None:
            return None
        name = row["display_name"] or row["email"].split("@", 1)[0]
        masked = name[:1] + "***" + (name[-1:] if len(name) > 2 else "")
        return {
            "rank": row["rank"] if row["total_tokens"] else None,
            "position": row["position"], "name": masked,
            "is_me": row["user_id"] == user["id"],
            **{key: row[key] for key in (
                "total_tokens", "input_tokens", "output_tokens", "cache_tokens", "cache_rows", "requests")},
        }

    return {"rows": [public_row(row) for row in result["rows"]],
            "me": public_row(result["me"]), "range": range,
            "since": since, "as_of": now, "page": page, "page_size": page_size,
            "total_users": result["total_users"], "total_tokens": result["total_tokens"]}


# ---- 邀请 ----

@router.get("/invite")
def invite(user=Depends(current_user)):
    return {
        "aff_code": user["aff_code"],
        "invitees": USER_DB.count_invitees(user["id"]),
        "earned": USER_DB.ledger_sum(user["id"], reason="affiliate"),
        "reward_policy": S.invite_rewards(),
        "rewarded_invitees": USER_DB.invite_reward_count(user["id"]),
        "registration_policy": registration_policy(),
        "require_invite": _require_invite(),
    }


# ---- 公告 ----


@router.get("/announcements")
def list_announcements(user=Depends(current_user)):
    """用户可见公告 + 每条的 read_at。

    read_at 落库而不是留在前端 localStorage:换台设备、清一次缓存就该重看一遍
    公告不合理,而「谁读过哪条」也是站长想知道的事(见 /admin/announcements
    的 read_count)。read_at < updated_at 即为未读 —— 改过正文的公告会重新亮。
    """
    reads = USER_DB.announcement_reads(user["id"])
    items = []
    for a in _ann_visible():
        row = dict(a)
        row["read_at"] = reads.get(a["id"], 0)
        row["unread"] = row["read_at"] < (a.get("updated_at")
                                          or a.get("created_at") or 1)
        items.append(row)
    return {"announcements": items,
            "unread": sum(1 for x in items if x["unread"])}


class AnnouncementReadReq(BaseModel):
    # 空/缺省 = 全部标已读(铃铛面板的「全部已读」);给 id 列表则只标这几条。
    ids: list[str] | None = None


@router.post("/announcements/read")
def mark_announcements_read(req: AnnouncementReadReq, user=Depends(current_user)):
    visible = {a["id"] for a in _ann_visible()}
    ids = [i for i in (req.ids or visible) if i in visible]
    n = USER_DB.mark_announcements_read(user["id"], ids)
    return {"ok": True, "marked": n}

# ---- 模型状态(小时级巡检) ----

@router.get("/admin/model-status")
def admin_model_status(user=Depends(require_admin)):
    """在营渠道每个模型的最近一次检测结果 + 最近 24 次心跳 + 7 天按天状态条。

    ok: 1=实测通过 0=实测失败 -1=号池空(没法测);没有记录 = 还没轮到。
    heartbeats: 按小时聚合(巡检每小时一轮),渠道视角取最差。
    bars: 渠道视角按天取最差 —— 当天任一次失败就是红。生成类(图/视频)
    不巡检(烧真金 credits),不出现在这里。"""
    stored = {(r["channel"], r["model"]): r
              for r in USER_DB.latest_model_status()}
    since = time.time() - model_status.RETENTION
    hist = {}
    for r in USER_DB.history_model_status(since):
        hist.setdefault((r["channel"], r["model"]), []).append(r)
    out = []
    for name, ad in sorted(adapter_mod.enabled_adapters().items()):
        if getattr(ad, "kind", "chat") == "generation":
            continue
        for model in sorted(model_status.monitored_models(ad)):
            r = stored.get((name, model)) or {}
            rows = hist.get((name, model), [])
            out.append({"channel": name, "model": model,
                        "kind": getattr(ad, "kind", "chat"),
                        "ok": r.get("ok"),
                        "latency_ms": r.get("latency_ms") or 0,
                        "ttft_ms": r.get("ttft_ms"),
                        "error": r.get("error"),
                        "checked_at": r.get("checked_at"),
                        "heartbeats": model_status.hour_bars(rows, worst=True),
                        "bars": model_status.day_bars(rows, worst=True)})
    return {"interval": config.MODEL_STATUS_INTERVAL,
            "running": model_status.is_running(),
            "rows": out}


@router.post("/admin/model-status/run")
async def admin_run_model_status(user=Depends(require_admin)):
    """手动触发一轮检测,立刻返回 —— 一轮要几分钟,不能挂在 HTTP 请求上。

    与定时轮共用一道闸:同时在跑的最多一轮。started=False 表示已有一轮在跑。"""
    if model_status.is_running():
        return {"ok": True, "started": False}
    asyncio.get_running_loop().create_task(model_status.run_checked(
        USER_DB, pool_state.POOL, pool_state.DB, adapter_mod.enabled_adapters))
    return {"ok": True, "started": True}


@router.get("/model-status")
def model_status_overview(user=Depends(current_user)):
    """用户侧模型状态:按对外模型名聚合,故障与暂不可用排最前。

    与管理台版本的区别:不出上游错误原话、不做手动触发 —— 用户只需要
    「现在哪个模型能不能用」。每个模型附带当前承载它的渠道名(channels),
    方便对照多渠道切换。bars 是用户视角的 7 天状态条:
    按天取最好(多渠道故障切换,当天任一渠道能用就算绿)。"""
    stored = {(r["channel"], r["model"]): r
              for r in USER_DB.latest_model_status()}
    since = time.time() - model_status.RETENTION
    hist = {}
    for r in USER_DB.history_model_status(since):
        hist.setdefault((r["channel"], r["model"]), []).append(r)
    smap = model_status.user_status_map(adapter_mod.enabled_adapters(), stored, hist,
                                       with_channels=True)
    rows = [{"model": m, **smap[m]} for m in sorted(smap)]
    order = {model_status.FAIL: 0, model_status.SKIP: 1, None: 2, model_status.OK: 3}
    rows.sort(key=lambda r: (order.get(r["ok"], 2), r["model"]))
    summary = {"ok": 0, "slow": 0, "fail": 0, "pending": 0}
    for r in rows:
        if r["ok"] is None:
            summary["pending"] += 1
        elif model_status.grade_of(r["ok"], r["ttft_ms"], r["latency_ms"]) == \
                model_status.BAR_YELLOW:
            summary["slow"] += 1
        elif r["ok"] == model_status.OK:
            summary["ok"] += 1
        else:
            summary["fail"] += 1
    return {"interval": config.MODEL_STATUS_INTERVAL,
            "summary": summary, "rows": rows}


# ---- 共享 Key ----
#
# 用户把自己在某条渠道上的 key 交上来换余额奖励,规则存 settings.share_rewards。
# 收 key 这件事本身有副作用(入库 + 发钱 + 打一次上游),所以:
#   * 只认登录用户(不挂 optional_user)
#   * 失败一律 400 带原因,不吞成 500
#   * 幂等由 credit_ledger 的 idem_key 兜底,重复提交不会重复发奖

class ShareKeyReq(BaseModel):
    channel: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=8, max_length=512)


@router.get("/share/keys")
def share_keys_info(user=Depends(current_user)):
    """共享规则 + 我交过的 key + 已经拿了多少。控制台「共享 Key」页一次拉全。"""
    return share_mod.my_summary(USER_DB, user["id"])


@router.post("/share/keys")
def share_key_submit(req: ShareKeyReq, user=Depends(current_user)):
    """交一把 key。验活通过才入库并按把发奖;不通过把上游原话回给用户。"""
    try:
        return share_mod.submit(USER_DB, user["id"], req.channel, req.key)
    except share_mod.ShareError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/admin/share/contributions")
def admin_share_contributions(limit: int = Query(200, ge=1, le=1000),
                              user=Depends(require_admin)):
    """最近由用户共享进来的 key(脱敏 identity,无明文 key)。"""
    return {"contributions": share_mod.contributions(limit=limit)}


@router.get("/admin/groups")
def admin_list_groups(user=Depends(require_admin)):
    return {"groups": USER_DB.list_groups()}


@router.post("/admin/groups")
def admin_create_group(req: GroupReq, user=Depends(require_admin)):
    if USER_DB.get_group_by_name(req.name):
        raise HTTPException(status_code=409, detail="group name exists")
    if req.billing_policy not in ("balance", "quota", "free"):
        raise HTTPException(status_code=400,
                            detail="billing_policy must be balance|quota|free")
    # 上架就是在卖东西:免费策略的套餐收钱说不通,0 元的"套餐"也不该出现在
    # 可购列表里(用户点了不扣钱、却换掉了自己的计费档,那是个白送的漏洞)。
    if req.listed and (req.price or 0) <= 0:
        raise HTTPException(status_code=400,
                            detail="listed plan needs price > 0")
    gid = USER_DB.create_group(
        name=req.name, rate_multiplier=req.rate_multiplier,
        supported_models=req.supported_models, billing_policy=req.billing_policy,
        concurrency_limit=req.concurrency_limit,
        daily_limit=req.daily_limit, weekly_limit=req.weekly_limit,
        monthly_limit=req.monthly_limit, limit_unit=req.limit_unit,
        is_default=req.is_default, status=req.status,
        listed=req.listed, price=req.price,
        duration_hours=req.duration_hours, notes=req.notes)
    return {"ok": True, "id": gid}


@router.patch("/admin/groups/{group_id}")
def admin_update_group(group_id: int, req: GroupUpdateReq, user=Depends(require_admin)):
    old = USER_DB.get_group(group_id)
    if not old:
        raise HTTPException(status_code=404, detail="group not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if fields.get("billing_policy") not in (None, "balance", "quota", "free"):
        raise HTTPException(status_code=400,
                            detail="billing_policy must be balance|quota|free")
    # 只改 listed 不带 price 时,要看库里现有的价 —— 否则「上架」这一下就把
    # 一个 0 元档推到可购列表里,用户点一次白换一个计费档。
    if fields.get("listed"):
        price = fields.get("price", old.get("price") or 0)
        if (price or 0) <= 0:
            raise HTTPException(status_code=400,
                                detail="listed plan needs price > 0")
    if fields:
        USER_DB.update_group(group_id, **fields)
        BILLING.invalidate()
        PRICING.invalidate()
    return {"ok": True}


@router.delete("/admin/groups/{group_id}")
def admin_delete_group(group_id: int, user=Depends(require_admin)):
    """删组。默认组不许删,还有用户挂着的也不许删。

    用户的 group_id / plan_group_id 指着一个不存在的组时,effective_group 返回
    None,那个用户的每次调用都会被模型白名单挡成 403 —— 报错里看不出是删组导致
    的。要让套餐停止售卖用「下架」,要让它停止生效用「停用」,这两条都可逆。
    """
    g = USER_DB.get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    if g.get("is_default"):
        raise HTTPException(status_code=400,
                            detail="default group cannot be deleted")
    refs = USER_DB.count_group_refs(group_id)
    if refs:
        raise HTTPException(
            status_code=409,
            detail=f"{refs} user(s) still on this group, disable it instead")
    USER_DB.delete_group(group_id)
    BILLING.invalidate()
    PRICING.invalidate()
    return {"ok": True}


@router.get("/admin/users")
def admin_list_users(limit: int = 100, offset: int = 0, q: str = None,
                     group_id: int = None, status: str = None, role: str = None,
                     user=Depends(require_admin)):
    """用户列表 + 检索。q 匹配邮箱/昵称子串、返佣码、数字 id;total 是过滤后的总数,
    分页器要靠它算页数。"""
    limit = max(1, min(int(limit), 500))
    users, total = USER_DB.search_users(q=q, group_id=group_id, status=status,
                                        role=role, limit=limit, offset=max(0, offset))
    for u in users:
        u.pop("password_hash", None)
        # TOTP 密钥拿到就能生成验证码,与密码哈希同级的机密;只回「开没开」
        u["totp_enabled"] = bool(u.pop("totp_secret", None))
        u.pop("totp_pending", None)
        u.pop("totp_last_step", None)
    return {"users": users, "total": total, "limit": limit, "offset": max(0, offset)}


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, req: UserAdminReq, user=Depends(require_admin)):
    target = USER_DB.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "group_id" in fields and not USER_DB.get_group(fields["group_id"]):
        raise HTTPException(status_code=400, detail="target group not found")
    if fields.get("role") not in (None, "user", "admin"):
        raise HTTPException(status_code=400, detail="role must be user or admin")
    # 不允许把最后一个 admin 降级/禁用,防止锁死自己
    if target["role"] == "admin" and (fields.get("role") == "user"
                                      or fields.get("status") == "disabled"):
        admins = [u for u in USER_DB.list_users(limit=10000)
                  if u["role"] == "admin" and u["status"] == "active"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400,
                                detail="cannot demote or disable the last active admin")
    if fields:
        USER_DB.update_user(user_id, **fields)
        BILLING.invalidate()  # 用户组/状态/角色变更需失效鉴权缓存
    return {"ok": True}


@router.post("/admin/users/{user_id}/password")
def admin_reset_password(user_id: int, req: AdminResetPwReq,
                         user=Depends(require_admin)):
    """管理员重置他人密码(无需旧密码)。该用户既有会话随之作废。"""
    if not USER_DB.get_user(user_id):
        raise HTTPException(status_code=404, detail="user not found")
    USER_DB.set_password(user_id, auth.hash_password(req.new_password))
    return {"ok": True}


@router.post("/admin/users/{user_id}/2fa/disable")
def admin_disable_totp(user_id: int, user=Depends(require_admin)):
    """手机丢了的兜底:另一位管理员替他关掉二次验证。不能关自己的 —— 自己的走
    /2fa/disable 要密码 + 验证码,这条路没有那两样,给自己开着就是绕过。"""
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="关闭自己的二次验证请走个人资料页")
    target = USER_DB.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    USER_DB.update_user(user_id, totp_secret=None, totp_pending=None, totp_last_step=0)
    return {"ok": True}


@router.get("/admin/settings")
def admin_get_settings(user=Depends(require_admin)):
    """运行时设置(存库,覆盖环境变量默认值)。

    epay_key 只回「设了没有」和末 4 位,不回明文:这个端点的凭据是浏览器里的 JWT,
    portal 前端任何一个 XSS 都能读走响应体。同理不给它设 defaults 回显 ——
    那会把环境变量里的商户密钥也一起吐出来。
    """
    key = S.epay_key()
    off = set(adapter_mod.disabled_channels())
    return {
        "require_invite": _require_invite(),
        "registration_closed": _registration_closed(),
        "registration_policy": registration_policy(),
        "default_group": USER_DB.get_setting("default_group", config.DEFAULT_GROUP),
        "defaults": {  # 环境变量里的默认值,便于对比。刻意不含 epay_key。
            "require_invite": config.REQUIRE_INVITE,
            "registration_closed": config.REGISTRATION_CLOSED,
            "community_min_level": config.COMMUNITY_MIN_LEVEL,
            "default_group": config.DEFAULT_GROUP,
            "payment_providers": config.PAYMENT_PROVIDERS,
            "min_topup": config.MIN_TOPUP,
            "site_url": config.SITE_URL,
            "epay_api_url": config.EPAY_API_URL,
            "epay_pid": config.EPAY_PID,
            "epay_usd_rate": config.EPAY_USD_RATE,
            "baipiao_pid": config.BAIPIAO_PID,
            "mao_per_usd": config.MAO_PER_USD,
            "disabled_channels": config.DISABLED_CHANNELS,
            "checkin_min": config.CHECKIN_MIN,
            "checkin_max": config.CHECKIN_MAX,
            "invite_rewards": S.invite_reward_defaults(),
            "share_rewards": S.share_reward_defaults(),
        },
        "pricing_catalog": pricing_catalog.status(),
        # 生效值(库里有就是库里的)
        "payment_providers": S.payment_providers(),
        "min_topup": S.min_topup(),
        "site_url": S.site_url(),
        "epay_api_url": S.epay_api_url(),
        "epay_pid": S.epay_pid(),
        "epay_usd_rate": S.epay_usd_rate(),
        "baipiao_pid": S.baipiao_pid(),
        "baipiao_key_set": bool(S.baipiao_key()),
        "mao_per_usd": S.mao_per_usd(),
        "epay_key_set": bool(key),
        "epay_key_tail": key[-4:] if len(key) >= 4 else "",
        "checkin_min": S.checkin_min(),
        "checkin_max": S.checkin_max(),
        "invite_rewards": S.invite_rewards(),
        "share_rewards": S.share_rewards(),
        # 邮件。密码同 epay_key 口径:只回「设了没有」
        "smtp_host": S.smtp_host(),
        "smtp_port": S.smtp_port(),
        "smtp_user": S.smtp_user(),
        "smtp_pass_set": bool(S.smtp_pass()),
        "smtp_from": S.smtp_from(),
        "smtp_from_name": S.smtp_from_name(),
        "smtp_security": S.smtp_security(),
        "site_name": S.site_name(),
        "mail_configured": mailer.configured(),
        # 哪些项已经被库里的值覆盖 —— 前端据此显示「已覆盖 / 来自环境变量」
        "from_db": {k: S.is_from_db(k)
                    for k in PAY_KEYS + OTHER_SETTING_KEYS},
        "registered_providers": sorted(all_providers()),
        # 渠道开关的当前值。渠道目录(模型清单/上下线状态)在 /admin/channels ——
        # 同一件事只有一处口径,这里只回「设置的值」。
        "disabled_channels": sorted(off),
        "plugins": config.PLUGINS,
    }




@router.patch("/admin/settings")
def admin_update_settings(req: SettingsReq, user=Depends(require_admin)):
    data = req.model_dump()
    # 「没传这个键」与「传了 null」是两个意图,不能都当成本次不改:
    # 管理台把数值框清空发出的就是 null,静默跳过会弹「已保存」而库里没动,
    # 重新加载后值又回来了 —— 界面显示一套、实际另一套。
    # 数值项也没有「空」这个合法值,想回到环境变量默认值只有 DELETE 一条路。
    nulled = [k for k in sorted(req.model_fields_set) if data.get(k) is None]
    if nulled:
        raise HTTPException(
            status_code=400,
            detail=f"{', '.join(nulled)} 不能为空;要恢复环境变量默认值请用 "
                   f"DELETE /admin/settings/{nulled[0]}")
    fields = {k: data[k] for k in req.model_fields_set}
    if "default_group" in fields and not USER_DB.get_group_by_name(fields["default_group"]):
        raise HTTPException(status_code=400, detail="default_group not found")
    known = set(all_providers())
    channels = _channel_names()
    for k in PAY_KEYS + OTHER_SETTING_KEYS:
        if k in fields:
            try:
                # 校验在写库之前。读取侧刻意不做静默兜底 —— 那会造出
                # 「界面显示 7.5、实际按 7.2 收」这种查不出来的账。
                fields[k] = S.validate(k, fields[k], known_providers=known,
                                       known_channels=channels)
            except S.SettingError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
    checkin_min = fields.get("checkin_min", S.checkin_min())
    checkin_max = fields.get("checkin_max", S.checkin_max())
    if checkin_min > checkin_max:
        raise HTTPException(status_code=400,
                            detail="签到最低额度不能高于最高额度")
    for k, v in fields.items():
        USER_DB.set_setting(k, v)
    if "disabled_channels" in fields or "channel_routing" in fields:
        # 注册表是纯内存的,写完库必须立刻推一次,否则「存库即生效」变成「重启才生效」
        apply_channel_switches()
    return {"ok": True, "updated": list(fields),
            # 回「实际注册成功的渠道」而不是「你提交的渠道」
            "active_providers": S.payment_providers(),
            "disabled_channels": adapter_mod.disabled_channels()}


@router.delete("/admin/settings/{key}")
def admin_reset_setting(key: str, user=Depends(require_admin)):
    """删掉一条运行时设置,让它回落环境变量默认值。

    这是「恢复默认」的唯一入口。没有它,管理员只能往输入框里填空,
    而空串在 present-wins 语义下是「明确设为空」= 停用,不是「恢复默认」。
    """
    if key not in PAY_KEYS and key not in OTHER_SETTING_KEYS and key not in (
            "require_invite", "registration_closed", "default_group"):
        raise HTTPException(status_code=400, detail=f"unknown setting: {key}")
    if key in ("checkin_min", "checkin_max"):
        low = config.CHECKIN_MIN if key == "checkin_min" else S.checkin_min()
        high = config.CHECKIN_MAX if key == "checkin_max" else S.checkin_max()
        if low > high:
            raise HTTPException(
                status_code=400,
                detail="恢复该默认值会导致签到最低额度高于最高额度")
    USER_DB.delete_setting(key)
    if key in ("disabled_channels", "channel_routing"):
        apply_channel_switches()
    return {"ok": True, "reset": key}


class MailTestReq(BaseModel):
    to: EmailStr


@router.post("/admin/mail/test")
def admin_mail_test(req: MailTestReq, user=Depends(require_admin)):
    """按当前生效的 SMTP 设置发一封测试邮件。失败把 SMTP 的原话回给管理员 ——
    「认证失败」和「连不上 465」是两种要改的东西,只回「发送失败」等于没说。"""
    subject, text, html = mailer.test_mail(S.site_name())
    try:
        mailer.send(str(req.to), subject, text, html)
    except mailer.MailError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, "to": str(req.to)}

