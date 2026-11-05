#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享 Key —— 用户把自己在某条渠道上的 key 交上来换余额奖励。

一句话流程:
    验活(拿这把 key 真发一次最小的请求)→ 入库当号用 → 按把记一笔奖励流水

几条必须守住的线:

  * **奖励的唯一口径是 credit()。** 余额的每一分都要有对应的流水行(见
    core/credit.py)。幂等键是 `sharekey:<渠道>:<identity>` —— 同一把 key 全站
    只发一次:自己重复交、别人先交过,都不会出第二笔钱。这条不靠「先查再写」的
    竞态判断,靠的是 credit_ledger 上 idem_key 的唯一索引。

  * **先验活再入库。** 不验就收,等于让任何人往号池里灌垃圾 key:巡检还没跑到,
    用户侧先吃一串 401。验活用的是和网关同一条 adapter.chat 路径,连不上的 key
    当场返回上游原话,不落库、不发奖。

  * **身份派生跟着渠道走。** identity 由 adapter.key_identity() 派生,和
    /admin/channels/{name}/keys 的批量导入是同一份口径 —— 两处派生不一致的话,
    同一把 key 从两个入口进会变成两个账号,去重就失效了。

  * **奖励算多少、收哪些渠道由设置在库。** 见 core/site_settings.share_rewards,
    管理台可改,存库即生效。
