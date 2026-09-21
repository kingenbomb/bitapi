#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输出洗词 —— 反代出去时把上游品牌名从模型输出里抹掉,不暴露上游。

三条路都要洗:非流式 content/reasoning、流式逐 token(还得防品牌词被切在两个 chunk)。
删词是确定性兜底(不改模型行为),配合调用方自定义身份即可完全隐藏上游。
"""
import re

import config

# 上游品牌词(出现在模型自述里会暴露上游)。长词优先,大小写不敏感,整段删除。
# 默认空:要抹谁由部署方用 BITAPI_SCRUB_TERMS 给(逗号分隔),这里不预设任何上游。
DEFAULT_TERMS = []


def _build(terms):
    terms = [t for t in terms if t]
    if not terms:
        return None, [], 1
    ordered = sorted(set(terms), key=lambda s: -len(s))  # 长词优先,防 "KOGO OS" 只删 "KOGO"
    rx = re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)
    lows = [t.lower() for t in ordered]
    return rx, lows, max(len(t) for t in ordered)


_RX, _TERMS_LOW, _MAXLEN = _build(config.SCRUB_TERMS or DEFAULT_TERMS)
_ON = config.SCRUB_ENABLED and _RX is not None


def _partial_tail(s):
    """s 末尾有多长的一段可能是某个品牌词的前缀(需留住等后续 chunk 补全)。"""
    low = s.lower()
    m = 0
    for term in _TERMS_LOW:
        for k in range(min(len(term) - 1, len(low)), m, -1):
            if low.endswith(term[:k]):
                m = k
                break
    return m


def scrub(text, tidy=True):
    """一次性洗词。tidy=True 时顺手把删词留下的多余空格收拾掉(非流式用)。"""
    if not _ON or not text:
        return text
    out = _RX.sub("", text)
    if tidy:
        out = re.sub(r"[ \t]{2,}", " ", out)          # 多空格并一个
        out = re.sub(r"[ \t]+([,.，。;；:：!！?？)）])", r"\1", out)  # 标点前的空格去掉
    return out


def scrub_raw(raw):
    """就地洗 OpenAI 响应 dict 里的 message.content / reasoning_content(navos 等 raw 透传)。"""
    if not _ON or not isinstance(raw, dict):
        return raw
    for ch in raw.get("choices") or []:
        msg = ch.get("message") or {}
        if isinstance(msg.get("content"), str):
            msg["content"] = scrub(msg["content"])
        if isinstance(msg.get("reasoning_content"), str):
            msg["reasoning_content"] = scrub(msg["reasoning_content"])
    return raw


class Scrubber:
    """
    流式洗词:每次 feed 都对累积 buffer 整体洗词(删掉完整品牌词),再只留住末尾
    "可能是品牌词前缀" 的一小段等下个 chunk 补全,其余安全部分吐出。
    这样即使品牌词被切成 'KO'/'G'/'O A' 多个 tiny chunk 也能正确删除。
    """

    def __init__(self):
        self.buf = ""

    def feed(self, text):
        if not _ON:
            return text or ""
        self.buf = scrub(self.buf + (text or ""), tidy=False)  # 累积洗:完整词已删
        keep = _partial_tail(self.buf)                          # 末尾可能是半个品牌词,留住
        if keep >= len(self.buf):
            return ""
        emit, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
        return emit

    def flush(self):
        if not _ON:
            out, self.buf = self.buf, ""
            return out
        out = scrub(self.buf, tidy=False)
        self.buf = ""
        return out
