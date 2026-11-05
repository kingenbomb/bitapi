"""Web image capability on the existing GPT accounts, policy and concurrency."""
import base64
import time

from core import account_management as M, account_runtime as R, gpt_runtime as G
from core.pool import apply_refresh
from . import MODEL
from .client import WebImageClient, mime_type
from .metering import estimate_image_usage


def supported(row):
    secret, meta = row.get("secret") or {}, row.get("meta") or {}
    token = row.get("token") or secret.get("access_token") or ""
    return bool(row.get("channel") == "gpt" and token and not token.startswith("at-")
                and secret.get("auth_mode") != "agentIdentity" and not meta.get("parent_account_id"))


def validate(row):
    if not supported(row):
        raise ValueError("Web 生图需要 GPT 主账号的 ChatGPT access_token，不支持 PAT、Agent Identity 或 Spark 影子账号")
    policy = G.settings(row)
    if policy["image_tool"] == "disabled":
        raise ValueError("此账号已禁用图片生成工具")
    if policy["model_whitelist"] and MODEL not in policy["model_whitelist"]:
        raise ValueError("此模型不在账号允许的模型列表中")


def image_models(row):
    try:
        validate(row)
    except ValueError:
        return []
    state = (row.get("meta") or {}).get("gpt_web_image") or {}
    return [{"id": MODEL, "name": "ChatGPT Web 生图", "backend": "web",
             "test_status": "verified" if state.get("ok") else "failed" if state else "untested",
             "error": state.get("error", ""), "checked_at": state.get("checked_at")}]


def record(db, row, *, error=None):
    state = {"checked_at": int(time.time()), "ok": error is None,
             "error": M.safe_error(error, row) if error else "",
             "error_kind": getattr(error, "error_kind", ""),
             "error_code": getattr(error, "code", None)}
    G.edit_meta(db, row["id"], lambda meta: meta.update(gpt_web_image=state))


def generate(db, adapter, row, prompt, *, size=None, notify=None, cancelled=None, record_usage=True):
    validate(row)
    # Share credential renewal and the family slot with Codex, without changing
    # Codex's health/quota based on the outcome of a Web image request.
    slot = None
    try:
        with R.test_budget():
            with R.network(db, row):
                apply_refresh(db, row, adapter.refresh_token(row))
            slot = adapter._acquire_slot(row)
        proxy_url = None
        if row.get("proxy_id"):
            proxy = db.get_pool_proxy(row["proxy_id"])
            if not proxy or not proxy["enabled"]:
                raise ValueError("账号代理不可用")
            proxy_url = proxy["url"]
        with R.dispatch(db, row, record=record_usage, clear_error=False):
            data = WebImageClient(row, proxy=proxy_url, notify=notify, cancelled=cancelled).generate(prompt, size)
        result = {"b64_json": base64.b64encode(data).decode(), "mime_type": mime_type(data),
                  "usage": estimate_image_usage(data, prompt)}
        record(db, row)
        return result
    except Exception as exc:
        if not isinstance(exc, InterruptedError):
            record(db, row, error=exc)
        raise
    finally:
        adapter._release_slot(slot)


def images(gateway, body, request, authorization):
    from fastapi import HTTPException
    model = MODEL
    for name in ("quality", "background", "output_format", "output_compression", "stream"):
        if name in body and body[name] is not None:
            raise HTTPException(400, "Web 生图暂不支持参数：" + name)
    if body.get("size") not in (None, "auto", "1024x1024", "1024x1536", "1536x1024"):
        raise HTTPException(400, "Web 生图 size 支持 auto、1024x1024、1024x1536、1536x1024；尺寸作为提示发送，上游不保证精确像素")
    user, group = gateway._authorize_gateway(authorization, model, request=request)
    R.bind_group(group)
    R.bind_request({"model": model})
    adapter = gateway.get_adapter("gpt")
    if not adapter or "gpt" not in gateway.model_routes(model):
        raise HTTPException(404, "Web 生图模型未启用")
    row = gateway.POOL.get_valid_account("gpt", adapter)
    if not row:
        raise HTTPException(503, "GPT 账号池暂无符合 Web 生图、分组与额度条件的账号")
    started, rid = time.time(), gateway._request_id()
    reason = "error"
    estimated = None
    try:
        result = generate(gateway.DB, adapter, row, body["prompt"], size=body.get("size"))
        usage = result["usage"]
        estimated = {**usage, "prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"]}
        reason = "done"
        return {"created": int(time.time()), "data": [{"b64_json": result["b64_json"]}], "usage": usage}
    except Exception as exc:
        raise HTTPException(getattr(exc, "code", None) or getattr(exc, "status", None) or 502,
                            M.safe_error(exc, row)) from exc
    finally:
        if user is not None:
            gateway.BILLING.record_usage(user, group, "gpt", model, adapter=adapter, units=1,
                request_id=rid, end_reason=reason, duration_ms=int((time.time() - started) * 1000),
                upstream_account_id=row["id"], client_ip=gateway._client_ip(request), estimated_usage=estimated)
