"""Account operations adapted to bit-api's adapters and SQLite storage.

Interaction reference: sub2api AccountsView / account components. Credentials
stay write-only on normal CRUD; export is an explicit administrator operation.
"""
import asyncio
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import time
import threading
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

from core import account_runtime, adapter as adapters, pool_state, portal_state
from core import db as dbmod
from core.pool import apply_refresh

STATUS_LABELS = {"active": "可用", "cooldown": "冷却", "exhausted": "耗尽",
                 "dead": "失效", "unchecked": "待检"}
EDITABLE = {"name", "account_type", "priority", "concurrency", "rate_multiplier",
            "group_ids", "account_expires_at", "auto_pause_on_expired", "temp_until",
            "temp_reason", "proxy_id", "quota_limit", "quota_daily_limit", "quota_weekly_limit",
            "base_rpm", "window_cost_limit"}
TYPE_SQL = "COALESCE(NULLIF(account_type,''),CASE WHEN channel='gpt' OR json_extract(secret,'$.refresh_token') IS NOT NULL THEN 'oauth' WHEN json_extract(secret,'$.api_key') IS NOT NULL THEN 'apikey' WHEN json_extract(secret,'$.password') IS NOT NULL THEN 'password' ELSE 'token' END)"
POOL_KIND_SQL = "CASE WHEN " + TYPE_SQL + "='apikey' THEN 'key' ELSE 'account' END"
_SYNC_LOCK = threading.Lock()
_SYNCING = set()
_MODEL_LOCK = threading.Lock()
_MODEL_SCANS = {}
SYNC_TTL = 300
PAUSED_SQL = "COALESCE(json_extract(meta,'$.admin_paused'),0)"
AVAILABLE_SQL = ("status='active' AND " + PAUSED_SQL + "=0 AND temp_until<=strftime('%s','now') "
                 "AND upstream_until<=strftime('%s','now') "
                 "AND (auto_pause_on_expired=0 OR account_expires_at=0 OR account_expires_at>strftime('%s','now')) "
                 "AND (json_extract(meta,'$.parent_account_id') IS NULL OR EXISTS (SELECT 1 FROM accounts p WHERE p.id=json_extract(accounts.meta,'$.parent_account_id') AND p.channel='gpt' AND p.status='active' AND COALESCE(json_extract(p.meta,'$.admin_paused'),0)=0 AND p.temp_until<=strftime('%s','now') AND (p.auto_pause_on_expired=0 OR p.account_expires_at=0 OR p.account_expires_at>strftime('%s','now'))))")
ZERO = {"requests": 0, "tokens": 0, "cost": 0.0, "user_cost": 0.0, "standard_cost": 0.0,
        "costed": 0}
TOKEN_SQL = ("input_tokens+output_tokens+COALESCE(json_extract(pricing_snapshot,'$.cache_read_tokens'),0)"
             "+COALESCE(json_extract(pricing_snapshot,'$.cache_write_tokens'),0)")
# costed = 有上游账号归属、因而算得出成本的行数。billing 只在 upstream_account_id 非空时
# 写 account_cost(否则 NULL),所以 SUM(account_cost) 只覆盖这部分。前端靠它区分
# 「真的零成本」和「压根没记成本」—— 后者直接显示 $0 会让人误判这条路不要钱。
USAGE_SQL = (f"COUNT(*) requests, COALESCE(SUM({TOKEN_SQL}),0) tokens, "
             "COUNT(account_cost) costed, "
             "COALESCE(SUM(account_cost),0) cost, COALESCE(SUM(actual_cost),0) user_cost, "
             "COALESCE(SUM(cost),0) standard_cost")


def today_start():
    from core.clock import day_start
    return day_start()


def account_or_404(aid):
    row = pool_state.DB.get_account(int(aid))
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, f"account not found: {aid}")
    return row


def adapter_of(row):
    adapter = adapters.get_adapter(row["channel"])
    if adapter is None:
        raise ValueError("渠道未加载：" + row["channel"])
    return adapter


def account_type(row):
    if row.get("account_type"):
        return row["account_type"]
    sec = row.get("secret") or {}
    return ("oauth" if row["channel"] == "gpt" or sec.get("refresh_token") else
            "apikey" if sec.get("api_key") else "password" if sec.get("password") else "token")


def pool_kind(row):
    return "key" if account_type(row) == "apikey" else "account"


def check_pool_kind(row, expected):
    if expected not in (None, "account", "key"):
        raise ValueError("未知池分类")
    if expected is not None and pool_kind(row) != expected:
        raise ValueError("记录不属于当前池分类，请重新选择")


def category_counts():
    with pool_state.DB._conn() as c:
        rows = c.execute("SELECT " + POOL_KIND_SQL + " kind,COUNT(*) n FROM accounts GROUP BY kind")
        return {"account": 0, "key": 0, **{r["kind"]: r["n"] for r in rows}}


def supports_proxy(adapter):
    from adapters.openai_compat import OpenAICompatAdapter
    return isinstance(adapter, OpenAICompatAdapter)


