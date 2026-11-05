"""Account import/export: bit-api backups, sub2api data, Codex and key lists."""
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, unquote

from core import account_management as M, gpt_accounts as G, pool_state


def _normalize(value, channel):
    if isinstance(value, str):
        if not channel:
            raise ValueError("导入裸 Key / Token 时请选择目标渠道")
        value = {"credentials": {"access_token" if channel == "gpt" else "api_key": value}}
    if not isinstance(value, dict):
        raise ValueError("账号条目必须是对象或凭据文本")
    channel = channel or value.get("channel")
    if not channel:
        platform = value.get("platform")
        channel = "gpt" if platform == "openai" and value.get("type") == "oauth" else platform
    if not channel:
        # Codex auth.json does not carry a platform.
        channel = "gpt" if any(k in value for k in ("tokens", "accessToken", "access_token", "agent_private_key", "agent_identity")) else None
    if not channel:
        raise ValueError("无法确定渠道，请选择目标渠道")
    M.adapter_of({"channel": channel})
    credentials = value.get("credentials") or value.get("secret") or value
    fields = {k: value[k] for k in M.EDITABLE if k in value and value[k] is not None}
    if channel == "gpt" and value.get("gpt_settings") is not None:
        fields["gpt_settings"] = value["gpt_settings"]
    if channel == "gpt" and value.get("parent_identity"):
        for key in ("priority", "concurrency", "group_ids", "proxy_id"):
            fields.pop(key, None)
        fields["paused"] = bool(value.get("paused"))
        return dict(channel="gpt", identity=value["identity"], parent_identity=value["parent_identity"],
                    credentials={}, token=None, token_exp=0, note=value.get("notes") or "", fields=fields, status="active")
    kind = value.get("account_type") or value.get("type")
    if kind:
        fields["account_type"] = "apikey" if kind in ("api_key", "apikey", "upstream") else kind
    if "paused" in value:
        fields["paused"] = bool(value["paused"])
    elif value.get("status") in ("inactive", "disabled"):
        fields["paused"] = True
    note = value.get("note") or value.get("notes") or ""
    token, token_exp = None, int(value.get("token_exp") or 0)
    if channel == "gpt":
        normalized = G.normalize_entry(credentials)
        credentials = {k: v for k, v in normalized.items() if k in (
            "access_token", "refresh_token", "id_token", "email", "account_id", "user_id", "plan_type", "client_id", "auth_mode", "agent_runtime_id", "agent_private_key", "task_id") and v}
        identity = value.get("identity") or G.build_identity(normalized)
        token, token_exp = normalized["access_token"], normalized["expires_at"]
        fields["account_type"] = "oauth"
    else:
        if not isinstance(credentials, dict) or not credentials:
            raise ValueError("凭据不能为空")
        token = value.get("token") or credentials.get("api_key") or credentials.get("access_token")
        identity = value.get("identity") or credentials.get("email") or value.get("email")
        if not identity:
            seed = token or json.dumps(credentials, sort_keys=True)
            identity = "key-" + hashlib.sha256(seed.encode()).hexdigest()[:24]
    if value.get("expires_at") and not value.get("channel"):
        fields["account_expires_at"] = G.epoch(value["expires_at"])
    status = value.get("status") or "unchecked"
    status = status if status in M.STATUS_LABELS else "unchecked"
    return dict(channel=channel, identity=identity, credentials=credentials, token=token,
                token_exp=token_exp, note=note, fields=fields, status=status)


