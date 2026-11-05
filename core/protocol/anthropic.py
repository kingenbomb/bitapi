"""Anthropic Messages ↔ OpenAI Chat 的格式翻译。

从 server.py 抽出来的纯协议转换:只做格式翻译,不碰数据库、不发请求。
所以可以直接用 dict 单测,不必起服务。

server.py 里仍保留同名别名(`from core.protocol.anthropic import _xxx`),
老调用点不用改。
"""
import copy
import json
import random
import string
import time

import config
from core import metering


def _cid():
    return "chatcmpl-" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _mid():
    return "msg_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _extract_output_text(resp):
    """从非流式 OpenAI 响应里取模型输出文本(估算 output token 用)。"""
    if not isinstance(resp, dict):
        return ""
    try:
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        return msg.get("content") or ""
    except Exception:
        return ""


# ---- Anthropic Messages 适配 ----


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block.get("text", "") for block in content
                   if isinstance(block, dict) and block.get("type") == "text")



def _image_part(block):
    """Anthropic image block → OpenAI image_url part。base64 拼成 data URI,url 直传。
    认不出的 source 形状返回 None(调用方跳过),不造一个坏的 part 送上游。"""
    src = block.get("source") or {}
    kind = src.get("type")
    if kind == "base64" and src.get("data"):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if kind == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None



def _user_content(blocks):
    """user 消息的内容:纯文字给字符串(逆向渠道只认字符串);带图片才给多模态数组。
    原先图片 block 被静默丢掉 —— 用户发了图,模型没看到,而且没有任何提示。"""
    parts = []
    has_image = False
    has_cache = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            part = {"type": "text", "text": block["text"]}
            # 保留客户端标的缓存断点:丢了它 = 告诉模型「这段没法缓存」,
            # Claude 的省钱/降延迟全靠它(cache_control 会一路透传到上游)。
            if block.get("cache_control"):
                part["cache_control"] = block["cache_control"]
                has_cache = True
            if block.get("prompt_cache_breakpoint") is not None:
                part["prompt_cache_breakpoint"] = copy.deepcopy(block["prompt_cache_breakpoint"])
                has_cache = True
            parts.append(part)
        elif block.get("type") == "image":
            part = _image_part(block)
            if part:
                if block.get("prompt_cache_breakpoint") is not None:
                    part["prompt_cache_breakpoint"] = copy.deepcopy(block["prompt_cache_breakpoint"])
                    has_cache = True
                parts.append(part)
                has_image = True
    if not has_image and not has_cache:
        return "".join(p["text"] for p in parts)
    return parts