def normalize_proxy_url(value, input_format="auto", scheme="http"):
    """Convert the selected credential layout to one canonical proxy URL."""
    value = value.strip()
    if input_format == "auto" and not value.lower().startswith(("http://", "https://")):
        candidates = set()
        for layout in ("host_port_user_pass", "user_pass_at_host_port", "user_pass_host_port", "host_port_at_user_pass"):
            try:
                candidates.add(normalize_proxy_url(value, layout, scheme))
            except ValueError:
                continue
        if len(candidates) == 1:
            return candidates.pop()
        if len(candidates) > 1:
            raise ValueError("无法唯一识别代理格式，请粘贴 http://用户名:密码@主机:端口 形式的完整地址")
        return normalize_proxy_url(f"{scheme}://{value}", "url", scheme)
    try:
        if any(char in value for char in "\r\n\t"):
            raise ValueError
        if not value.lower().startswith(("http://", "https://")) and input_format != "url":
            if input_format == "host_port_user_pass":
                host, port, user, password = re.fullmatch(
                    r"(\[[^\]]+\]|[^:]+):(\d+):([^:]+):(.*)", value).groups()
                address = f"{host}:{port}"
            elif input_format == "user_pass_host_port":
                user, password, host, port = re.fullmatch(
                    r"([^:]+):(.*):(\[[^\]]+\]|[^:\[\]]+):(\d+)", value).groups()
                address = f"{host}:{port}"
            else:
                if input_format == "user_pass_at_host_port":
                    credentials, address = value.rsplit("@", 1)
                elif input_format == "host_port_at_user_pass":
                    address, credentials = value.split("@", 1)
                else:
                    raise ValueError
                user, password = credentials.split(":", 1)
            if not user or scheme not in ("http", "https"):
                raise ValueError
            address_parts = urlsplit(f"{scheme}://{address}")
            if address_parts.port is None or address_parts.username is not None:
                raise ValueError
            value = f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{address}"
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or any(char.isspace() for char in parsed.hostname)
                or port == 0 or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        credentials = ""
        if parsed.username is not None:
            if not parsed.username:
                raise ValueError
            credentials = quote(unquote(parsed.username), safe="")
            if parsed.password is not None:
                credentials += ":" + quote(unquote(parsed.password), safe="")
            credentials += "@"
        return f"{parsed.scheme}://{credentials}{host}:{port or (443 if parsed.scheme == 'https' else 80)}"
    except (ValueError, AttributeError) as exc:
        raise ValueError("代理地址无效，请检查主机、端口或使用完整代理 URL") from exc


def test_proxy(proxy):
    """Probe the GPT API through this proxy, without account credentials."""
    import urllib.error
    import urllib.request
    import config
    from core.gpt_transport import open_request

    target = config.GPT_BASE_URL.rstrip("/") + "/models?client_version=" + quote(config.GPT_CLIENT_VERSION)
    request = urllib.request.Request(target, headers={"Accept": "application/json",
        "User-Agent": f"codex_cli_rs/{config.GPT_CLIENT_VERSION}", "originator": "codex_cli_rs"})
    started, status, is_json, message = time.monotonic(), None, False, ""
    try:
        with account_runtime.test_budget(10):
            try:
                with open_request(request, 10, proxy["url"]) as response:
                    status = response.response.status_code
                    is_json = "json" in response.headers.get("Content-Type", "").lower()
            except urllib.error.HTTPError as exc:
                status = exc.code
                is_json = "json" in (exc.headers.get("Content-Type", "") if exc.headers else "").lower()
                exc.close()
    except Exception as exc:
        status = None
        if isinstance(exc, TimeoutError):
            message = "连接 GPT 超时（10 秒）"
        elif "407" in str(exc):
            message = "代理认证失败，请检查用户名和密码"
        elif "ssl" in str(exc).lower() or "certificate" in str(exc).lower():
            message = "与 GPT 的 TLS 连接失败，请检查代理网络"
        else:
            message = "无法通过此代理连接 GPT，请检查代理地址、端口和网络"
    elapsed = round((time.monotonic() - started) * 1000)
    ok = status in (200, 401) and is_json
    if status is not None:
        message = ("已连通 GPT（HTTP 401 为未携带账号凭据时的正常响应）" if ok and status == 401 else
                   "已连通 GPT" if ok else "代理认证失败，请检查用户名和密码" if status == 407 else
                   "GPT 拒绝访问或触发网页验证（HTTP 403）" if status == 403 else
                   "GPT 正在限流，请稍后重试（HTTP 429）" if status == 429 else
                   "GPT 返回了非 API 响应，可能触发网页验证" if status in (200, 401) else f"GPT 返回 HTTP {status}")
    return {"ok": ok, "latency_ms": elapsed if status is not None else None,
            "elapsed_ms": elapsed, "http_status": status, "message": message}


