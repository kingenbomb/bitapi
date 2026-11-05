#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
有界 SSE 读取 —— 所有 key 池型 OpenAI 兼容渠道共用的一段。

HTTPResponse 的迭代器走 readline():上游若一直发字节却不发换行,那一次调用永远
不返回 —— socket 超时被每个到达的字节不断重置,一个 Python 线程握着 GIL 让缓冲区
无限长。这里按块读取，按有效输出刷新空闲期限，并限制未结束行的大小与等待时长。

原先只在某一个渠道实现里;抽出来是因为通用 OpenAI 兼容渠道(adapters/openai_compat.py)
要同一份保护 —— 每个渠道各写一遍迟早有一个漏掉超时。
"""
import time
import json

STREAM_READ_SIZE = 16 * 1024
STREAM_LINE_LIMIT = 256 * 1024
STREAM_PARTIAL_LINE_TIMEOUT = 10


class StreamIdleTimeout(TimeoutError):
    code = 504


class FirstOutputTimeout(StreamIdleTimeout):
    pass


def is_output(value):
    """Model progress, excluding comments, empty deltas and lifecycle heartbeats."""
    if isinstance(value, (str, bytes)):
        data = data_of(value)
        if not data:
            return False
        if data == "[DONE]":
            return True
        try:
            value = json.loads(data)
        except (ValueError, TypeError):
            return False
    if not isinstance(value, dict):
        return False
    if value.get("error"):
        return True
    for choice in value.get("choices") or []:
        delta = choice.get("delta") or choice.get("message") or {}
        if choice.get("finish_reason") or any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls", "function_call")):
            return True
    kind = value.get("type") or ""
    return bool(kind in ("error", "response.failed", "response.completed", "response.done", "response.incomplete",
                         "response.output_item.added", "response.output_item.done")
                or kind.endswith(".delta") and value.get("delta") or value.get("partial_image_b64"))


def iter_sse_lines(response, timeout, line_limit=STREAM_LINE_LIMIT, output_check=None):
    """逐行产出(含结尾换行的 bytes)。response 需有 read1 或 read;两者都没有
    (测试里的假响应)就退回直接迭代 —— 那种对象本来就是按行给的。"""
    read = getattr(response, "read1", None) or getattr(response, "read", None)
    if read is None:
        yield from response
        return
    idle = max(0.001, timeout)
    deadline = time.monotonic() + idle
    pending = bytearray()
    partial_since = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StreamIdleTimeout("upstream stream idle timeout: no model output")
        if hasattr(response, "set_read_timeout"):
            response.set_read_timeout(remaining)
        chunk = read(STREAM_READ_SIZE)
        now = time.monotonic()
        if now >= deadline:
            raise StreamIdleTimeout("upstream stream idle timeout: no model output")
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not pending:
            partial_since = now
        pending.extend(chunk)

        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(pending[:newline + 1])
            del pending[:newline + 1]
            partial_since = now if pending else None
            if len(line) > line_limit:
                raise ValueError("upstream SSE line exceeds size limit")
            if (output_check or is_output)(line):
                deadline = now + idle
            yield line

        if len(pending) > line_limit:
            raise ValueError("upstream SSE line exceeds size limit")
        if (pending and partial_since is not None
                and now - partial_since >= STREAM_PARTIAL_LINE_TIMEOUT):
            raise TimeoutError("upstream stream sent an unterminated SSE line")
        time.sleep(0)


def data_of(line):
    """一行 SSE → data 载荷字符串;不是 data 行返回 None。bytes 或 str 都收。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.rstrip("\r\n")
    if not line.startswith("data:"):
        return None
    return line[5:].strip()
