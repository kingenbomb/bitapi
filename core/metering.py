#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
计量层 —— 统一官方正版反代与逆向渠道的用量口径。

设计核心:**上游真实 usage 优先,估算兜底**。同一套 resolve_usage 逻辑对所有渠道生效:
  - 官方 API 与正版反代(billing_mode=upstream):上游返回标准 usage → 直接采信
  - 自建中转、逆向接口(billing_mode=estimate):上游无 usage → tiktoken 估算
  - 图/视频生成(billing_mode=per_request):无 token 概念 → tokens=0,按次计量
即便声明了 estimate 的渠道意外返回真实 usage,也优先采信(upstream 永远压过 estimate),
所以接入未来任何"带 usage 的官方反代"零改动:声明 billing_mode=upstream 即可,
甚至不声明(默认 estimate)也能在检测到真 usage 时自动走 upstream 口径。

tiktoken 估算用 cl100k_base 通用编码器(对非 OpenAI 模型是合理近似);
首次需要 vocab,离线不可用时退化为字符数/4 粗估,保证永不因计量失败影响对话。
"""
from core.adapter import BILL_PER_REQUEST

# token 来源标记(写入 usage_logs.token_source,便于区分真实/估算)
SRC_UPSTREAM = "upstream"      # 上游返回的真实 usage
SRC_ESTIMATE = "estimate"      # tiktoken 本地估算
SRC_PER_REQUEST = "per_request"  # 生成类,按次(tokens=0)

_ENC = None
_ENC_TRIED = False


def _encoder():
    global _ENC, _ENC_TRIED
    if _ENC_TRIED:
        return _ENC
    _ENC_TRIED = True
    try:
        import tiktoken
        _ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENC = None  # 离线/无 vocab:退化字符估算
    return _ENC


def count_tokens(text):
    """估算一段文本的 token 数。tiktoken 可用则精确,否则字符数/4 粗估。"""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _messages_text(messages):
    """把 OpenAI messages 拼成用于估算的纯文本(含 role 前缀近似对话开销)。"""
    parts = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # 多模态 content:仅累加文本块
            for blk in content:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
    return "\n".join(parts)


def _valid_upstream_usage(usage):
    """上游 usage 是否可采信:至少有一个非零的 prompt/completion/total。"""
    if not isinstance(usage, dict):
        return False
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = usage.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def estimate_usage(request_messages, output_text):
    """给客户端看的估算 usage(OpenAI 形状)。与 resolve_usage 的估算分支同一套
    计数,所以响应里的数和账单里的数是同一个 —— 用户拿着响应能核对账。

    只给客户端,不给嗅探/计量:计量层自己会算同一个数并如实标 token_source=estimate;
    把这份塞进嗅探会被当成上游真实值。"""
    it = count_tokens(_messages_text(request_messages))
    ot = count_tokens(output_text or "")
    return {"prompt_tokens": it, "completion_tokens": ot, "total_tokens": it + ot}


def resolve_usage(billing_mode, upstream_usage, request_messages, output_text):
    """归一化一次请求的用量。返回 (input_tokens, output_tokens, source)。

    参数:
      billing_mode    : adapter.billing_mode(upstream/estimate/per_request)
      upstream_usage  : 上游返回的 usage dict(可能为 None/空)
      request_messages: 入站 OpenAI messages(用于估算 input)
      output_text     : 归并后的模型输出文本(用于估算 output)
    """
    # 1) 上游真实 usage 永远优先(无论声明什么 mode)
    if _valid_upstream_usage(upstream_usage):
        it = int(upstream_usage.get("prompt_tokens") or 0)
        ot = int(upstream_usage.get("completion_tokens") or 0)
        if not it and not ot:
            # 只给了 total_tokens:全记到 output(无法拆分)
            ot = int(upstream_usage.get("total_tokens") or 0)
        return it, ot, SRC_UPSTREAM

    # 2) 生成类:按次,不计 token
    if billing_mode == BILL_PER_REQUEST:
        return 0, 0, SRC_PER_REQUEST

    # 3) 估算(逆向渠道默认路径,以及 upstream 声明但本次没拿到 usage 的兜底)
    it = count_tokens(_messages_text(request_messages))
    ot = count_tokens(output_text or "")
    return it, ot, SRC_ESTIMATE