def fingerprint(row):
    # Normal traffic and probes must not invalidate an administrator's preview.
    values = {k: v for k, v in row.items() if k not in {
        "last_used_at", "last_check", "updated_at", "balance", "token", "token_exp", "last_error"}}
    values["meta"] = {k: v for k, v in (row.get("meta") or {}).items() if k not in {
        "pool_upstream_usage", "pool_gpt_profile", "pool_gpt_sync", "pool_gpt_reset_credits", "pool_test_models",
        "gpt_rate_limits", "gpt_sessions", "gpt_auto_reset_state", "pool_gpt_referrals", "pool_gpt_manifest"}}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def filters_sql(filters):
    where, args = ["1=1"], []
    kind = filters.get("pool_kind")
    if kind:
        if kind not in ("account", "key"):
            raise ValueError("未知池分类")
        where.append("(" + POOL_KIND_SQL + ")=?")
        args.append(kind)
    for key in ("channel", "account_type", "proxy_id"):
        value = filters.get(key)
        if value not in (None, "", "all"):
            where.append((TYPE_SQL if key == "account_type" else key) + "=?")
            args.append(value)
    q = (filters.get("q") or "").strip()
    if q:
        where.append("(identity LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR CAST(id AS TEXT)=? OR COALESCE(json_extract(meta,'$.note'),'') LIKE ? ESCAPE '\\')")
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend(["%" + escaped + "%", "%" + escaped + "%", q, "%" + escaped + "%"])
    status = filters.get("status")
    if status in STATUS_LABELS:
        where.append("status=?")
        args.append(status)
    elif status == "paused":
        where.append(PAUSED_SQL + "<>0")
    elif status == "temporary":
        where.append("temp_until>?")
        args.append(int(time.time()))
    elif status == "upstream":
        where.append("upstream_until>?")
        args.append(int(time.time()))
    elif status == "expired":
        where.append("account_expires_at>0 AND account_expires_at<=?")
        args.append(int(time.time()))
    elif status:
        raise ValueError("未知状态：" + status)
    if filters.get("schedulable") in (True, "true", "1", 1):
        where.append("(" + AVAILABLE_SQL + ") AND channel IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(list(adapters.all_adapters())))
    elif filters.get("schedulable") in (False, "false", "0", 0):
        where.append("NOT ((" + AVAILABLE_SQL + ") AND channel IN (SELECT value FROM json_each(?)))")
        args.append(json.dumps(list(adapters.all_adapters())))
    privacy = filters.get("privacy_mode")
    if privacy == "__unset__":
        where.append("json_extract(meta,'$.pool_privacy_mode') IS NULL")
    elif privacy:
        where.append("json_extract(meta,'$.pool_privacy_mode')=?")
        args.append(privacy)
    group = filters.get("group_id")
    if group not in (None, "", "all"):
        if str(group) == "ungrouped":
            where.append("json_array_length(group_ids)=0")
        else:
            where.append("? IN (SELECT value FROM json_each(group_ids))")
            args.append(int(group))
    return " AND ".join(where), args


def matching_rows(filters, *, limit=500, offset=0, sort="id", order="desc", before_id=None):
    where, args = filters_sql(filters)
    if before_id is not None:
        where += " AND id<?"
        args.append(int(before_id))
    allowed = {"id", "identity", "name", "status", "priority", "concurrency", "rate_multiplier",
               "last_used_at", "created_at", "updated_at", "account_expires_at", "token_exp"}
    key = sort if sort in allowed else "id"
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"SELECT * FROM accounts WHERE {where} ORDER BY {key} {direction}, id {direction}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [max(1, min(int(limit), 500)), max(0, int(offset))]
    with pool_state.DB._conn() as c:
        return [pool_state.DB._row(r) for r in c.execute(sql, args)]


def usage_map(ids, since, after_id=None):
    if not ids:
        return {}
    out = {}
    # Keep parameter counts bounded for SQLite builds with a 999-variable limit.
    with portal_state.USER_DB._conn() as c:
        for start in range(0, len(ids), 400):
            part = ids[start:start + 400]
            marks = ",".join("?" for _ in part)
            extra = " AND id>?" if after_id is not None else ""
            args = [*part, int(since)] + ([after_id] if after_id is not None else [])
            rows = c.execute(f"SELECT upstream_account_id, {USAGE_SQL} FROM usage_logs "
                             f"WHERE upstream_account_id IN ({marks}) AND created_at>=?{extra} GROUP BY upstream_account_id",
                             args).fetchall()
            out.update({r["upstream_account_id"]: {k: r[k] for k in ZERO} for r in rows})
    return out


def view(row, today=None, week=None, total_usage=None):
    now = int(time.time())
    if today is None:
        today = usage_map([row["id"]], today_start())
    if week is None:
        week = usage_map([row["id"]], now - 7 * 86400)
    if total_usage is None:
        total_usage = usage_map([row["id"]], 0)
    meta, sec = row.get("meta") or {}, row.get("secret") or {}
    paused = bool(meta.get("admin_paused"))
    expired = bool(row.get("account_expires_at") and row["account_expires_at"] <= now)
    temporary = (row.get("temp_until") or 0) > now
    upstream_blocked = (row.get("upstream_until") or 0) > now
    blocked = paused or temporary or upstream_blocked or (expired and row.get("auto_pause_on_expired", 1))
    adapter = adapters.get_adapter(row["channel"])
    exp = int(row.get("token_exp") or 0)
    data = {k: row.get(k) for k in EDITABLE}
    data.update({"id": row["id"], "identity": row["identity"], "channel": row["channel"],
                 "name": row.get("name") or row["identity"], "account_type": account_type(row),
                 "status": row["status"], "paused": paused, "expired": expired,
                 "status_label": "已停用" if paused else "临时暂停" if temporary else "已到期" if expired else "上游限流" if upstream_blocked else STATUS_LABELS.get(row["status"], row["status"]),
                 "schedulable": row["status"] == "active" and not blocked and adapter is not None,
                 "note": meta.get("note") or "", "last_error": row.get("last_error") or "",
                 "has_refresh_token": bool(sec.get("refresh_token")),
                 "has_api_key": bool(sec.get("api_key") or row.get("token")),
                 "credential_fields": sorted(k for k, v in sec.items() if v is not None),
                 "expires_at": exp or None, "expires_in": exp - now if exp else None,
                 "expiry_warning": bool(exp and 0 < exp - now <= 3600),
                 "token_expired": bool(exp and exp <= now), "balance": row.get("balance"),
                 "last_check": row.get("last_check") or 0, "last_used_at": row.get("last_used_at") or None,
                 "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                 "inflight": account_runtime.capacity(row["id"]),
                 "current_rpm": account_runtime.rpm(row["id"]),
                 "today": (today or {}).get(row["id"], dict(ZERO)),
                 "total_usage": total_usage.get(row["id"], dict(ZERO)),
                 "week": (week or {}).get(row["id"], dict(ZERO)),
                 "capabilities": list(getattr(adapter, "capabilities", [])),
                 "models": list(getattr(adapter, "models", [])),
                 "pool_kind": pool_kind(row),
                 "can_proxy": supports_proxy(adapter), "channel_loaded": adapter is not None})
    data["can_usage"] = callable(getattr(adapter, "account_usage", None))
    data["can_privacy"] = callable(getattr(adapter, "disable_training", None))
    data["upstream_usage"] = meta.get("pool_upstream_usage")
    data["privacy_mode"] = meta.get("pool_privacy_mode")
    if row["channel"] == "gpt":
        from core import codex, gpt_runtime
        data["last_error_kind"] = "capacity" if codex.is_capacity_error(data["last_error"]) else ""
        from core.gpt_profile import normalize_usage
        profile = meta.get("pool_gpt_profile") or {}
        data["email"] = profile.get("email") or meta.get("email") or sec.get("email")
        data["plan_type"] = gpt_runtime.settings(row).get("plan_override") or profile.get("plan_type") or (data["upstream_usage"] or {}).get("plan_type") or meta.get("plan_type") or sec.get("plan_type")
        data["chatgpt_account_id"] = profile.get("account_id") or sec.get("account_id") or meta.get("account_id")
        data["subscription_expires_at"] = profile.get("subscription_expires_at")
        data["workspace_name"] = profile.get("workspace_name")
        data["gpt_sync"] = meta.get("pool_gpt_sync") or {}
        data["gpt_reset_credits"] = meta.get("pool_gpt_reset_credits")
        data["gpt_settings"] = gpt_runtime.settings(row)
        data["parent_account_id"] = meta.get("parent_account_id")
        data["auth_mode"] = sec.get("auth_mode") or "oauth"
        from core.gpt_models import entries
        data["models"] = [m["slug"] for m in entries(row, getattr(adapter, "configured_models", []))]
        if meta.get("parent_account_id"):
            data["can_proxy"] = False
            parent = pool_state.DB.get_account(meta["parent_account_id"])
            if not parent or parent["status"] != "active" or (parent.get("meta") or {}).get("admin_paused") or parent.get("temp_until", 0) > now or (parent.get("auto_pause_on_expired") and 0 < parent.get("account_expires_at", 0) <= now):
                data.update(schedulable=False, status_label="主账号不可用")
        data["upstream_until"] = row.get("upstream_until") or 0
        data["upstream_reason"] = row.get("upstream_reason") or ""
        data["model_rate_limits"] = {key: value for key, value in (meta.get("gpt_rate_limits") or {}).items() if value > now}
        data["gpt_auto_reset_state"] = meta.get("gpt_auto_reset_state")
        data["gpt_referrals"] = meta.get("pool_gpt_referrals")
        data["usage_windows"] = {}
        if data["upstream_usage"]:
            data["upstream_usage"] = normalize_usage(data["upstream_usage"])
        for key, fallback in (("five_hour", 18000), ("seven_day", 604800)):
            window = (data["upstream_usage"] or {}).get(key)
            duration = window["limit_window_seconds"] if window else fallback
            reset = window.get("reset_at") if window else None
            since = max(0, (reset if reset and reset > now else now) - duration)
            stats = usage_map([row["id"]], since).get(row["id"], dict(ZERO))
            data["usage_windows"][key] = stats
            if window:
                window["window_stats"] = stats
    return data


def validate_changes(row, changes):
    values = {k: v for k, v in changes.items() if k in EDITABLE}
    if (row.get("meta") or {}).get("parent_account_id") and any(k in changes for k in ("credentials", "proxy_id", "group_ids", "priority", "concurrency")):
        inherited = {"proxy_id", "group_ids", "priority", "concurrency"}
        if "credentials" in changes or any(changes[k] != row.get(k) for k in inherited if k in changes):
            raise ValueError("Spark 的凭据、代理、分组、优先级与并发继承主账号，请编辑主账号")
    if row["channel"] == "gpt" and values.get("account_type") not in (None, "oauth"):
        raise ValueError("GPT 原生账号使用 OAuth 凭据")
    groups = values.get("group_ids")
    if groups is not None:
        known = {g["id"] for g in portal_state.USER_DB.list_groups()}
        if any(g not in known for g in groups):
            raise ValueError("包含不存在的用户分组")
        values["group_ids"] = sorted(set(groups))
    proxy_id = values.get("proxy_id")
    if proxy_id:
        if not supports_proxy(adapter_of(row)):
            raise ValueError("该渠道尚不支持按账号设置代理")
        if not pool_state.DB.get_pool_proxy(proxy_id):
            raise ValueError("代理不存在")
    if any(key in changes for key in ("paused", "note", "gpt_settings")):
        meta = dict(row.get("meta") or {})
        if "paused" in changes:
            meta["admin_paused"] = changes["paused"]
        if "note" in changes:
            meta["note"] = (changes["note"] or "").strip()
        if "gpt_settings" in changes:
            if row["channel"] != "gpt":
                raise ValueError("GPT 设置仅适用于 GPT 账号")
            policy = {**(meta.get("gpt_settings") or {}), **changes["gpt_settings"]}
            for name in ("model_mapping", "compact_model_mapping"):
                if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value)
                       for pair in policy.get(name, {}).items() for value in pair):
                    raise ValueError("模型映射包含无效的模型 ID")
            if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) for value in policy.get("model_whitelist", [])):
                raise ValueError("模型白名单包含无效的模型 ID")
            reserved = {"authorization", "x-api-key", "host", "content-length", "cookie", "chatgpt-account-id", "chatgpt-user-id"}
            if any(key.lower() in reserved or not re.fullmatch(r"[A-Za-z0-9-]+", key) or "\n" in key + value or "\r" in key + value
                   for key, value in policy.get("header_overrides", {}).items()):
                raise ValueError("请求头不能覆盖认证、工作区、Cookie 或包含换行")
            if meta.get("parent_account_id") and policy.get("auto_reset"):
                raise ValueError("请在主账号上设置自动重置券")
            meta["gpt_settings"] = policy
        values["meta"] = meta
    status = changes.get("status")
    if status is not None:
        if status not in STATUS_LABELS:
            raise ValueError("未知健康状态")
        values["status"] = status
    credentials = changes.get("credentials")
    if credentials is not None:
        if (row.get("meta") or {}).get("parent_account_id"):
            raise ValueError("Spark 影子账号继承主账号凭据，请编辑主账号")
        if not credentials:
            raise ValueError("凭据不能为空；不修改时请省略此字段")
        sec = {**(row.get("secret") or {}), **credentials}
        for key in ("api_key", "access_token", "password"):
            if key in credentials and (not isinstance(credentials[key], str) or not credentials[key].strip()):
                raise ValueError(key + " 必须是非空文本")
        values["secret"] = sec
        if row["channel"] == "gpt":
            from core.gpt_accounts import normalize_entry
            normalized = normalize_entry(sec)
            values.update(token=normalized["access_token"], token_exp=normalized["expires_at"])
        elif "api_key" in credentials:
            values["token"] = credentials["api_key"]
        elif "access_token" in credentials:
            values["token"] = credentials["access_token"]
    return values


