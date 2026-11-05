"""OpenAI Responses API ↔ Chat Completions 的格式翻译。

从 server.py 抽出来的纯协议转换:只做格式翻译,不碰数据库、不发请求。
所以可以直接用 dict 单测,不必起服务。

server.py 里仍保留同名别名(`from core.protocol.responses import _xxx`),
老调用点不用改。
"""
import copy
import json
import random
import secrets
import string
import time

from fastapi import HTTPException


def _cid():
    return "chatcmpl-" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _mid():
    return "msg_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _rid():
    return "resp_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))



def _part_to_chat(part):
    """Responses 的 content part → chat 的 content part。认不出的返回 None。"""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind in ("input_text", "output_text", "text") and isinstance(part.get("text"), str):
        out = {"type": "text", "text": part["text"]}
        if part.get("prompt_cache_breakpoint") is not None:
            out["prompt_cache_breakpoint"] = copy.deepcopy(part["prompt_cache_breakpoint"])
        return out
    if kind == "input_image":
        url = part.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if url:
            out = {"type": "image_url", "image_url": {"url": url}}
            if part.get("detail"):
                out["image_url"]["detail"] = part["detail"]
            if part.get("prompt_cache_breakpoint") is not None:
                out["prompt_cache_breakpoint"] = copy.deepcopy(part["prompt_cache_breakpoint"])
            return out
    return None



def _flatten_parts(parts):
    """全是文字就折成字符串(逆向渠道只认字符串),带图片才留数组。"""
    if all(p.get("type") == "text" and p.get("prompt_cache_breakpoint") is None for p in parts):
        return "".join(p["text"] for p in parts)
    return parts



