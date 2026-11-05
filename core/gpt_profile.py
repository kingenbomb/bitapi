"""Public GPT account/profile fields and sub2api-compatible usage windows.

Only selected workspace data is returned. Upstream primary/secondary positions
do not imply 5h/7d; the mapping follows sub2api's Codex snapshot normalization.
"""
import copy
import math
import time
from decimal import Decimal, InvalidOperation

from core.gpt_accounts import epoch


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def normalize_usage(data):
    out = copy.deepcopy(data)
    primary, secondary = out.get("primary"), out.get("secondary")
    p_seconds = _number((primary or {}).get("limit_window_seconds"))
    s_seconds = _number((secondary or {}).get("limit_window_seconds"))
    if p_seconds and s_seconds:
        primary_short = p_seconds < s_seconds
    elif p_seconds:
        primary_short = p_seconds <= 6 * 3600
    elif s_seconds:
        primary_short = s_seconds > 6 * 3600
    else:
        primary_short = False
    for key, raw, fallback in (("five_hour", primary if primary_short else secondary, 18000),
                                ("seven_day", secondary if primary_short else primary, 604800)):
        if not isinstance(raw, dict):
            out[key] = None
            continue
        utilization = _number(raw.get("used_percent"))
        reset = epoch(raw.get("reset_at"))
        if not reset and _number(raw.get("reset_after_seconds")) is not None:
            reset = int(out.get("fetched_at") or time.time()) + int(float(raw["reset_after_seconds"]))
        seconds = _number(raw.get("limit_window_seconds"))
        out[key] = {"utilization": utilization, "used_percent": utilization,
                    "reset_at": reset or None, "limit_window_seconds": int(seconds or fallback)}
    return out


def quota_snapshot(data):
    """Project the public quota fields used by sub2api, without account IDs."""
    fetched = int(time.time())
    def limits(raw):
        raw = raw if isinstance(raw, dict) else {}
        def window(key):
            value = raw.get(key)
            return ({k: value.get(k) for k in ("used_percent", "reset_at", "reset_after_seconds", "limit_window_seconds")}
                    if isinstance(value, dict) else None)
        return normalize_usage({"primary": window("primary_window"), "secondary": window("secondary_window"),
            "allowed": raw.get("allowed") if isinstance(raw.get("allowed"), bool) else None,
            "limit_reached": raw.get("limit_reached") if isinstance(raw.get("limit_reached"), bool) else None,
            "fetched_at": fetched})
    out = limits(data.get("rate_limit"))
    out["plan_type"] = data.get("plan_type")
    credits = data.get("credits")
    out["credits"] = None
    if isinstance(credits, dict):
        balance = credits.get("balance")
        try:
            number = Decimal(balance) if isinstance(balance, str) else None
            if number is None or not number.is_finite() or number < 0:
                balance = None
        except InvalidOperation:
            balance = None
        out["credits"] = {k: credits.get(k) if isinstance(credits.get(k), bool) else None
                          for k in ("has_credits", "unlimited", "overage_limit_reached")}
        out["credits"]["balance"] = balance
    review = data.get("code_review_rate_limit")
    out["code_review"] = limits(review) if isinstance(review, dict) else None
    out["additional_limits"] = [{"name": item.get("limit_name") or item.get("metered_feature") or "额外额度",
        "metered_feature": item.get("metered_feature"), **limits(item.get("rate_limit"))}
        for item in (data.get("additional_rate_limits") or []) if isinstance(item, dict)]
    return out


def reset_credit_snapshot(data):
    """Read reset-credit counts/expirations only; upstream credit IDs stay out."""
    count, entries = None, None
    while isinstance(data, dict):
        candidate = _number(data.get("available_count", data.get("availableCount")))
        if count is None and candidate is not None and candidate >= 0 and candidate.is_integer():
            count = int(candidate)
        nested = next((data[k] for k in ("credits", "rate_limit_reset_credits", "items", "data")
                       if isinstance(data.get(k), (list, dict))), None)
        data = nested
    if isinstance(data, list):
        entries = [item for item in data if isinstance(item, dict)
            and str(item.get("reset_type", item.get("resetType", ""))).lower() in ("", "codex_rate_limits")
            and str(item.get("status") or "").lower() in ("", "available")]
    if count is None:
        if entries is None:
            raise ValueError("上游未返回可用重置次数")
        count = len(entries)
    expirations = []
    for item in entries or []:
        try:
            expiry = epoch(item.get("expires_at", item.get("expiresAt")))
        except ValueError:
            continue
        if expiry:
            expirations.append(expiry)
    return {"available_count": count, "expirations": sorted(expirations), "fetched_at": int(time.time())}


def select_profile(data, account_id=None, identity=None):
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        raise ValueError("上游未返回账号资料")
    candidates = []
    for key, candidate in accounts.items():
        if not isinstance(candidate, dict):
            continue
        account = candidate.get("account") or {}
        actual_id = account.get("account_id") or key
        candidates.append((actual_id, candidate))
    matches = [(aid, item) for aid, item in candidates if account_id and aid == account_id]
    if account_id and len(matches) != 1:
        raise ValueError("上游资料中未找到此账号的工作区，未同步其他工作区资料")
    if not account_id:
        defaults = [(aid, item) for aid, item in candidates if (item.get("account") or {}).get("is_default")]
        matches = defaults if len(defaults) == 1 else candidates
        if len(matches) != 1:
            raise ValueError("上游返回多个工作区，请先导入包含 account_id 的完整凭据")
    actual_id, selected = matches[0]
    account, entitlement = selected.get("account") or {}, selected.get("entitlement") or {}
    return {"account_id": actual_id,
            "email": account.get("email") or (data.get("user") or {}).get("email") or (identity or {}).get("email"),
            "user_id": (identity or {}).get("user_id"),
            "plan_type": account.get("plan_type") or entitlement.get("subscription_plan"),
            "subscription_expires_at": epoch(entitlement.get("expires_at")) or None,
            "workspace_name": account.get("name"), "synced_at": int(time.time())}