def patch_account(aid, changes):
    row = account_or_404(aid)
    values = validate_changes(row, changes)
    if "meta" in values:
        from core.gpt_runtime import edit_meta, quota_block
        meta = values.pop("meta")
        patch = {target: meta[target] for key, target in {
            "paused": "admin_paused", "note": "note", "gpt_settings": "gpt_settings"}.items() if key in changes}
        def update(current):
            current.update(patch)
            if row["channel"] == "gpt":
                until, reason = quota_block({**row, "meta": current})
                return {"upstream_until": int(until), "upstream_reason": reason}
        edit_meta(pool_state.DB, aid, update, **values)
    else:
        pool_state.DB.update_account(aid, **values)
    inherited = {k: v for k, v in values.items() if k in ("proxy_id", "group_ids", "priority", "concurrency")}
    if row["channel"] == "gpt" and inherited:
        with pool_state.DB._conn() as conn:
            conn.execute("UPDATE accounts SET " + ",".join(key + "=?" for key in inherited) + " WHERE channel='gpt' AND json_extract(meta,'$.parent_account_id')=?",
                [*[json.dumps(v) if isinstance(v, list) else v for v in inherited.values()], aid])
    if row["channel"] == "gpt" and "gpt_settings" in changes:
        from core.gpt_models import rebuild
        rebuild(adapter_of(row))
    return view(account_or_404(aid))


