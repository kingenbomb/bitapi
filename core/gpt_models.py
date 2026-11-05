"""One account model policy for discovery, client manifests and live routing."""
import copy
import time

import config
from core import gpt_runtime as G

# Text model candidates from sub2api/internal/pkg/openai/constants.go.
# Candidates become available only after this account returns a nonempty answer.
TEST_CANDIDATES = (
    "gpt-5.6-sol", "gpt-6", "gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "gpt-5.2",
)


def checks(row, adapter):
    policy = G.settings(row)
    return {name: result for name, result in ((row.get("meta") or {}).get("pool_model_checks") or {}).items()
            if (not policy["model_whitelist"] or name in policy["model_whitelist"])
            and result.get("client_version") == config.GPT_CLIENT_VERSION
            and result.get("upstream_model") == adapter.upstream_account_model(row, name)}


def test_candidates(row, adapter):
    meta, policy = row.get("meta") or {}, G.settings(row)
    manifest = (meta.get("pool_gpt_manifest") or {}).get("models") or []
    names = [item["slug"] for item in manifest if isinstance(item, dict) and isinstance(item.get("slug"), str)
             and item.get("supported_in_api") is not False
             and (item.get("visibility") != "hide" or item["slug"].startswith("gpt-"))]
    names += list(adapter.configured_models) + list(TEST_CANDIDATES)
    names += list(policy["model_mapping"]) + list(policy["model_whitelist"]) + list(checks(row, adapter))
    if meta.get("parent_account_id"):
        names = list(policy["model_whitelist"])
    else:
        with adapter._database()._conn() as conn:
            children = conn.execute("SELECT * FROM accounts WHERE channel='gpt' AND json_extract(meta,'$.parent_account_id')=?", (row["id"],)).fetchall()
        shadow_models = {model for child in children for model in G.settings(adapter._database()._row(child))["model_whitelist"]}
        names = [name for name in names if name not in shadow_models]
    return list(dict.fromkeys(name for name in names
                             if (not policy["model_whitelist"] or name in policy["model_whitelist"])
                             and not name.startswith("gpt-image-") and name != "codex-auto-review"))


def record_test(adapter, row, result):
    now, name = int(time.time()), result["model"]
    def merge(meta):
        results = meta.setdefault("pool_model_checks", {})
        previous = results.get(name) or {}
        results[name] = {"ok": result["ok"], "checked_at": now,
                         "verified_at": now if result["ok"] else previous.get("verified_at"),
                         "upstream_model": adapter.upstream_account_model(row, name),
                         "client_version": config.GPT_CLIENT_VERSION,
                         "elapsed_ms": result["elapsed_ms"], "error": result.get("error", ""),
                         "error_kind": result.get("error_kind", ""), "error_code": result.get("error_code")}
    G.edit_meta(adapter._database(), row["id"], merge)
    rebuild(adapter)


def entries(row, defaults):
    meta, policy = row.get("meta") or {}, G.settings(row)
    raw = (meta.get("pool_gpt_manifest") or {}).get("models") or []
    visible = {item["slug"]: item for item in raw if isinstance(item, dict) and item.get("slug")
               and item.get("visibility") != "hide" and item.get("supported_in_api") is not False}
    if not visible and not meta.get("parent_account_id"):
        visible = {model: {"slug": model, "display_name": model} for model in defaults}
    if meta.get("parent_account_id"):
        visible = {model: {"slug": model, "display_name": model} for model in policy["model_whitelist"]}
    for public, upstream in policy["model_mapping"].items():
        visible[public] = {**copy.deepcopy(visible.get(upstream) or {}), "slug": public, "display_name": public}
    for name, result in (meta.get("pool_model_checks") or {}).items():
        if result.get("client_version") != config.GPT_CLIENT_VERSION:
            continue
        if result.get("upstream_model") != policy["model_mapping"].get(name, name):
            continue
        # A failed probe describes the latest request, not model support. Keep
        # advertised and previously verified models through overloads/timeouts.
        error = (result.get("error") or "").lower()
        unsupported = any(marker in error for marker in (
            "model_not_found", "model_not_supported", "unsupported_model",
            "model is not supported", "unknown model"))
        if result.get("ok") or (result.get("verified_at") and not unsupported):
            visible.setdefault(name, {"slug": name, "display_name": name, "supported_in_api": True, "visibility": "list"})
        elif unsupported:
            visible.pop(name, None)
    if policy["model_whitelist"]:
        visible = {name: item for name, item in visible.items() if name in policy["model_whitelist"]}
    from core.gpt_web.runtime import image_models
    for item in image_models(row):
        visible[item["id"]] = {"slug": item["id"], "display_name": item["name"], "supported_in_api": True,
                               "visibility": "list", "type": "image"}
    return list(visible.values())


def public_models(adapter, group_id=None):
    models = {}
    for row in adapter._database().list_accounts("gpt", limit=100000):
        if (row.get("meta") or {}).get("parent_account_id"):
            try:
                parent = G.credential_account(adapter._database(), row)
            except ValueError:
                continue
            row["group_ids"] = parent.get("group_ids") or []
        if group_id is not None and row.get("group_ids") and group_id not in row["group_ids"]:
            continue
        for item in entries(row, adapter.configured_models):
            models.setdefault(item["slug"], item)
    return list(models.values())


def rebuild(adapter):
    discovered = public_models(adapter)
    adapter.models = [item["slug"] for item in discovered] or list(adapter.configured_models)


def cache_manifest(adapter, row, data, version):
    snapshot = {"models": data.get("models") or [], "fetched_at": int(time.time()), "client_version": version}
    if row.get("id"):
        G.edit_meta(adapter._database(), row["id"], lambda meta: meta.update(pool_gpt_manifest=snapshot))
        rebuild(adapter)
    return snapshot


def create_shadow(db, parent, models, name=None):
    if parent["channel"] != "gpt" or (parent.get("meta") or {}).get("parent_account_id"):
        raise ValueError("Spark 影子必须关联 GPT 主账号")
    if not models:
        raise ValueError("请选择或填写此账号支持的 Spark 模型 ID")
    now = int(time.time())
    meta = {"parent_account_id": parent["id"], "quota_dimension": "spark", "gpt_settings": {"model_whitelist": models}}
    import json
    with db._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM accounts WHERE channel='gpt' AND json_extract(meta,'$.parent_account_id')=?", (parent["id"],)).fetchone():
            raise ValueError("此主账号已经有 Spark 影子账号")
        aid = conn.execute("INSERT INTO accounts(channel,identity,name,account_type,secret,meta,status,priority,concurrency,group_ids,proxy_id,created_at,updated_at) "
            "VALUES('gpt',?,?, 'oauth','{}',?,'active',?,?,?,?,?,?)", (
                "spark-shadow:" + str(parent["id"]), (name or (parent.get("name") or parent["identity"]) + " (Spark)")[:100],
                json.dumps(meta), parent.get("priority", 50), parent.get("concurrency", 0), json.dumps(parent.get("group_ids") or []), parent.get("proxy_id"), now, now)).lastrowid
    return db.get_account(aid)
