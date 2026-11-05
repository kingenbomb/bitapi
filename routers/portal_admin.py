#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portal 的管理端路由(/api/admin/* 与随管理端一起暴露的几组端点)。

从 routers/portal.py 拆出来的:这一块只服务 require_admin,和用户侧的路由没有
调用关系,单独放一个文件最省心。router 来自 portal_shared(同一个 /api 实例),
所以 prefix 与 SessionRoute 的会话鉴权照旧。
"""
import os
import secrets
import time
from datetime import datetime, timedelta
from datetime import date as _date

import config
from fastapi import Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core import adapter as adapter_mod
from core import channels as channels_mod
from core import mailer
from core import model_status
from core import orders as orders_mod
from core import pool_state
from core import portal_state
from core import pricing_catalog
from core import site_settings as S
from core.clock import SITE_TZ, day_start as site_day_start
from core.credit import REASON_ADMIN, credit
from core.hooks import emit
from core.payments import get_provider
from core.portal_state import (USER_DB,
                               apply_channel_switches, ensure_default_group)
from core.redeem import (TYPE_BALANCE, TYPE_INVITATION, RedeemError,
                         generate_codes, redeem)
from routers.portal_shared import (AdminResetPwReq, GroupReq, GroupUpdateReq,
                                   InviteRewardsReq, ShareRewardsReq, SettingsReq,
                                   UserAdminReq, _ANN_KEY, _ANN_LEVELS, _ANN_MODES,
                                   _CSV_MAX, _ann_all, _ann_find, _ann_lock,
                                   _ann_sorted, _channel_names, _live_usage_snapshot,
                                   _usage_csv, _usage_filters, require_admin, router)


def _db():
    """当前 UserDB —— 从 portal_state 现取,不在本模块缓存(换库时不会漏)。"""
    return portal_state.USER_DB


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
    total_users = _db().count_users()
    for a in _ann_sorted(_ann_all()):
        row = dict(a)
        row["read_count"] = _db().announcement_read_count(a["id"])
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
        _db().set_setting(_ANN_KEY, items)
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
        _db().set_setting(_ANN_KEY, items)
        return {"ok": True, "announcement": items[i]}


@router.delete("/admin/announcements/{ann_id}")
def admin_delete_announcement(ann_id: str, user=Depends(require_admin)):
    with _ann_lock:
        items = _ann_all()
        i = _ann_find(items, ann_id)
        if i < 0:
            raise HTTPException(status_code=404, detail="announcement not found")
        items.pop(i)
        _db().set_setting(_ANN_KEY, items)
    # 已读记录跟着删,不留孤儿行(id 是随机的,不会被后来的公告复用)
    _db().drop_announcement_reads(ann_id)
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

    kogo 暴露一个裸 `kg` 模型,手写 `kg-*` 会漏掉它 —— 公共前缀给的是 `kg`,
    正好躲开这个坑。模型是 vendor/model 形状、没有公共前缀的渠道返回 None:
    那种渠道只能逐个选,给一个错的通配比不给更糟。
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
    key_concurrency: int | None = 0
    notes: str | None = None


def _channel_row(name):
    row = _db().get_channel(name)
    if row is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return row


def _reload_channels():
    """增删改之后重放注册表并重推开关。渠道开关存的是名字,删了的渠道留在里面无害。"""
    from core.portal_state import load_data_channels
    load_data_channels()
    apply_channel_switches()
    portal_state.BILLING.invalidate()


@router.post("/admin/channels")
def admin_create_channel(req: ChannelReq, user=Depends(require_admin)):
    """建一个 OpenAI 兼容渠道。存库即注册,模型立刻出现在清单与路由里;
    key 另导(/admin/channels/{name}/keys),没 key 之前调用会 503「没有可用账号」。"""
    try:
        data = channels_mod.validate(req.model_dump(), _db())
    except channels_mod.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    name = data.pop("name")
    _db().create_channel(name, **data)
    _reload_channels()
    return {"ok": True, "name": name, "channel": _db().get_channel(name)}


@router.patch("/admin/channels/{name}")
def admin_update_channel(name: str, req: ChannelReq, user=Depends(require_admin)):
    _channel_row(name)
    if not channels_mod.is_data_channel(name):
        raise HTTPException(status_code=400, detail="代码渠道不能在管理台修改")
    body = req.model_dump()
    body["name"] = name       # 不支持改名:accounts.channel 挂在名字上,改名等于丢号池
    try:
        data = channels_mod.validate(body, _db(), editing=name)
    except channels_mod.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    data.pop("name")
    _db().update_channel(name, **data)
    _reload_channels()
    return {"ok": True, "channel": _db().get_channel(name)}


@router.delete("/admin/channels/{name}")
def admin_delete_channel(name: str, user=Depends(require_admin)):
    """删渠道连同它的号池。号挂在渠道名上,渠道没了号就没有任何路径能用到,
    留着只会让号池面板上多一堆无主账号。"""
    _channel_row(name)
    if not channels_mod.is_data_channel(name):
        raise HTTPException(status_code=400, detail="代码渠道不能在管理台删除")
    deleted = pool_state.DB.delete_channel(name)
    _db().delete_channel_config(name)
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
    _db().set_setting("channel_routing", routing)
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
    if name == "gpt":
        raise HTTPException(status_code=400, detail="GPT OAuth 请使用导入账号入口")
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


@router.post("/admin/gpt/import")
def admin_import_gpt(body: dict | list = Body(...), user=Depends(require_admin)):
    """原生 GPT OAuth 导入：写入现有 accounts 号池。

    与 /admin/gpt/import(管理密钥)共用 adapters.gpt 的同一份解析与去重 ——
    两边口径一致，同一个号从两个入口导不会重复入池。
    """
    ad = adapter_mod.get_adapter("gpt")
    if ad is None:
        raise HTTPException(status_code=404, detail="gpt adapter not loaded")
    try:
        result = ad.import_body(pool_state.DB, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    st = pool_state.DB.stats("gpt").get("gpt") or {"total": 0, "by_status": {}}
    result["pool"] = {"total": st["total"], **(st.get("by_status") or {})}
    return {"ok": True, "channel": "gpt", **result}


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
    if not ad.supports_chat(model):
        raise HTTPException(status_code=400, detail=f"{model} 是结构化预测模型，不能做对话测试")
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


# ---- 模型状态(小时级巡检) ----


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


# ---- 管理端 · 定价 ----

class PricingReq(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
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
    rows = _db().list_pricing(group_id if group_id is not None else "__all__")
    return {"pricing": rows, "catalog": pricing_catalog.status()}


@router.post("/admin/pricing")
def admin_upsert_pricing(req: PricingReq, user=Depends(require_admin)):
    if req.billing_mode not in ("token", "per_request", "free"):
        raise HTTPException(status_code=400,
                            detail="billing_mode must be token|per_request|free")
    if req.group_id is not None and not _db().get_group(req.group_id):
        raise HTTPException(status_code=400, detail="group not found")
    data = req.model_dump()
    pattern = data.pop("model_pattern")
    gid = data.pop("group_id")
    pid = _db().upsert_pricing(pattern, gid, **data)
    portal_state.PRICING.invalidate()
    return {"ok": True, "id": pid}


@router.delete("/admin/pricing/{pricing_id}")
def admin_delete_pricing(pricing_id: int, user=Depends(require_admin)):
    if not _db().delete_pricing(pricing_id):
        raise HTTPException(status_code=404, detail="pricing not found")
    portal_state.PRICING.invalidate()
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
                if k in _db().PRICING_FIELDS and k not in ("model_pattern", "group_id")}
        try:
            _db().upsert_pricing(row["model_pattern"], row.get("group_id"), **data)
            imported += 1
        except Exception as e:
            skipped.append({"model_pattern": row.get("model_pattern"), "error": str(e)})
    portal_state.PRICING.invalidate()
    return {"ok": True, "imported": imported, "skipped": skipped}


# ---- 管理端 · 兑换码 / 订单 / 余额 ----


# ---- 管理端 · 兑换码 / 订单 / 余额 ----

class CodeGenReq(BaseModel):
    count: int = Field(ge=1, le=1000)
    # 邀请码不入账,面额恒 0,所以 value 可以不填;余额码填 0 会被 generate_codes 拒。
    value: float = Field(default=0, allow_inf_nan=False)
    expires_at: int = 0
    notes: str | None = None
    type: str = TYPE_BALANCE     # balance=余额兑换码 | invitation=注册邀请码


@router.get("/admin/codes")
def admin_list_codes(status: str = None, type: str = None, limit: int = 100,
                     offset: int = 0, user=Depends(require_admin)):
    """兑换码与邀请码同表,type 不传就是两种都给。管理台两个页面各自带上 type,
    否则邀请码会混进兑换码列表里显示成一堆 $0.00 的码。"""
    return {"codes": _db().list_codes(status=status, type=type,
                                        limit=min(limit, 500), offset=offset)}


@router.post("/admin/codes")
def admin_generate_codes(req: CodeGenReq, user=Depends(require_admin)):
    if req.type not in (TYPE_BALANCE, TYPE_INVITATION):
        raise HTTPException(
            status_code=400, detail=f"type must be {TYPE_BALANCE}|{TYPE_INVITATION}")
    # 邀请码的面额一律抹成 0:留一个非零面额在库里,迟早有人照余额码去理解它
    value = 0 if req.type == TYPE_INVITATION else req.value
    try:
        codes = generate_codes(_db(), req.count, value, type=req.type,
                               expires_at=req.expires_at, notes=req.notes)
    except RedeemError as e:
        raise HTTPException(status_code=400,
                            detail={"code": e.code, "message": e.detail}) from e
    return {"ok": True, "codes": codes}


@router.delete("/admin/codes/{code}")
def admin_disable_code(code: str, user=Depends(require_admin)):
    if not _db().disable_code(code.strip().upper()):
        raise HTTPException(status_code=404, detail="code not found or already used")
    return {"ok": True}


@router.get("/admin/orders")
def admin_list_orders(status: str = None, limit: int = 100, offset: int = 0,
                      user=Depends(require_admin)):
    return {"orders": _db().list_orders(status=status, limit=min(limit, 500),
                                          offset=offset)}


@router.post("/admin/orders/{out_trade_no}/requery")
def admin_requery_order(out_trade_no: str, user=Depends(require_admin)):
    """人工触发一次查单 + 到账。

    自动对账每 600 秒一跳,这个入口给的是「用户在线催单」和「自动路径也没救回来」
    两种情况。它跟 /admin/users/{id}/balance 手工加钱的区别是可审计:
    到账走的还是订单自己的 recharge_code,流水 reason=recharge 且带订单号,
    手工调额那笔 reason=admin,事后对不上任何订单。
    """
    order = _db().get_order_by_trade_no(out_trade_no)
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
        credited = orders_mod.settle_query(_db(), order, res)
    return {"ok": True, "upstream_status": status or "unknown",
            "credited": credited,
            "order": _db().get_order_by_trade_no(out_trade_no)}


class BalanceAdjustReq(BaseModel):
    amount: float = Field(allow_inf_nan=False)           # 正=加,负=扣
    notes: str | None = None


@router.post("/admin/users/{user_id}/balance")
def admin_adjust_balance(user_id: int, req: BalanceAdjustReq,
                         user=Depends(require_admin)):
    """管理员调额。走 credit(),流水可查。amount 可正可负。"""
    target = _db().get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if req.amount == 0:
        raise HTTPException(status_code=400, detail="amount must not be zero")
    idem = f"admin:{user['id']}:{user_id}:{int(time.time() * 1000)}"
    row, _ = credit(user_id, req.amount, REASON_ADMIN, idem,
                    meta={"by": user["id"], "notes": req.notes})
    portal_state.BILLING.invalidate()
    return {"ok": True, "balance": row.get("balance_after")}


@router.get("/admin/ledger")
def admin_user_ledger(user_id: int, limit: int = 100, offset: int = 0,
                      user=Depends(require_admin)):
    return {"entries": _db().list_ledger(user_id, limit=min(limit, 500),
                                           offset=offset)}


# ---- 管理端 · 全站用量日志 / 看板 ----


# ---- 管理端 · 全站用量日志 / 看板 ----

def _admin_usage_scope(email, user_id):
    """email 与 user_id 二选一定位用户。邮箱查不到返回 -1:让检索结果为空,
    而不是 404 —— 搜索框里敲错一个字母得到「0 条」比得到报错页顺手。"""
    if email:
        u = _db().get_user_by_email(email.lower().strip())
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
    rows, total = _db().query_usage(limit=limit, offset=max(0, offset),
                                      with_user=True, **f)
    return {"logs": rows, "total": total, "limit": limit, "offset": max(0, offset)}


@router.get("/admin/usage/live")
def admin_live_usage(user=Depends(require_admin)):
    """号池筛选区的全站读数，不受日志筛选影响。"""
    return _live_usage_snapshot()


@router.get("/admin/usage/export.csv")
def admin_usage_export(user_id: int = None, email: str = None, api_key_id: int = None,
                       model: str = None, channel: str = None, since: int = None,
                       until: int = None, end_reason: str = None,
                       user=Depends(require_admin)):
    f = _usage_filters(model, channel, since, until, end_reason, api_key_id)
    f["user_id"] = _admin_usage_scope(email, user_id)
    rows, _ = _db().query_usage(limit=_CSV_MAX, offset=0, with_user=True, **f)
    return _usage_csv(rows, with_user=True,
                      filename=f"site-usage-{_date.today().isoformat()}.csv")


def _window_stats(since, until=None):
    """一个时间窗的经营读数:用量合计 + 流水按原因 + 已完成订单。"""
    usage = _db().usage_totals(since=since, until=until)
    ledger = _db().ledger_by_reason(since, until)
    orders = _db().orders_completed(since, until)

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
    day_start = site_day_start(now)
    windows = {
        "today": _window_stats(day_start),
        "week": _window_stats(now - 7 * 86400),
        "month": _window_stats(now - 30 * 86400),
    }
    top_users = _db().usage_group_by("user_id", since=now - 7 * 86400, limit=10)
    brief = _db().users_brief([x["key"] for x in top_users])
    for x in top_users:
        x.update(brief.get(x["key"], {}))
    # 近 N 天按本地日历日补齐:没记录的日子也要有格子,折线不能把空天画成直连
    raw = {r["date"]: r for r in _db().usage_daily_all(days=days)}
    series = []
    for k in range(days - 1, -1, -1):
        d = datetime.fromtimestamp(now - k * 86400, SITE_TZ).date().isoformat()
        r = raw.get(d) or {"requests": 0, "users": 0, "tokens": 0, "cost": 0.0}
        series.append({"date": d, "label": d[5:], "requests": r["requests"],
                       "users": r["users"], "tokens": r["tokens"], "cost": r["cost"]})
    return {
        "generated_at": now,
        "windows": windows,
        "total_usage": _db().usage_totals(),
        "series": series,
        "by_channel": _db().usage_group_by("channel", since=now - 7 * 86400),
        "by_model": _db().usage_group_by("model", since=now - 7 * 86400, limit=12),
        "top_users": top_users,
        "users": {
            "total": _db().count_users(),
            "new_today": _db().count_users_since(day_start),
            "new_week": _db().count_users_since(now - 7 * 86400),
            "new_month": _db().count_users_since(now - 30 * 86400),
            "active_today": windows["today"]["usage"]["users"],
            "active_week": windows["week"]["usage"]["users"],
            "active_month": windows["month"]["usage"]["users"],
        },
        "balances": _db().balance_totals(),
    }