def safe_error(error, row):
    from pydantic import ValidationError
    if isinstance(error, ValidationError):
        return "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"]
                         for e in error.errors(include_input=False, include_context=False))[:800]
    message = str(error)
    def redact(value):
        nonlocal message
        if isinstance(value, dict):
            for child in value.values():
                redact(child)
        elif isinstance(value, str) and len(value) >= 6:
            message = message.replace(value, "[redacted]")
    redact(row.get("secret") or {})
    redact(row.get("token"))
    return re.sub(r"(?i)(Bearer|AgentAssertion)\s+[^\s\"']+", r"\1 [redacted]", message)[:800]


def refresh_account(row):
    adapter = adapter_of(row)
    if not adapter.has(adapters.CAP_REFRESH):
        raise ValueError("该账号使用静态凭据，无需续期")
    with account_runtime.network(pool_state.DB, row):
        update = (adapter._ensure_access(row, force=True) if hasattr(adapter, "_ensure_access")
                  else adapter.refresh_token(row))
    apply_refresh(pool_state.DB, row, update)
    pool_state.DB.update_account(row["id"], last_error="")


def sync_gpt_account(row, force=False):
    if row["channel"] != "gpt" or pool_kind(row) != "account":
        raise ValueError("此同步仅适用于 GPT 账号池")
    aid = row["id"]
    now = int(time.time())
    with _SYNC_LOCK:
        latest = account_or_404(aid)
        previous = (latest.get("meta") or {}).get("pool_gpt_sync") or {}
        if aid in _SYNCING:
            return {"ok": True, "syncing": True, "account": view(latest)}
        delay = 60 if previous.get("errors") else SYNC_TTL
        if not force and now - int(previous.get("attempted_at") or 0) < delay:
            return {"ok": not bool(previous.get("errors")), "cached": True, "account": view(latest)}
        _SYNCING.add(aid)
    updates, errors = {}, {}
    try:
        adapter = adapter_of(latest)
        with account_runtime.network(pool_state.DB, latest):
            try:
                apply_refresh(pool_state.DB, latest, adapter.refresh_token(latest))
            except Exception as exc:
                errors["credentials"] = safe_error(exc, latest)
            if not errors:
                methods = (("usage", "account_usage"),) if (latest.get("meta") or {}).get("parent_account_id") else (("usage", "account_usage"), ("profile", "account_profile"), ("reset_credits", "account_reset_credits"))
                for key, method in methods:
                    try:
                        updates[key] = getattr(adapter, method)(latest)
                        if key == "profile" and updates[key].get("subscription_error"):
                            errors["subscription"] = updates[key].pop("subscription_error")
                    except Exception as exc:
                        errors[key] = safe_error(exc, latest)
        from core.gpt_runtime import edit_meta, quota_block
        def merge(meta):
            if "usage" in updates and updates["usage"].get("fetched_at", now) >= (meta.get("pool_upstream_usage") or {}).get("fetched_at", 0):
                meta["pool_upstream_usage"] = updates["usage"]
            if "reset_credits" in updates:
                meta["pool_gpt_reset_credits"] = updates["reset_credits"]
            if "profile" in updates:
                profile = updates["profile"]
                old_profile = meta.get("pool_gpt_profile") or {}
                if errors.get("subscription") and old_profile.get("account_id") == profile.get("account_id"):
                    profile["subscription_expires_at"] = old_profile.get("subscription_expires_at")
                meta["pool_gpt_profile"] = profile
            sync = {**(meta.get("pool_gpt_sync") or previous), "attempted_at": now, "errors": errors}
            if updates:
                sync["updated_at"] = int(time.time())
            if not errors:
                sync["synced_at"] = int(time.time())
            meta["pool_gpt_sync"] = sync
            until, reason = quota_block({**latest, "meta": meta})
            return {"upstream_until": int(until), "upstream_reason": reason}
        edit_meta(pool_state.DB, aid, merge)
        return {"ok": not bool(errors), "partial": bool(updates and errors), "errors": errors,
                "account": view(account_or_404(aid))}
    finally:
        with _SYNC_LOCK:
            _SYNCING.discard(aid)


