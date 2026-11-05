"""Native Codex Responses, compact and owned, expiring conversation contexts.

HTTP and WebSocket turns use the same account, policy and billing path. Only
completed turns are retained, for one hour, to replay store=false conversations.
"""
import copy
import hashlib
import json
import queue
import threading
import time

from fastapi import HTTPException
from core import account_runtime as R, codex, gpt_runtime as G, sse
import config
from adapters.openai_compat import UpstreamError


def owner():
    client = R.request_context().get("client") or {}
    return hashlib.sha256((client.get("owner", "") + ":" + str(R.group_id())).encode()).hexdigest()


def initialize(db):
    with db._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS gpt_responses (id TEXT PRIMARY KEY, owner TEXT NOT NULL, account_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, context TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gpt_response_expiry ON gpt_responses(expires_at)")


def items(value):
    return [{"role": "user", "content": value}] if isinstance(value, str) else copy.deepcopy(value or [])


def expand(db, body):
    payload = copy.deepcopy(body)
    previous = payload.pop("previous_response_id", None)
    if previous:
        initialize(db)
        with db._conn() as conn:
            row = conn.execute("SELECT * FROM gpt_responses WHERE id=? AND owner=? AND expires_at>?", (previous, owner(), int(time.time()))).fetchone()
        if not row:
            raise HTTPException(400, "previous_response_id 不存在、已过期或不属于当前密钥与分组")
        R.request_context()["preferred_account_id"] = row["account_id"]
        payload["input"] = json.loads(row["context"]) + items(payload.get("input"))
    return payload


def remember(db, row, payload, response):
    if not response.get("id"):
        return
    context = json.dumps(items(payload.get("input")) + (response.get("output") or []), ensure_ascii=False)
    # Large image results are carried to the caller; retain their item reference,
    # rather than persisting megabytes of generated pixels per conversation.
    if len(context.encode()) > 2 * 1024 * 1024:
        return
    initialize(db)
    now = int(time.time())
    with db._conn() as conn:
        conn.execute("DELETE FROM gpt_responses WHERE expires_at<=?", (now,))
        conn.execute("INSERT OR REPLACE INTO gpt_responses VALUES(?,?,?,?,?)", (response["id"], owner(), row["id"], now + 3600, context))


def payload_for(adapter, row, body, compact=False):
    from core.gpt_web.runtime import MODEL as WEB_IMAGE_MODEL
    if body.get("model") == WEB_IMAGE_MODEL:
        raise HTTPException(400, "Web 生图模型请使用 /v1/images/generations")
    policy = G.settings(row)
    if compact and policy["compact_mode"] == "disabled":
        raise HTTPException(400, "此账号已禁用上下文压缩")
    payload = copy.deepcopy(body)
    if not isinstance(payload.get("input"), (str, list)):
        raise HTTPException(400, "input 必须是文本或消息数组")
    payload["input"] = items(payload["input"])
    if any(not isinstance(item, dict) for item in payload["input"]):
        raise HTTPException(400, "input 数组项必须是对象")
    payload["model"] = adapter.upstream_account_model(row, body["model"], compact=compact)
    payload["store"] = False
    if not compact:
        payload["stream"] = True
        if not isinstance(payload.get("instructions"), str):
            payload["instructions"] = "You are a helpful assistant."
    if not policy["passthrough"]:
        allowed = {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "text", "include", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention", "stream", "store", "truncation"}
        payload = {k: v for k, v in payload.items() if k in allowed}
    for key in ("temperature", "top_p", "max_output_tokens", "max_tokens", "background"):
        payload.pop(key, None)
    tools = payload.get("tools") or []
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise HTTPException(400, "tools 必须是工具对象数组")
    for item in payload["input"]:
        if item.get("type") == "additional_tools":
            tools.extend(item.get("tools") or [])
    payload["input"] = [item for item in payload["input"] if item.get("type") != "additional_tools"]
    if any(item.get("type") == "web_search_call" for item in payload["input"]) and not any(tool.get("type") in ("web_search", "web_search_preview") for tool in tools):
        tools.append({"type": "web_search"})
    if policy["flatten_namespaces"]:
        flattened, names = [], set()
        for tool in tools:
            flatten = tool.get("type") == "namespace" and tool.get("name") != "image_gen"
            children = tool.get("tools") if flatten else [tool]
            for child in children or []:
                child = copy.deepcopy(child)
                if flatten:
                    child["name"] = tool["name"] + "__" + child["name"]
                if child.get("name") in names:
                    raise HTTPException(400, "命名空间展开后的工具名称冲突")
                names.add(child.get("name"))
                flattened.append(child)
        tools = flattened
        for item in payload["input"]:
            namespace = item.get("namespace")
            if namespace and namespace != "image_gen" and item.get("type") in ("function_call", "custom_tool_call", "tool_call", "mcp_tool_call"):
                item["name"] = namespace + "__" + item["name"]
                item.pop("namespace", None)
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and choice.get("namespace") and choice["namespace"] != "image_gen":
            choice["name"] = choice.pop("namespace") + "__" + choice["name"]
    spark = (row.get("meta") or {}).get("quota_dimension") == "spark" or "spark" in payload["model"].lower()
    if spark and any(t.get("type") == "image_generation" for t in tools):
        raise HTTPException(400, "Spark 模型不支持原生图片生成工具，请切换主账号模型")
    if policy["image_tool"] == "disabled":
        tools = [t for t in tools if t.get("type") != "image_generation"]
        if isinstance(payload.get("tool_choice"), dict) and payload["tool_choice"].get("type") == "image_generation":
            raise HTTPException(400, "此账号已禁用图片生成工具")
    elif policy["image_tool"] == "enabled" and not spark and not compact and not any(t.get("type") == "image_generation" for t in tools):
        tools.append({"type": "image_generation"})
    if tools or "tools" in payload:
        payload["tools"] = tools
    return payload


def restore_namespaces(event, body):
    names = {}
    tools = list(body.get("tools") or [])
    for item in items(body.get("input")):
        if item.get("type") == "additional_tools":
            tools.extend(item.get("tools") or [])
    for tool in tools:
        if tool.get("type") == "namespace" and tool.get("name") != "image_gen":
            for child in tool.get("tools") or []:
                if child.get("name"):
                    names[tool["name"] + "__" + child["name"]] = (tool["name"], child["name"])
    def restore(value):
        if isinstance(value, dict):
            if value.get("type") in ("function_call", "custom_tool_call", "tool_call", "mcp_tool_call") and value.get("name") in names:
                value["namespace"], value["name"] = names[value["name"]]
            for child in value.values():
                if isinstance(child, (dict, list)):
                    restore(child)
        elif isinstance(value, list):
            for child in value:
                restore(child)
    restore(event)
    return event


def open_ws(adapter, row, payload):
    import urllib.parse
    import websocket
    headers = adapter._codex_headers(row, payload)
    headers["OpenAI-Beta"] = "responses_websockets=2026-02-06"
    credential = G.credential_account(adapter._database(), row)
    timeout = config.GPT_RESPONSE_HEADER_TIMEOUT or config.GPT_STREAM_IDLE_TIMEOUT
    budget = R.bounded_timeout(timeout)
    options = {"timeout": budget, "header": [k + ": " + v for k, v in headers.items()], "suppress_origin": True}
    if credential.get("proxy_id"):
        proxy = adapter._database().get_pool_proxy(credential["proxy_id"])
        if not proxy or not proxy["enabled"]:
            raise ValueError("账号代理不可用")
        url = urllib.parse.urlsplit(proxy["url"])
        if url.scheme != "http":
            raise HTTPException(400, "WebSocket 上游连接当前需要 HTTP CONNECT 代理")
        options.update(http_proxy_host=url.hostname, http_proxy_port=url.port or 80, proxy_type="http")
        if url.username:
            options["http_proxy_auth"] = (urllib.parse.unquote(url.username), urllib.parse.unquote(url.password or ""))
    try:
        connection = websocket.create_connection(adapter._url().replace("https://", "wss://", 1).replace("http://", "ws://", 1), **options)
    except websocket.WebSocketTimeoutException:
        R.bounded_timeout(expired=budget < timeout)
        raise sse.StreamIdleTimeout("upstream stream idle timeout: no response headers") from None
    G.capture_headers(adapter._database(), row, connection.getheaders())
    return connection


def _events(adapter, db, row, body, holder, *, compact=False, socket_state=None):
    payload = payload_for(adapter, row, body, compact)
    started = time.monotonic()
    output = {}
    with R.dispatch(db, row):
        if compact:
            payload.pop("stream", None)
            response = adapter._request(adapter._url().rstrip("/") + "/compact", json.dumps(payload).encode(), {**adapter._codex_headers(row, payload), "Accept": "application/json"})
            try:
                result = json.loads(response.read())
                G.capture_headers(db, row, getattr(response, "headers", {}))
            finally:
                response.close()
            holder.update(terminal=True, response=result, usage=codex.usage_to_chat(result.get("usage")))
            yield {"type": "response.completed", "response": result}
            return
        response = None
        connection = None
        if socket_state is not None:
            # Both modes use native WS v2. ctx_pool reconnects between turns;
            # passthrough keeps the same upstream connection for this client.
            def read_events():
                nonlocal connection
                import websocket
                if socket_state.get("account_id") != row["id"] and socket_state.get("connection"):
                    socket_state.pop("connection").close()
                connection = socket_state.get("connection") or open_ws(adapter, row, payload)
                socket_state.update(connection=connection, account_id=row["id"])
                connection.settimeout(R.bounded_timeout(config.GPT_STREAM_IDLE_TIMEOUT))
                connection.send(json.dumps({**payload, "type": "response.create"}, ensure_ascii=False))
                idle = config.GPT_STREAM_IDLE_TIMEOUT
                deadline = time.monotonic() + idle
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise sse.StreamIdleTimeout("upstream stream idle timeout: no model output")
                    budget = R.bounded_timeout(remaining)
                    connection.settimeout(budget)
                    try:
                        opcode, raw = connection.recv_data(control_frame=True)
                    except websocket.WebSocketTimeoutException:
                        R.bounded_timeout(expired=budget < remaining)
                        raise sse.StreamIdleTimeout("upstream stream idle timeout: no model output") from None
                    R.bounded_timeout()
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        break
                    if opcode not in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                        continue
                    if not raw:
                        break
                    if time.monotonic() >= deadline:
                        raise sse.StreamIdleTimeout("upstream stream idle timeout: no model output")
                    event = json.loads(raw)
                    if event.get("type") in ("error", "response.failed"):
                        connection.close()
                        socket_state.pop("connection", None)
                        raise codex.error_from_event(event)
                    if sse.is_output(event):
                        deadline = time.monotonic() + idle
                    yield event
                    if event.get("type") in ("response.completed", "response.done", "response.incomplete"):
                        return
                raise UpstreamError(502, "GPT WebSocket 未返回完成事件")
            from core.gpt_request_policy import retry_events
            upstream = retry_events(read_events, payload, line_limit=32 * 1024 * 1024, db=db, account_id=row["id"])
        else:
            response = adapter.response_events(row, payload, line_limit=32 * 1024 * 1024)
            upstream = response
        try:
            for event in upstream:
                if G.settings(row)["flatten_namespaces"]:
                    event = restore_namespaces(event, body)
                kind = event.get("type", "")
                if "frt_ms" not in holder and (kind.endswith(".delta") or kind == "response.output_item.added"):
                    holder["frt_ms"] = int((time.monotonic() - started) * 1000)
                if kind == "response.output_item.done":
                    output[event.get("output_index", len(output))] = event.get("item") or {}
                if kind in ("response.completed", "response.done", "response.incomplete"):
                    result = event.get("response") or {}
                    if result.get("error") or result.get("status") == "failed":
                        raise codex.error_from_event(event)
                    if not result.get("output") and output:
                        result["output"] = [output[k] for k in sorted(output)]
                    if holder.get("expect_image") and not any(item.get("type") == "image_generation_call" and item.get("result") for item in result.get("output") or []):
                        raise UpstreamError(502, "GPT 上游未返回生成图片")
                    result["model"] = body["model"]
                    holder.update(terminal=True, response=result, usage=codex.usage_to_chat(result.get("usage")), text="".join(p.get("text", "") for item in result.get("output") or [] for p in item.get("content") or [] if isinstance(p, dict)))
                    remember(db, row, body, result)
                yield event
                if holder.get("terminal"):
                    return
            raise UpstreamError(502, "GPT stream ended before response.completed")
        finally:
            upstream.close()
            if response:
                response.close()
            if connection and (not holder.get("terminal") or G.settings(row)["ws_mode"] == "ctx_pool"):
                connection.close()
                socket_state.pop("connection", None)


def events(adapter, db, row, body, holder, *, compact=False, socket_state=None):
    try:
        yield from _events(adapter, db, row, body, holder, compact=compact, socket_state=socket_state)
    except Exception as error:
        from core.gpt_request_policy import mark_failure
        mark_failure(db, row["id"], error, body.get("model"))
        raise


def routed_events(gateway, adapter, row, body, holder, *, compact=False, socket_state=None):
    from core.gpt_request_policy import can_switch
    excluded, first_timeouts = [], 0
    attempts = config.GPT_MAX_ACCOUNT_SWITCHES + 1
    for _ in range(attempts):
        excluded.append(row["id"])
        holder["account_id"] = row["id"]
        committed = False
        try:
            for event in events(adapter, gateway.DB, row, body, holder, compact=compact, socket_state=socket_state):
                committed = True
                yield event
            return
        except Exception as error:
            if isinstance(error, sse.FirstOutputTimeout):
                first_timeouts += 1
            if committed or len(excluded) >= attempts or not can_switch(error, len(excluded), first_timeouts):
                raise
            next_row = gateway.POOL.get_valid_account("gpt", adapter, exclude=excluded)
            if not next_row:
                raise
            row = next_row


def select(gateway, body, request, authorization):
    model = gateway._canonical_model(body.get("model", ""))
    user, group = gateway._authorize_gateway(authorization, model, request=request)
    R.bind_group(group)
    adapter = gateway.get_adapter("gpt")
    if not adapter or "gpt" not in gateway.model_routes(model):
        raise HTTPException(404, "此模型不属于已启用的 GPT 账号池")
    R.bind_request({**body, "model": model})
    R.request_context().pop("preferred_account_id", None)
    expanded = expand(gateway.DB, {**body, "model": model})
    if not expanded.get("input"):
        raise HTTPException(400, "input required")
    row = gateway.POOL.get_valid_account("gpt", adapter)
    if not row:
        raise HTTPException(503, "GPT 账号池暂无符合模型、分组与额度条件的账号")
    return user, group, adapter, row, expanded


def settlement(gateway, user, group, adapter, body, holder, started, rid, request):
    def settle(reason):
        if user is not None:
            gateway.BILLING.record_usage(user, group, "gpt", body["model"], adapter=adapter,
                upstream_usage=holder.get("usage"), request_messages=items(body.get("input")), output_text=holder.get("text"),
                stream=bool(body.get("stream")), duration_ms=int((time.time() - started) * 1000),
                request_id=rid, end_reason=reason, first_token_ms=holder.get("frt_ms"), client_ip=gateway._client_ip(request))
    return settle


def respond(gateway, body, request, authorization, compact=False, expect_image=False):
    user, group, adapter, row, expanded = select(gateway, body, request, authorization)
    # Validate before starting SSE so policy errors retain their HTTP status.
    payload_for(adapter, row, expanded, compact)
    holder, rid, started = {"expect_image": expect_image}, gateway._request_id(), time.time()
    settle = settlement(gateway, user, group, adapter, body, holder, started, rid, request)
    def stream():
        pending = queue.Queue(maxsize=64)
        def produce():
            try:
                for event in routed_events(gateway, adapter, row, expanded, holder, compact=compact):
                    pending.put("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n")
            except Exception as exc:
                holder["failed_reason"] = "idle_timeout" if isinstance(exc, sse.StreamIdleTimeout) else "scanner_error"
                from core.account_management import safe_error
                current = gateway.DB.get_account(holder.get("account_id", row["id"])) or row
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None) or 502
                pending.put("event: error\ndata: " + json.dumps({"type": "error", "error": {
                    "message": safe_error(exc, current), "status_code": status}}) + "\n\n")
            finally:
                pending.put(None)
        threading.Thread(target=R.worker_target(produce), daemon=True, name="gpt-response").start()
        while True:
            event = pending.get()
            if event is None:
                return
            yield event
    if body.get("stream") and not compact:
        return gateway._BilledStreamingResponse(gateway._billed_stream(stream(), holder, settle), media_type="text/event-stream", headers={"X-Request-Id": rid})
    try:
        for _ in routed_events(gateway, adapter, row, expanded, holder, compact=compact):
            pass
    except Exception as exc:
        settle("idle_timeout" if isinstance(exc, sse.StreamIdleTimeout) else "scanner_error")
        if isinstance(exc, HTTPException):
            raise
        from core.account_management import safe_error
        from core.gpt_request_policy import status_code
        current = gateway.DB.get_account(holder.get("account_id", row["id"])) or row
        raise HTTPException(status_code(exc) or 502, safe_error(exc, current)) from exc
    settle("done")
    return holder["response"]


def images(gateway, body, request, authorization):
    if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
        raise HTTPException(400, "prompt required")
    if body.get("n", 1) != 1:
        raise HTTPException(400, "GPT 原生图片工具每次请求生成 1 张图片")
    if body.get("response_format", "b64_json") != "b64_json":
        raise HTTPException(400, "GPT 原生图片返回 b64_json")
    from core.gpt_web.runtime import MODEL as WEB_IMAGE_MODEL, images as web_images
    if gateway._canonical_model(body.get("model", "")) == WEB_IMAGE_MODEL:
        return web_images(gateway, body, request, authorization)
    tool = {"type": "image_generation", **{k: body[k] for k in ("size", "quality", "background", "output_format", "output_compression") if k in body}}
    result = respond(gateway, {"model": body.get("model"), "input": body["prompt"], "tools": [tool], "tool_choice": {"type": "image_generation"}}, request, authorization, expect_image=True)
    data = [{"b64_json": item["result"], **({"revised_prompt": item["revised_prompt"]} if item.get("revised_prompt") else {})} for item in result.get("output") or [] if item.get("type") == "image_generation_call" and item.get("result")]
    if not data:
        raise HTTPException(502, "GPT 上游未返回生成图片")
    return {"created": int(time.time()), "data": data, "usage": result.get("usage")}


def manifest(gateway, request, authorization, x_api_key=None):
    from fastapi.responses import JSONResponse, Response
    token = (authorization or x_api_key or "").removeprefix("Bearer ").strip()
    group, key = None, None
    if not gateway.secret_equal(token, gateway.config.API_KEY):
        resolved = gateway.BILLING.resolve_user_by_key(token)
        if not resolved:
            raise HTTPException(401, "Unauthorized")
        user, group = resolved
        key = user.get("_api_key")
    adapter = gateway.enabled_adapters().get("gpt")
    models = adapter.public_models((group or {}).get("id")) if adapter else []
    if group is not None:
        models = [m for m in models if gateway.BILLING.check_model_allowed(group, m["slug"], key)]
    data = {"models": models}
    etag = '"' + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=60", "Vary": "Authorization, x-api-key"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(data, headers=headers)


async def websocket_session(gateway, socket):
    import asyncio
    from starlette.websockets import WebSocketDisconnect
    token = (socket.headers.get("authorization") or socket.headers.get("x-api-key") or "").removeprefix("Bearer ").strip()
    if not gateway.secret_equal(token, gateway.config.API_KEY) and not gateway.BILLING.resolve_user_by_key(token):
        await socket.close(code=1008)
        return
    await socket.accept()
    state = {}
    R.request_context().setdefault("client", {})["transport"] = "ws"
    try:
        while True:
            try:
                body = await asyncio.wait_for(socket.receive_json(), timeout=120)
            except (WebSocketDisconnect, asyncio.TimeoutError):
                return
            if not isinstance(body, dict) or body.get("type") != "response.create":
                await socket.send_json({"type": "error", "error": {"code": "invalid_request", "message": "expected response.create"}})
                continue
            body = {k: v for k, v in body.items() if k != "type"}
            body["stream"] = True
            loop, queue = asyncio.get_running_loop(), asyncio.Queue(maxsize=64)
            def put(event):
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
            def run():
                holder, settle, row = {}, None, None
                try:
                    user, group, adapter, row, expanded = select(gateway, body, socket, socket.headers.get("authorization") or socket.headers.get("x-api-key"))
                    settle = settlement(gateway, user, group, adapter, body, holder, time.time(), gateway._request_id(), socket)
                    for event in routed_events(gateway, adapter, row, expanded, holder, socket_state=state):
                        put(event)
                except Exception as exc:
                    holder["failed_reason"] = "idle_timeout" if isinstance(exc, sse.StreamIdleTimeout) else "scanner_error"
                    from core.account_management import safe_error
                    from core.gpt_request_policy import status_code
                    current = gateway.DB.get_account(holder.get("account_id", (row or {}).get("id"))) if row else None
                    put({"type": "error", "error": {"code": str(status_code(exc) or 502), "message": str(exc.detail) if isinstance(exc, HTTPException) else safe_error(exc, current or row or {})}})
                finally:
                    try:
                        if settle:
                            settle("done" if holder.get("terminal") and not holder.get("failed_reason") else holder.get("failed_reason") or "scanner_error")
                    finally:
                        release = socket.scope.get("state", {}).pop("billing_release", None)
                        if release:
                            release()
                        put(None)
            task = asyncio.create_task(asyncio.to_thread(run))
            gone = False
            while True:
                event = await queue.get()
                if event is None:
                    break
                if not gone:
                    try:
                        await socket.send_json(event)
                    except (WebSocketDisconnect, RuntimeError):
                        gone = True
            await task
            if gone:
                return
    finally:
        if state.get("connection"):
            from anyio import CancelScope
            with CancelScope(shield=True):
                await asyncio.to_thread(state["connection"].close)
