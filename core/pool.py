#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用号池逻辑(基于 DB + Adapter,与网站无关)。

取号时保证 token 有效(过期用 adapter 刷)、余额够(低于阈值标 exhausted 跳过)。
这套逻辑对所有 channel 通用,新网站只要实现 adapter 即可复用。
"""
import time
from contextlib import nullcontext

from core import db as dbmod
from core import account_runtime
from core.adapter import CAP_BALANCE, CAP_HEALTH, CAP_REFRESH
from core.hooks import emit

# 账号「真的废了」的信号。其余一切(超时、连接重置、上游 5xx、限流、说不清的报错)
# 都只是「这次不行」,不该让账号永久离开号池。
_TERMINAL_MARKERS = (
    "invalid_grant", "unauthorized", "user blocked", "account blocked",
    "permission-denied", "permission_denied", "forbidden", "disabled",
    "quota exhausted", "quota_exceeded", "insufficient_quota",
    "free-usage-exhausted", "payment required", "payment_required",
    "spending-limit",
)


def classify_failure(exc):
    """上游异常 → "terminal"(账号废了) / "ratelimit"(上游此刻忙) / "transient"(过会儿再试)。

    判不准时按 transient:错判成 cooldown 下一轮巡检会纠正,错判成 dead 号就
    永久没了(dead 不再被取号,也是 purge 的默认清理目标)。这与
    渠道适配器的 _probe_file 同口径 —— 未知的不可用状态也落 cooldown。

    429 单独分一类:它说的是「上游此刻满了」,跟这把 key 好不好没有关系。有的上游
    并发额度是全平台共享的(AMD GPU Cloud 的模型级 64 并发就是这种),这时候冷却
    账号纯属自伤 —— 号本身没问题,冷掉之后要等下一轮巡检(默认 30 分钟)才回场,
    一次限流就能让整个渠道黑半小时。terminal 标记优先于 429:有些上游用 429 报
    「额度用尽」,那种得让号退场。
    """
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if isinstance(code, int) and code in (401, 402, 403):
        return "terminal"
    msg = str(exc or "").lower()
    if any(m in msg for m in _TERMINAL_MARKERS):
        return "terminal"
    if code == 429 or "too many requests" in msg \
            or "rate limit" in msg or "rate_limit" in msg:
        return "ratelimit"
    return "transient"


def _is_free_account(adapter, acct):
    """adapter 说这个号当前免额度(会员/赠送期内)吗?没实现就按 False。"""
    fn = getattr(adapter, "is_free", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(acct))
    except Exception:
        return False


def _is_auth_banned(exc):
    """这次失败是不是**授权被封**(401/403) —— 不是额度问题。

    401 unauthorized / 403 forbidden / account disabled 说的都是「这把凭据
    被上游拒了」,换多少额度都救不回来;而 402 credits exhausted 只是没额。
    两者在库里都落 exhausted 的话,「封了多少 / 用完多少」永远分不清,
    也没法在封号潮刚起势时告警 —— 所以这里把它们分开。
    """
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if code in (401, 403):
        return True
    s = str(exc or "").lower()
    return any(k in s for k in (
        "unauthorized", "forbidden", "account disabled", "user blocked",
        "account blocked", "invalid_grant", "permission-denied",
        "permission_denied"))


_BAN_WINDOW_SEC = 600
_BAN_ALERT_AT = 5


def _note_auth_ban():
    """统计授权封号;短时间内成批出现就发事件(告警插件订阅它)。

    判据:10 分钟内 >= 5 个号被判「授权被封」。这通常是上游在批量扫号,
    此刻继续注册/继续取号只会把剩下的号也送掉 —— 站长该第一时间知道。
    """
    now = time.time()
    _BAN_TIMES.append(now)
    while _BAN_TIMES and now - _BAN_TIMES[0] > _BAN_WINDOW_SEC:
        _BAN_TIMES.pop(0)
    if len(_BAN_TIMES) >= _BAN_ALERT_AT and not _BAN_ALERTED[0]:
        _BAN_ALERTED[0] = True
        try:
            emit("pool.ban_wave", window_sec=_BAN_WINDOW_SEC,
                 banned=len(_BAN_TIMES), threshold=_BAN_ALERT_AT)
        except Exception:
            pass
        print("[pool] ⚠️ 授权封号激增: %d 分钟内 %d 个号被判死" % (
            _BAN_WINDOW_SEC // 60, len(_BAN_TIMES)), flush=True)


_BAN_TIMES = []
_BAN_ALERTED = [False]


def _stateless(adapter):
    """该渠道的 key 是否「没有本地状态」(见 Adapter.stateless_keys)。

    adapter=None 时按 False:老调用方(以及巡检那条路)保持原状态机不变。
    """
    return bool(getattr(adapter, "stateless_keys", False))


def apply_refresh(db, acct, upd, **extra):
    """把 adapter.refresh_token() 的返回值落库,并同步回内存里的 acct。

    **轮换出来的长期凭据必须跟着写回。** 有的上游刷新时轮换 refresh_token:
    有的上游把新的那份放在 upd["secret"],有的放在 upd["meta"]["_refresh_token"]。
    漏写就等于把这号唯一能续命的凭据扔掉 —— 下次拿旧的去换,上游回 invalid_grant,
    而 invalid_grant 在 classify_failure 里是终态,号当场被判死。号本身还活着,
    但库里那支已经作废,换不回来。

    取号热路径(Pool.get_valid_account)和巡检(scheduler._check_one)共用这一处,
    专门为了不再出现「一边记得写、另一边忘了」。2026-09-03~06 的 xai 号池整池
    3923 个就是这么没的:adapters/xai.py 一直返回正确的 secret,调用方两个都丢了。
    """
    if acct.get("channel") == "gpt" and upd.get("_persisted"):
        if extra:
            db.update_account(acct["id"], **extra)
        current = db.get_account(acct["id"])
        if current:
            acct.update(current)
        return
    fields = {"token": upd["token"], "token_exp": upd.get("token_exp", 0)}
    for key in ("meta", "secret"):
        if upd.get(key) is not None:
            fields[key] = upd[key]
            acct[key] = upd[key]
    fields.update(extra)
    db.update_account(acct["id"], **fields)
    acct["token"] = upd["token"]
    acct["token_exp"] = upd.get("token_exp", 0)


class Pool:
    def __init__(self, db, min_balance=0.05, refresh_margin=600):
        self.db = db
        self.min_balance = min_balance
        self.margin = refresh_margin

    def get_valid_account(self, channel, adapter, max_tries=20, exclude=None):
        """挑一个可用账号,保证 token 有效、余额够。返回 acct(含新 token)或 None。

        拿不到号时 emit pool.empty:这是站长最该第一时间知道的事实(用户此刻全在
        吃 503),而核心自己不决定怎么通知 —— 告警插件订阅它。"""
        tried = 0
        refresh_deadline = time.monotonic() + 30
        excluded = list(exclude or ())
        waiting = []
        ctx = account_runtime.request_context()
        from core.gpt_web import MODEL as WEB_IMAGE_MODEL
        web_image = channel == "gpt" and ctx.get("model") == WEB_IMAGE_MODEL
        if channel == "gpt":
            from core import gpt_runtime
            preferred = ctx.get("preferred_account_id") or gpt_runtime.preferred_id(self.db, ctx.get("client"), account_runtime.group_id())
        else:
            preferred = None
        while tried < max_tries:
            if channel == "gpt" and time.monotonic() >= refresh_deadline:
                break
            acct = self.db.pick_active(channel, prefer=getattr(adapter, "prefer_expr", None),
                                       group_id=account_runtime.group_id(), exclude=excluded,
                                       **({"web_image": True} if web_image else {}),
                                       **({"preferred_id": preferred} if preferred else {}))
            wait_for_slot = False
            if not acct and waiting:
                acct = waiting.pop(0)
                wait_for_slot = True
            if not acct:
                emit("pool.empty", channel=channel, reason="no_active")
                return None
            if channel == "gpt" and not gpt_runtime.eligible(self.db, acct, ctx.get("model"), ctx.get("client"), getattr(adapter, "configured_models", None)):
                excluded.append(acct["id"])
                continue
            if not wait_for_slot and account_runtime.saturated(acct, self.db):
                if channel == "gpt":
                    family, limit = account_runtime.capacity_limit(self.db, acct)
                    if limit and account_runtime.capacity(family) >= limit and not (acct.get("base_rpm") and account_runtime.rpm(acct["id"]) >= acct["base_rpm"]):
                        waiting.append(acct)
                excluded.append(acct["id"])
                continue
            if any(acct.get(k) for k in ("quota_limit", "quota_daily_limit", "quota_weekly_limit", "window_cost_limit")):
                from core.account_management import quota_exceeded
                if quota_exceeded(acct):
                    excluded.append(acct["id"])
                    continue
            tried += 1

            # token 惰性刷新:没过期不动
            if adapter.has(CAP_REFRESH):
                exp = acct.get("token_exp") or 0
                if not acct.get("token") or exp - time.time() < self.margin:
                    try:
                        with account_runtime.network(self.db, acct), (account_runtime.credential_budget(max(.001, refresh_deadline - time.monotonic())) if channel == "gpt" else nullcontext()):
                            upd = adapter.refresh_token(acct)
                        apply_refresh(self.db, acct, upd,
                                      **({} if web_image else {"status": dbmod.ST_ACTIVE}))
                    except Exception as e:
                        if web_image:
                            from core.gpt_web.runtime import record
                            record(self.db, acct, error=e)
                            excluded.append(acct["id"])
                        else:
                            if channel == "gpt":
                                from core.gpt_request_policy import mark_failure
                                mark_failure(self.db, acct["id"], e, ctx.get("model"), phase="refresh")
                                excluded.append(acct["id"])
                            else:
                                self.mark_failure(acct["id"], e, adapter=adapter)
                        continue

            # 余额检查。会员/免费额度期内的号**不消耗** credits(余额恒 0),
            # 只按余额判耗时这些号会被整批踢出池子 —— 渠道表现为「没号」,
            # 上游实际还能用。所以先问 adapter 这个号是不是免额度。
            if adapter.has(CAP_BALANCE) and not _is_free_account(adapter, acct):
                bal = adapter.balance(acct)
                if bal is None:
                    self.db.set_status(acct["id"], dbmod.ST_DEAD)
                    continue
                self.db.update_account(acct["id"], balance=bal,
                                       last_check=int(time.time()))
                acct["balance"] = bal
                if bal < self.floor_for(adapter):
                    exhausted_status = (dbmod.ST_DEAD
                                        if getattr(adapter, "exhausted_is_dead", False)
                                        else dbmod.ST_EXHAUSTED)
                    self.db.set_status(acct["id"], exhausted_status)
                    continue

            # 轮换:上面的余额检查会顺带推进 last_check,没有余额/存活检查的渠道
            # (只声明 CAP_REFRESH 的渠道)整轮都不推 —— 而 pick_active 按
            # last_check ASC 挑号,不推就等于每次都返回同一个号,请求全堆在一个号上。
            # 那正是上游封号的直接原因,所以这里给这类渠道补一次轮换。
            if not adapter.has(CAP_BALANCE) and not adapter.has(CAP_HEALTH):
                self._rotate(acct["id"])
            if channel == "gpt":
                gpt_runtime.bind_session(self.db, acct, ctx.get("client"), account_runtime.group_id())
            return acct
        emit("pool.empty", channel=channel, reason="all_tries_failed")
        return None

    def floor_for(self, adapter):
        """该渠道的额度门槛。渠道自己声明的优先 —— 全局那个阈值是按「美元余额」
        定的(0.05),而按 credits 计价的渠道最便宜一次就要 2 分,拿全局值判会把
        额度不够的号留在 active 里,每次取到它都白跑一趟。"""
        floor = getattr(adapter, "min_balance", None)
        return self.min_balance if floor is None else floor

    def mark_exhausted(self, acct_id, dead=False, adapter=None):
        """额度耗尽。透传型 key 池没有额度概念 —— 见 mark_failure 的说明,只轮换。"""
        if _stateless(adapter):
            self._rotate(acct_id)
            return "rotate"
        status = dbmod.ST_DEAD if dead else dbmod.ST_EXHAUSTED
        self.db.set_status(acct_id, status)
        return status

    def mark_cooldown(self, acct_id):
        """临时不可用:跳过本次取号,下一轮巡检会重新验活并转回 active。"""
        self.db.set_status(acct_id, dbmod.ST_COOLDOWN)

    def _rotate(self, acct_id):
        """不罚号,只把它排到队尾。pick_active 按 last_check ASC 挑号,推进这个
        时间戳就等于「下一次换别的号」,而账号仍在 active 里随时可用。"""
        self.db.update_account(acct_id, last_check=int(time.time()))

    def mark_failure(self, acct_id, exc, dead=False, adapter=None):
        """按异常性质落状态。

          ratelimit → 不动状态(号没问题,只是上游此刻忙),只把 last_check 往前挪
          transient → cooldown
          terminal  → exhausted / dead

        返回落定的状态(ratelimit 返回 "ratelimit",透传型 key 池的非终态返回 "rotate"),
        调用方可用于日志。

        限流不罚号是刻意的:pick_active 只挑 active,冷却一下就要等巡检才回场;
        而共享并发额度的上游(AMD GPU Cloud)满载时所有 key 一起 429,罚号会把整个
        渠道一次性打黑。不改状态但推进 last_check —— pick_active 是按
        last_check ASC 排的,这样下一次取号自然轮到别的号,不会死盯着同一把。

        adapter.stateless_keys 的渠道把这个口径推到 transient:那种 key 没有本地
        状态可判,上游抖一下(502、超时、模型临时下架报的 400)不代表 key 坏了,
        而 cooldown 要等下一轮巡检(默认 30 分钟)才回场 —— 一次抖动就能把整个渠道
        park 半小时。实测 AMD GPU Cloud 的网关会对同一把好 key 先回 502
        「Model gateway is unavailable」、再回 400「Unsupported model」,一分钟后自愈。
        terminal 不在豁免范围:401/402/403 说的就是这把 key 本身不行,该退场。
        """
        if getattr(adapter, "name", None) == "gpt":
            from core.gpt_request_policy import mark_failure
            return mark_failure(self.db, acct_id, exc, account_runtime.request_context().get("model"))
        kind = classify_failure(exc)
        if kind == "ratelimit":
            self._rotate(acct_id)
            return "ratelimit"
        if kind == "transient":
            if _stateless(adapter):
                self._rotate(acct_id)
                return "rotate"
            self.mark_cooldown(acct_id)
            return dbmod.ST_COOLDOWN
        # 授权被封(401/403/account disabled)与「没额度」分开处理:
        #  - 402 = 真没额,落 exhausted
        #  - 401/403 = 这把凭据被拒。**但实测上游的 401 会瞬时恢复**
        #    (同一批号几分钟后又能用),所以判死会被 purge 误删好号。
        #    落 cooldown:先移出轮换,让巡检把它验活再放回。同时计数/告警,
        #    封号潮刚起势就能看到,而不是等到「no available upstream」。
        if _is_auth_banned(exc) and not dead:
            self.mark_cooldown(acct_id)
            _note_auth_ban()
            return dbmod.ST_COOLDOWN
        status = dbmod.ST_DEAD if dead else dbmod.ST_EXHAUSTED
        self.db.set_status(acct_id, status)
        return status