"""
import hashlib
import time

from core import adapter as adapter_mod
from core import db as dbmod
from core import pool_state
from core import site_settings as S
from core.adapter import CAP_CHAT
from core.credit import credit
from core.hooks import emit

# 流水 reason(与 core/credit.py 里那组约定并列)
REASON_SHARE = "share_key"

# key 形状的粗筛。真正的判据是上游答不答,这里只挡明显不是 key 的输入
# (空串、超长、带空白的复制粘贴),免得把一次网络往返浪费在显然的垃圾上。
KEY_MIN_LEN, KEY_MAX_LEN = 8, 512


class ShareError(ValueError):
    """可预期的失败:开关关着、渠道不收、key 重复、key 验不过。

    调用方(portal 路由)把它翻成 400 —— 这类都是「用户这次没成」,不是服务端故障。
    """


# ---- 规则 ----

def settings():
    """当前生效的共享规则 {enabled, amount, channels, max_per_user}。"""
    return S.share_rewards()


def _shareable(ad):
    """这个渠道收不收共享 key。

    只要「key 池型 + 支持对话」的渠道:proxy=True 的是上游自带号池的转发型,
    往里塞 key 没有意义;不支持对话的(纯向量 / 账号管理型)同理。
    """
    if ad is None or getattr(ad, "proxy", False):
        return False
    if not ad.has(CAP_CHAT):
        return False
    return callable(getattr(ad, "key_identity", None))


def allowed_channels(cfg=None):
    """允许共享的渠道名。设置里填了就用填的(顺带滤掉已不存在的),
    留空 = 所有 key 池型对话渠道。"""
    cfg = cfg or settings()
    if cfg["channels"]:
        return [c for c in cfg["channels"] if _shareable(adapter_mod.get_adapter(c))]
    return sorted(n for n, ad in adapter_mod.all_adapters().items() if _shareable(ad))


def _identity(ad, key):
    kid = getattr(ad, "key_identity", None)
    if callable(kid):
        return kid(key)
    # 与 core/pool_state.import_keys 的同一条兜底:渠道没实现 key_identity 时
    # 也要有一个稳定、脱敏的 identity,否则入库会撞唯一键。
    return "manual-" + hashlib.sha1(key.encode()).hexdigest()[:16]


def reward_count(user_db, user_id):
    """该用户已拿到奖励的 key 把数(数流水,不是另存计数)。"""
    return user_db.count_ledger(user_id, reason=REASON_SHARE)


# ---- 验活 ----

def probe(ad, key, model=None):
    """用这把 key 发一次最小请求,确认上游认它。返回验活用的模型名。

    走的是 adapter.chat —— 与网关取号后转发完全同一条路,能验出「格式对但上游
    拒收」(401/402/403)与「模型名不对」(400)这些只有真发一次才看得见的问题。
    失败把上游原话包进 ShareError,让用户知道是 key 的问题还是模型的问题。
    """
    model = (model or "").strip() or (ad.models[0] if ad.models else "")
    if not model:
        raise ShareError("该渠道没有可用于验活的模型,请先在管理台配模型清单")
    acct = {"secret": {"api_key": key}}
    try:
        ad.chat(acct, [{"role": "user", "content": "ping"}],
                stream=False, model=model,
                body={"max_tokens": 1, "temperature": 0})
    except Exception as e:
        raise ShareError(f"key 验活失败:{str(e)[:200]}") from e
    return model


# ---- 主流程 ----

def submit(user_db, user_id, channel, key, *, check_probe=True):
    """收下一把共享 key:验活 → 入库 → 发奖。返回结果字典。

    失败一律抛 ShareError(含原因),不返回半成品状态:要么这把 key 真的进了池子
    并且奖励到位,要么什么都没发生(验活失败 / 重复 / 超限)。
    """
    cfg = settings()
    if not cfg["enabled"]:
        raise ShareError("共享功能未开启")

    key = str(key or "").strip()
    if not (KEY_MIN_LEN <= len(key) <= KEY_MAX_LEN) or any(c.isspace() for c in key):
        raise ShareError(f"key 长度需在 {KEY_MIN_LEN}~{KEY_MAX_LEN} 之间且不含空白")

    ad = adapter_mod.get_adapter(str(channel or "").strip())
    if ad is None:
        raise ShareError(f"未知渠道:{channel}")
    if not _shareable(ad):
        raise ShareError(f"{ad.name} 不支持共享 key")

    allowed = allowed_channels(cfg)
    if ad.name not in allowed:
        raise ShareError(f"{ad.name} 不在允许共享的渠道里")

    if cfg["max_per_user"] and reward_count(user_db, user_id) >= cfg["max_per_user"]:
        raise ShareError(f"已达每人最多 {cfg['max_per_user']} 把的奖励上限")

    identity = _identity(ad, key)
    if pool_state.DB.get_by_identity(ad.name, identity):
        raise ShareError("这把 key 已经在号池里了")

    model = probe(ad, key) if check_probe else ""

    # 先落库(upsert 按 channel+identity 唯一,天然幂等),再发奖。
    # 两步之间任何一步失败都不会重复给钱:奖励那一步由 idem_key 兜底。
    aid = pool_state.DB.upsert_account(
        ad.name, identity, secret={"api_key": key}, status=dbmod.ST_ACTIVE,
        meta={"shared_by": int(user_id), "shared_at": int(time.time()),
              "probe_model": model, "source": "user_share"})
    pool_state.DB.update_account(aid, status=dbmod.ST_ACTIVE)

    _, created = credit(user_id, cfg["amount"], reason=REASON_SHARE,
                        idem_key=f"sharekey:{ad.name}:{identity}",
                        meta={"channel": ad.name, "identity": identity})
    if not created:
        # 幂等键已存在 = 这把 key 之前已经发过奖(并发的另一次提交赢了)。
        # 号已经在池子里,但这次没有钱可发 —— 如实告诉用户,别假装成功。
        raise ShareError("这把 key 已经被收录过了")

    emit("share.key_accepted", user_id=int(user_id), channel=ad.name,
         identity=identity, amount=float(cfg["amount"]))
    return {"ok": True, "channel": ad.name, "identity": identity,
            "reward": float(cfg["amount"]), "probe_model": model}


# ---- 视图 ----

def my_summary(user_db, user_id, limit=50):
    """用户侧「我的共享」:规则 + 我交过哪些 + 拿了多少。"""
    cfg = settings()
    rows = pool_state.DB.list_shared(user_id=user_id, limit=limit)
    items = []
    for r in rows:
        meta = r.get("meta") or {}
        items.append({
            "identity": r.get("identity"),
            "channel": r.get("channel"),
            "status": r.get("status"),
            "shared_at": (meta or {}).get("shared_at") or r.get("created_at"),
            "probe_model": (meta or {}).get("probe_model") or "",
        })
    return {"enabled": cfg["enabled"], "amount": cfg["amount"],
            "channels": allowed_channels(cfg), "max_per_user": cfg["max_per_user"],
            "rewarded_count": reward_count(user_db, user_id),
            "earned": user_db.ledger_sum(user_id, reason=REASON_SHARE),
            "items": items}


def contributions(limit=200):
    """管理侧:最近由用户共享进来的 key(不含 key 明文,只给脱敏 identity)。"""
    out = []
    for r in pool_state.DB.list_shared(limit=limit):
        meta = r.get("meta") or {}
        out.append({
            "channel": r.get("channel"), "identity": r.get("identity"),
            "status": r.get("status"), "shared_by": (meta or {}).get("shared_by"),
            "shared_at": (meta or {}).get("shared_at") or r.get("created_at"),
            "probe_model": (meta or {}).get("probe_model") or "",
        })
    return out
