#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定价与计价 —— 结对单价制(直接填真实美元单价),含长上下文阶梯。

解析链(命中即返回):
  1. model_pricing · group 专属 · 精确名
  2. model_pricing · group 专属 · prefix*
  3. model_pricing · 全局 · 精确名 → prefix*
  4. 内置价目表 → LiteLLM 价目表(pricing_catalog)
  5. 兜底:免费(cost=0)—— 永不因缺价拒绝请求

价格单位:token 类为「美元/1M token」,per_request 为「美元/次」。

长上下文阶梯(整次跳档,非累进):
  total_ctx = input + cache_write + cache_read   (不含 output)
  total_ctx > long_threshold 时,该项换用长档单价;长档单价缺失则该项单独回落普通价。
  判定用严格大于(等于阈值仍算便宜档),对齐 new-api 的 `len <= T ? 便宜 : 贵`。
"""
import threading
import time

MODE_TOKEN = "token"
MODE_PER_REQUEST = "per_request"
MODE_FREE = "free"

TIER_OFFICIAL = "official"
TIER_LONG = "long"

_MILLION = 1_000_000.0

FREE_PRICING = {
    "model_pattern": None, "billing_mode": MODE_FREE, "source": "fallback",
    "input_price": 0.0, "output_price": 0.0,
    "cache_read_price": 0.0, "cache_write_price": 0.0,
    "per_request_price": 0.0, "long_threshold": 0,
}


class Pricing:
    def __init__(self, user_db, catalog=None, cache_ttl=30):
        self.db = user_db
        self.catalog = catalog
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    # ---- 解析 ----

    def resolve(self, model, group_id=None):
        """返回定价 dict(必含 billing_mode 与各单价字段)+ source 标记。"""
        key = (model, group_id)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        pricing = self._resolve_uncached(model, group_id)
        with self._lock:
            self._cache[key] = (now + self.cache_ttl, pricing)
        return pricing

    def _resolve_uncached(self, model, group_id):
        # 1~2: group 专属(精确 → prefix*)
        if group_id is not None:
            hit = self._match(self.db.list_pricing(group_id), model)
            if hit:
                return dict(hit, source="group")
        # 3: 全局(精确 → prefix*)
        hit = self._match(self.db.list_pricing(None), model)
        if hit:
            return dict(hit, source="global")
        # 4: 内置价目表 → LiteLLM 价目表
        if self.catalog is not None:
            cat = self.catalog.lookup(model)
            if cat:
                return dict(cat, source=cat.get("source") or "litellm")
        # 5: 兜底免费
        return dict(FREE_PRICING, model_pattern=model)

    @staticmethod
    def _match(rows, model):
        """精确名优先于 prefix*;多个 prefix* 命中时取最长前缀(更具体者胜)。"""
        best_prefix, best_len = None, -1
        for r in rows:
            pat = r.get("model_pattern") or ""
            if pat == model:
                return r
            if pat == "*":
                if best_len < 0:
                    best_prefix, best_len = r, 0
            elif pat.endswith("*") and model.startswith(pat[:-1]):
                plen = len(pat) - 1
                if plen > best_len:
                    best_prefix, best_len = r, plen
        return best_prefix

    # ---- 计价 ----

    @staticmethod
    def compute(pricing, in_tok=0, out_tok=0, cache_read=0, cache_write=0,
                units=1, rate_multiplier=1.0):
        """返回 (cost, actual_cost, snapshot)。cost 为原价,actual_cost 乘倍率后。

        snapshot 冻结本次计价参数(档位 + 实际生效单价 + total_ctx + 倍率),
        改价后历史账仍可复算。
        """
        mode = pricing.get("billing_mode") or MODE_FREE
        mult = rate_multiplier if rate_multiplier is not None else 1.0

        if mode == MODE_FREE:
            cost = 0.0
            snap = {"mode": MODE_FREE, "tier": TIER_OFFICIAL,
                    "rate_multiplier": mult, "source": pricing.get("source")}
            return cost, 0.0, snap

        if mode == MODE_PER_REQUEST:
            unit_price = _f(pricing.get("per_request_price"))
            cost = unit_price * (units or 1)
            snap = {"mode": MODE_PER_REQUEST, "tier": TIER_OFFICIAL,
                    "per_request_price": unit_price, "units": units or 1,
                    "rate_multiplier": mult, "source": pricing.get("source")}
            return cost, cost * mult, snap

        # token 模式
        total_ctx = (in_tok or 0) + (cache_write or 0) + (cache_read or 0)
        threshold = pricing.get("long_threshold") or 0
        is_long = bool(threshold) and total_ctx > threshold
        tier = TIER_LONG if is_long else TIER_OFFICIAL

        def pick(long_key, base_key):
            """逐项回落:长档生效且该项长档价已配置才用长档,否则用普通价。"""
            if is_long:
                lv = pricing.get(long_key)
                if lv is not None:
                    return _f(lv)
            return _f(pricing.get(base_key))

        in_p = pick("long_input_price", "input_price")
        out_p = pick("long_output_price", "output_price")
        cw_p = pick("long_cache_write_price", "cache_write_price")
        cr_p = pick("long_cache_read_price", "cache_read_price")

        cost = ((in_tok or 0) * in_p + (out_tok or 0) * out_p
                + (cache_write or 0) * cw_p + (cache_read or 0) * cr_p) / _MILLION
        snap = {
            "mode": MODE_TOKEN, "tier": tier, "total_ctx": total_ctx,
            "long_threshold": threshold,
            "input_price": in_p, "output_price": out_p,
            "cache_write_price": cw_p, "cache_read_price": cr_p,
            "rate_multiplier": mult, "source": pricing.get("source"),
        }
        return cost, cost * mult, snap


def _f(v):
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
