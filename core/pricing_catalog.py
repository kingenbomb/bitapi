#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置价目表 + LiteLLM 价目表 —— 未手工配价时的兜底来源。

项目别名先查 data/pricing_seed.json 中人工核对过的官网价/站点媒体售价，
再查 LiteLLM。LiteLLM 启动时加载本地 JSON(不存在则尝试拉一次)，后台按间隔
刷新；拉不到仍保留内置价。价目表不进 DB —— 管理员手配定价仍在库里且优先。

LiteLLM 的单价是「美元/单 token」,本模块换算成本项目统一的「美元/1M token」。
"""
import json
import os
import threading
import time
import urllib.request

import config

_LOCK = threading.Lock()
_DATA = {}          # model_name → pricing dict(已换算为每 1M)
_BUILTIN_ROWS = []  # 项目别名 → 官网价或站点媒体售价(支持 prefix*)
_LOADED_AT = 0
_SOURCE = "none"    # file | remote | none


def _seed_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "pricing_seed.json")


def _load_builtin(path=None):
    """加载项目别名价目表。失败只影响这一层，不阻断 LiteLLM 与服务启动。"""
    path = path or _seed_path()
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"[pricing] 内置价目表读取失败 {path}: {e}", flush=True)
        return 0
    rows = []
    for raw in payload.get("pricing", []):
        if not isinstance(raw, dict) or not raw.get("model_pattern"):
            continue
        row = dict(raw)
        row["source"] = "builtin"
        rows.append(row)
    global _BUILTIN_ROWS
    with _LOCK:
        _BUILTIN_ROWS = rows
    return len(rows)


def _match(rows, model):
    """精确名优先；prefix* 取最长前缀，与数据库定价解析规则一致。"""
    best, best_len = None, -1
    for row in rows:
        pat = row.get("model_pattern") or ""
        if pat == model:
            return row
        if pat == "*" and best_len < 0:
            best, best_len = row, 0
        elif pat.endswith("*") and model.startswith(pat[:-1]):
            plen = len(pat) - 1
            if plen > best_len:
                best, best_len = row, plen
    return best


def _per_million(v):
    try:
        return float(v) * 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _convert(name, raw):
    """LiteLLM 条目 → 本项目定价结构。只取 chat 类,过滤无 token 价的条目。"""
    if not isinstance(raw, dict):
        return None
    in_c = raw.get("input_cost_per_token")
    out_c = raw.get("output_cost_per_token")
    if in_c is None and out_c is None:
        return None  # 只有图片价之类的条目不参与 token 计费,避免按 $0 计
    return {
        "model_pattern": name,
        "billing_mode": "token",
        "input_price": _per_million(in_c),
        "output_price": _per_million(out_c),
        "cache_read_price": _per_million(raw.get("cache_read_input_token_cost")),
        "cache_write_price": _per_million(raw.get("cache_creation_input_token_cost")),
        "per_request_price": 0.0,
        # LiteLLM 的长上下文是倍率制,本项目用绝对单价:换算成长档单价
        "long_threshold": int(raw.get("long_context_input_token_threshold") or 0),
        "long_input_price": (
            _per_million(in_c) * float(raw["long_context_input_cost_multiplier"])
            if raw.get("long_context_input_cost_multiplier") and in_c is not None else None),
        "long_output_price": (
            _per_million(out_c) * float(raw["long_context_output_cost_multiplier"])
            if raw.get("long_context_output_cost_multiplier") and out_c is not None else None),
        "long_cache_read_price": None,
        "long_cache_write_price": None,
    }


def _ingest(payload):
    out = {}
    for name, raw in (payload or {}).items():
        if name == "sample_spec":
            continue
        conv = _convert(name, raw)
        if conv:
            out[name] = conv
    return out


def load_from_file(path=None):
    path = path or config.PRICING_CATALOG_PATH
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"[pricing] 价目表读取失败 {path}: {e}", flush=True)
        return 0
    data = _ingest(payload)
    global _DATA, _LOADED_AT, _SOURCE
    with _LOCK:
        _DATA = data
        _LOADED_AT = int(time.time())
        _SOURCE = "file"
    return len(data)


def fetch_remote(url=None, timeout=30, save=True):
    """拉取远端价目表并落盘。失败抛异常,由调用方决定是否容错。"""
    url = url or config.PRICING_CATALOG_URL
    if not url:
        raise RuntimeError("PRICING_CATALOG_URL not configured")
    req = urllib.request.Request(url, headers={"User-Agent": "bit-api/pricing"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = _ingest(payload)
    if not data:
        raise RuntimeError("remote catalog produced no usable entries")
    if save:
        path = config.PRICING_CATALOG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    global _DATA, _LOADED_AT, _SOURCE
    with _LOCK:
        _DATA = data
        _LOADED_AT = int(time.time())
        _SOURCE = "remote"
    return len(data)


def ensure_loaded():
    """启动调用：内置项目价始终加载，LiteLLM 本地优先、没有再远程拉取。"""
    _load_builtin()
    if load_from_file():
        return status()
    try:
        fetch_remote()
    except Exception as e:
        print(f"[pricing] LiteLLM 价目表初始化失败(保留内置项目价): {e}", flush=True)
    return status()


def refresh():
    """定时刷新入口。失败保留旧值。"""
    try:
        n = fetch_remote()
        print(f"[pricing] 价目表已刷新: {n} 条", flush=True)
        return n
    except Exception as e:
        print(f"[pricing] 价目表刷新失败(保留旧值): {e}", flush=True)
        return 0


def lookup(model):
    with _LOCK:
        builtin = _match(_BUILTIN_ROWS, model)
        if builtin:
            return dict(builtin)
        row = _DATA.get(model)
        return dict(row, source="litellm") if row else None


def status():
    with _LOCK:
        return {
            "entries": len(_BUILTIN_ROWS) + len(_DATA),
            "builtin_entries": len(_BUILTIN_ROWS),
            "litellm_entries": len(_DATA),
            "loaded_at": _LOADED_AT,
            "source": f"builtin+{_SOURCE}" if _BUILTIN_ROWS else _SOURCE,
        }


class Catalog:
    """给 Pricing 用的薄封装(便于测试注入)。"""

    @staticmethod
    def lookup(model):
        return lookup(model)

    @staticmethod
    def status():
        return status()