def import_data(content, channel=None, update_existing=False, preview=False, contents=None, pool_kind=None):
    texts = [content, *(contents or [])]
    if sum(len(text.encode("utf-8")) for text in texts) > G.MAX_IMPORT_BYTES:
        raise ValueError("导入文件合计不能超过 10 MB")
    values = [value for text in texts for value in G.parse_import_text(text)]
    proxy_map = {}
    documents = []
    for text in texts:
        offset, decoder = 0, json.JSONDecoder()
        while offset < len(text):
            while offset < len(text) and text[offset].isspace():
                offset += 1
            if offset >= len(text):
                break
            try:
                document, offset = decoder.raw_decode(text, offset)
            except ValueError:
                break
            documents.extend(document if isinstance(document, list) else [document])
    for document in documents:
        for proxy in (document.get("proxies") or []) if isinstance(document, dict) else []:
            protocol, host = proxy.get("protocol"), proxy.get("host")
            if protocol not in ("http", "https") or not host or any(c in host for c in "/@?#"):
                raise ValueError("导入的代理必须使用 HTTP / HTTPS")
            auth = ""
            if proxy.get("username"):
                auth = quote(proxy["username"], safe="") + ":" + quote(proxy.get("password") or "", safe="") + "@"
            hostname = "[" + host + "]" if ":" in host else host
            url = f"{protocol}://{auth}{hostname}:{int(proxy['port'])}"
            if not 0 < int(proxy["port"]) <= 65535:
                raise ValueError("代理端口无效")
            proxy_map[proxy["proxy_key"]] = {"name": proxy["name"], "url": url, "enabled": proxy.get("status") != "inactive"}
    if not values or len(values) > 1000:
        raise ValueError("每批需要 1–1000 个账号")
    from routers.admin_pool import PoolPatchReq
    prepared, results = [], {}
    # Validate every entry before handing the GPT subset to its batch importer.
    # This performs one account-index scan per batch, not one scan per account.
    for index, value in enumerate(values, 1):
        row = None
        try:
            entry = _normalize(value, channel)
            imported_proxy = proxy_map.get(value.get("proxy_key")) if isinstance(value, dict) else None
            if isinstance(value, dict) and value.get("proxy_key") and not imported_proxy:
                raise ValueError("账号引用的代理不在导入文件中")
            if imported_proxy:
                if not M.supports_proxy(M.adapter_of(entry)):
                    raise ValueError("该渠道不支持导入的账号代理")
                entry["fields"].pop("proxy_id", None)
            old = pool_state.DB.get_by_identity(entry["channel"], entry["identity"])
            row = old or {"channel": entry["channel"], "identity": entry["identity"], "secret": {}, "meta": {}}
            if entry.get("parent_identity"):
                row = {**row, "meta": {**(row.get("meta") or {}), "parent_account_id": 1}}
            request = PoolPatchReq(**entry["fields"], note=entry["note"], **({} if entry.get("parent_identity") else {"credentials": entry["credentials"]}))
            changes = request.model_dump(exclude_unset=True)
            validated = M.validate_changes(row, changes)
            M.check_pool_kind({**row, **validated}, pool_kind)
            prepared.append({"index": index, "entry": entry, "changes": changes, "proxy": imported_proxy})
        except Exception as exc:
            results[index] = {"index": index, "action": "failed", "message": M.safe_error(exc, row or {})}

    gpt_items = [item for item in prepared if item["entry"]["channel"] == "gpt" and not item["entry"].get("parent_identity")]
    gpt_outcomes = {}
    if gpt_items:
        batch = G.import_accounts(pool_state.DB, [item["entry"]["credentials"] for item in gpt_items],
                                  update_existing=update_existing, dry_run=preview)
        gpt_outcomes = {item["index"]: outcome for item, outcome in zip(gpt_items, batch["items"])}
    seen = set()
    for item in sorted(prepared, key=lambda item: bool(item["entry"].get("parent_identity"))):
        index, entry, request_changes = item["index"], item["entry"], item["changes"]
        row = None
        try:
            key = (entry["channel"], entry["identity"])
            old = pool_state.DB.get_by_identity(*key)
            outcome = gpt_outcomes.get(index)
            if entry.get("parent_identity"):
                parent = pool_state.DB.get_by_identity("gpt", entry["parent_identity"])
                if parent is None:
                    parent_item = next((i for i in gpt_items if i["entry"]["identity"] == entry["parent_identity"]), None)
                    if not preview or not parent_item or gpt_outcomes[parent_item["index"]]["action"] == "failed":
                        raise ValueError("请同时导入或先导入 Spark 对应的主账号")
                policy = entry["fields"].get("gpt_settings") or {}
                if not policy.get("model_whitelist"):
                    raise ValueError("Spark 导入记录缺少模型白名单")
                child = pool_state.DB.get_by_identity("gpt", "spark-shadow:" + str(parent["id"])) if parent else None
                action = "skipped" if child and not update_existing else "updated" if child else "imported"
                aid = child["id"] if child else None
                if not preview and action != "skipped":
                    from core.gpt_models import create_shadow
                    child = child or create_shadow(pool_state.DB, parent, policy["model_whitelist"], entry["fields"].get("name"))
                    aid = child["id"]
                    M.patch_account(aid, request_changes)
                results[index] = {"index": index, "name": entry["fields"].get("name") or entry["identity"], "channel": "gpt", "action": action, "message": ""}
                continue
            elif outcome is not None:
                action = outcome["action"]
                if action == "failed":
                    raise ValueError(outcome["message"])
                aid = outcome.get("account_id")
            else:
                action = "skipped" if key in seen or (old and not update_existing) else "updated" if old else "imported"
                aid = old["id"] if old else None
            if action != "skipped" and not preview:
                # Read the canonical GPT row after deduplication/rotation so a
                # metadata edit cannot discard its existing pause/source state.
                row = M.account_or_404(aid) if outcome is not None else old or {
                    "channel": entry["channel"], "identity": entry["identity"], "secret": {}, "meta": {}}
                patch = dict(request_changes)
                if outcome is not None:
                    patch.pop("credentials", None)
                changes = M.validate_changes(row, patch)
                proxy = item["proxy"]
                if proxy:
                    with pool_state.DB._conn() as c:
                        c.execute("INSERT OR IGNORE INTO pool_proxies(name,url,enabled) VALUES(?,?,?)",
                                  (proxy["name"], proxy["url"], int(proxy["enabled"])))
                        changes["proxy_id"] = c.execute("SELECT id FROM pool_proxies WHERE url=?", (proxy["url"],)).fetchone()[0]
                if outcome is None:
                    secret, meta = changes.pop("secret"), changes.pop("meta", {})
                    aid = pool_state.DB.upsert_account(*key, secret=secret, meta=meta, status=entry["status"])
                    if entry["token"]:
                        changes.update(token=entry["token"], token_exp=entry["token_exp"])
                if outcome is not None:
                    if "proxy_id" in changes:
                        patch["proxy_id"] = changes["proxy_id"]
                    M.patch_account(aid, patch)
                else:
                    pool_state.DB.update_account(aid, **changes)
            seen.add(key)
            results[index] = {"index": index, "name": entry["fields"].get("name") or (outcome or {}).get("name") or entry["identity"],
                              "channel": entry["channel"], "action": action,
                              "message": "已存在" if action == "skipped" else ""}
        except Exception as exc:
            results[index] = {"index": index, "action": "failed", "message": M.safe_error(exc, row or {})}
    ordered = [results[index] for index in sorted(results)]
    if not preview and any(item["entry"]["channel"] == "gpt" for item in prepared):
        from core.gpt_models import rebuild
        rebuild(M.adapter_of({"channel": "gpt"}))
    counts = {action: sum(item["action"] == action for item in ordered) for action in ("imported", "updated", "skipped", "failed")}
    return {"total": len(values), **counts, "items": ordered, "preview": preview}