def account_test_models(row):
    adapter = adapter_of(row)
    configured = [{"id": model, "name": model, "description": ""}
                  for model in adapter.models if adapter.supports_chat(model)]
    if row["channel"] != "gpt":
        return {"models": configured, "source": "configured", "allow_custom": False}
    from core.gpt_models import checks
    from core.codex import is_capacity_error
    from core.gpt_runtime import settings
    results = checks(row, adapter)
    meta, policy = row.get("meta") or {}, settings(row)
    snapshot = meta.get("pool_gpt_manifest") or {}
    models = {}
    manifest = snapshot.get("models") or []
    if meta.get("parent_account_id"):
        manifest = [{"slug": name} for name in policy["model_whitelist"]]
    for item in manifest:
        name = item.get("slug") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name or (policy["model_whitelist"] and name not in policy["model_whitelist"]):
            continue
        # The administrator's test picker includes upstream-hidden and untested
        # models. Test outcomes annotate this catalog instead of removing entries.
        models.setdefault(name, {"id": name, "name": item.get("display_name") or name,
                                 "hidden": item.get("visibility") == "hide",
                                 "supported_in_api": item.get("supported_in_api"), "origin": "upstream"})
    for name, result in results.items():
        if result.get("ok"):
            models.setdefault(name, {"id": name, "name": name, "hidden": False, "origin": "verified"})
    for name, item in models.items():
        result = results.get(name) or {}
        status = ("verified" if result.get("ok") else "busy" if result.get("error_kind") == "capacity" or is_capacity_error(result.get("error", ""))
                  else "failed" if result else "untested")
        item.update(test_status=status, verified_at=result.get("verified_at"), checked_at=result.get("checked_at"),
                    elapsed_ms=result.get("elapsed_ms"), error=result.get("error", ""))
    models = list(models.values())
    with _MODEL_LOCK:
        scan = dict(_MODEL_SCANS.get(row["id"]) or {"running": False})
    from core.gpt_web.runtime import image_models
    return {"models": models, "image_models": image_models(row), "source": "upstream" if manifest else "verified", "allow_custom": True, "scan": scan,
            "verified_count": sum(item["test_status"] == "verified" for item in models),
            "manifest_fetched_at": snapshot.get("fetched_at"),
            "results": [{"id": name, **result} for name, result in results.items()],
            "fetched_at": max((result["checked_at"] for result in results.values()), default=0)}


