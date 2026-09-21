#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据渠道 —— channels 表里的每一行实例化成一个 OpenAICompatAdapter 注册进 core/adapter。

管理台「渠道」页建一条(base_url / 模型 / 映射 / 额外头),存库即生效,不写代码、
不重启。代码渠道(adapters/*.py)照旧 import 即注册;两类在注册表里平级,网关、
号池、巡检、渠道开关都不区分来源。

reload() 是幂等的整体重放:先把上一次由本模块注册的都摘掉,再按表重新注册。
只摘自己注册过的 —— 代码渠道不归这里管,不能因为表里没有就把它们摘了。

校验在写库之前(validate),口径:
  - name 是 [a-z0-9_-]{2,32},不能撞代码渠道(那会把 import 注册的顶掉),
    不能撞别的数据渠道
  - 模型名可以与别的渠道重名(一模型多渠道,按 priority / weight 路由、失败换渠道),
    但不能撞任何渠道名 —— 渠道名本身也可作为 model 路由
  - base_url 必须 http(s),不带 query
"""
import re
import urllib.parse

from adapters.openai_compat import OpenAICompatAdapter
from core.adapter import all_adapters, register_adapter, unregister_adapter

CHANNEL_TYPES = ("openai",)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# 本模块注册过的渠道名。reload 时据此摘旧的。
_LOADED = set()


class ChannelError(ValueError):
    pass


def loaded():
    return set(_LOADED)


def is_data_channel(name):
    return name in _LOADED


def build(row):
    return OpenAICompatAdapter(
        name=row["name"], base_url=row["base_url"],
        chat_path=row.get("chat_path") or "/chat/completions",
        models=row.get("models") or [], model_map=row.get("model_map") or {},
        headers=row.get("headers") or {}, timeout=row.get("timeout") or 0,
        source="data", notes=row.get("notes") or "")


def reload(db):
    """按表重放注册。返回当前生效的数据渠道名列表。"""
    for name in list(_LOADED):
        unregister_adapter(name)
    _LOADED.clear()
    for row in db.list_channels():
        if row.get("type", "openai") not in CHANNEL_TYPES:
            print(f"[channels] 跳过未知类型 {row['name']}: {row.get('type')}", flush=True)
            continue
        register_adapter(build(row))
        _LOADED.add(row["name"])
    return sorted(_LOADED)


# ---- 写入侧校验 ----

def _as_list(v, label):
    if v is None:
        return []
    if isinstance(v, str):
        v = [x.strip() for chunk in v.split("\n") for x in chunk.split(",")]
    if not isinstance(v, (list, tuple)):
        raise ChannelError(f"{label} 需是列表")
    out = []
    for x in v:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _as_dict(v, label):
    if v is None or v == "":
        return {}
    if isinstance(v, str):
        # 允许「对外名=上游名」逐行写
        out = {}
        for line in v.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line and ":" not in line:
                raise ChannelError(f"{label} 每行要写成 a=b")
            sep = "=" if "=" in line else ":"
            k, val = line.split(sep, 1)
            out[k.strip()] = val.strip()
        return out
    if not isinstance(v, dict):
        raise ChannelError(f"{label} 需是对象")
    return {str(k).strip(): str(val).strip() for k, val in v.items()
            if str(k).strip() and str(val).strip()}


def validate(data, db, editing=None):
    """返回规范化后的字段字典。editing = 正在编辑的渠道名(允许与自己同名/同模型)。"""
    out = {}
    name = str(data.get("name") or editing or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ChannelError("渠道名只能用小写字母、数字、- 和 _,2~32 位")
    others = {n: ad for n, ad in all_adapters().items() if n != editing}
    if name != editing:
        if name in others and not is_data_channel(name):
            raise ChannelError(f"{name} 是代码里定义的渠道,不能覆盖")
        if name in others or (db.get_channel(name) is not None):
            raise ChannelError(f"渠道 {name} 已存在")
    out["name"] = name

    ctype = str(data.get("type") or "openai").strip().lower()
    if ctype not in CHANNEL_TYPES:
        raise ChannelError(f"类型只支持 {'/'.join(CHANNEL_TYPES)}")
    out["type"] = ctype

    base = str(data.get("base_url") or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query:
        raise ChannelError("base_url 需要带 http:// 或 https:// 的完整地址,不带 query")
    out["base_url"] = base

    path = str(data.get("chat_path") or "/chat/completions").strip()
    if not path.startswith("/"):
        path = "/" + path
    out["chat_path"] = path

    models = _as_list(data.get("models"), "模型清单")
    if not models:
        raise ChannelError("至少填一个模型名")
    # 与别的渠道同名的模型是允许的 —— 那正是「一模型多渠道」:按 priority / weight
    # 路由,失败换渠道(core/adapter.model_routes)。但模型名不能撞渠道名:渠道名本身
    # 也可作为 model 路由(model_to_channel 里 name → name),撞了会把那条路顶掉。
    for m in models:
        if m in others or m == name:
            raise ChannelError(f"模型名 {m} 与渠道名冲突")
    out["models"] = models

    mm = _as_dict(data.get("model_map"), "模型映射")
    unknown = [k for k in mm if k not in models]
    if unknown:
        raise ChannelError(f"模型映射里的 {unknown[0]} 不在模型清单里")
    out["model_map"] = mm

    headers = _as_dict(data.get("headers"), "额外请求头")
    for k in headers:
        if k.lower() in ("authorization", "content-type"):
            raise ChannelError(f"请求头 {k} 由渠道自己生成,不能覆盖")
    out["headers"] = headers

    t = data.get("timeout")
    try:
        t = int(t or 0)
    except (TypeError, ValueError) as e:
        raise ChannelError("超时需是整数秒") from e
    if t < 0 or t > 3600:
        raise ChannelError("超时需在 0~3600 秒之间,0 = 用默认")
    out["timeout"] = t
    out["notes"] = str(data.get("notes") or "").strip()[:500]
    return out
