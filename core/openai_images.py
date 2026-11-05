"""Image API routing for mixed OpenAI-compatible key-pool channels."""
import time

from fastapi import HTTPException

from adapters.openai_compat import UpstreamError
from core import account_runtime as R


def supports(adapter, model):
    return bool(adapter and callable(getattr(adapter, "supports_images", None)) and adapter.supports_images(model))


def generate(gateway, body, served):
    if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
        raise HTTPException(400, "prompt required")
    if body.get("stream"):
        raise HTTPException(400, "此图片接口使用非流式响应，请关闭 stream")
    _, model, payload, routes = gateway._resolve_routes(body)
    payload["n"] = gateway._gen_count(payload)
    for channel, adapter in routes:
        if not supports(adapter, model):
            continue
        row = gateway.POOL.get_valid_account(channel, adapter)
        if row is None:
            continue
        started = time.monotonic()
        try:
            # One submitted image request: never retry a timeout/unknown result.
            result = R.call(gateway.DB, adapter, "images", row, payload)
        except Exception as exc:
            from core.account_management import safe_error
            if not isinstance(exc, UpstreamError) or exc.code not in (400, 404, 422):
                gateway.POOL.mark_failure(row["id"], exc, adapter=adapter)
            raise HTTPException(getattr(exc, "code", 502), safe_error(exc, row)) from exc
        raw = result.get("usage") or {}
        usage = {**raw, "prompt_tokens": raw.get("input_tokens", raw.get("prompt_tokens", 0)),
                 "completion_tokens": raw.get("output_tokens", raw.get("completion_tokens", 0))}
        if raw.get("input_tokens_details"):
            usage["prompt_tokens_details"] = raw["input_tokens_details"]
        usage.setdefault("total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
        served.update(channel=channel, adapter=adapter, usage=usage, units=len(result["data"]),
                      duration_ms=int((time.monotonic() - started) * 1000))
        return result
    raise HTTPException(503, "此生图模型暂无可用的渠道账号")
