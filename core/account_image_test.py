"""Administrator-only, single-account image probes without user billing."""
import base64
import json
import time

import config
from core import account_management as M, account_runtime as R, codex, gpt_runtime as G, pool_state, sse
from core.gpt_native import payload_for
from core.pool import apply_refresh


def prepare(row, model, prompt):
    if row["channel"] != "gpt":
        raise ValueError("生图测试目前仅支持 GPT 账号")
    model = M.test_model(row, model)
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("请输入生图描述")
    adapter = M.adapter_of(row)
    policy = G.settings(row)
    if policy["model_whitelist"] and model not in policy["model_whitelist"]:
        raise ValueError("此模型不在账号允许的模型列表中")
    from core.gpt_web import runtime as W
    if model == W.MODEL:
        W.validate(row)
        return adapter, {"model": model, "prompt": prompt}
    body = {"model": model, "input": prompt,
            "tools": [{"type": "image_generation", "quality": "low", "output_format": "png"}],
            "tool_choice": {"type": "image_generation"}}
    # Reuse the inference policy, including disabled image tools and Spark.
    return adapter, payload_for(adapter, row, body)


def _image(item):
    encoded = item.get("result")
    if not isinstance(encoded, str) or not encoded:
        return None
    data = base64.b64decode(encoded, validate=True)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("上游未返回有效的图片文件")
    return {"b64_json": encoded, "mime_type": mime}


def run(row, model, prompt, notify, cancelled):
    adapter, payload = prepare(row, model, prompt)
    started = time.monotonic()
    result = None
    response = None
    slot = None
    try:
        notify("progress", {"message": "正在准备生图"})
        from core.gpt_web import runtime as W
        if model == W.MODEL:
            image = W.generate(pool_state.DB, adapter, row, prompt.strip(), record_usage=False,
                               notify=lambda message: notify("progress", {"message": message}), cancelled=cancelled)
            usage = image.pop("usage")
            result = {"ok": True, "images": [image], "usage": usage, "reply": "已生成 1 张图片"}
            return _finish(row, model, prompt, result, started, notify, cancelled)
        # Only setup/queueing is bounded by the short administrator test budget.
        # Generation itself uses the shared output-idle timeout, with no total cap.
        with R.test_budget():
            with R.network(pool_state.DB, row):
                if not row.get("token") or (row.get("token_exp") or 0) < time.time() + 60:
                    apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
            slot = adapter._acquire_slot(row)
        if cancelled.is_set():
            return
        output = {}
        last_progress = 0
        with R.dispatch(pool_state.DB, row, record=False):
            response = adapter.response_events(row, payload, line_limit=32 * 1024 * 1024)
            for event in response:
                if cancelled.is_set():
                    return
                kind = event.get("type", "")
                if sse.is_output(event) and time.monotonic() - last_progress >= 1:
                    notify("progress", {"message": "正在生成图片"})
                    last_progress = time.monotonic()
                if kind == "response.output_item.done":
                    output[event.get("output_index", len(output))] = event.get("item") or {}
                if kind in ("response.completed", "response.done", "response.incomplete"):
                    completed = event.get("response") or {}
                    if completed.get("error") or completed.get("status") == "failed":
                        raise codex.error_from_event(event)
                    items = completed.get("output") or list(output.values())
                    images = [_image(item) for item in items if item.get("type") == "image_generation_call"]
                    images = [image for image in images if image]
                    if not images:
                        raise ValueError("上游未返回生成图片，此模型可能不支持生图工具")
                    result = {"ok": True, "images": images, "usage": completed.get("usage"),
                              "reply": "已生成 " + str(len(images)) + " 张图片"}
                    pool_state.DB.update_account(row["id"], status="active", last_check=int(time.time()), last_error="")
                    break
            if result is None:
                raise ValueError("上游连接已结束，未收到完整的图片结果")
    except Exception as exc:
        result = {"ok": False, "error": M.safe_error(exc, row),
                  "error_code": getattr(exc, "code", None) or getattr(exc, "status", None),
                  "error_kind": getattr(exc, "error_kind", "")}
        if codex.is_capacity_error(exc):
            result.update(error_kind="capacity", error="GPT 上游繁忙，请稍后重试生图")
        # An image-tool rejection must not invalidate a working chat model/account.
    finally:
        if response is not None:
            response.close()
        adapter._release_slot(slot)
    _finish(row, model, prompt, result, started, notify, cancelled)


def _finish(row, model, prompt, result, started, notify, cancelled):
    if result is None or cancelled.is_set():
        return
    result.update(kind="image", account_id=row["id"], model=model, prompt=prompt.strip(),
                  elapsed_ms=int((time.monotonic() - started) * 1000))
    # Retain the outcome, not megabytes of pixels, in the existing test history.
    stored = {key: value for key, value in result.items() if key != "images"}
    with pool_state.DB._conn() as conn:
        conn.execute("INSERT INTO pool_test_results(account_id,created_at,model,result) VALUES(?,?,?,?)",
                     (row["id"], int(time.time()), model, json.dumps(stored)))
    notify("result", result)
