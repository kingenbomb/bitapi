#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
有界 SSE 读取 —— 所有 key 池型 OpenAI 兼容渠道共用的一段。

HTTPResponse 的迭代器走 readline():上游若一直发字节却不发换行,那一次调用永远
不返回 —— socket 超时被每个到达的字节不断重置,一个 Python 线程握着 GIL 让缓冲区
无限长。这里改成按块读、块与块之间让出 GIL,并对「整次请求的总时长」与「一行不结束
的时长」各设上界,超过就抛,把线程还回去。

原先只在某一个渠道实现里;抽出来是因为通用 OpenAI 兼容渠道(adapters/openai_compat.py)
要同一份保护 —— 每个渠道各写一遍迟早有一个漏掉超时。
"""
import time

STREAM_READ_SIZE = 16 * 1024
STREAM_LINE_LIMIT = 256 * 1024
STREAM_PARTIAL_LINE_TIMEOUT = 10


def iter_sse_lines(response, timeout):
    """逐行产出(含结尾换行的 bytes)。response 需有 read1 或 read;两者都没有
    (测试里的假响应)就退回直接迭代 —— 那种对象本来就是按行给的。"""
    read = getattr(response, "read1", None) or getattr(response, "read", None)
    if read is None:
        yield from response
        return
    deadline = time.monotonic() + max(1, timeout)
    pending = bytearray()
    partial_since = None

    while True:
        chunk = read(STREAM_READ_SIZE)
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("upstream stream exceeded total timeout")
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
            if len(line) > STREAM_LINE_LIMIT:
                raise ValueError("upstream SSE line exceeds size limit")
            yield line

        if len(pending) > STREAM_LINE_LIMIT:
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
