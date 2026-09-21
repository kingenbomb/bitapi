#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户接入层 REST API(/api/*)—— 注册/登录/资料/密钥/用量/计费/钱包/兑码/订单/管理端。

注册强制邀请码(settings.require_invite 覆盖 config.REQUIRE_INVITE)时,invite_code
只接受管理台发的一次性注册邀请码(invitation 型兑换码)。老用户的 aff_code 仅在
开放注册时用于绑定返佣关系,不能绕过邀请制。首个用户(库空)免码成为 admin。
会话用 JWT(Authorization: Bearer <jwt>),与网关 /v1/* 的 sk- key 鉴权分离。
"""
import hashlib
import ipaddress
import os
import secrets
import threading
import time
from datetime import date as _date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

import config
from core import auth
from core import adapter as adapter_mod
from core import channels as channels_mod
from core import community_auth
from core import mailer
from core import pool_state
from core import orders as orders_mod
from core import pricing_catalog
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
                          CommunityInviteError, CommunityUserDisabled)

router = APIRouter(prefix="/api")


# ---- 依赖:JWT 当前用户 ----

def current_user(authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    payload = auth.decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = USER_DB.get_user(int(payload["sub"]))
    if not user or user["status"] != "active":
        raise HTTPException(status_code=401, detail="user not found or disabled")
    # 改过密码之后,之前签发的会话一律作废。找回密码的意义之一就是把偷走会话的
    # 人踢出去;JWT 无状态,能做到这一点的只有「签发时间 < 改密时间 → 拒」。
    if int(payload.get("iat") or 0) < int(user.get("password_changed_at") or 0):
        raise HTTPException(status_code=401, detail="session expired after password change")
    return user


def require_admin(user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def optional_user(authorization: str = Header(None)):
    """不带 Authorization 就按游客放行,带了就必须是有效登录。

    token 存在但过期/无效仍然 401,不静默降级成游客 —— 否则一个自认为已登录的
    用户会看到游客口径的价,而页面上没有任何迹象说明他其实掉线了。
    """
    if not (authorization or "").strip():
        return None
    return current_user(authorization)


# ---- 请求体 ----

class RegisterReq(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    invite_code: str = ""


class LoginReq(BaseModel):
    email: EmailStr
    password: str


class CommunityStartReq(BaseModel):
    purpose: str = Field(pattern="^(login|bind)$")


class CommunityFinishReq(BaseModel):
    invite_code: str = Field(default="", max_length=128)


class KeyCreateReq(BaseModel):
    name: str = ""
    count: int = Field(default=1, ge=1, le=50)
    expires_at: int = 0          # unix 秒,0=永不过期
    quota: float = Field(default=0, ge=0)     # 本密钥消费上限(美元),0=不限
    allowed_models: list[str] | None = None   # 空/None=跟随分组
    allowed_ips: list[str] | None = None      # 空/None=不限


class KeyUpdateReq(BaseModel):
    name: str | None = None
    status: str | None = None
    expires_at: int | None = None
    quota: float | None = Field(default=None, ge=0)
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
    code: str


# ---- 认证 ----

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


def _claim_invite_code(code):
    """占用一张管理台发的一次性邀请码;抢到返回 True。

    先占用再建号,不是先建号再占用:反过来的话并发的两个注册都能通过校验,
    一张一次性码进两个人。代价是占用那一刻还没有 user_id,建号之后回填
    (bind_code_user)。过期在这里判 —— claim_code 只看 status。
    """
    rec = USER_DB.get_code(code)
    if not rec or rec["type"] != TYPE_INVITATION:
        return False
    if rec.get("expires_at") and rec["expires_at"] < int(time.time()):
        return False
    return USER_DB.claim_code(code, None)


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


@router.post("/register")
def register(req: RegisterReq, request: Request = None):
    email = req.email.lower().strip()
    ip = throttle.client_ip(request)
    try:
        throttle.check((throttle.REGISTER_IP, ip, "register"))
    except throttle.AuthThrottled as e:
        raise _throttled(e) from e
    if USER_DB.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="email already registered")

    is_first = USER_DB.count_users() == 0
    inviter_id = None
    claimed = None      # 已占用的一次性邀请码,建号后回填使用者
    role = "user"

    if is_first:
        role = "admin"  # 首个用户免码,成为根邀请人/管理员
    else:
        require_invite = _require_invite()
        code = auth.normalize_aff_code(req.invite_code)
        if require_invite and not code:
            raise HTTPException(status_code=403, detail="invite code required")
        if require_invite:
            # 邀请制的通行证只能由管理员发放。个人返佣码可以无限传播,
            # 若也能过这道门,开启邀请制就失去意义。
            if _claim_invite_code(code):
                claimed = code
            else:
                raise HTTPException(status_code=403, detail="invalid invite code")
        elif code:
            # 开放注册时个人码只负责返佣归因；一次性注册码仍可照常使用。
            inviter = USER_DB.get_user_by_aff_code(code)
            if inviter:
                inviter_id = inviter["id"]
            elif _claim_invite_code(code):
                claimed = code
            else:
                raise HTTPException(status_code=403, detail="invalid invite code")

    group = ensure_default_group()
    aff = _alloc_aff_code()

    uid = USER_DB.create_user(
        email=email,
        password_hash=auth.hash_password(req.password),
        aff_code=aff,
        group_id=group["id"] if group else None,
        inviter_id=inviter_id,
        role=role)
    if claimed:
        USER_DB.bind_code_user(claimed, uid)
    # 注册赠额。放在 emit 之前:插件可能在 user.registered 里读余额,
    # 先记账再广播,插件看到的就是最终值。
    if config.SIGNUP_BONUS:
        credit(uid, config.SIGNUP_BONUS, REASON_SIGNUP, f"signup:{uid}",
               meta={"source": "register"})
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
        authorize_url = community_auth.authorize_url(state)
    except community_auth.CommunityAuthError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    now = int(time.time())
    USER_DB.create_community_flow(
        community_auth.digest(state), community_auth.digest(nonce),
        req.purpose, user["id"] if user else None,
        now + community_auth.FLOW_TTL)
    _set_community_flow_cookie(response, nonce)
    return {"ok": True, "authorize_url": authorize_url}


def community_oauth_callback(request: Request, code="", state=""):
    """生产登记的无 `/api` callback；由 server.py 的公开路由调用。"""
    nonce = request.cookies.get(community_auth.FLOW_COOKIE, "")
    if not code or not state or not nonce:
        raise HTTPException(status_code=400,
                            detail="community authorization state is missing")
    now = int(time.time())
    flow = USER_DB.claim_community_flow(
        community_auth.digest(state), community_auth.digest(nonce), now)
    if not flow:
        raise HTTPException(status_code=400,
                            detail="community authorization state is invalid or expired")
    try:
        upstream_token = community_auth.exchange_code(code)
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
def community_auth_finish(req: CommunityFinishReq, request: Request,
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

    code = auth.normalize_aff_code(req.invite_code)
    if not code:
        # 不消费 ready flow；前端补邀请码后可用同一个 HttpOnly cookie 再提交。
        return {"ok": True, "needs_invite": True,
                "community": _public_community(flow)}

    group = ensure_default_group()
    created = None
    for _ in range(10):
        aff = _alloc_aff_code()
        try:
            created = USER_DB.register_community_user(
                flow["id"], code, aff,
                group_id=group["id"] if group else None)
            break
        except CommunityAffCodeConflict:
            continue
        except CommunityInviteError as e:
            raise HTTPException(status_code=403,
                                detail="invalid invite code") from e
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
        "rpm_limit": (group or {}).get("rpm_limit") or 0,
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
def email_bind(req: EmailBindReq, user=Depends(current_user)):
    """发验证码。配了 SMTP 就真发;没配就退回原来的「写日志」口径并如实告知
    (sent=False),前端据此提示「站点未接邮件,请联系站长」而不是「已发送」。"""
    code = auth.generate_email_code()
    email = req.email.lower().strip()
    USER_DB.add_email_verification(user["id"], email, code)
    if not mailer.configured():
        print(f"[portal] email verify code for user {user['id']} "
              f"({email}): {code}", flush=True)
        return {"ok": True, "sent": False,
                "message": "站点未配置邮件服务,验证码已写入服务端日志"}
    subject, text, html = mailer.verify_email_mail(S.site_name(), code, 15)
    try:
        mailer.send(email, subject, text, html)
    except mailer.MailError as e:
        raise HTTPException(status_code=502, detail=f"邮件发送失败:{e}") from e
    return {"ok": True, "sent": True}


@router.post("/email/verify")
def email_verify(req: EmailVerifyReq, user=Depends(current_user)):
    verif = USER_DB.get_latest_verification_by_user(user["id"])
    if not verif:
        raise HTTPException(status_code=400, detail="no pending verification")
    if verif.get("verified_at"):
        raise HTTPException(status_code=400, detail="already verified")
    if verif["expires_at"] < int(time.time()):
        raise HTTPException(status_code=400, detail="code expired")
    if req.code.strip() != verif["code"]:
        raise HTTPException(status_code=400, detail="wrong code")
    USER_DB.mark_verification_used(verif["id"])
    USER_DB.update_user(user["id"], email_verified_at=int(time.time()))
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
    today_usage = USER_DB.api_key_usage_map(user["id"], since=now - 86400)
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

def _usage_filters(model, channel, since, until, end_reason, api_key_id=None):
    """用户侧与管理端共用的检索条件整理。since/until 是 unix 秒;
    since 传 0/None = 不限。"""
    f = {"model": (model or "").strip() or None,
         "channel": (channel or "").strip() or None,
         "since": int(since) if since else 0,
         "until": int(until) if until else None,
         "end_reason": (end_reason or "").strip() or None}
    if api_key_id is not None:
        f["api_key_id"] = api_key_id
    return f


_CSV_MAX = 10000   # 单次导出上限。再大走分段:一次把全表拉进内存会把 1G 的机器顶爆


def _usage_csv(rows, with_user=False, filename="usage.csv"):
    """usage_logs 行 → CSV 响应。列顺序固定,快照里的字段(首字延迟/结束原因/档位)
    抠出来平铺 —— 拿去 Excel 里对账的人不该自己解析 JSON。"""
    import csv
    import io

    from fastapi.responses import Response

    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["id", "time", "channel", "model", "input_tokens", "output_tokens",
            "cache_tokens", "cost", "actual_cost", "billing_mode", "token_source",
            "stream", "duration_ms", "first_token_ms", "end_reason", "tier",
            "request_id"]
    if with_user:
        head[2:2] = ["email", "key"]
    w.writerow(head)
    for r in rows:
        snap = r.get("pricing_snapshot") or {}
        if not isinstance(snap, dict):
            snap = {}
        total_ctx = snap.get("total_ctx")
        cache = max((total_ctx or 0) - (r.get("input_tokens") or 0), 0) if total_ctx else ""
        line = [r.get("id"),
                datetime.fromtimestamp(r.get("created_at") or 0).strftime("%Y-%m-%d %H:%M:%S"),
                r.get("channel") or "", r.get("model") or "",
                r.get("input_tokens") or 0, r.get("output_tokens") or 0, cache,
                r.get("cost") or 0, r.get("actual_cost") or 0,
                r.get("billing_mode") or "", r.get("token_source") or "",
                1 if r.get("stream") else 0, r.get("duration_ms") or 0,
                snap.get("frt_ms", ""), snap.get("end_reason", ""),
                snap.get("tier", ""), snap.get("request_id", "")]
        if with_user:
            line[2:2] = [r.get("email") or "", r.get("key_name") or ""]
        w.writerow(line)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


_RANGE_SPAN = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}


def _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason):
    """用户侧列表 / 合计 / 导出共用的条件。时间窗:显式传 since/until 就用它们,
    否则按 range 取窗口 —— 三处必须同一口径,否则「今日」页面上导出的却是全部历史。"""
    if not since and not until:
        since = int(time.time()) - _RANGE_SPAN.get(range, 86400)
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
        user["id"], int(time.time()) - _RANGE_SPAN.get(range, 86400))
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    f = _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason)
    rows, total = USER_DB.query_usage(limit=limit, offset=offset,
                                      user_id=user["id"], **f)
    totals = USER_DB.usage_totals(user_id=user["id"], **f)
    return {"range": range, "summary": summary, "recent": rows, "totals": totals,
            "total": total, "limit": limit, "offset": offset,
            "since": f["since"], "until": f["until"]}


@router.get("/usage/export.csv")
def usage_export(range: str = "day", model: str = None, channel: str = None,
                 api_key_id: int = None, since: int = None, until: int = None,
                 end_reason: str = None, user=Depends(current_user)):
    f = _user_usage_filters(range, model, channel, api_key_id, since, until, end_reason)
    rows, _ = USER_DB.query_usage(limit=_CSV_MAX, offset=0, user_id=user["id"], **f)
    return _usage_csv(rows, with_user=False,
                      filename=f"usage-{_date.today().isoformat()}.csv")


_CHECKIN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
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
    且 month 只是近 30 天不是「累计」。这里今日走 24 小时窗口、累计走全量,
    另附 RPM/TPM(按有记录的时间跨度平摊)与平均响应时长。
    """
    now = int(time.time())
    today = USER_DB.usage_overview(user["id"], since=now - 86400)
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
        d = _date.fromtimestamp(now - k * 86400).isoformat()
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
        "rpm_limit": (group or {}).get("rpm_limit") or 0,
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
        "rpm_limit": group["rpm_limit"],
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

# 图/视频模型把规格编进模型名第 3 段起(如 foo-vid/model-x/1080p/10s),
# 同一模型能展开出十几条。广场按前两段折叠成一张卡,规格进 variants。
_SPEC_SEGMENTS = 3


def _collapse_key(model):
    """返回 (卡片名, 规格后缀)。段数不足 3 的模型不折叠,后缀为空。"""
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
    from core.adapter import CAP_CHAT, enabled_adapters, model_to_channel

    if user:
        group = USER_DB.effective_group(user)
    else:
        group = ensure_default_group()
    gid = group["id"] if group else None
    primary = model_to_channel()
    cards = {}
    for channel, ad in enabled_adapters().items():
        # 能不能在线体验,取渠道自己声明的 CAP_CHAT —— 图/视频渠道
        # 走 /v1/images|videos/generations,塞进对话框只会得到 404。
        can_chat = ad.has(CAP_CHAT)
        for m in ad.models:
            if group and not BILLING.check_model_allowed(group, m):
                continue
            name, spec = _collapse_key(m)
            pr = PRICING.resolve(m, gid)
            row = cards.get(name)
            if row is None:
                # 同一模型挂多个渠道时卡片归首选渠道,channels 记全部 —— 广场上的
                # 「渠道」是产品口径的归属,备胎数量只作提示
                row = cards[name] = {
                    "model": name, "channel": primary.get(m, channel), "chat": can_chat,
                    "channels": [], "billing_mode": pr.get("billing_mode"),
                    "price_source": pr.get("source"),
                    "variants": [],
                }
                row.update({k: pr.get(k) for k in _PRICE_KEYS})
            if channel not in row["channels"]:
                row["channels"].append(channel)
            if spec:
                row["variants"].append(
                    {"spec": spec, "model": m,
                     "billing_mode": pr.get("billing_mode"),
                     "price_source": pr.get("source"),
                     "per_request_price": pr.get("per_request_price")})
    for row in cards.values():
        _fold_variants(row)
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
def redeem_code(req: RedeemReq, user=Depends(current_user)):
    try:
        ledger_row, code_row = redeem(USER_DB, req.code, user)
    except RedeemError as e:
        raise HTTPException(status_code=400,
                            detail={"code": e.code, "message": e.detail}) from e
    BILLING.invalidate()
    return {"ok": True, "value": code_row["value"],
            "balance": ledger_row.get("balance_after")}


class OrderReq(BaseModel):
    amount: float = Field(gt=0)
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
        "rpm_limit": g.get("rpm_limit") or 0,
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
        "min_topup": S.min_topup()}


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


# ---- 邀请 ----

@router.get("/invite")
def invite(user=Depends(current_user)):
    return {
        "aff_code": user["aff_code"],
        "invitees": USER_DB.count_invitees(user["id"]),
        "earned": USER_DB.ledger_sum(user["id"], reason="affiliate"),
    }


# ---- 公告 ----

_ANN_KEY = "announcements"
_ANN_LEVELS = ("info", "success", "warning", "error")
_ANN_MODES = ("silent", "popup")
# 公告正文存 settings 里的一个 JSON 列表,不单开表:一个自托管站的公告是个位数量级,
# 建表要连带迁移、索引、清理三件事,不值当。代价是写入必须读改写,不是原子操作,
# 所以加一把进程内锁 —— 单 worker 本来就是本项目的部署前提(见 README)。
# 已读状态是另一回事:它按用户增长且必须跨设备一致,落在 announcement_reads 表。
_ann_lock = threading.Lock()


def _ann_all():
    raw = USER_DB.get_setting(_ANN_KEY) or []
    return raw if isinstance(raw, list) else []


def _ann_sorted(items):
    """置顶优先,同组内按发布时间倒序。"""
    return sorted(items, key=lambda a: (0 if a.get("pinned") else 1,
                                        -(a.get("created_at") or 0)))


def _ann_find(items, aid):
    for i, a in enumerate(items):
        if a.get("id") == aid:
            return i
    return -1


def _ann_visible(now=None):
    """当前该展示的公告:已发布 + 落在展示窗口内。"""
    ts = int(now if now is not None else time.time())
    out = []
    for a in _ann_all():
        if not a.get("active"):
            continue
        starts = a.get("starts_at") or 0
        ends = a.get("ends_at") or 0
        if starts and ts < starts:
            continue
        if ends and ts > ends:
            continue
        out.append(a)
    return _ann_sorted(out)


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


# ---- 管理端(admin only) ----

class GroupReq(BaseModel):
    name: str
    rate_multiplier: float = 1.0
    supported_models: list = []
    billing_policy: str = "free"
    rpm_limit: int = 0
    daily_limit: int = 0
    weekly_limit: int = 0
    monthly_limit: int = 0
    limit_unit: str = "requests"
    is_default: int = 0
    status: str = "active"
    # 订阅套餐。listed=0 时这个分组只是管理员内部分配用的档位,不进用户端可购列表。
    listed: int = 0
    price: float = Field(default=0, ge=0)
    duration_hours: int = Field(default=0, ge=0)   # 1=小时卡 24=天卡 720=月卡 0=不限时
    notes: str | None = None


class GroupUpdateReq(BaseModel):
    rate_multiplier: float | None = None
    supported_models: list | None = None
    billing_policy: str | None = None
    rpm_limit: int | None = None
    daily_limit: int | None = None
    weekly_limit: int | None = None
    monthly_limit: int | None = None
    limit_unit: str | None = None
    is_default: int | None = None
    status: str | None = None
    listed: int | None = None
    price: float | None = Field(default=None, ge=0)
    duration_hours: int | None = Field(default=None, ge=0)
    notes: str | None = None


class UserAdminReq(BaseModel):
    group_id: int | None = None
    status: str | None = None
    role: str | None = None


class AdminResetPwReq(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


class SettingsReq(BaseModel):
    require_invite: bool | None = None
    default_group: str | None = None
    checkin_min: float | None = None
    checkin_max: float | None = None
    # 支付设置。None = 本次不改;给值 = 存库并从此覆盖环境变量。
    # 想恢复环境变量默认值走 DELETE /admin/settings/{key},不是往框里填空 ——
    # 空串在这里是「明确设为空」(等于停用),两个意图不能混成一个。
    payment_providers: list | None = None
    min_topup: float | None = None
    site_url: str | None = None
    epay_api_url: str | None = None
    epay_pid: str | None = None
    epay_key: str | None = None
    epay_usd_rate: float | None = None
    # 渠道上下线。传全量列表(不是增删单个):开关是一排复选,一次提交一份完整状态。
    disabled_channels: list | None = None
    # 渠道路由 {name: {priority, weight}},全量映射。单个渠道改用
    # PATCH /admin/channels/{name}/routing 更顺手。
    channel_routing: dict | None = None
    # 邮件设置。smtp_pass 只写不读,留空 = 不改。
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_from: str | None = None
    smtp_from_name: str | None = None
    smtp_security: str | None = None
    site_name: str | None = None


PAY_KEYS = ("payment_providers", "min_topup", "site_url",
            "epay_api_url", "epay_pid", "epay_key", "epay_usd_rate")
# 存库的其它设置项。与 PAY_KEYS 分开是因为支付那组有自己的「生效渠道」回执。
OTHER_SETTING_KEYS = ("disabled_channels", "channel_routing", "checkin_min",
                      "checkin_max") + S.SMTP_KEYS


class AnnouncementReq(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    level: str = "info"
    pinned: bool = False
    active: bool = True
    # silent=只进铃铛;popup=登录后弹一次(弹过即已读,不再弹)
    notify_mode: str = "silent"
    starts_at: int = 0      # 0=立即生效
    ends_at: int = 0        # 0=永久


class AnnouncementUpdateReq(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    level: str | None = None
    pinned: bool | None = None
    active: bool | None = None
    notify_mode: str | None = None
    starts_at: int | None = None
    ends_at: int | None = None


def _ann_check_window(starts, ends):
    if starts and ends and ends <= starts:
        raise HTTPException(status_code=400,
                            detail="ends_at must be later than starts_at")


@router.get("/admin/announcements")
def admin_list_announcements(user=Depends(require_admin)):
    """管理端看全部,含草稿与已过期的,并带每条的已读人数。

    read_count 是站长发完公告后第一个想知道的数,所以在列表里就给,
    不另开一个 read-status 端点。
    """
    items = []
    total_users = USER_DB.count_users()
    for a in _ann_sorted(_ann_all()):
        row = dict(a)
        row["read_count"] = USER_DB.announcement_read_count(a["id"])
        row["user_count"] = total_users
        items.append(row)
    return {"announcements": items}


@router.post("/admin/announcements")
def admin_create_announcement(req: AnnouncementReq, user=Depends(require_admin)):
    if req.level not in _ANN_LEVELS:
        raise HTTPException(status_code=400,
                            detail="level must be " + "|".join(_ANN_LEVELS))
    if req.notify_mode not in _ANN_MODES:
        raise HTTPException(status_code=400,
                            detail="notify_mode must be " + "|".join(_ANN_MODES))
    _ann_check_window(req.starts_at, req.ends_at)
    now = int(time.time())
    with _ann_lock:
        items = _ann_all()
        if len(items) >= 50:
            raise HTTPException(status_code=400,
                                detail="too many announcements, delete some first")
        row = {"id": secrets.token_hex(6), "title": req.title.strip(),
               "body": req.body.strip(), "level": req.level,
               "pinned": bool(req.pinned), "active": bool(req.active),
               "notify_mode": req.notify_mode,
               "starts_at": int(req.starts_at or 0),
               "ends_at": int(req.ends_at or 0),
               "created_at": now, "updated_at": now, "by": user["email"]}
        items.append(row)
        USER_DB.set_setting(_ANN_KEY, items)
    return {"ok": True, "announcement": row}


@router.patch("/admin/announcements/{ann_id}")
def admin_update_announcement(ann_id: str, req: AnnouncementUpdateReq,
                              user=Depends(require_admin)):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if fields.get("level") not in (None,) + _ANN_LEVELS:
        raise HTTPException(status_code=400,
                            detail="level must be " + "|".join(_ANN_LEVELS))
    if fields.get("notify_mode") not in (None,) + _ANN_MODES:
        raise HTTPException(status_code=400,
                            detail="notify_mode must be " + "|".join(_ANN_MODES))
    with _ann_lock:
        items = _ann_all()
        i = _ann_find(items, ann_id)
        if i < 0:
            raise HTTPException(status_code=404, detail="announcement not found")
        for k in ("title", "body"):
            if k in fields:
                fields[k] = fields[k].strip()
        merged = dict(items[i])
        merged.update(fields)
        _ann_check_window(merged.get("starts_at") or 0, merged.get("ends_at") or 0)
        items[i] = merged
        # 改了内容就抬 updated_at:read_at 落在它之前的人重新算未读。
        if fields:
            items[i]["updated_at"] = int(time.time())
        USER_DB.set_setting(_ANN_KEY, items)
        return {"ok": True, "announcement": items[i]}


@router.delete("/admin/announcements/{ann_id}")
def admin_delete_announcement(ann_id: str, user=Depends(require_admin)):
    with _ann_lock:
        items = _ann_all()
        i = _ann_find(items, ann_id)
        if i < 0:
            raise HTTPException(status_code=404, detail="announcement not found")
        items.pop(i)
        USER_DB.set_setting(_ANN_KEY, items)
    # 已读记录跟着删,不留孤儿行(id 是随机的,不会被后来的公告复用)
    USER_DB.drop_announcement_reads(ann_id)
    return {"ok": True}


def _common_prefix(items):
    """一组字符串的公共前缀。min/max 那两个就够 —— 字典序最小与最大的公共前缀
    就是全体的公共前缀。"""
    if not items:
        return ""
    a, b = min(items), max(items)
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return a[:i]


def _channel_wildcard(models):
    """整个渠道的通配写法,从模型名算出来,不猜。

    渠道里若有裸名模型(比如既暴露 `foo` 又暴露 `foo-bar`),手写 `foo-*` 会漏掉它
    —— 公共前缀给的是 `foo`,正好躲开这个坑。模型名是 vendor/model 形状的渠道没有
    公共前缀,返回 None:那种渠道只能逐个选,给一个错的通配比不给更糟。
    """
    if len(models) < 2:
        return None
    pre = _common_prefix(models)
    return pre + "*" if len(pre) >= 2 else None


@router.get("/admin/channels")
def admin_list_channels(user=Depends(require_admin)):
    """渠道目录:每个渠道的模型清单 + 上下线状态 + 来源 + 号池计数。

    给三处界面用:分组编辑里的「可用模型」下拉(要真实模型名,不能让管理员靠手打)、
    站点设置里的渠道开关、以及「渠道」页。列全量含已下线的 —— 已下线的渠道也要能
    出现在开关里,否则关了就再也开不回来。
    """
    off = set(adapter_mod.disabled_channels())
    pool = pool_state.DB.stats()
    # 每个模型挂了几个渠道:>1 的在界面上标出来,站长才知道哪些模型有备胎
    owners = {}
    for n, ad in adapter_mod.all_adapters().items():
        for m in ad.models:
            owners.setdefault(m, []).append(n)
    out = []
    for name, ad in sorted(adapter_mod.all_adapters().items()):
        models = sorted(ad.models)
        st = pool.get(name) or {"total": 0, "by_status": {}}
        priority, weight = adapter_mod.routing_of(name)
        item = {"name": name, "kind": getattr(ad, "kind", "chat"),
                "disabled": name in off, "models": models,
                "wildcard": _channel_wildcard(models),
                "priority": priority, "weight": weight,
                "shared_models": sorted(m for m in models if len(owners.get(m, [])) > 1),
                "source": "data" if channels_mod.is_data_channel(name) else "code",
                "billing_mode": getattr(ad, "billing_mode", None),
                "stateless_keys": bool(getattr(ad, "stateless_keys", False)),
                "streaming": bool(getattr(ad, "streaming", False)),
                "proxy": bool(getattr(ad, "proxy", False)),
                "capabilities": list(getattr(ad, "capabilities", [])),
                "pool": {"total": st["total"], **(st.get("by_status") or {})}}
        if hasattr(ad, "describe"):
            item["config"] = ad.describe()
        out.append(item)
    return {"channels": out}


class ChannelReq(BaseModel):
    """数据渠道。models / model_map / headers 既收结构化值也收文本(多行 / a=b)。"""
    name: str | None = None
    type: str = "openai"
    base_url: str
    chat_path: str | None = "/chat/completions"
    models: list | str | None = None
    model_map: dict | str | None = None
    headers: dict | str | None = None
    timeout: int | None = 0
    notes: str | None = None


def _channel_row(name):
    row = USER_DB.get_channel(name)
    if row is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return row


def _reload_channels():
    """增删改之后重放注册表并重推开关。渠道开关存的是名字,删了的渠道留在里面无害。"""
    from core.portal_state import load_data_channels
    load_data_channels()
    apply_channel_switches()
    BILLING.invalidate()


@router.post("/admin/channels")
def admin_create_channel(req: ChannelReq, user=Depends(require_admin)):
    """建一个 OpenAI 兼容渠道。存库即注册,模型立刻出现在清单与路由里;
    key 另导(/admin/channels/{name}/keys),没 key 之前调用会 503「没有可用账号」。"""
    try:
        data = channels_mod.validate(req.model_dump(), USER_DB)
    except channels_mod.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    name = data.pop("name")
    USER_DB.create_channel(name, **data)
    _reload_channels()
    return {"ok": True, "name": name, "channel": USER_DB.get_channel(name)}


@router.patch("/admin/channels/{name}")
def admin_update_channel(name: str, req: ChannelReq, user=Depends(require_admin)):
    _channel_row(name)
    if not channels_mod.is_data_channel(name):
        raise HTTPException(status_code=400, detail="代码渠道不能在管理台修改")
    body = req.model_dump()
    body["name"] = name       # 不支持改名:accounts.channel 挂在名字上,改名等于丢号池
    try:
        data = channels_mod.validate(body, USER_DB, editing=name)
    except channels_mod.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    data.pop("name")
    USER_DB.update_channel(name, **data)
    _reload_channels()
    return {"ok": True, "channel": USER_DB.get_channel(name)}


@router.delete("/admin/channels/{name}")
def admin_delete_channel(name: str, user=Depends(require_admin)):
    """删渠道连同它的号池。号挂在渠道名上,渠道没了号就没有任何路径能用到,
    留着只会让号池面板上多一堆无主账号。"""
    _channel_row(name)
    if not channels_mod.is_data_channel(name):
        raise HTTPException(status_code=400, detail="代码渠道不能在管理台删除")
    deleted = pool_state.DB.delete_channel(name)
    USER_DB.delete_channel_config(name)
    _reload_channels()
    return {"ok": True, "name": name, "accounts_deleted": deleted}


class RoutingReq(BaseModel):
    priority: int = Field(default=0, ge=-1000, le=1000)
    weight: int = Field(default=1, ge=0, le=1000)


@router.patch("/admin/channels/{name}/routing")
def admin_set_channel_routing(name: str, req: RoutingReq, user=Depends(require_admin)):
    """改一个渠道的 priority / weight。存进 settings.channel_routing(全量映射的一项),
    立刻推进注册表 —— 同一模型挂多个渠道时,谁先谁后、同档怎么分流就看这两个数。"""
    if adapter_mod.get_adapter(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {name}")
    routing = dict(S.channel_routing())
    routing[name] = {"priority": req.priority, "weight": req.weight}
    try:
        routing = S.validate("channel_routing", routing, known_channels=_channel_names())
    except S.SettingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    USER_DB.set_setting("channel_routing", routing)
    apply_channel_switches()
    return {"ok": True, "name": name, "priority": req.priority, "weight": req.weight,
            "routing": adapter_mod.channel_routing()}


class KeysReq(BaseModel):
    keys: list | None = None
    key: str | None = None


@router.post("/admin/channels/{name}/keys")
def admin_import_channel_keys(name: str, req: KeysReq, user=Depends(require_admin)):
    """给任意 key 池型渠道(代码的或数据的)导 key。与 /admin/import-keys 同一份
    去重逻辑(core/pool_state.import_keys)。"""
    ad = adapter_mod.get_adapter(name)
    if ad is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {name}")
    keys = pool_state.parse_keys(req.model_dump())
    if not keys:
        raise HTTPException(status_code=400, detail="keys required")
    imported, skipped = pool_state.import_keys(ad, keys)
    st = pool_state.DB.stats(name).get(name) or {"total": 0, "by_status": {}}
    return {"ok": True, "channel": name, "imported": imported, "skipped": skipped,
            "total": len(keys), "pool": {"total": st["total"], **(st.get("by_status") or {})}}


class ChannelTestReq(BaseModel):
    model: str | None = None
    prompt: str = Field(default="Reply with the single word: pong", max_length=500)


@router.post("/admin/channels/{name}/test")
def admin_test_channel(name: str, req: ChannelTestReq, user=Depends(require_admin)):
    """从号池取一把号,非流式发一句话,回耗时、上游模型名与回复片段;失败把上游
    状态码与响应体原话回给管理员 —— 「401 invalid api key」和「404 model not found」
    是两种要改的东西,只回「失败」等于没说。不计费、不写用量。"""
    ad = adapter_mod.get_adapter(name)
    if ad is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {name}")
    if not ad.has(adapter_mod.CAP_CHAT) or getattr(ad, "proxy", False):
        raise HTTPException(status_code=400, detail="只有对话型 key 池渠道支持测试")
    model = (req.model or "").strip() or (ad.models[0] if ad.models else "")
    if model not in ad.models:
        raise HTTPException(status_code=400, detail=f"{model} 不是该渠道的模型")
    acct = pool_state.POOL.get_valid_account(name, ad)
    if acct is None:
        raise HTTPException(status_code=503, detail="号池里没有可用账号,先导入 key")
    started = time.time()
    try:
        reply = ad.chat(acct, [{"role": "user", "content": req.prompt}],
                        stream=False, model=model,
                        body={"max_tokens": 32, "temperature": 0})
    except Exception as e:
        # 与网关同一套判定,让坏 key 在这里也退场;但不 raise 到 500
        pool_state.POOL.mark_failure(acct["id"], e, adapter=ad)
        return {"ok": False, "channel": name, "model": model,
                "identity": acct.get("identity"),
                "elapsed_ms": int((time.time() - started) * 1000),
                "error": str(e)[:800]}
    raw = (reply or {}).get("raw") if isinstance(reply, dict) else None
    content = ""
    if isinstance(raw, dict):
        msg = ((raw.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content") or ""
    return {"ok": True, "channel": name, "model": model,
            "identity": acct.get("identity"),
            "elapsed_ms": int((time.time() - started) * 1000),
            "reply": (content or "")[:200],
            "usage": (raw or {}).get("usage") if isinstance(raw, dict) else None}


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
        rpm_limit=req.rpm_limit,
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
        "default_group": USER_DB.get_setting("default_group", config.DEFAULT_GROUP),
        "defaults": {  # 环境变量里的默认值,便于对比。刻意不含 epay_key。
            "require_invite": config.REQUIRE_INVITE,
            "default_group": config.DEFAULT_GROUP,
            "payment_providers": config.PAYMENT_PROVIDERS,
            "min_topup": config.MIN_TOPUP,
            "site_url": config.SITE_URL,
            "epay_api_url": config.EPAY_API_URL,
            "epay_pid": config.EPAY_PID,
            "epay_usd_rate": config.EPAY_USD_RATE,
            "disabled_channels": config.DISABLED_CHANNELS,
            "checkin_min": config.CHECKIN_MIN,
            "checkin_max": config.CHECKIN_MAX,
        },
        "pricing_catalog": pricing_catalog.status(),
        # 生效值(库里有就是库里的)
        "payment_providers": S.payment_providers(),
        "min_topup": S.min_topup(),
        "site_url": S.site_url(),
        "epay_api_url": S.epay_api_url(),
        "epay_pid": S.epay_pid(),
        "epay_usd_rate": S.epay_usd_rate(),
        "epay_key_set": bool(key),
        "epay_key_tail": key[-4:] if len(key) >= 4 else "",
        "checkin_min": S.checkin_min(),
        "checkin_max": S.checkin_max(),
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


def _channel_names():
    """所有注册的渠道名,含已下线的。校验拿它挡拼错的名字。"""
    return sorted(adapter_mod.all_adapters())


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
            "require_invite", "default_group"):
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


# ---- 管理端 · 库备份 ----

@router.get("/admin/backup")
def admin_backup_status(user=Depends(require_admin)):
    """最新一份在哪、多大、几份、下次几点。管理台据此显示「上次备份 N 小时前」——
    没有这个读数,自动备份挂了三个月也没人知道。"""
    from core import backup as backup_mod
    return backup_mod.current_status()


@router.post("/admin/backup")
def admin_backup_now(user=Depends(require_admin)):
    """立刻做一份。给「要动数据库之前先备一份」这个动作一个按钮。"""
    from core import backup as backup_mod
    try:
        done = backup_mod.backup_once(config.DB_PATH, backup_mod.resolve_dir(),
                                      keep=config.BACKUP_KEEP)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"backup failed: {e}") from e
    return {"ok": True, "name": os.path.basename(done["path"]),
            "size": done["size"], "pruned": done["pruned"],
            "status": backup_mod.current_status()}


# ---- 管理端 · 定价 ----

class PricingReq(BaseModel):
    model_pattern: str
    group_id: int | None = None
    billing_mode: str = "token"
    input_price: float = 0
    output_price: float = 0
    cache_read_price: float = 0
    cache_write_price: float = 0
    per_request_price: float = 0
    long_threshold: int = 0
    long_input_price: float | None = None
    long_output_price: float | None = None
    long_cache_read_price: float | None = None
    long_cache_write_price: float | None = None
    notes: str | None = None


class PricingImportReq(BaseModel):
    pricing: list
    replace: bool = False   # True=先清空同 scope 的旧定价


@router.get("/admin/pricing")
def admin_list_pricing(group_id: int = None, user=Depends(require_admin)):
    rows = USER_DB.list_pricing(group_id if group_id is not None else "__all__")
    return {"pricing": rows, "catalog": pricing_catalog.status()}


@router.post("/admin/pricing")
def admin_upsert_pricing(req: PricingReq, user=Depends(require_admin)):
    if req.billing_mode not in ("token", "per_request", "free"):
        raise HTTPException(status_code=400,
                            detail="billing_mode must be token|per_request|free")
    if req.group_id is not None and not USER_DB.get_group(req.group_id):
        raise HTTPException(status_code=400, detail="group not found")
    data = req.model_dump()
    pattern = data.pop("model_pattern")
    gid = data.pop("group_id")
    pid = USER_DB.upsert_pricing(pattern, gid, **data)
    PRICING.invalidate()
    return {"ok": True, "id": pid}


@router.delete("/admin/pricing/{pricing_id}")
def admin_delete_pricing(pricing_id: int, user=Depends(require_admin)):
    if not USER_DB.delete_pricing(pricing_id):
        raise HTTPException(status_code=404, detail="pricing not found")
    PRICING.invalidate()
    return {"ok": True}


@router.post("/admin/pricing/import")
def admin_import_pricing(req: PricingImportReq, user=Depends(require_admin)):
    """批量导入定价(与 GET /admin/pricing 的 pricing 字段格式一致,可往返)。"""
    imported, skipped = 0, []
    for row in req.pricing:
        if not isinstance(row, dict) or not row.get("model_pattern"):
            skipped.append(row)
            continue
        data = {k: v for k, v in row.items()
                if k in USER_DB.PRICING_FIELDS and k not in ("model_pattern", "group_id")}
        try:
            USER_DB.upsert_pricing(row["model_pattern"], row.get("group_id"), **data)
            imported += 1
        except Exception as e:
            skipped.append({"model_pattern": row.get("model_pattern"), "error": str(e)})
    PRICING.invalidate()
    return {"ok": True, "imported": imported, "skipped": skipped}


# ---- 管理端 · 兑换码 / 订单 / 余额 ----

class CodeGenReq(BaseModel):
    count: int = Field(ge=1, le=1000)
    # 邀请码不入账,面额恒 0,所以 value 可以不填;余额码填 0 会被 generate_codes 拒。
    value: float = 0
    expires_at: int = 0
    notes: str | None = None
    type: str = TYPE_BALANCE     # balance=余额兑换码 | invitation=注册邀请码


@router.get("/admin/codes")
def admin_list_codes(status: str = None, type: str = None, limit: int = 100,
                     offset: int = 0, user=Depends(require_admin)):
    """兑换码与邀请码同表,type 不传就是两种都给。管理台两个页面各自带上 type,
    否则邀请码会混进兑换码列表里显示成一堆 $0.00 的码。"""
    return {"codes": USER_DB.list_codes(status=status, type=type,
                                        limit=min(limit, 500), offset=offset)}


@router.post("/admin/codes")
def admin_generate_codes(req: CodeGenReq, user=Depends(require_admin)):
    if req.type not in (TYPE_BALANCE, TYPE_INVITATION):
        raise HTTPException(
            status_code=400, detail=f"type must be {TYPE_BALANCE}|{TYPE_INVITATION}")
    # 邀请码的面额一律抹成 0:留一个非零面额在库里,迟早有人照余额码去理解它
    value = 0 if req.type == TYPE_INVITATION else req.value
    try:
        codes = generate_codes(USER_DB, req.count, value, type=req.type,
                               expires_at=req.expires_at, notes=req.notes)
    except RedeemError as e:
        raise HTTPException(status_code=400,
                            detail={"code": e.code, "message": e.detail}) from e
    return {"ok": True, "codes": codes}


@router.delete("/admin/codes/{code}")
def admin_disable_code(code: str, user=Depends(require_admin)):
    if not USER_DB.disable_code(code.strip().upper()):
        raise HTTPException(status_code=404, detail="code not found or already used")
    return {"ok": True}


@router.get("/admin/orders")
def admin_list_orders(status: str = None, limit: int = 100, offset: int = 0,
                      user=Depends(require_admin)):
    return {"orders": USER_DB.list_orders(status=status, limit=min(limit, 500),
                                          offset=offset)}


@router.post("/admin/orders/{out_trade_no}/requery")
def admin_requery_order(out_trade_no: str, user=Depends(require_admin)):
    """人工触发一次查单 + 到账。

    自动对账每 600 秒一跳,这个入口给的是「用户在线催单」和「自动路径也没救回来」
    两种情况。它跟 /admin/users/{id}/balance 手工加钱的区别是可审计:
    到账走的还是订单自己的 recharge_code,流水 reason=recharge 且带订单号,
    手工调额那笔 reason=admin,事后对不上任何订单。
    """
    order = USER_DB.get_order_by_trade_no(out_trade_no)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    provider = get_provider(order["provider"])
    if provider is None:
        raise HTTPException(status_code=400,
                            detail=f"provider not loaded: {order['provider']}")
    try:
        res = provider.query_order(out_trade_no)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"query failed: {e}") from e
    status = (res or {}).get("status")
    credited = False
    if status == "paid":
        USER_DB.mark_order_paid(out_trade_no)
        credited = orders_mod.fulfil(USER_DB, out_trade_no)
    return {"ok": True, "upstream_status": status or "unknown",
            "credited": credited,
            "order": USER_DB.get_order_by_trade_no(out_trade_no)}


class BalanceAdjustReq(BaseModel):
    amount: float           # 正=加,负=扣
    notes: str | None = None


@router.post("/admin/users/{user_id}/balance")
def admin_adjust_balance(user_id: int, req: BalanceAdjustReq,
                         user=Depends(require_admin)):
    """管理员调额。走 credit(),流水可查。amount 可正可负。"""
    target = USER_DB.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if req.amount == 0:
        raise HTTPException(status_code=400, detail="amount must not be zero")
    idem = f"admin:{user['id']}:{user_id}:{int(time.time() * 1000)}"
    row, _ = credit(user_id, req.amount, REASON_ADMIN, idem,
                    meta={"by": user["id"], "notes": req.notes})
    BILLING.invalidate()
    return {"ok": True, "balance": row.get("balance_after")}


@router.get("/admin/ledger")
def admin_user_ledger(user_id: int, limit: int = 100, offset: int = 0,
                      user=Depends(require_admin)):
    return {"entries": USER_DB.list_ledger(user_id, limit=min(limit, 500),
                                           offset=offset)}


# ---- 管理端 · 全站用量日志 / 看板 ----

def _admin_usage_scope(email, user_id):
    """email 与 user_id 二选一定位用户。邮箱查不到返回 -1:让检索结果为空,
    而不是 404 —— 搜索框里敲错一个字母得到「0 条」比得到报错页顺手。"""
    if email:
        u = USER_DB.get_user_by_email(email.lower().strip())
        return u["id"] if u else -1
    return user_id


@router.get("/admin/usage")
def admin_usage(limit: int = 50, offset: int = 0, user_id: int = None,
                email: str = None, api_key_id: int = None, model: str = None,
                channel: str = None, since: int = None, until: int = None,
                end_reason: str = None, user=Depends(require_admin)):
    """全站用量日志,带邮箱与密钥名。这是「谁在半夜刷了 3000 次」「这个渠道
    今天是不是一直 eof」这类问题的唯一入口 —— 原先只有用户看自己那 50 条。"""
    limit = max(1, min(int(limit), 500))
    f = _usage_filters(model, channel, since, until, end_reason, api_key_id)
    f["user_id"] = _admin_usage_scope(email, user_id)
    rows, total = USER_DB.query_usage(limit=limit, offset=max(0, offset),
                                      with_user=True, **f)
    return {"logs": rows, "total": total, "limit": limit, "offset": max(0, offset)}


@router.get("/admin/usage/export.csv")
def admin_usage_export(user_id: int = None, email: str = None, api_key_id: int = None,
                       model: str = None, channel: str = None, since: int = None,
                       until: int = None, end_reason: str = None,
                       user=Depends(require_admin)):
    f = _usage_filters(model, channel, since, until, end_reason, api_key_id)
    f["user_id"] = _admin_usage_scope(email, user_id)
    rows, _ = USER_DB.query_usage(limit=_CSV_MAX, offset=0, with_user=True, **f)
    return _usage_csv(rows, with_user=True,
                      filename=f"site-usage-{_date.today().isoformat()}.csv")


def _window_stats(since, until=None):
    """一个时间窗的经营读数:用量合计 + 流水按原因 + 已完成订单。"""
    usage = USER_DB.usage_totals(since=since, until=until)
    ledger = USER_DB.ledger_by_reason(since, until)
    orders = USER_DB.orders_completed(since, until)

    def amt(reason):
        return (ledger.get(reason) or {}).get("amount") or 0.0

    return {
        "usage": usage,
        "orders": orders,
        # 进账:充值(订单真金白银)+ 兑换码(可能是站长送的也可能是卖的,分开列)
        "recharge": amt("recharge"),
        "redeem": amt("redeem"),
        # 消费:balance 策略下真实从余额扣掉的钱(usage 流水是负数,取反)
        "consumed": -amt("usage"),
        # 套餐售卖:购买是负数流水,取反就是卖出的额度
        "plans": -amt("plan"),
        # 营销支出:签到 + 注册赠额 + 返佣 + 管理员手工加的
        "giveaway": amt("checkin") + amt("signup") + amt("affiliate") + max(amt("admin"), 0.0),
        "ledger": ledger,
    }


@router.get("/admin/stats")
def admin_stats(days: int = 14, user=Depends(require_admin)):
    """看板:今日 / 近 7 天 / 近 30 天三个窗口的经营读数、日序列、按渠道/模型/用户
    的消耗排行、用户增长与余额负债。全部来自 usage_logs / credit_ledger / orders /
    users 四张已有的表,没有新的写路径。"""
    now = int(time.time())
    days = max(1, min(int(days), 90))
    day_start = int(datetime.combine(_date.today(), datetime.min.time()).timestamp())
    windows = {
        "today": _window_stats(day_start),
        "week": _window_stats(now - 7 * 86400),
        "month": _window_stats(now - 30 * 86400),
    }
    top_users = USER_DB.usage_group_by("user_id", since=now - 7 * 86400, limit=10)
    brief = USER_DB.users_brief([x["key"] for x in top_users])
    for x in top_users:
        x.update(brief.get(x["key"], {}))
    # 近 N 天按本地日历日补齐:没记录的日子也要有格子,折线不能把空天画成直连
    raw = {r["date"]: r for r in USER_DB.usage_daily_all(days=days)}
    series = []
    for k in range(days - 1, -1, -1):
        d = _date.fromtimestamp(now - k * 86400).isoformat()
        r = raw.get(d) or {"requests": 0, "users": 0, "tokens": 0, "cost": 0.0}
        series.append({"date": d, "label": d[5:], "requests": r["requests"],
                       "users": r["users"], "tokens": r["tokens"], "cost": r["cost"]})
    return {
        "generated_at": now,
        "windows": windows,
        "series": series,
        "by_channel": USER_DB.usage_group_by("channel", since=now - 7 * 86400),
        "by_model": USER_DB.usage_group_by("model", since=now - 7 * 86400, limit=12),
        "top_users": top_users,
        "users": {
            "total": USER_DB.count_users(),
            "new_today": USER_DB.count_users_since(day_start),
            "new_week": USER_DB.count_users_since(now - 7 * 86400),
            "new_month": USER_DB.count_users_since(now - 30 * 86400),
            "active_today": windows["today"]["usage"]["users"],
            "active_week": windows["week"]["usage"]["users"],
            "active_month": windows["month"]["usage"]["users"],
        },
        "balances": USER_DB.balance_totals(),
    }
