#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portal 各子路由共用的东西:router 实例、登录依赖、跨模块共用的请求体模型。

为什么要单独一个文件:`portal_admin.py` 要 `require_admin`,而用户侧路由又要用到
管理端一起定义的几个模型(GroupReq / SettingsReq 等)。两边直接互相 import 会
循环,所以把这类**双向共用**的符号集中到这里。

router 只在这里建一次(`prefix="/api"`, `route_class=SessionRoute`)。各子模块
`from .portal_shared import router` 之后往**同一个实例**上注册 —— 别自己新建
APIRouter,否则 prefix 和会话鉴权(CSRF/Origin 校验)都会丢。
"""
import sys
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core import adapter as adapter_mod
from core import auth
from core import portal_state
from core import site_settings as S
from core.clock import SITE_TZ, day_start as site_day_start
from core.session import SessionRoute

USER_DB = portal_state.USER_DB
router = APIRouter(prefix="/api", route_class=SessionRoute)


def _db():
    """当前生效的 UserDB —— 每次都从 portal_state 现取,不在本模块缓存。

    换库有两条路径:portal_state.rebind(db) 与测试里直接 `mod.USER_DB = db`。
    本模块缓存一份自己的引用,第二条路径就会漏掉,表现是「写进去了但读不到」:
    不报错,只是悄悄查的是旧库。所以这里只认 portal_state 的当前值。
    """
    return portal_state.USER_DB


def bind(db):
    """rebind 时本模块无需做什么(不缓存库引用),保留是为了兼容登记流程。"""


portal_state.register_binder(bind)


def current_user(authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    payload = auth.decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = _db().get_user(int(payload["sub"]))
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


class InviteRewardsReq(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool = Field(strict=True)
    amount: float = Field(strict=True, ge=0, allow_inf_nan=False)
    max_invitees: int = Field(strict=True, ge=0)


class ShareRewardsReq(BaseModel):
    """共享 Key 规则。channels 留空 = 所有 key 池型对话渠道都收。"""
    model_config = {"extra": "forbid"}
    enabled: bool = Field(strict=True)
    amount: float = Field(strict=True, ge=0, allow_inf_nan=False)
    channels: list[str] = Field(default_factory=list)
    max_per_user: int = Field(strict=True, ge=0)


class GroupReq(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str
    rate_multiplier: float = 1.0
    supported_models: list = []
    billing_policy: str = "free"
    concurrency_limit: int = Field(default=0, ge=0)
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
    model_config = ConfigDict(allow_inf_nan=False)
    rate_multiplier: float | None = None
    supported_models: list | None = None
    billing_policy: str | None = None
    concurrency_limit: int | None = Field(default=None, ge=0)
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
    # 总开关:True 时任何入口都不再建号。None = 本次不改。
    registration_closed: bool | None = None
    default_group: str | None = None
    community_min_level: int | None = Field(default=None, strict=True, ge=0, le=6)
    checkin_min: float | None = None
    checkin_max: float | None = None
    invite_rewards: InviteRewardsReq | None = None
    # 共享 Key 奖励规则。全量提交(开关 + 每把金额 + 允许渠道 + 每人上限),
    # 与邀请奖励同一形状:一次一份完整状态,没有「只改一半」的语义。
    share_rewards: ShareRewardsReq | None = None
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
    baipiao_pid: str | None = None
    baipiao_key: str | None = None
    mao_per_usd: float | None = None
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
            "epay_api_url", "epay_pid", "epay_key", "epay_usd_rate",
            "baipiao_pid", "baipiao_key", "mao_per_usd")
# 存库的其它设置项。与 PAY_KEYS 分开是因为支付那组有自己的「生效渠道」回执。
OTHER_SETTING_KEYS = ("disabled_channels", "channel_routing", "checkin_min",
                      "checkin_max", "community_min_level", "invite_rewards",
                      "share_rewards") + S.SMTP_KEYS


# ==== 用户侧与管理端共用的辅助 ====

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
            "request_id", "cache_read_tokens", "cache_write_tokens", "client_ip"]
    if with_user:
        head[2:2] = ["email", "key"]
    w.writerow(head)
    for r in rows:
        snap = r.get("pricing_snapshot") or {}
        if not isinstance(snap, dict):
            snap = {}
        total_ctx = snap.get("total_ctx")
        cache_read = snap.get("cache_read_tokens")
        cache_write = snap.get("cache_write_tokens")
        cache = (cache_read + cache_write
                 if cache_read is not None and cache_write is not None else
                 max(total_ctx - (r.get("input_tokens") or 0), 0)
                 if total_ctx is not None else "")
        line = [r.get("id"),
                datetime.fromtimestamp(r.get("created_at") or 0, SITE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                r.get("channel") or "", r.get("model") or "",
                r.get("input_tokens") or 0, r.get("output_tokens") or 0, cache,
                r.get("cost") or 0, r.get("actual_cost") or 0,
                r.get("billing_mode") or "", r.get("token_source") or "",
                1 if r.get("stream") else 0, r.get("duration_ms") or 0,
                snap.get("frt_ms", ""), snap.get("end_reason", ""),
                snap.get("tier", ""), snap.get("request_id", ""),
                cache_read, cache_write, snap.get("client_ip", "")]
        if with_user:
            line[2:2] = [r.get("email") or "", r.get("key_name") or ""]
        w.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(
            ("=", "+", "-", "@")) or isinstance(value, str) and value.startswith(("\t", "\r", "\n"))
            else value for value in line])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


_RANGE_SPAN = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}


def _live_usage_snapshot(user_id=None):
    now = int(time.time())
    day_start = site_day_start(now)
    return {**_db().live_usage(now, day_start, user_id=user_id), "as_of": now,
            "day_start": day_start, "timezone": "Asia/Shanghai", "window_seconds": 60}


_ANN_KEY = "announcements"
_ANN_LEVELS = ("info", "success", "warning", "error")
_ANN_MODES = ("silent", "popup")
# 公告正文存 settings 里的一个 JSON 列表,不单开表:一个自托管站的公告是个位数量级,
# 建表要连带迁移、索引、清理三件事,不值当。代价是写入必须读改写,不是原子操作,
# 所以加一把进程内锁 —— 单 worker 本来就是本项目的部署前提(见 README)。
# 已读状态是另一回事:它按用户增长且必须跨设备一致,落在 announcement_reads 表。
_ann_lock = threading.Lock()


def _ann_all():
    raw = _db().get_setting(_ANN_KEY) or []
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


def _channel_names():
    """所有注册的渠道名,含已下线的。校验拿它挡拼错的名字。"""
    return sorted(adapter_mod.all_adapters())
