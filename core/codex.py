"""Chat Completions ↔ Codex Responses 协议转换（sub2api 的原生 OAuth 路径）。

上游始终 store=false、stream=true。非流式收集 response.completed；流式逐事件转换，
保留 function call 增量、reasoning 与 usage。只有上游终态才能产生完成帧。
"""
import json
import time
import uuid
import copy

from adapters.openai_compat import UpstreamError
from core import sse


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _content_parts(content, text_type):
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            raise UpstreamError(400, "Unsupported message content")
        if part.get("type") in ("text", "input_text", "output_text"):
            result = {"type": text_type, "text": part.get("text", "")}
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            result = {"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image}
            if isinstance(image, dict) and image.get("detail"):
                result["detail"] = image["detail"]
        else:
            raise UpstreamError(400, "Unsupported message content: " + str(part.get("type")))
        if part.get("prompt_cache_breakpoint") is not None:
            result["prompt_cache_breakpoint"] = copy.deepcopy(part["prompt_cache_breakpoint"])
        parts.append(result)
    return parts


def chat_to_responses(body, model):
    inputs, instructions = [], []
    messages = body.get("messages") or []
    marked_instructions = any(
        message.get("role") in ("system", "developer") and
        (message.get("prompt_cache_breakpoint") is not None or any(
            isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])))
        for message in messages)
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")
        if role in ("system", "developer") and not marked_instructions:
            instructions.append(_text(content))
            continue
        if role == "system":
            role = "developer"
        if role in ("tool", "function"):
            marked_output = isinstance(content, list) and any(
                isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None for part in content)
            output = (_content_parts(content, "input_text") if marked_output
                      else content if isinstance(content, str) else json.dumps(content))
            item = {"type": "function_call_output", "call_id": message.get("tool_call_id", ""), "output": output}
            if message.get("prompt_cache_breakpoint") is not None:
                item["prompt_cache_breakpoint"] = copy.deepcopy(message["prompt_cache_breakpoint"])
            inputs.append(item)
            continue
        text_type = "output_text" if role == "assistant" else "input_text"
        parts = _content_parts(content, text_type)
        if parts:
            item = {"type": "message", "role": role, "content": parts}
            if message.get("prompt_cache_breakpoint") is not None:
                item["prompt_cache_breakpoint"] = copy.deepcopy(message["prompt_cache_breakpoint"])
            inputs.append(item)
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            inputs.append({"type": "function_call", "call_id": call.get("id") or "call_" + uuid.uuid4().hex,
                           "name": fn.get("name", ""), "arguments": fn.get("arguments", "")})
    payload = {"model": model, "input": inputs, "stream": True, "store": False,
               "instructions": "\n\n".join(t for t in instructions if t.strip()) or "You are a helpful assistant."}
    for key in ("parallel_tool_calls", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention"):
        if body.get(key) is not None:
            payload[key] = copy.deepcopy(body[key])
    reasoning = body.get("reasoning") or {}
    if body.get("reasoning_effort"):
        reasoning = {**reasoning, "effort": body["reasoning_effort"]}
    if reasoning:
        payload["reasoning"] = reasoning
        payload["include"] = ["reasoning.encrypted_content"]
    tools = body.get("tools") or [{"type": "function", "function": f} for f in body.get("functions") or []]
    if tools:
        payload["tools"] = []
        for tool in tools:
            if tool.get("type") != "function":
                raise UpstreamError(400, "Only function tools are supported in chat/completions")
            fn = tool.get("function") or tool
            payload["tools"].append({"type": "function", **{k: fn[k] for k in ("name", "description", "parameters", "strict") if k in fn}})
    choice = body.get("tool_choice") or body.get("function_call")
    if choice is not None:
        if isinstance(choice, dict) and choice.get("function"):
            choice = {"type": "function", "name": choice["function"]["name"]}
        elif isinstance(choice, dict) and choice.get("name"):
            choice = {"type": "function", "name": choice["name"]}
        payload["tool_choice"] = choice
    fmt = body.get("response_format")
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_schema":
            payload["text"] = {"format": {"type": "json_schema", **(fmt.get("json_schema") or {})}}
        elif fmt.get("type") == "json_object":
            payload["text"] = {"format": {"type": "json_object"}}
    # temperature/top_p/max_output_tokens 等在 Codex OAuth 路径不支持，不上送。
    return payload


def usage_to_chat(usage):
    usage = usage or {}
    inp, out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    from core.metering import cache_tokens, cache_write_ttls
    details = dict(usage.get("input_tokens_details") or {})
    read, written = cache_tokens(usage)
    for key, value in (("cached_tokens", read), ("cache_write_tokens", written),
                       ("cache_write_tokens_details", cache_write_ttls(usage))):
        if value is not None:
            details[key] = value
    return {"prompt_tokens": inp, "completion_tokens": out,
            "total_tokens": int(usage.get("total_tokens") or inp + out),
            "prompt_tokens_details": details,
            "completion_tokens_details": dict(usage.get("output_tokens_details") or {})}


def error_from_event(event):
    response = event.get("response") or {}
    error = response.get("error") or event.get("error") or event
    if not isinstance(error, dict):
        error = {"message": str(error)}
    text = str(error.get("message") or error.get("code") or "Codex upstream failed")
    label = str(error.get("code") or error.get("type") or "").lower()
    code = error.get("status_code") or error.get("status")
    valid_status = isinstance(code, int) and 400 <= code <= 599
    if (not valid_status or code >= 500) and (label in ("server_is_overloaded", "slow_down") or "servers are currently overloaded" in text.lower()):
        code = 503
    elif not valid_status:
        code = (429 if any(k in label for k in ("rate_limit", "usage_limit")) else
                401 if "auth" in label or "invalid_api_key" in label else
                403 if "permission" in label else 502)
    result = UpstreamError(code, text)
    result.upstream_code = label
    result.details = {"error": error}
    return result


def is_capacity_error(error):
    if isinstance(getattr(error, "code", None), int) and error.code < 500 and error.code != 400:
        return False
    return (getattr(error, "upstream_code", "") in ("server_is_overloaded", "slow_down")
            or any(marker in str(error).lower() for marker in ("server_is_overloaded", "servers are currently overloaded")))


def iter_events(response, timeout, line_limit=256 * 1024):
    for line in sse.iter_sse_lines(response, timeout, line_limit=line_limit):
        from core.account_runtime import bounded_timeout
        bounded_timeout()
        data = sse.data_of(line)
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            raise UpstreamError(502, "Malformed Codex SSE event") from None
        if not isinstance(event, dict):
            raise UpstreamError(502, "Invalid Codex SSE event")
        if event.get("type") in ("error", "response.failed"):
            raise error_from_event(event)
        yield event


def response_to_chat(response, model):
    text, reasoning, calls = [], [], []
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif kind == "reasoning":
            reasoning.append(_text(item.get("summary")))
        elif kind == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id"), "type": "function",
                          "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")}})
    message = {"role": "assistant", "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = calls
    finish = "length" if response.get("status") == "incomplete" else "tool_calls" if calls else "stop"
    result = {"id": response.get("id") or "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
              "created": response.get("created_at") or int(time.time()), "model": model,
              "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if response.get("usage") is not None:
        result["usage"] = usage_to_chat(response["usage"])
    return result


def collect_response(events):
    output = {}
    for event in events:
        kind = event.get("type")
        if kind == "response.output_item.done":
            output[event.get("output_index", len(output))] = event.get("item") or {}
        elif kind in ("response.completed", "response.done", "response.incomplete"):
            response = event.get("response") or {}
            if response.get("error") or response.get("status") == "failed":
                raise error_from_event(event)
            if not response.get("output") and output:
                response["output"] = [output[k] for k in sorted(output)]
            return response
    raise UpstreamError(502, "Codex stream ended before response.completed")


def stream_to_chat(events, model):
    rid, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
    tools, arguments_seen, item_keys = {}, set(), {}
    text_seen = False

    def chunk(delta, finish=None, usage=None):
        result = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta or finish else []}
        if usage is not None:
            result["usage"] = usage
        return "data: " + json.dumps(result, ensure_ascii=False) + "\n\n"

    def announce(key, item):
        tools[key] = {"index": len(tools), "id": item.get("call_id") or item.get("id"),
                      "type": "function", "function": {"name": item.get("name", ""), "arguments": ""}}
        if item.get("id"):
            item_keys[item["id"]] = key
        return chunk({"tool_calls": [tools[key]]})

    for event in events:
        kind = event.get("type")
        key = event.get("output_index", item_keys.get(event.get("item_id")))
        if kind == "response.created":
            response = event.get("response") or {}
            rid, created = response.get("id") or rid, response.get("created_at") or created
        elif kind in ("response.output_text.delta", "response.refusal.delta"):
            text_seen = True
            yield chunk({"content": event.get("delta", "")})
        elif kind == "response.reasoning_summary_text.delta":
            yield chunk({"reasoning_content": event.get("delta", "")})
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                if key not in tools:
                    yield announce(key, item)
                if kind.endswith("done") and key not in arguments_seen and item.get("arguments"):
                    yield chunk({"tool_calls": [{"index": tools[key]["index"], "function": {"arguments": item["arguments"]}}]})
                    arguments_seen.add(key)
        elif kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            if key not in tools:
                raise UpstreamError(502, "Function arguments arrived before function identity")
            if kind.endswith("delta") or key not in arguments_seen:
                arguments = event.get("delta") if kind.endswith("delta") else event.get("arguments")
                if arguments:
                    yield chunk({"tool_calls": [{"index": tools[key]["index"], "function": {"arguments": arguments}}]})
                    arguments_seen.add(key)
        elif kind in ("response.completed", "response.done", "response.incomplete"):
            response = event.get("response") or {}
            if response.get("error") or response.get("status") == "failed":
                raise error_from_event(event)
            chat = response_to_chat(response, model)
            message = chat["choices"][0]["message"]
            if not text_seen and message.get("content"):
                yield chunk({"content": message["content"]})
            if not tools:
                for index, call in enumerate(message.get("tool_calls") or []):
                    yield chunk({"tool_calls": [{"index": index, **call}]})
            finish = "length" if response.get("status") == "incomplete" else "tool_calls" if tools or message.get("tool_calls") else "stop"
            yield chunk({}, finish=finish)
            if response.get("usage") is not None:
                yield chunk({}, usage=usage_to_chat(response["usage"]))
            yield "data: [DONE]\n\n"
            return
    raise UpstreamError(502, "Codex stream ended before response.completed")