def begin_model_scan(row):
    if row["channel"] != "gpt":
        raise ValueError("此项检测仅支持 GPT 账号")
    with _MODEL_LOCK:
        if (_MODEL_SCANS.get(row["id"]) or {}).get("running"):
            return False
        _MODEL_SCANS[row["id"]] = {"running": True, "done": 0, "total": 0, "current": "", "error": "",
                                   "started_at": int(time.time())}
    return True


def scan_account_models(aid):
    """Serial, nonbilling hi probes. A rejected candidate never penalizes the account."""
    def progress(**values):
        with _MODEL_LOCK:
            _MODEL_SCANS[aid].update(values)
    try:
        row = account_or_404(aid)
        adapter = adapter_of(row)
        try:
            with account_runtime.network(pool_state.DB, row):
                apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
                adapter.account_models(row)
        except Exception as exc:
            progress(error="模型目录同步失败，继续检测已知型号：" + safe_error(exc, row))
        from core.gpt_models import test_candidates
        candidates = test_candidates(account_or_404(aid), adapter)
        progress(total=len(candidates))
        for index, model in enumerate(candidates):
            progress(current=model)
            result = run_test(account_or_404(aid), model, discovery=True)
            # One retry for transient upstream failures; an unsupported model is not retried.
            if not result["ok"] and result.get("error_code") in (502, 503, 504) and result.get("error_kind") != "capacity":
                result = run_test(account_or_404(aid), model, discovery=True)
            progress(done=index + 1)
            if not result["ok"] and result.get("error_code") in (401, 402, 403, 429):
                progress(error="检测暂停：" + result["error"])
                break
    except Exception as exc:
        progress(error=safe_error(exc, pool_state.DB.get_account(aid) or {}))
    finally:
        progress(running=False, current="", finished_at=int(time.time()))


def test_model(row, model=None):
    adapter = adapter_of(row)
    models = list(getattr(adapter, "models", []))
    if not adapter.has(adapters.CAP_CHAT):
        raise ValueError("该渠道不支持对话测试")
    model = model or (models[0] if models else "")
    if row["channel"] == "gpt":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
            raise ValueError("请输入有效的上游模型 ID")
    elif model not in models or not adapter.supports_chat(model):
        raise ValueError("请选择该渠道的对话模型")
    return model


def run_test(row, model=None, *, discovery=False):
    adapter = adapter_of(row)
    model = test_model(row, model)
    started = time.time()
    local_wait = False
    try:
        with account_runtime.test_budget() if row["channel"] == "gpt" else nullcontext():
            if adapter.has(adapters.CAP_REFRESH) and (not row.get("token") or
                    (row.get("token_exp") or 0) < time.time() + 60):
                with account_runtime.network(pool_state.DB, row):
                    apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
            with account_runtime.dispatch(pool_state.DB, row, record=False):
                reply = adapter.chat(row, [{"role": "user", "content": "hi"}],
                                     stream=False, model=model, body={"max_tokens": 256})
        raw = reply.get("raw") if isinstance(reply, dict) else None
        message = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content")
                   if isinstance(raw, dict) else reply.get("content") if isinstance(reply, dict) else reply)
        if isinstance(message, list):
            message = "\n".join(item.get("text", "") for item in message
                                if isinstance(item, dict) and isinstance(item.get("text"), str))
        if not str(message or "").strip():
            raise RuntimeError("上游未返回文本回答")
        if not discovery:
            pool_state.DB.update_account(row["id"], status="active", last_check=int(time.time()), last_error="")
        result = {"ok": True, "reply": str(message), "usage": (raw or {}).get("usage")}
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "status", None)
        status = row["status"]
        local_wait = isinstance(exc, (account_runtime.AccountBusyError, account_runtime.AccountTestTimeout))
        if not discovery and not local_wait and not (row["channel"] == "gpt" and code in (400, 404, 422)):
            status = pool_state.POOL.mark_failure(row["id"], exc, adapter=adapter)
        error = safe_error(exc, row)
        if not discovery and not local_wait:
            pool_state.DB.update_account(row["id"], last_error=error)
        result = {"ok": False, "error": error, "error_code": code, "status_after": status}
        if row["channel"] == "gpt":
            from core.codex import is_capacity_error
            if is_capacity_error(exc):
                result.update(error_kind="capacity", upstream_error=error,
                              error="GPT 上游繁忙，暂未获得模型容量，请稍后再试")
    result.update(account_id=row["id"], model=model, prompt="hi", elapsed_ms=int((time.time() - started) * 1000))
    if row["channel"] == "gpt" and (result["ok"] or not local_wait):
        from core.gpt_models import record_test
        record_test(adapter, row, result)
    with pool_state.DB._conn() as c:
        c.execute("INSERT INTO pool_test_results(account_id,created_at,model,result) VALUES(?,?,?,?)",
                  (row["id"], int(time.time()), model, json.dumps(result)))
    return result