def _anthropic_to_openai(body):
    messages = []
    sys_src = body.get("system")
    system = _content_text(sys_src)
    if system:
        # system 上带缓存断点时用数组形式带过去(否则纯字符串,上游最稳)
        if isinstance(sys_src, list) and any(
                isinstance(b, dict) and (b.get("cache_control") or b.get("prompt_cache_breakpoint") is not None) for b in sys_src):
            parts = []
            for b in sys_src:
                if not isinstance(b, dict) or b.get("type") != "text":
                    continue
                part = {"type": "text", "text": b.get("text") or ""}
                if b.get("cache_control"):
                    part["cache_control"] = b["cache_control"]
                if b.get("prompt_cache_breakpoint") is not None:
                    part["prompt_cache_breakpoint"] = copy.deepcopy(b["prompt_cache_breakpoint"])
                parts.append(part)
            messages.append({"role": "system", "content": parts})
        else:
            messages.append({"role": "system", "content": system})
    for source in body.get("messages") or []:
        role = source.get("role")
        content = source.get("content", "")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        text = _content_text(blocks)
        if role == "assistant":
            calls = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append({"id": block.get("id") or _cid(), "type": "function",
                                  "function": {"name": block.get("name", "tool"),
                                               "arguments": json.dumps(block.get("input") or {})}})
            message = {"role": "assistant", "content": text or None}
            if any(isinstance(block, dict) and block.get("prompt_cache_breakpoint") is not None for block in blocks):
                message["content"] = _user_content(blocks)
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            continue
        user_content = _user_content(blocks)
        if user_content:
            messages.append({"role": role or "user", "content": user_content})
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_content = block.get("content", "")
            marked = isinstance(tool_content, list) and any(
                isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None for part in tool_content)
            result = (_user_content(tool_content) if marked
                      else _content_text(tool_content) or json.dumps(tool_content))
            messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                             "content": result})
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        item = {"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }}
        if tool.get("cache_control"):
            item["cache_control"] = tool["cache_control"]   # 工具定义缓存断点
        tools.append(item)
    out = {"model": body.get("model", ""), "messages": messages,
           "stream": bool(body.get("stream", False)), "max_tokens": body.get("max_tokens")}
    for key in ("prompt_cache_options", "prompt_cache_key", "prompt_cache_retention"):
        if body.get(key) is not None:
            out[key] = copy.deepcopy(body[key])
    if isinstance(body.get("cache_control"), dict):
        out["cache_control"] = body["cache_control"]         # 顶层自动缓存
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and choice.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif kind:
            out["tool_choice"] = kind
    return out



def _anthropic_event(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"



def _anthropic_usage(usage, fallback_in=0, fallback_out=0):
    """OpenAI usage → Anthropic usage。上游没给真实值时用估算(与计量层同一套),
    Claude Code 靠这两个数显示花费与上下文占用 —— 一直给 0 等于告诉用户「没花钱」。"""
    from core.metering import cache_tokens, cache_write_ttls, resolve_usage, SRC_UPSTREAM
    it, ot, source = resolve_usage("upstream", usage, [], "")
    if source != SRC_UPSTREAM:
        return {"input_tokens": int(fallback_in), "output_tokens": int(fallback_out)}
    cached, written = cache_tokens(usage)
    # Anthropic input_tokens excludes cache reads/writes; OpenAI includes them.
    out = {"input_tokens": max(0, it - (cached or 0) - (written or 0)),
           "output_tokens": ot}
    if cached is not None:
        out["cache_read_input_tokens"] = cached
    if written is not None:
        out["cache_creation_input_tokens"] = written
    ttls = cache_write_ttls(usage)
    if ttls is not None:
        out["cache_write_tokens_details"] = ttls
    return out



def _anthropic_stop(finish, has_tools):
    if has_tools or finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    return "end_turn"



def _anthropic_message_from_openai(response, requested_model, request_messages=None):
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if message.get("reasoning_content") and config.ANTHROPIC_EXPOSE_REASONING:
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content.append({"type": "tool_use", "id": call.get("id") or _cid(),
                        "name": fn.get("name", "tool"), "input": args})
    est = metering.estimate_usage(request_messages or [],
                                  (message.get("reasoning_content") or "") + (message.get("content") or ""))
    return {"id": _mid(), "type": "message", "role": "assistant", "model": requested_model,
            "content": content,
            "stop_reason": _anthropic_stop(choice.get("finish_reason"), bool(message.get("tool_calls"))),
            "stop_sequence": None,
            "usage": _anthropic_usage(response.get("usage"), est["prompt_tokens"], est["completion_tokens"])}



def _anthropic_stream(openai_lines, requested_model, request_messages=None):
    """OpenAI SSE → Anthropic SSE。

    协议完整性(对齐 sub2api 的"确定性输出"):
      - message_start 未发出时**不补任何结束事件** —— 宁可空流也不给残缺流
      - 见到终止事件才算完整;上游断流时仍补齐 content_block_stop/message_delta/
        message_stop,避免客户端永久挂起
    usage:message_start 给输入估算(请求在手,不必等上游),message_delta 给最终值 ——
    上游真实 usage 帧到了用它,没到用输出文字的估算,与计量层同一套。
    """
    message_id = _mid()
    started = False
    blocks, calls, next_index, stop_reason = {}, {}, 0, None
    est_in = metering.estimate_usage(request_messages or [], "")["prompt_tokens"]
    out_parts = []
    usage = None

    def start_block(kind, block):
        nonlocal next_index
        index = next_index
        next_index += 1
        blocks[index] = kind
        return index, _anthropic_event("content_block_start", {
            "type": "content_block_start", "index": index, "content_block": block})

    def close_blocks():
        for index in list(blocks):
            yield _anthropic_event("content_block_stop", {"type": "content_block_stop", "index": index})
            blocks.pop(index, None)

    def message_start():
        return _anthropic_event("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "model": requested_model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": est_in, "output_tokens": 0}}})

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
        if packet.get("error"):
            yield _anthropic_event("error", {"type": "error", "error": packet["error"]})
            return
        if not started:
            started = True
            yield message_start()
        if isinstance(packet.get("usage"), dict) and packet["usage"].get("total_tokens"):
            usage = packet["usage"]        # 覆盖式:取最近一次
        choice = (packet.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content") and config.ANTHROPIC_EXPOSE_REASONING:
            out_parts.append(delta["reasoning_content"])
            index = next((i for i, kind in blocks.items() if kind == "thinking"), None)
            if index is None:
                index, event = start_block("thinking", {"type": "thinking", "thinking": ""})
                yield event
            yield _anthropic_event("content_block_delta", {"type": "content_block_delta", "index": index,
                "delta": {"type": "thinking_delta", "thinking": delta["reasoning_content"]}})
        if delta.get("content"):
            out_parts.append(delta["content"])
            index = next((i for i, kind in blocks.items() if kind == "text"), None)
            if index is None:
                index, event = start_block("text", {"type": "text", "text": ""})
                yield event
            yield _anthropic_event("content_block_delta", {"type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": delta["content"]}})
        for pos, call in enumerate(delta.get("tool_calls") or []):
            key = call.get("index", pos)
            fn = call.get("function") or {}
            if key not in calls:
                call_id = call.get("id") or _cid()
                index, event = start_block("tool_use", {"type": "tool_use", "id": call_id,
                    "name": fn.get("name", "tool"), "input": {}})
                calls[key] = index
                yield event
            arguments = fn.get("arguments")
            if arguments:
                yield _anthropic_event("content_block_delta", {"type": "content_block_delta",
                    "index": calls[key], "delta": {"type": "input_json_delta", "partial_json": arguments}})
        finish = choice.get("finish_reason")
        if finish:
            stop_reason = _anthropic_stop(finish, bool(calls))
    if not started:
        # message_start 从未发出 → 不补结束事件,宁可空流也不给残缺流
        return
    yield from close_blocks()
    est_out = metering.estimate_usage([], "".join(out_parts))["completion_tokens"]
    yield _anthropic_event("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": stop_reason or "end_turn", "stop_sequence": None},
        "usage": _anthropic_usage(usage, est_in, est_out)})
    yield _anthropic_event("message_stop", {"type": "message_stop"})

