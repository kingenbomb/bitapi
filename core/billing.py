#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
计费编排 —— 鉴权解析、限流预检、按策略计费结算。

三种计费策略(groups.billing_policy)平级可选:
  balance  余额扣费。预检 balance > 0,结算走 credit(-actual_cost)
  quota    日/周/月用量限额(不扣钱)
  free     不计费,只限速

密钥级管控(api_keys 的 quota / allowed_models / allowed_ips)只能在分组基础上
「收紧」,不能扩权:模型必须同时过分组白名单与密钥白名单;额度是本密钥的累计
消费上限,钱仍从 users.balance 扣。用户自助创建的密钥因此无法给自己升级套餐。

用量(tokens)由 core.metering 归一(上游真实优先、tiktoken 估算兜底);
价格由 core.pricing 解析(手配定价 → 内置目录价 → LiteLLM → 免费);
钱的变动一律经 core.credit(幂等)。结算后 emit("usage.recorded")。

限流状态(RPM)用进程内滑动窗口,依赖单 worker。日/周/月额度从 usage_logs 实时聚合。
"""
import ipaddress
import threading
import time

import config
from core.hooks import emit

# 限流/计费错误码
LIMIT_RPM = "RPM_LIMIT_EXCEEDED"
LIMIT_DAILY = "DAILY_LIMIT_EXCEEDED"
LIMIT_WEEKLY = "WEEKLY_LIMIT_EXCEEDED"
LIMIT_MONTHLY = "MONTHLY_LIMIT_EXCEEDED"
INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
KEY_QUOTA_EXHAUSTED = "KEY_QUOTA_EXHAUSTED"

POLICY_BALANCE = "balance"
POLICY_QUOTA = "quota"
POLICY_FREE = "free"

_DAY = 86400
_WEEK = 7 * 86400
_MONTH = 30 * 86400


class RateLimitError(Exception):
    """映射 429。"""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class InsufficientBalanceError(Exception):
    """映射 402。"""

    def __init__(self, detail="insufficient balance", balance=0.0):
        super().__init__(detail)
        self.code = INSUFFICIENT_BALANCE
        self.detail = detail
        self.balance = balance


class KeyQuotaError(Exception):
    """密钥自身的消费上限用尽。也映射 402:语义同族(要加额度才能继续)。"""

    def __init__(self, used=0.0, quota=0.0):
        detail = f"key quota exhausted: {used:.6f}/{quota:.6f}"
        super().__init__(detail)
        self.code = KEY_QUOTA_EXHAUSTED
        self.detail = detail
        self.used = used
        self.quota = quota


class Billing:
    def __init__(self, user_db, cache_ttl=None, pricing=None):
        self.db = user_db
        self.cache_ttl = cache_ttl if cache_ttl is not None else config.AUTH_CACHE_TTL
        self.pricing = pricing
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._rpm = {}
        self._rpm_lock = threading.Lock()

    # ---- 鉴权解析 ----

    def resolve_user_by_key(self, key):
        """明文 key → (user, group)。禁用/过期/失效返回 None。带秒级缓存。

        密钥记录挂在 user["_api_key"] 上而不改返回元组:调用点很多,
        加第三个返回值会散落一片解包改动。"""
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1], hit[2]

        rec = self.db.get_api_key(key)
        if not rec or rec["status"] != "active":
            return None
        expires_at = rec.get("expires_at") or 0
        if expires_at and expires_at < now:
            return None
        user = self.db.get_user(rec["user_id"])
        if not user or user["status"] != "active":
            return None
        group = self.db.effective_group(user)
        user = dict(user, _api_key_id=rec["id"], _api_key=rec)
        # 缓存不得越过密钥到期时间
        ttl = self.cache_ttl
        if expires_at:
            ttl = min(ttl, max(0, expires_at - now))
        with self._cache_lock:
            self._cache[key] = (now + ttl, user, group)
        return user, group

    def invalidate(self, key=None):
        with self._cache_lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()

    # ---- 模型白名单 ----

    @staticmethod
    def _match_patterns(patterns, model):
        """精确名与 '前缀*' 通配;'*' 全通。"""
        for p in patterns:
            if p == "*":
                return True
            if isinstance(p, str) and p.endswith("*"):
                if model.startswith(p[:-1]):
                    return True
            elif p == model:
                return True
        return False

    @classmethod
    def check_model_allowed(cls, group, model, key=None):
        """分组白名单(空列表=不允许) ∩ 密钥白名单(空列表=跟随分组)。

        两者语义故意不同:分组是管理员配的授权范围,没配就是没授权;
        密钥是用户自己配的收紧项,没配就是不额外收紧。"""
        if not group:
            return False
        patterns = group.get("supported_models") or []
        if not patterns or not cls._match_patterns(patterns, model):
            return False
        return cls.check_key_models(key, model)

    @classmethod
    def check_key_models(cls, key, model):
        """只看密钥级白名单。空/未配=不限制。"""
        allow = (key or {}).get("allowed_models") or []
        if not allow:
            return True
        return cls._match_patterns(allow, model)

    # ---- IP 白名单 ----

    @staticmethod
    def check_key_ip(key, client_ip):
        """密钥级 IP/CIDR 白名单。空=不限;配了却拿不到 IP 一律拒绝。"""
        allow = (key or {}).get("allowed_ips") or []
        if not allow:
            return True
        if not client_ip:
            return False
        try:
            ip = ipaddress.ip_address(str(client_ip).strip())
        except ValueError:
            return False
        for entry in allow:
            try:
                if ip in ipaddress.ip_network(str(entry).strip(), strict=False):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def policy_of(group):
        return (group or {}).get("billing_policy") or POLICY_FREE

    # ---- 请求前预检 ----

    def precheck(self, user, group):
        """RPM → 密钥额度 → 按 billing_policy 分流。超限抛 RateLimitError,
        余额不足抛 InsufficientBalanceError,密钥额度用尽抛 KeyQuotaError。
        group 为 None 时只跳过分组额度检查,密钥额度仍然生效。"""
        self._check_key_quota(user)
        if not group:
            return
        uid = user["id"]

        rpm = group.get("rpm_limit") or 0
        if rpm > 0:
            now = time.time()
            with self._rpm_lock:
                window = [t for t in self._rpm.get(uid, []) if now - t < 60]
                if len(window) >= rpm:
                    self._rpm[uid] = window
                    raise RateLimitError(LIMIT_RPM, f"rate limit {rpm}/min exceeded")
                window.append(now)
                self._rpm[uid] = window

        policy = self.policy_of(group)
        if policy == POLICY_BALANCE:
            fresh = self.db.get_user(uid) or user
            balance = fresh.get("balance") or 0.0
            if balance <= 0:
                raise InsufficientBalanceError(
                    f"balance {balance:.6f} is not enough", balance=balance)
        elif policy == POLICY_QUOTA:
            self._check_quota(uid, group)

    def _check_key_quota(self, user):
        """密钥自身的消费上限。不预占:与余额透支同策略,允许本次超,下次拦住。

        没配上限的密钥(绝大多数)只读缓存快照就能判掉,不打库 —— 这是每个网关
        请求都走的热路径。配了上限才回库取实时 used_quota:缓存里的那份由
        touch_api_key 写入后不失效,会滞后一个 AUTH_CACHE_TTL,足够连续超额。
        改上限本身走 PATCH,那里会 invalidate,所以快照里的 quota 一定是新的。
        没有快照(直接调 precheck,如测试与非网关路径)时一律回库。"""
        kid = user.get("_api_key_id")
        if not kid:
            return
        cached = user.get("_api_key")
        if cached is not None and (cached.get("quota") or 0) <= 0:
            return
        rec = self.db.get_api_key_by_id(kid) or cached or {}
        quota = rec.get("quota") or 0
        if quota <= 0:
            return
        used = rec.get("used_quota") or 0
        if used >= quota:
            raise KeyQuotaError(used=used, quota=quota)

    def _check_quota(self, uid, group):
        unit = group.get("limit_unit") or "requests"
        measure = self.db.usage_tokens if unit == "tokens" else self.db.usage_count
        now = int(time.time())
        for limit_key, span, code in (
                ("daily_limit", _DAY, LIMIT_DAILY),
                ("weekly_limit", _WEEK, LIMIT_WEEKLY),
                ("monthly_limit", _MONTH, LIMIT_MONTHLY)):
            limit = group.get(limit_key) or 0
            if limit > 0 and measure(uid, now - span) >= limit:
                raise RateLimitError(code, f"{limit_key} {limit} {unit} exceeded")

    # 兼容旧调用名
    check_rate_limit = precheck

    # ---- 结算 ----

    def record_usage(self, user, group, channel, model, adapter=None,
                     upstream_usage=None, request_messages=None, output_text=None,
                     stream=False, duration_ms=0, units=1,
                     request_id=None, end_reason=None, first_token_ms=None):
        """写 usage_logs 并按策略扣费。返回 usage_log 的 id(重复结算返回既有 id)。

          adapter          : 命中的 adapter(读 billing_mode 作计量提示)
          upstream_usage   : 上游 usage dict(上游如实返回时才有)
          request_messages : 入站 OpenAI messages(估算 input)
          output_text      : 模型输出文本(估算 output)
          units            : per_request 模式的计费份数(图片张数等)
          request_id       : 幂等键来源。同一 request_id 重复结算只扣一次
          end_reason       : done | eof | client_gone | scanner_error | error
          first_token_ms   : 首字延迟(ms),流式争议时的复盘依据

        透支策略(对齐 sub2api):不做额度预占,允许本次请求把余额扣成负数,
        保证进行中的请求能完成;下一次请求由 precheck 的 balance<=0 拦住。
        风险上界 = 单个 in-flight 批次,不随时间累积。
        """
        from core.adapter import BILL_ESTIMATE
        from core.metering import resolve_usage

        mode_hint = getattr(adapter, "billing_mode", BILL_ESTIMATE) if adapter else BILL_ESTIMATE
        in_tok, out_tok, source = resolve_usage(
            mode_hint, upstream_usage, request_messages, output_text)
        cache_read, cache_write = _cache_tokens(upstream_usage)

        mult = (group.get("rate_multiplier") if group else 1.0) or 1.0
        policy = self.policy_of(group)

        if self.pricing is not None:
            priced = self.pricing.resolve(model, (group or {}).get("id"))
            cost, actual_cost, snapshot = self.pricing.compute(
                priced, in_tok=in_tok, out_tok=out_tok,
                cache_read=cache_read, cache_write=cache_write,
                units=units, rate_multiplier=mult)
            billing_mode = snapshot.get("mode", "free")
        else:
            cost = actual_cost = 0.0
            billing_mode = "free"
            snapshot = {"mode": "free", "tier": "official", "rate_multiplier": mult}

        snapshot["policy"] = policy
        if request_id:
            snapshot["request_id"] = request_id
        if end_reason:
            snapshot["end_reason"] = end_reason
        if first_token_ms is not None:
            snapshot["frt_ms"] = first_token_ms

        # 幂等:优先用 request_id 做扣费键。先扣费(抢到幂等键者才写日志),
        # 避免"日志写了但重复扣费"或"重复日志"。
        idem = f"usage:{request_id}" if request_id else None
        if policy == POLICY_BALANCE and actual_cost > 0 and idem:
            from core.credit import REASON_USAGE, credit
            _row, created = credit(
                user["id"], -actual_cost, REASON_USAGE, idem,
                meta={"model": model, "channel": channel,
                      "request_id": request_id, "end_reason": end_reason})
            if not created:
                # 该 request_id 已结算过:不重复写 usage_log,返回既有日志 id
                return _existing_log_id(self.db, user["id"], request_id)

        log_id = self.db.add_usage(
            user_id=user["id"], api_key_id=user.get("_api_key_id"),
            channel=channel, model=model,
            input_tokens=in_tok, output_tokens=out_tok,
            cost=cost, actual_cost=actual_cost, billing_mode=billing_mode,
            pricing_snapshot=snapshot, stream=stream,
            duration_ms=duration_ms, token_source=source)

        # 无 request_id(非网关路径/旧调用)时退化用 log_id 作幂等键
        if policy == POLICY_BALANCE and actual_cost > 0 and not idem:
            from core.credit import REASON_USAGE, credit
            credit(user["id"], -actual_cost, REASON_USAGE, f"usage:log:{log_id}",
                   meta={"model": model, "channel": channel, "log_id": log_id})

        # 密钥的最后使用时间与已用额度:放在幂等闸门之后,重复结算不会重复累加。
        # 不清鉴权缓存 —— 额度判定在 _check_key_quota 里读实时行,缓存里的
        # 密钥快照只作兜底;清了会让每次请求都回落到查库。
        # 失败不能影响主流程:这是展示与限额用的旁路数据,不是账。
        kid = user.get("_api_key_id")
        if kid:
            try:
                self.db.touch_api_key(kid, spent=actual_cost)
            except Exception:
                pass

        emit("usage.recorded", log_id=log_id, user=user, group=group,
             model=model, channel=channel, cost=cost, actual_cost=actual_cost,
             input_tokens=in_tok, output_tokens=out_tok, snapshot=snapshot,
             request_id=request_id, end_reason=end_reason)
        return log_id


def _existing_log_id(db, user_id, request_id):
    """重复结算时找回首次的 usage_log id(靠 snapshot 里的 request_id)。"""
    for row in db.recent_usage(user_id, limit=200):
        snap = row.get("pricing_snapshot") or {}
        if isinstance(snap, dict) and snap.get("request_id") == request_id:
            return row["id"]
    return None


def _cache_tokens(usage):
    """从上游 usage 里抠缓存 token(OpenAI / Anthropic 两种字段名)。"""
    if not isinstance(usage, dict):
        return 0, 0
    read = (usage.get("cache_read_input_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0)
    write = usage.get("cache_creation_input_tokens") or 0
    try:
        return int(read or 0), int(write or 0)
    except (TypeError, ValueError):
        return 0, 0