def operate(row, action, changes=None, model=None):
    aid = row["id"]
    if action == "sync":
        result = sync_gpt_account(row, force=True)
        if not result["ok"]:
            raise ValueError("；".join(result.get("errors", {}).values()))
        return result
    elif action in ("pause", "resume"):
        patch_account(aid, {"paused": action == "pause"})
    elif action == "edit":
        patch_account(aid, changes or {})
    elif action in ("reset", "recover"):
        pool_state.DB.update_account(aid, status="unchecked", temp_until=0, temp_reason="", last_error="", upstream_until=0, upstream_reason="")
        if row["channel"] == "gpt":
            from core.gpt_runtime import edit_meta
            edit_meta(pool_state.DB, aid, lambda meta: meta.update(gpt_rate_limits={}))
    elif action == "refresh":
        refresh_account(row)
    elif action == "reset_quota":
        with portal_state.USER_DB._conn() as c:
            last_id = c.execute("SELECT COALESCE(MAX(id),0) FROM usage_logs WHERE upstream_account_id=?", (aid,)).fetchone()[0]
        pool_state.DB.update_account(aid, quota_reset_at=int(time.time()), quota_reset_log_id=last_id)
    elif action == "balance":
        adapter = adapter_of(row)
        if not adapter.has(adapters.CAP_BALANCE):
            raise ValueError("该渠道不支持查询余额")
        balance = adapter.balance(row)
        if balance is None:
            raise ValueError("上游未返回余额")
        pool_state.DB.update_account(aid, balance=balance)
    elif action in ("usage", "privacy"):
        adapter = adapter_of(row)
        fn = getattr(adapter, "account_usage" if action == "usage" else "disable_training", None)
        if not callable(fn):
            raise ValueError("该渠道不支持此项上游操作")
        with account_runtime.network(pool_state.DB, row):
            if adapter.has(adapters.CAP_REFRESH):
                apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
            result = fn(row)
        latest = account_or_404(aid)
        if action == "usage" and row["channel"] == "gpt":
            from core.gpt_runtime import save_usage
            save_usage(pool_state.DB, latest, result)
        else:
            from core.gpt_runtime import edit_meta
            edit_meta(pool_state.DB, aid, lambda meta: meta.update({"pool_upstream_usage" if action == "usage" else "pool_privacy_mode": result}))
        return {"ok": True, "result": result}
    elif action == "test":
        result = run_test(row, model)
        if not result["ok"]:
            raise ValueError(result["error"])
        return result
    elif action == "delete":
        adapter = adapters.get_adapter(row["channel"])
        if adapter is not None and hasattr(adapter, "delete_account"):
            adapter.delete_account(pool_state.DB, row["identity"])
        else:
            pool_state.DB.delete_account(row["channel"], row["identity"])
        with pool_state.DB._conn() as c:
            c.execute("DELETE FROM pool_test_plans WHERE account_id=?", (aid,))
    else:
        raise ValueError("未知操作：" + action)
    return {"ok": True}


def quota_exceeded(row):
    now = int(time.time())
    for key, since in (("quota_limit", 0), ("quota_daily_limit", today_start()),
                       ("quota_weekly_limit", now - 7 * 86400), ("window_cost_limit", now - 5 * 3600)):
        if row.get(key):
            usage = usage_map([row["id"]], since, after_id=row.get("quota_reset_log_id") or 0)
            if usage.get(row["id"], ZERO)["cost"] >= row[key]:
                return True
    return False


async def scheduled_tests_loop():
    """One process, bounded probes. Plans never enable administrator-paused accounts."""
    while True:
        try:
            await asyncio.to_thread(run_due_tests)
            from core.gpt_actions import run_maintenance
            await asyncio.to_thread(run_maintenance)
        except Exception as exc:
            print("[pool] scheduled tests failed: " + type(exc).__name__, flush=True)
        await asyncio.sleep(15)


def run_due_tests():
    now = int(time.time())
    with pool_state.DB._conn() as c:
        plans = [dict(r) for r in c.execute(
            "SELECT * FROM pool_test_plans WHERE enabled=1 AND next_run<=? LIMIT 20", (now,))]
        for plan in plans:
            c.execute("UPDATE pool_test_plans SET next_run=? WHERE account_id=?",
                      (now + plan["interval_seconds"], plan["account_id"]))
    def execute(plan):
        try:
            result = run_test(account_or_404(plan["account_id"]), plan["model"])
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__}
        with pool_state.DB._conn() as c:
            c.execute("UPDATE pool_test_plans SET last_run=?,last_result=? WHERE account_id=?",
                      (int(time.time()), json.dumps(result), plan["account_id"]))
        pool_state.DB.close()
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(execute, plans))