def _responses_to_openai(body):
    """Responses 请求 → chat/completions 请求。"""
    if body.get("previous_response_id"):
        raise HTTPException(status_code=400, detail={
            "code": "stateless", "message": "this gateway does not store responses; "
            "send the full conversation in `input` instead of previous_response_id"})
    messages = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    items = [{"role": "user", "content": inp}] if isinstance(inp, str) else list(inp or [])
    pending_calls = []       # 连续的 function_call 合成一条 assistant 消息

    def flush_calls():
        if pending_calls:
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or "message"
        if kind == "function_call":
            pending_calls.append({"id": item.get("call_id") or item.get("id") or _cid(),
                                  "type": "function",
                                  "function": {"name": item.get("name", "tool"),
                                               "arguments": item.get("arguments") or "{}"}})
            continue
        flush_calls()
        if kind == "function_call_output":
            out = item.get("output")
            marked = isinstance(out, list) and any(isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None for part in out)
            content = [_part_to_chat(part) for part in out] if marked else out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            message = {"role": "tool", "tool_call_id": item.get("call_id", ""), "content": content}
            if item.get("prompt_cache_breakpoint") is not None:
                message["prompt_cache_breakpoint"] = copy.deepcopy(item["prompt_cache_breakpoint"])
            messages.append(message)
            continue
        if kind != "message":
            continue          # reasoning / item_reference 等历史项对上游没有意义
        role = item.get("role") or "user"
        if role == "developer":
            role = "system"
        content = item.get("content")
        marker = ({"prompt_cache_breakpoint": copy.deepcopy(item["prompt_cache_breakpoint"])}
                  if item.get("prompt_cache_breakpoint") is not None else {})
        if isinstance(content, str):
            messages.append({"role": role, "content": content, **marker})
            continue
        parts = [p for p in (_part_to_chat(x) for x in (content or [])) if p]
        if parts:
            messages.append({"role": role, "content": _flatten_parts(parts), **marker})
    flush_calls()

    out = {"model": body.get("model", ""), "messages": messages,
           "stream": bool(body.get("stream", False))}
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls", "prompt_cache_options", "prompt_cache_key", "prompt_cache_retention"):
        if body.get(k) is not None:
            out[k] = copy.deepcopy(body[k])
    effort = (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None
    if effort:
        out["reasoning_effort"] = effort
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        out["response_format"] = ({"type": "json_object"} if fmt["type"] == "json_object"
                                  else {"type": "json_schema",
                                        "json_schema": {k: v for k, v in fmt.items() if k != "type"}})
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            raise HTTPException(status_code=400, detail={
                "code": "unsupported_tool",
                "message": f"built-in tool {tool.get('type')} is not supported; only function tools"})
        if not tool.get("name"):
            continue
        fn = {"name": tool["name"], "description": tool.get("description", ""),
              "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}
        if tool.get("strict") is not None:
            fn["strict"] = tool["strict"]
        tools.append({"type": "function", "function": fn})
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    elif choice in ("auto", "none", "required"):
        out["tool_choice"] = choice
    return out



def _responses_usage(usage):
    u = usage if isinstance(usage, dict) else {}
    it = int(u.get("prompt_tokens") or 0)
    ot = int(u.get("completion_tokens") or 0)
    from core.metering import cache_tokens, cache_write_ttls
    details = dict(u.get("prompt_tokens_details") or {})
    cached, written = cache_tokens(u)
    if cached is not None:
        details["cached_tokens"] = cached
    if written is not None:
        details["cache_write_tokens"] = written
    ttls = cache_write_ttls(u)
    if ttls is not None:
        details["cache_write_tokens_details"] = ttls
    reasoning = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    return {"input_tokens": it, "output_tokens": ot, "total_tokens": it + ot,
            "input_tokens_details": details,
            "output_tokens_details": {"reasoning_tokens": reasoning}}



def _response_shell(body, model, rid, created, status="in_progress"):
    """响应对象的外壳,created / in_progress / completed 三处同一份。"""
    return {"id": rid, "object": "response", "created_at": created, "status": status,
            "model": model, "output": [], "error": None, "incomplete_details": None,
            "instructions": body.get("instructions"),
            "max_output_tokens": body.get("max_output_tokens"),
            "parallel_tool_calls": body.get("parallel_tool_calls", True),
            "tool_choice": body.get("tool_choice", "auto"), "tools": body.get("tools") or [],
            "temperature": body.get("temperature", 1.0), "top_p": body.get("top_p", 1.0),
            "metadata": body.get("metadata") or {}, "store": False,
            "usage": None}



def _openai_to_response(chat, body, model):
    """chat/completions 非流式响应 → Responses 响应。"""
    rid, created = _rid(), int(time.time())
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []
    if message.get("content"):
        output.append({"type": "message", "id": _mid(), "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": message["content"],
                                    "annotations": []}]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append({"type": "function_call", "id": "fc_" + secrets.token_hex(12),
                       "call_id": call.get("id") or _cid(), "name": fn.get("name", "tool"),
                       "arguments": fn.get("arguments") or "{}", "status": "completed"})
    finish = choice.get("finish_reason")
    resp = _response_shell(body, model, rid, created,
                           status="incomplete" if finish == "length" else "completed")
    if finish == "length":
        resp["incomplete_details"] = {"reason": "max_output_tokens"}
    resp["output"] = output
    resp["usage"] = _responses_usage(chat.get("usage"))
    return resp



def _responses_stream(openai_lines, body, model):
    """chat SSE → Responses 事件流。

    文本走 message 项(output_text.delta),工具调用走 function_call 项
    (function_call_arguments.delta),最后 response.completed 带全量 output 与 usage。
    一帧都没来就什么都不发 —— 与 Anthropic 那条同一口径,宁可空流也不给残缺流。"""
    rid, created = _rid(), int(time.time())
    seq = [0]

    def ev(kind, **payload):
        seq[0] += 1
        payload.update({"type": kind, "sequence_number": seq[0]})
        return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    started = False
    output = []                 # 已开始的输出项(按 output_index)
    text_index = None           # 文本 message 项的 output_index
    text_parts = []
    calls = {}                  # chat 里的 tool_call index → output_index
    usage = None
    finish = None
    for line in openai_lines:
        if not isinstance(line, str) or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not started:
            started = True
            shell = _response_shell(body, model, rid, created)
            yield ev("response.created", response=shell)
            yield ev("response.in_progress", response=shell)
        if isinstance(packet.get("usage"), dict) and packet["usage"].get("total_tokens"):
            usage = packet["usage"]
        choice = (packet.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            if text_index is None:
                text_index = len(output)
                item = {"type": "message", "id": _mid(), "status": "in_progress",
                        "role": "assistant", "content": []}
                output.append(item)
                yield ev("response.output_item.added", output_index=text_index, item=item)
                yield ev("response.content_part.added", item_id=item["id"], output_index=text_index,
                         content_index=0, part={"type": "output_text", "text": "", "annotations": []})
            text_parts.append(delta["content"])
            yield ev("response.output_text.delta", item_id=output[text_index]["id"],
                     output_index=text_index, content_index=0, delta=delta["content"])
        for pos, call in enumerate(delta.get("tool_calls") or []):
            key = call.get("index", pos)
            fn = call.get("function") or {}
            if key not in calls:
                idx = len(output)
                item = {"type": "function_call", "id": "fc_" + secrets.token_hex(12),
                        "call_id": call.get("id") or _cid(), "name": fn.get("name", "tool"),
                        "arguments": "", "status": "in_progress"}
                output.append(item)
                calls[key] = idx
                yield ev("response.output_item.added", output_index=idx, item=dict(item))
            item = output[calls[key]]
            if fn.get("arguments"):
                item["arguments"] += fn["arguments"]
                yield ev("response.function_call_arguments.delta", item_id=item["id"],
                         output_index=calls[key], delta=fn["arguments"])
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    if not started:
        return
    if text_index is not None:
        item = output[text_index]
        text = "".join(text_parts)
        item["content"] = [{"type": "output_text", "text": text, "annotations": []}]
        item["status"] = "completed"
        yield ev("response.output_text.done", item_id=item["id"], output_index=text_index,
                 content_index=0, text=text)
        yield ev("response.content_part.done", item_id=item["id"], output_index=text_index,
                 content_index=0, part=item["content"][0])
        yield ev("response.output_item.done", output_index=text_index, item=item)
    for key, idx in calls.items():
        item = output[idx]
        item["status"] = "completed"
        yield ev("response.function_call_arguments.done", item_id=item["id"], output_index=idx,
                 arguments=item["arguments"])
        yield ev("response.output_item.done", output_index=idx, item=item)
    final = _response_shell(body, model, rid, created,
                            status="incomplete" if finish == "length" else "completed")
    if finish == "length":
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    final["output"] = output
    final["usage"] = _responses_usage(usage)
    yield ev("response.completed", response=final)

