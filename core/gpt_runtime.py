"""GPT account scheduling, quota snapshots and conversation ownership.

Snapshots and policy are persisted on the existing account; process-local locks
only protect live requests. No quota query or model request consumes a reset card.
"""
import hashlib
import json
import math
import time


DEFAULTS = {
    "pause_5h": 1.0, "pause_7d": 1.0, "auto_reset": False,
    "reset_5h": 1.0, "reset_7d": 1.0, "model_whitelist": [], "model_mapping": {},
    "cli_only": False, "ws_mode": "off", "passthrough": True,
    "flatten_namespaces": False, "compact_mode": "auto", "compact_model_mapping": {},
    "image_tool": "auto", "fingerprint_mode": "canonical", "header_overrides": {},
    "plan_override": None,
}


def settings(row):
    return {**DEFAULTS, **((row.get("meta") or {}).get("gpt_settings") or {})}


def edit_meta(db, aid, change, **fields):
    """Serialize read/modify/write so response headers cannot discard admin edits."""
    with db._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        raw = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if raw is None:
            raise ValueError("GPT 账号不存在")
        row = db._row(raw)
        meta = dict(row.get("meta") or {})
        extra = change(meta) or {}
        updates = {**fields, **extra, "meta": json.dumps(meta, ensure_ascii=False), "updated_at": int(time.time())}
        updates = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in updates.items()}
        conn.execute("UPDATE accounts SET " + ",".join(key + "=?" for key in updates) + " WHERE id=?",
                     [*updates.values(), aid])
    return meta


def credential_account(db, row):
    parent_id = (row.get("meta") or {}).get("parent_account_id")
    if not parent_id:
        return row
    parent = db.get_account(parent_id)
    if not parent or parent["channel"] != "gpt" or (parent.get("meta") or {}).get("parent_account_id"):
        raise ValueError("Spark 主账号不存在或无效")
    return parent


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def quota_block(row, now=None):
    now = time.time() if now is None else now
    meta, policy = row.get("meta") or {}, settings(row)
    usage = meta.get("pool_upstream_usage") or {}
    blocked = []
    for key, threshold in (("five_hour", policy["pause_5h"]), ("seven_day", policy["pause_7d"])):
        window = usage.get(key)
        if not window or threshold is None:
            continue
        value, reset = _number(window.get("utilization")), window.get("reset_at")
        if value is not None and value >= threshold * 100 and (not reset or reset > now):
            # A missing reset never turns a stale snapshot into a permanent ban.
            fallback = 5 if value >= 100 and usage.get("source") == "response_headers" else 300
            until = reset or (usage.get("fetched_at") or 0) + fallback
            if until > now:
                blocked.append((until, "5h" if key == "five_hour" else "7d"))
    if blocked:
        return max(blocked)[0], "/".join(item[1] for item in blocked) + " 上游额度达到暂停阈值"
    if usage.get("allowed") is False and not any(
            (usage.get(key) or {}).get("reset_at", now + 1) <= now for key in ("five_hour", "seven_day")):
        until = (usage.get("fetched_at") or 0) + 60
        if until > now:
            return until, "上游暂不允许请求"
    return 0, ""


def eligible(db, row, model=None, client=None, defaults=None):
    meta = row.get("meta") or {}
    policy = settings(row)
    now = time.time()
    from core.gpt_web.runtime import MODEL as WEB_IMAGE_MODEL, image_models
    if model == WEB_IMAGE_MODEL:
        return bool(image_models(row) and (not policy["cli_only"] or (client or {}).get("codex")))
    if (client or {}).get("transport") == "ws" and policy["ws_mode"] == "off":
        return False
    if quota_block(row, now)[0]:
        return False
    limits = meta.get("gpt_rate_limits") or {}
    if max(limits.get("*", 0), limits.get(model or "", 0)) > now:
        return False
    if policy["cli_only"] and not (client or {}).get("codex"):
        return False
    whitelist = policy["model_whitelist"]
    if model and whitelist and model not in whitelist:
        return False
    if model and defaults is not None:
        from core.gpt_models import entries
        if model not in {item["slug"] for item in entries(row, defaults)}:
            return False
        if not meta.get("parent_account_id"):
            with db._conn() as conn:
                children = conn.execute("SELECT * FROM accounts WHERE channel='gpt' AND json_extract(meta,'$.parent_account_id')=?", (row["id"],)).fetchall()
            if any(model in settings(db._row(child))["model_whitelist"] for child in children):
                return False
    if meta.get("parent_account_id"):
        if model and whitelist and model not in whitelist:
            return False
        try:
            parent = credential_account(db, row)
        except ValueError:
            return False
        pmeta = parent.get("meta") or {}
        if parent["status"] != "active" or pmeta.get("admin_paused") or parent.get("temp_until", 0) > now:
            return False
        if parent.get("account_expires_at") and parent.get("auto_pause_on_expired") and parent["account_expires_at"] <= now:
            return False
    return True