def export_data(rows):
    accounts, proxies = [], {}
    for row in rows:
        proxy_key = None
        if row.get("proxy_id"):
            proxy = pool_state.DB.get_pool_proxy(row["proxy_id"])
            if proxy:
                proxy_key = "pool:" + str(proxy["id"])
                parsed = urlsplit(proxy["url"])
                proxies[proxy_key] = {"proxy_key": proxy_key, "name": proxy["name"], "protocol": parsed.scheme,
                    "host": parsed.hostname, "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                    "username": unquote(parsed.username or ""), "password": unquote(parsed.password or ""),
                    "status": "active" if proxy["enabled"] else "inactive"}
        accounts.append({"channel": row["channel"], "identity": row["identity"], "proxy_key": proxy_key,
            "name": row.get("name") or row["identity"], "type": M.account_type(row),
            "platform": "openai" if row["channel"] == "gpt" else row["channel"],
            "credentials": row.get("secret") or {}, "token": row.get("token"),
            "token_exp": row.get("token_exp"), "status": row["status"],
            "notes": (row.get("meta") or {}).get("note") or "",
            "paused": bool((row.get("meta") or {}).get("admin_paused")),
            **{k: row.get(k) for k in M.EDITABLE if k not in ("name", "proxy_id")}})
        if row["channel"] == "gpt":
            accounts[-1]["gpt_settings"] = (row.get("meta") or {}).get("gpt_settings") or {}
            parent_id = (row.get("meta") or {}).get("parent_account_id")
            if parent_id:
                parent = pool_state.DB.get_account(parent_id)
                if parent:
                    accounts[-1]["parent_identity"] = parent["identity"]
    return {"type": "bit-api-accounts", "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(), "accounts": accounts, "proxies": list(proxies.values())}
