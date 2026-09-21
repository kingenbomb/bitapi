#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用号池逻辑(基于 DB + Adapter,与网站无关)。

取号时保证 token 有效(过期用 adapter 刷)、余额够(低于阈值标 exhausted 跳过)。
这套逻辑对所有 channel 通用,新网站只要实现 adapter 即可复用。
"""
import time

from core import db as dbmod
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
    判不准的未知不可用状态也落 cooldown,与网关热路径同一口径。

    429 单独分一类:它说的是「上游此刻满了」,跟这把 key 好不好没有关系。有的上游
    并发额度是全平台共享的(有的上游按模型限总并发),这时候冷却
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


def _stateless(adapter):
    """该渠道的 key 是否「没有本地状态」(见 Adapter.stateless_keys)。

    adapter=None 时按 False:老调用方(以及巡检那条路)保持原状态机不变。
    """
    return bool(getattr(adapter, "stateless_keys", False))


class Pool:
    def __init__(self, db, min_balance=0.05, refresh_margin=600):
        self.db = db
        self.min_balance = min_balance
        self.margin = refresh_margin

    def get_valid_account(self, channel, adapter, max_tries=20):
        """挑一个可用账号,保证 token 有效、余额够。返回 acct(含新 token)或 None。

        拿不到号时 emit pool.empty:这是站长最该第一时间知道的事实(用户此刻全在
        吃 503),而核心自己不决定怎么通知 —— 告警插件订阅它。"""
        tried = 0
        while tried < max_tries:
            acct = self.db.pick_active(channel)
            if not acct:
                emit("pool.empty", channel=channel, reason="no_active")
                return None
            tried += 1

            # token 惰性刷新:没过期不动
            if adapter.has(CAP_REFRESH):
                exp = acct.get("token_exp") or 0
                if not acct.get("token") or exp - time.time() < self.margin:
                    try:
                        upd = adapter.refresh_token(acct)
                        self.db.update_account(
                            acct["id"], token=upd["token"],
                            token_exp=upd.get("token_exp", 0),
                            meta=upd.get("meta", acct.get("meta")),
                            status=dbmod.ST_ACTIVE)
                        acct["token"] = upd["token"]
                        acct["token_exp"] = upd.get("token_exp", 0)
                        if upd.get("meta"):
                            acct["meta"] = upd["meta"]
                    except Exception as e:
                        self.mark_failure(acct["id"], e, adapter=adapter)
                        continue

            # 余额检查
            if adapter.has(CAP_BALANCE):
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

            # 轮换:上面的余额检查会顺带推进 last_check,只声明了 CAP_REFRESH 的渠道
            # 整轮都不推 —— 而 pick_active 按 last_check ASC 挑号,不推就等于每次都
            # 返回同一个号,请求全堆在一个号上。那正是上游封号的直接原因,
            # 所以这里给这类渠道补一次轮换。
            if not adapter.has(CAP_BALANCE) and not adapter.has(CAP_HEALTH):
                self._rotate(acct["id"])
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

    def mark_dead(self, acct_id):
        self.db.set_status(acct_id, dbmod.ST_DEAD)

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
        而共享并发额度的上游满载时所有 key 一起 429,罚号会把整个
        渠道一次性打黑。不改状态但推进 last_check —— pick_active 是按
        last_check ASC 排的,这样下一次取号自然轮到别的号,不会死盯着同一把。

        adapter.stateless_keys 的渠道把这个口径推到 transient:那种 key 没有本地
        状态可判,上游抖一下(502、超时、模型临时下架报的 400)不代表 key 坏了,
        而 cooldown 要等下一轮巡检(默认 30 分钟)才回场 —— 一次抖动就能把整个渠道
        park 半小时。实测有的上游网关会对同一把好 key 先回 502
        「Model gateway is unavailable」、再回 400「Unsupported model」,一分钟后自愈。
        terminal 不在豁免范围:401/402/403 说的就是这把 key 本身不行,该退场。
        """
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
        status = dbmod.ST_DEAD if dead else dbmod.ST_EXHAUSTED
        self.db.set_status(acct_id, status)
        return status