def capture_headers(db, row, headers):
    headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    now = int(time.time())
    windows = {}
    for slot in ("primary", "secondary"):
        window = {}
        for suffix, field in (("used-percent", "used_percent"), ("window-minutes", "limit_window_seconds"),
                              ("reset-after-seconds", "reset_after_seconds")):
            value = _number(headers.get("x-codex-" + slot + "-" + suffix))
            if value is not None:
                window[field] = value * 60 if suffix == "window-minutes" else value
        if window:
            if "reset_after_seconds" in window:
                window["reset_at"] = now + int(window["reset_after_seconds"])
            windows[slot] = window
    if not windows:
        return False
    from core.gpt_profile import normalize_usage
    def update(meta):
        previous = dict(meta.get("pool_upstream_usage") or {})
        for key, values in windows.items():
            previous[key] = {**(previous.get(key) or {}), **values}
        previous.update(fetched_at=now, source="response_headers")
        previous.update(allowed=None, limit_reached=None)
        meta["pool_upstream_usage"] = normalize_usage(previous)
        until, reason = quota_block({**row, "meta": meta}, now)
        return {"upstream_until": int(until), "upstream_reason": reason}
    edit_meta(db, row["id"], update)
    return True


def save_usage(db, row, snapshot):
    def update(meta):
        meta["pool_upstream_usage"] = snapshot
        until, reason = quota_block({**row, "meta": meta})
        return {"upstream_until": int(until), "upstream_reason": reason}
    return edit_meta(db, row["id"], update)


def rate_limit_reset(error, now=None):
    """Only exhausted Codex windows or explicit body reset times prove quota loss."""
    now = time.time() if now is None else now
    headers = {str(k).lower(): v for k, v in (getattr(error, "headers", None) or {}).items()}
    exhausted, resets = False, []
    for slot in ("primary", "secondary"):
        used = _number(headers.get("x-codex-" + slot + "-used-percent"))
        after = _number(headers.get("x-codex-" + slot + "-reset-after-seconds"))
        if used is not None and used >= 100:
            exhausted = True
            if after is not None and after > 0:
                resets.append(now + after)
    details = getattr(error, "details", {}) or {}
    value = details.get("error", details) if isinstance(details, dict) else {}
    if isinstance(value, dict):
        reset = _number(value.get("resets_at") or value.get("reset_at"))
        if reset and reset > 1e12:
            reset /= 1000
        if reset and reset > now:
            exhausted = True
            resets.append(reset)
        after = _number(value.get("resets_in_seconds"))
        if after is not None and after > 0:
            exhausted = True
            resets.append(now + after)
    return exhausted, max(resets, default=now + 5)


def record_limit(db, row, error, model=None, *, final=False):
    if getattr(error, "code", None) != 429 or getattr(error, "_gpt_limit_recorded", False):
        return False
    headers = {str(k).lower(): v for k, v in (getattr(error, "headers", None) or {}).items()}
    capture_headers(db, row, headers)
    now = time.time()
    exhausted, until = rate_limit_reset(error, now)
    if not exhausted and not final:
        return False
    def update(meta):
        limits = {k: v for k, v in (meta.get("gpt_rate_limits") or {}).items() if v > now}
        limits[model or "*"] = max(limits.get(model or "*", 0), math.ceil(until))
        meta["gpt_rate_limits"] = limits
    edit_meta(db, row["id"], update)
    error._gpt_limit_recorded = True
    return True


def session_key(client, group_id=None):
    value = (client or {}).get("session")
    if not value:
        return None
    # Namespace by caller and group so unrelated users never share ownership.
    return hashlib.sha256((str((client or {}).get("owner", "")) + ":" + str(group_id) + ":" + value).encode()).hexdigest()


def preferred_id(db, client, group_id=None):
    key = session_key(client, group_id)
    if not key:
        return None
    with db._conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE channel='gpt' AND status='active' "
            "AND json_extract(meta,?)>? ORDER BY priority,id LIMIT 1", ('$.gpt_sessions."' + key + '"', int(time.time()))).fetchone()
    return row["id"] if row else None


def bind_session(db, row, client, group_id=None):
    key = session_key(client, group_id)
    if not key:
        return
    now = int(time.time())
    def update(meta):
        bindings = {k: v for k, v in (meta.get("gpt_sessions") or {}).items() if v > now}
        bindings[key] = now + 3600
        meta["gpt_sessions"] = bindings
    edit_meta(db, row["id"], update)
