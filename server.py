#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bit-api 统一网关 —— FastAPI:OpenAI 端点 + 管理 API + 上报。

  POST /v1/chat/completions   OpenAI 兼容(按 model 路由到 channel,取号+切号)
  POST /v1/messages           Anthropic Messages 兼容(内部归一到 OpenAI 执行层)
  GET  /v1/models             聚合所有 channel 的模型
  GET  /admin/stats           各渠道号池统计
  GET  /admin/accounts        账号列表
  POST /admin/accounts        本地注册 worker 上报入库
"""
import asyncio
import json
import queue
import random
import secrets
import string
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

import config
from core import auth
from core import db as dbmod
from core import metering
from core import scrub as scrubmod
from core import throttle
from core.adapter import (GenerationSubmittedError, all_adapters,
                          enabled_adapters, get_adapter, model_routes,
                          model_to_channel)
from core.pool_state import DB, POOL, import_keys, parse_keys
from core.scheduler import scan_once, scanner_loop
from core.portal_state import (BILLING, apply_channel_switches,
                               ensure_default_group,
                               init_pricing_catalog, load_data_channels,
                               load_payment_providers, load_plugins,
                               refresh_pricing_catalog)
from core.billing import InsufficientBalanceError, KeyQuotaError, RateLimitError
from routers.portal import (community_oauth_callback as portal_community_callback,
                            current_user as portal_current_user,
                            router as portal_router)

# 触发 adapter 注册(import 即注册)。加渠道照 docs/adapters.md 往这里加一行。
import adapters.grok  # noqa: F401  (xAI 官方接口,自持 OAuth 续期)


@asynccontextmanager
async def lifespan(app):
    # 默认密钥 + 非回环监听 = 没有密码。main.py 已经查过一遍;这里再查是给
    # `uvicorn server:app --host 0.0.0.0` 这种绕开 main.py 的起法 —— README 里正是这么写的。
    from core import preflight
    ok, problems = preflight.check()
    if not ok:
        raise RuntimeError("拒绝启动:" + " ".join(problems))
    # 确保默认套餐组存在(新用户注册分配)
    try:
        g = ensure_default_group()
        print(f"[bitapi] 默认套餐组: {g['name']}(id={g['id']})", flush=True)
    except Exception as e:
        print(f"[bitapi] 默认组初始化失败: {e}", flush=True)
    # 定价价目表(内置目录价 + LiteLLM 补充价)
    try:
        st = init_pricing_catalog()
        print(f"[bitapi] 定价目录: {st['entries']} 条(来源 {st['source']})", flush=True)
    except Exception as e:
        print(f"[bitapi] 定价目录初始化失败: {e}", flush=True)
    # 数据渠道(管理台建的 OpenAI 兼容上游)先进注册表,再套开关 —— 开关校验要认得
    # 这些名字。代码渠道在上面 import 时已经注册。
    try:
        loaded_ch = load_data_channels()
        if loaded_ch:
            print(f"[bitapi] 数据渠道: {', '.join(loaded_ch)}", flush=True)
    except Exception as e:
        print(f"[bitapi] 数据渠道加载失败: {e}", flush=True)
    # 渠道上下线开关(存库,优先于 BITAPI_DISABLED_CHANNELS)。注册表是纯内存的,
    # 不会自己读库,所以启动和每次改设置后都要推一次。
    try:
        off = apply_channel_switches()
        if off:
            print(f"[bitapi] 已下线渠道: {', '.join(off)}", flush=True)
    except Exception as e:
        print(f"[bitapi] 渠道开关加载失败: {e}", flush=True)
    # 插件(返佣等策略层),import 即注册 hooks
    loaded = load_plugins()
    if loaded:
        print(f"[bitapi] 插件已加载: {', '.join(loaded)}", flush=True)
    # 支付渠道,import 即注册
    pays = load_payment_providers()
    if pays:
        print(f"[bitapi] 支付渠道: {', '.join(pays)}", flush=True)
    # 启动时让声明了 sync_pool 的渠道同步一次外部号源,面板立即可见。
    # 不写死渠道名:谁有这个能力谁同步,新增渠道不必回来改这里。
    for _name, _ad in all_adapters().items():
        if not hasattr(_ad, "sync_pool"):
            continue
        try:
            print(f"[bitapi] {_name} 号池同步(full): {_ad.sync_pool(DB, full=True)}",
                  flush=True)
        except Exception as e:
            print(f"[bitapi] {_name} 同步失败: {e}", flush=True)
    task = asyncio.create_task(scanner_loop(
        DB, config.SCAN_INTERVAL, config.SCAN_CONCURRENCY,
        config.REFRESH_MARGIN, config.MIN_BALANCE))
    print(f"[bitapi] 巡检器启动(每 {config.SCAN_INTERVAL}s,并发 {config.SCAN_CONCURRENCY})", flush=True)
    housekeeping = asyncio.create_task(_housekeeping_loop())
    yield
    task.cancel()
    housekeeping.cancel()


async def _housekeeping_loop():
    """后台维护:订单对账(先查上游再写过期)+ 定时刷新价目表 + 到期备份。"""
    while True:
        await asyncio.sleep(600)
        try:
            from core import orders as _orders
            from core.portal_state import USER_DB
            credited, expired = await asyncio.to_thread(
                _orders.reconcile_orders, USER_DB)
            if credited:
                print(f"[bitapi] 查单补到账 {credited} 笔", flush=True)
            if expired:
                print(f"[bitapi] 过期订单 {expired} 笔", flush=True)
        except Exception as e:
            print(f"[bitapi] 订单对账失败: {e}", flush=True)
        interval = config.PRICING_REFRESH_INTERVAL
        if interval > 0 and int(time.time()) % max(interval, 1) < 600:
            try:
                await asyncio.to_thread(refresh_pricing_catalog)
            except Exception as e:
                print(f"[bitapi] 价目表刷新失败: {e}", flush=True)
        try:
            from core import media_cache
            swept = await asyncio.to_thread(media_cache.sweep)
            if swept:
                print(f"[bitapi] 生成媒体清理: {swept} 个过期文件", flush=True)
        except Exception as e:
            print(f"[bitapi] 生成媒体清理失败: {e}", flush=True)
        try:
            from core import backup as _backup
            done = await asyncio.to_thread(_backup.run_if_due)
            if done:
                print(f"[bitapi] 库备份完成: {done['path']} "
                      f"({done['size']} 字节,清理旧份 {done['pruned']})", flush=True)
        except Exception as e:
            # 备份失败不能静默:这是「盘坏了才发现三个月没备份」那类事故的入口。
            # 这里打日志,告警插件订阅 backup.failed 事件另行通知。
            print(f"[bitapi] 库备份失败: {e}", flush=True)
            from core.hooks import emit
            emit("backup.failed", error=str(e))


app = FastAPI(title="bit-api", lifespan=lifespan)
app.include_router(portal_router)


@app.get("/oauth/community", include_in_schema=False)
def community_oauth_callback(request: Request, code: str = "", state: str = ""):
    return portal_community_callback(request, code=code, state=state)

_STREAM_END = object()


# ---- 鉴权 ----

def _check(authorization, expected):
    """/admin/* 号池端点的鉴权:管理密钥(脚本、注册 worker 上报)或管理员 JWT
    (管理台)。原先只认密钥,管理台要操作号池得另配一份 nginx Basic Auth 注入密钥 ——
    那份配置不在仓里,缺了它面板外壳就是公开的。认 JWT 之后管理台自己就能用。"""
    tok = (authorization or "").replace("Bearer ", "").strip()
    if tok == expected:
        return
    payload = auth.decode_session_token(tok) if tok else None
    if payload and payload.get("role") == "admin":
        from core.portal_state import USER_DB
        user = USER_DB.get_user(int(payload.get("sub") or 0))
        if (user and user.get("status") == "active" and user.get("role") == "admin"
                and int(payload.get("iat") or 0) >= int(user.get("password_changed_at") or 0)):
            return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _client_ip(request):
    """密钥 IP 白名单的取值。与登录限速共用一份实现(core/throttle.client_ip),
    两处各写一遍的话反代头的处理迟早分叉。"""
    return throttle.client_ip(request)


def _authorize_gateway(authorization, model, x_api_key=None, request=None):
    """网关热路径鉴权 + 计费预检。返回 (user, group) 或 (None, None)(master key 兜底)。

    - master key(config.API_KEY): 内部/兜底,跳过用户体系与限流。
    - 用户 sk- key: 查库解析 user+group → 校验 IP 白名单 → 校验模型白名单
      (分组 ∩ 密钥) → 按计费策略预检(含密钥额度)。
    超限抛 429,余额/密钥额度不足抛 402,无权限抛 403,未知 key 抛 401。
    """
    tok = (authorization or x_api_key or "").replace("Bearer ", "").strip()
    if tok == config.API_KEY:
        return None, None  # master key 兜底,不计费不限流
    resolved = BILLING.resolve_user_by_key(tok)
    if not resolved:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user, group = resolved
    _enforce_limits(user, group, model, api_key=user.get("_api_key"),
                    request=request)
    return user, group


def _enforce_limits(user, group, model, api_key=None, request=None):
    """IP 白名单 → 模型白名单 → 计费预检。网关与在线体验共用同一套判定。

    体验页走 JWT、没有 sk- key,api_key 传 None:IP 与密钥额度这两项本就是
    密钥级收紧项,没有密钥就没有额外收紧;分组白名单、RPM、余额/额度照旧生效。
    抽出来是为了别让两条入口各写一套 —— 一旦分叉,「广场能点、体验能发、
    网关却 403」这类不一致就会长出来,而且只有用户会先撞上。
    """
    if not BILLING.check_key_ip(api_key, _client_ip(request)):
        raise HTTPException(status_code=403,
                            detail="client IP not allowed for this key")
    if not BILLING.check_model_allowed(group, model, api_key):
        raise HTTPException(status_code=403,
                            detail=f"model not allowed for your plan: {model}")
    try:
        BILLING.precheck(user, group)
    except RateLimitError as e:
        raise HTTPException(status_code=429,
                            detail={"code": e.code, "message": e.detail}) from e
    except KeyQuotaError as e:
        raise HTTPException(status_code=402,
                            detail={"code": e.code, "message": e.detail,
                                    "used": e.used, "quota": e.quota}) from e
    except InsufficientBalanceError as e:
        raise HTTPException(status_code=402,
                            detail={"code": e.code, "message": e.detail,
                                    "balance": e.balance}) from e


# ---- OpenAI 端点 ----

def _cid():
    return "chatcmpl-" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _mid():
    return "msg_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _canonical_model(model):
    """允许 Claude CLI 使用 mp-fable-5[1M] 这样的展示名。"""
    models = model_to_channel()
    if model in models:
        return model
    if isinstance(model, str) and model.endswith("]") and "[" in model:
        base = model.rsplit("[", 1)[0]
        if base in models:
            return base
    return model


# 需要把 function tools 编成文本协议的上游:纯文本渠道不认 tool_calls,只能把
# 函数签名写进 prompt、再从回复里解析调用。仓库自带的渠道都原生支持 tools,
# 所以这里是空的;接一个纯文本上游时把渠道名加进来。
_VIRTUAL_TOOL_CHANNELS = set()
_TOOL_BEGIN = "<<<BITAPI_TOOLS>>>"
_TOOL_END = "<<<END_BITAPI_TOOLS>>>"


def _virtual_tooling_requested(channel, body):
    return (channel in _VIRTUAL_TOOL_CHANNELS
            and bool(body.get("tools"))
            and body.get("tool_choice") != "none")


def _prepare_virtual_tool_request(channel, body):
    """Turn declared functions into a text protocol for text-only upstreams."""
    if not _virtual_tooling_requested(channel, body):
        return body, None

    definitions = []
    allowed = set()
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            continue
        function = tool.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        allowed.add(name)
        definitions.append({
            "name": name,
            "description": function.get("description", ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        })

    if not definitions:
        return body, None

    marker = "[[BITAPI-CALL-" + secrets.token_hex(6) + "]]"
    directive = (
        "You may call only the tools declared below. If a tool is needed, output the "
        f"marker {marker} followed immediately by { _TOOL_BEGIN } and a JSON array of "
        "objects with exactly name and params fields, then " + _TOOL_END + ". "
        "params must be a JSON object. Do not describe this protocol or invent tool names.\n\n"
        "Declared tools:\n" + json.dumps(definitions, ensure_ascii=False)
    )
    choice = body.get("tool_choice")
    if choice == "required":
        directive += "\nYou must call at least one declared tool in this response."
    elif isinstance(choice, dict):
        forced = ((choice.get("function") or {}).get("name"))
        if forced in allowed:
            directive += f"\nYou must call the declared tool {forced}."
    if body.get("parallel_tool_calls") is False:
        directive += "\nCall no more than one tool."

    routed = dict(body)
    routed["messages"] = [{"role": "system", "content": directive}, *(body.get("messages") or [])]
    for field in ("tools", "tool_choice", "parallel_tool_calls"):
        routed.pop(field, None)
    return routed, {"marker": marker, "allowed": allowed}


def _parse_virtual_tool_calls(content, virtual):
    """Accept only declared functions; malformed protocol text never becomes a call."""
    text = content or ""
    marker_at = text.find(virtual["marker"])
    if marker_at < 0:
        return text, []
    prefix = text[:marker_at].strip()
    remainder = text[marker_at + len(virtual["marker"]):]
    begin = remainder.find(_TOOL_BEGIN)
    end = remainder.find(_TOOL_END, begin + len(_TOOL_BEGIN)) if begin >= 0 else -1
    if begin < 0 or end < 0:
        return prefix, []
    try:
        items = json.loads(remainder[begin + len(_TOOL_BEGIN):end].strip())
    except json.JSONDecodeError:
        return prefix, []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return prefix, []

    calls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        params = item.get("params")
        if name not in virtual["allowed"] or not isinstance(params, dict):
            continue
        calls.append({
            "id": "call_" + secrets.token_hex(12),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)},
        })
    return prefix, calls


def _resolve_routes(body):
    """一次请求的候选渠道序列:(requested, effective, routed_body, [(channel, adapter), ...])。

    序列由 core/adapter.model_routes 按 priority / weight 排好;调用方从头试,
    没号 / 换遍号仍失败 / 上游限流就换下一个渠道。空序列 = 未知模型 → 404。"""
    requested = body.get("model", "")
    effective = _canonical_model(requested)
    routes = [(name, get_adapter(name)) for name in model_routes(effective)]
    routes = [(n, ad) for n, ad in routes if ad is not None]
    if not routes:
        raise HTTPException(status_code=404, detail=f"unknown model: {requested}")
    routed = dict(body)
    routed["model"] = effective
    return requested, effective, routed, routes


def _resolve_request(body):
    """首选渠道那一条。给只认单渠道的路径用(生成型 / 虚拟工具判定)。"""
    requested, effective, routed, routes = _resolve_routes(body)
    channel, adapter = routes[0]
    return requested, effective, channel, adapter, routed


# 换渠道的信号:没号(503)、换遍号仍失败(502)、上游限流(429)。
# 4xx 里的其它(400 请求体不对、403 无权)是这次请求本身的问题,换渠道也一样。
_FAILOVER_STATUS = (502, 503, 429)


@app.get("/v1/models")
def list_models(authorization: str = Header(None), x_api_key: str = Header(None, alias="x-api-key")):
    tok = (authorization or x_api_key or "").replace("Bearer ", "").strip()
    group = None
    api_key = None
    if tok != config.API_KEY:
        resolved = BILLING.resolve_user_by_key(tok)
        if not resolved:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, group = resolved
        api_key = user.get("_api_key")
    data = []
    now = int(time.time())
    # enabled_adapters():下线的渠道不进清单。master key 也一样 —— 下线是站点级
    # 动作,留个能调但看不见的后门只会让「为什么它还在扣额度」查不出来。
    # 同一模型挂多个渠道时只列一次,owned_by 是首选渠道 —— 清单是给客户端选模型的,
    # 不是渠道表。
    seen = set()
    primary = model_to_channel()
    for name, ad in enabled_adapters().items():
        for m in ad.models:
            if m in seen:
                continue
            # 用户 key:分组白名单 ∩ 密钥白名单;master key:全部。
            # 这里必须和网关一致,否则客户端拉到的模型调用时会 403。
            if group is not None and not BILLING.check_model_allowed(group, m, api_key):
                continue
            seen.add(m)
            data.append({"id": m, "object": "model", "created": now,
                         "owned_by": primary.get(m, name)})
    return {"object": "list", "data": data}


def _nonstream_via(channel, adapter, routed):
    """在一个渠道上完成一次非流式请求。返回 (response, upstream_usage)。

    upstream_usage 只在上游真的给了 usage 时非空。响应体里的 usage 可能是估算值
    (合成响应),计量层不能拿它当真实值 —— 否则 token_source 记成 upstream 就撒谎了。
    失败以 HTTPException 抛出,由调用方决定换不换渠道。"""
    routed, virtual = _prepare_virtual_tool_request(channel, routed)
    model = routed["model"]
    messages = routed.get("messages") or []
    if adapter.proxy:
        try:
            resp = adapter.proxy_chat(routed, stream=False)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"proxy error: {e}") from e
        return resp, (resp.get("usage") if isinstance(resp, dict) else None)
    reply = _sync(adapter, channel, messages, model, routed)
    if reply.get("raw") and not virtual:
        raw = scrubmod.scrub_raw(reply["raw"])
        return raw, (raw.get("usage") if isinstance(raw, dict) else None)
    content = reply["content"]
    calls = []
    if virtual:
        content, calls = _parse_virtual_tool_calls(content, virtual)
    msg = {"role": "assistant", "content": scrubmod.scrub(content)}
    if reply.get("reasoning"):
        msg["reasoning_content"] = scrubmod.scrub(reply["reasoning"])
    if calls:
        msg["tool_calls"] = calls
    # 合成响应的 usage 不再是三个 0:上游给了真实 usage 就用它,没给就按计量层同一套
    # 估算 —— 用户拿着响应里的数能和账单对上。计量层自己会重算并如实标 estimate,
    # 这里的估算值不影响 token_source。
    real = reply.get("usage")
    real = real if isinstance(real, dict) and real.get("total_tokens") else None
    usage = real or metering.estimate_usage(messages, (reply.get("reasoning") or "") + (content or ""))
    return {
        "id": _cid(), "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": usage,
    }, real


def _openai_nonstream(body, served=None):
    """非流式:沿候选渠道序列依次试,第一个成功的作答。served 传个 dict 进来会被写上
    实际作答的 channel(计费要记真正花了钱的那个渠道,不是请求开始时猜的首选)与
    upstream_usage(上游真实 usage,没有就是 None —— 响应里的可能是估算值)。"""
    _, _, routed, routes = _resolve_routes(body)
    first_channel, first_adapter = routes[0]
    if getattr(first_adapter, "kind", None) == "generation":
        if served is not None:
            served["channel"] = first_channel
            served["upstream_usage"] = None
        return _gen_chat_completion(first_adapter, routed)   # 见「生成型渠道走 chat」
    last_exc = None
    for i, (channel, adapter) in enumerate(routes):
        try:
            resp, upstream_usage = _nonstream_via(channel, adapter, routed)
        except HTTPException as e:
            if e.status_code in _FAILOVER_STATUS and i < len(routes) - 1:
                print(f"[route] {routed['model']} 在 {channel} 失败({e.status_code}),"
                      f"换 {routes[i + 1][0]}", flush=True)
                last_exc = e
                continue
            raise
        if served is not None:
            served["channel"] = channel
            served["upstream_usage"] = upstream_usage
        return resp
    raise last_exc


def _openai_response_as_sse(response):
    """Encode a completed virtual response as OpenAI SSE for tool-capable clients."""
    cid = response.get("id") or _cid()
    created = response.get("created", int(time.time()))
    model = response.get("model", "")
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    def chunk(delta=None, finish=None, usage=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            payload["usage"] = usage
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    yield chunk({"role": "assistant"})
    if message.get("content"):
        yield chunk({"content": message["content"]})
    for index, call in enumerate(message.get("tool_calls") or []):
        yield chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                      "function": call.get("function") or {}}]})
    yield chunk({}, choice.get("finish_reason", "stop"), response.get("usage"))
    yield "data: [DONE]\n\n"


def _force_include_usage(body):
    """强开 stream_options.include_usage:客户端没要也打开,并原样透传给下游,
    让级联代理也能拿到 usage 计费(对齐 sub2api)。"""
    if not isinstance(body, dict) or not body.get("stream"):
        return body
    opts = dict(body.get("stream_options") or {})
    if opts.get("include_usage") is True:
        return body
    opts["include_usage"] = True
    out = dict(body)
    out["stream_options"] = opts
    return out


def _role_chunk(cid, created, model):
    return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk",
                                  "created": created, "model": model,
                                  "choices": [{"index": 0, "delta": {"role": "assistant"},
                                               "finish_reason": None}]}) + "\n\n"


def _openai_stream(body, usage_holder=None):
    """流式:沿候选渠道序列试。一个渠道「一帧都没吐就败了」(没号 / 换遍号仍失败)
    才换下一个 —— 已经开始往客户端写的流不能中途换上游,那会拼出一份两头缝的回答。

    三种内层的失败信号统一成「返回 False 且不 yield 任何东西」:
      _stream      普通型,自己拼帧;role 帧由这里先发(取号之前,首字节不等上游)
      _stream_sse  透传型,上游的帧原样过来,role 帧在上游第一帧里
      _proxy_stream 转发型,不经号池,不判失败,永远算作答
    usage_holder["channel"] 记下实际作答的渠道,结算按它记账。"""
    _, _, routed, routes = _resolve_routes(body)
    first_channel, first_adapter = routes[0]
    if getattr(first_adapter, "kind", None) == "generation":
        if usage_holder is not None:
            usage_holder["channel"] = first_channel
        yield from _gen_chat_stream(first_adapter, routed, usage_holder)
        return
    if _virtual_tooling_requested(first_channel, routed):
        # virtual tool 会先跑完非流式请求、再在本地合成 SSE。合成帧也必须经过
        # 嗅探，否则客户端收到完整回答，结算侧却因为看不到终止帧而记成 eof。
        served = {}
        for line in _openai_response_as_sse(_openai_nonstream(body, served=served)):
            if usage_holder is not None:
                _sniff_usage(line, usage_holder)
            yield line
        if usage_holder is not None:
            if served.get("channel"):
                usage_holder["channel"] = served["channel"]
            if not served.get("upstream_usage"):
                # 合成终止帧里的 usage 是给客户端看的估算值,不能冒充上游真实值;
                # 计量层自己会算同一个估算数并如实标 estimate
                usage_holder.pop("usage", None)
        return
    routed = _force_include_usage(routed)
    model = routed["model"]
    messages = routed.get("messages") or []
    cid, created = _cid(), int(time.time())
    role_sent = False
    for i, (channel, adapter) in enumerate(routes):
        if adapter.proxy:
            inner = _proxy_stream(adapter, routed, usage_holder)
        elif getattr(adapter, "streaming", False):
            inner = _stream_sse(adapter, channel, model, routed, usage_holder=usage_holder)
        else:
            if not role_sent:
                yield _role_chunk(cid, created, model)
                role_sent = True
            inner = _stream(adapter, channel, messages, model, routed,
                            usage_holder=usage_holder, cid=cid, created=created)
        ok = yield from inner
        if ok is not False:
            if usage_holder is not None:
                usage_holder["channel"] = channel
            return
        if i < len(routes) - 1:
            print(f"[route] {model} 在 {channel} 一帧未出,换 {routes[i + 1][0]}", flush=True)
    # 全部候选都没出一帧:给客户端一个能解析的收尾,而不是空流
    if not role_sent:
        yield _role_chunk(cid, created, model)
    payload = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
               "choices": [{"index": 0, "delta": {"content": f"[no available upstream for {model}]"},
                            "finish_reason": None}]}
    yield "data: " + json.dumps(payload) + "\n\n"
    payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    payload["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    yield "data: " + json.dumps(payload) + "\n\n"
    yield "data: [DONE]\n\n"


def _billed_stream(inner, usage_holder, settle):
    """流式计费生命周期包装器(不做额度预占,结算允许透支)。

    职责:
      - 记录首字延迟 _t0(由 _sniff_usage 填 frt_ms)
      - 判定 end_reason: done(见终止事件) / eof(上游断流未见终止事件)
        / client_gone(客户端断连) / scanner_error(上游异常)
      - **客户端断连后不 break**,继续排空上游把 usage 合并完
        (否则上游照常计费而平台漏记)
      - 无论何种结局都在最后调 settle(结算幂等由 request_id 保证)
    """
    usage_holder["_t0"] = time.time()
    end_reason = "eof"
    try:
        for chunk in inner:
            try:
                yield chunk
            except GeneratorExit:
                # 客户端断连:继续排空上游以完成计费,但不再向下游写
                end_reason = "client_gone"
                try:
                    for _rest in inner:
                        pass
                except Exception:
                    pass
                raise
        if usage_holder.get("terminal"):
            end_reason = "done"
    except GeneratorExit:
        raise
    except Exception as e:
        end_reason = "scanner_error"
        usage_holder["error"] = str(e)
        raise
    finally:
        usage_holder.pop("_t0", None)
        try:
            settle(end_reason)
        except Exception as e:
            print(f"[billing] 结算失败: {e}", flush=True)


@app.post("/v1/chat/completions")
def chat_completions(body: dict, request: Request, authorization: str = Header(None)):
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, _canonical_model(model),
                                     request=request)
    messages = body.get("messages") or []
    stream = bool(body.get("stream", False))
    if not messages:
        raise HTTPException(status_code=400, detail="messages required")
    cmodel = _canonical_model(model)
    channel = model_to_channel().get(cmodel)
    adapter = get_adapter(channel) if channel else None
    started = time.time()
    request_id = _request_id()

    if stream:
        return _stream_chat_billed(body, user, group, channel, adapter,
                                   messages, model, started, request_id)

    served = {}
    resp = _openai_nonstream(body, served=served)
    elapsed_ms = int((time.time() - started) * 1000)
    if user is not None:
        # 记实际作答的渠道:失败换渠道之后,钱是在后一个渠道花的。
        # upstream_usage 取 served 里的真实值,不取响应体 —— 响应体里的可能是给客户端
        # 看的估算值,计量层拿它当真实值会把 token_source 记成 upstream。
        channel = served.get("channel") or channel
        BILLING.record_usage(
            user, group, channel, model, adapter=get_adapter(channel) or adapter,
            upstream_usage=served.get("upstream_usage"),
            request_messages=messages,
            output_text=_extract_output_text(resp),
            stream=False,
            duration_ms=elapsed_ms,
            request_id=request_id, end_reason="done",
            # 非流协议在完整 JSON 返回前没有可见 token，首字即响应到达。
            first_token_ms=elapsed_ms)
    return resp


def _request_id():
    return "req_" + secrets.token_hex(12)


def _stream_chat_billed(body, user, group, channel, adapter, messages, model,
                        started, request_id):
    """流式对话 + 结算,网关与在线体验共用。

    两条入口只在「怎么认人」上不同(sk- key vs JWT),出字与记账必须是同一段
    代码:体验页的消费要和网关的落在同一张 usage_logs 上,用户在「使用记录」里
    看到的才是全部账,而不是「体验的那些不知道去哪了」。
    """
    usage_holder = {}

    def settle(end_reason):
        if user is None:
            return
        if usage_holder.get("nobill"):
            # 生成没出图(没号/上游失败)不能收钱 —— 生图走 chat 这条路以前
            # 正是「200 空流照扣一次 per_request」的地方。
            return
        # 记实际作答的渠道:失败换渠道之后,钱是在后一个渠道花的
        used = usage_holder.get("channel") or channel
        BILLING.record_usage(
            user, group, used, model, adapter=get_adapter(used) or adapter,
            upstream_usage=usage_holder.get("usage"),
            request_messages=messages,
            output_text=usage_holder.get("text"),
            stream=True,
            duration_ms=int((time.time() - started) * 1000),
            request_id=request_id, end_reason=end_reason,
            first_token_ms=usage_holder.get("frt_ms"))

    return StreamingResponse(
        _billed_stream(_openai_stream(body, usage_holder), usage_holder, settle),
        media_type="text/event-stream",
        headers={"X-Request-Id": request_id})


# ---- 在线体验(控制台内的对话试用) ----

@app.post("/api/playground/chat")
def playground_chat(body: dict, request: Request,
                    user=Depends(portal_current_user)):
    """控制台里的在线体验:JWT 鉴权,其余与 /v1/chat/completions 完全同路。

    不另开一套额度或免费池 —— 体验就是一次真实请求,扣自己的余额/额度,
    如实进「使用记录」。这样用户在体验里看到的价和真接进去的价是同一个,
    不会出现「试用挺便宜、接上去翻倍」。

    只收对话渠道:图/视频渠道走 /v1/images|videos/generations,
    塞进来只会从 adapter 里得到一个看不懂的错误,不如在门口说清楚。
    恒定流式:页面要逐字出,非流式那条路没有消费者。
    """
    from core.adapter import CAP_CHAT
    from core.portal_state import USER_DB

    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="messages required")
    model = _canonical_model(body.get("model", ""))
    channel = model_to_channel().get(model)
    adapter = get_adapter(channel) if channel else None
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown model: {model}")
    if not adapter.has(CAP_CHAT):
        raise HTTPException(
            status_code=400,
            detail=f"{model} 不是对话模型,不能在线体验(渠道 {channel})")

    group = USER_DB.effective_group(user)
    _enforce_limits(user, group, model, api_key=None, request=request)
    routed = dict(body, model=model, stream=True)
    return _stream_chat_billed(routed, user, group, channel, adapter,
                               messages, model, time.time(), _request_id())


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
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            part = _image_part(block)
            if part:
                parts.append(part)
                has_image = True
    if not has_image:
        return "".join(p["text"] for p in parts)
    return parts


def _anthropic_to_openai(body):
    messages = []
    system = _content_text(body.get("system"))
    if system:
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
            result = _content_text(block.get("content", "")) or json.dumps(block.get("content", ""))
            messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                             "content": result})
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        tools.append({"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }})
    out = {"model": body.get("model", ""), "messages": messages,
           "stream": bool(body.get("stream", False)), "max_tokens": body.get("max_tokens")}
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
    u = usage if isinstance(usage, dict) and usage.get("total_tokens") else None
    if u is None:
        return {"input_tokens": int(fallback_in), "output_tokens": int(fallback_out)}
    out = {"input_tokens": int(u.get("prompt_tokens") or 0),
           "output_tokens": int(u.get("completion_tokens") or 0)}
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or u.get("cache_read_input_tokens") or 0)
    if cached:
        out["cache_read_input_tokens"] = int(cached)
    if u.get("cache_creation_input_tokens"):
        out["cache_creation_input_tokens"] = int(u["cache_creation_input_tokens"])
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


@app.post("/v1/messages/count_tokens")
def messages_count_tokens(body: dict, request: Request, authorization: str = Header(None),
                          x_api_key: str = Header(None, alias="x-api-key")):
    """Anthropic 的 count_tokens:Claude Code 每轮都调它算上下文占用,没有这个端点
    客户端会一直报 404。用与计费同一套估算(tiktoken cl100k),不问上游、不计费、
    不占 RPM —— 它是每轮对话的伴随请求,占了 RPM 会把真实请求挤掉。
    鉴权仍要过(坏 key 401、模型不在白名单 403),否则这是个无鉴权的免费端点。"""
    model = _canonical_model(body.get("model", ""))
    tok = (authorization or x_api_key or "").replace("Bearer ", "").strip()
    if tok != config.API_KEY:
        resolved = BILLING.resolve_user_by_key(tok)
        if not resolved:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, group = resolved
        if not BILLING.check_model_allowed(group, model, user.get("_api_key")):
            raise HTTPException(status_code=403, detail=f"model not allowed for your plan: {model}")
    messages = _anthropic_to_openai(body).get("messages") or []
    n = metering.estimate_usage(messages, "")["prompt_tokens"]
    if body.get("tools"):
        n += metering.count_tokens(json.dumps(body["tools"], ensure_ascii=False))
    return {"input_tokens": n}


@app.post("/v1/messages")
def messages_api(body: dict, request: Request, authorization: str = Header(None), x_api_key: str = Header(None, alias="x-api-key")):
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, _canonical_model(model),
                                     x_api_key, request=request)
    if not body.get("messages"):
        raise HTTPException(status_code=400, detail="messages required")
    openai_body = _anthropic_to_openai(body)
    cmodel = _canonical_model(model)
    channel = model_to_channel().get(cmodel)
    adapter = get_adapter(channel) if channel else None
    req_messages = openai_body.get("messages") or []
    started = time.time()
    request_id = _request_id()
    if openai_body["stream"]:
        usage_holder = {}

        def settle(end_reason):
            if user is None:
                return
            used = usage_holder.get("channel") or channel
            BILLING.record_usage(
                user, group, used, model, adapter=get_adapter(used) or adapter,
                upstream_usage=usage_holder.get("usage"),
                request_messages=req_messages,
                output_text=usage_holder.get("text"),
                stream=True,
                duration_ms=int((time.time() - started) * 1000),
                request_id=request_id, end_reason=end_reason,
                first_token_ms=usage_holder.get("frt_ms"))

        return StreamingResponse(
            _billed_stream(
                _anthropic_stream(_openai_stream(openai_body, usage_holder), model,
                                  request_messages=req_messages),
                usage_holder, settle),
            media_type="text/event-stream",
            headers={"X-Request-Id": request_id})
    served = {}
    openai_resp = _openai_nonstream(openai_body, served=served)
    elapsed_ms = int((time.time() - started) * 1000)
    if user is not None:
        channel = served.get("channel") or channel
        BILLING.record_usage(
            user, group, channel, model, adapter=get_adapter(channel) or adapter,
            upstream_usage=served.get("upstream_usage"),
            request_messages=req_messages,
            output_text=_extract_output_text(openai_resp),
            stream=False,
            duration_ms=elapsed_ms,
            request_id=request_id, end_reason="done",
            first_token_ms=elapsed_ms)
    return _anthropic_message_from_openai(openai_resp, model, request_messages=req_messages)


def _norm(result):
    """adapter.chat 返回值归一 → (content, reasoning, raw, usage)。"""
    if isinstance(result, dict):
        return (result.get("content", ""), result.get("reasoning"), result.get("raw"),
                result.get("usage"))
    return result or "", None, None, None


def _upstream_failed(channel, model, error, kind="error"):
    """一次请求在换遍号之后仍然失败 —— 这不是某个号的问题,是渠道此刻整体不好。
    发事实,不决定怎么通知;告警插件按渠道去重后推给站长。"""
    from core.hooks import emit
    emit("upstream.failed", channel=channel, model=model, error=str(error or ""),
         kind=kind)


def _sync(adapter, channel, messages, model, body=None, max_switch=6):
    """非流式:取号 → chat,空回复/失败切号重试。返回 {content, reasoning, raw}。"""
    last_err = None
    last_kind = None
    for _ in range(max_switch):
        acct = POOL.get_valid_account(channel, adapter)
        if not acct:
            raise HTTPException(status_code=503, detail=f"no available account for {channel}")
        try:
            content, reasoning, raw, usage = _norm(
                adapter.chat(acct, messages, stream=False, model=model, body=body))
        except Exception as e:
            last_err = str(e)
            last_kind = POOL.mark_failure(
                acct["id"], e, dead=getattr(adapter, "exhausted_is_dead", False),
                adapter=adapter)
            continue
        if content or raw:
            return {"content": content, "reasoning": reasoning, "raw": raw, "usage": usage}
        POOL.mark_exhausted(acct["id"], dead=getattr(adapter, "exhausted_is_dead", False),
                            adapter=adapter)  # 空回复,换号
    # 全是上游限流时如实回 429:客户端 SDK 见 429 会自己退避重试,回 502 只会让它
    # 当成我们坏了。号一个都没罚,上游一松就恢复。
    _upstream_failed(channel, model, last_err,
                     "ratelimit" if last_kind == "ratelimit" else "error")
    if last_kind == "ratelimit":
        raise HTTPException(status_code=429,
                            detail=f"upstream rate limited: {last_err}")
    raise HTTPException(status_code=502, detail=f"all retries failed: {last_err}")


def _sniff_usage(line, usage_holder):
    """从一条 OpenAI SSE 行里嗅探计费所需信息(边转发边嗅,不缓存整流)。

      usage_holder["usage"]     : 上游真实 usage。**重复发送时取最近一次**,不累加
      usage_holder["text"]      : 累积的 delta.content 文本(估算 output 用)
      usage_holder["terminal"]  : 是否见到终止事件([DONE] 或 finish_reason)
      usage_holder["frt_ms"]    : 首个内容 token 的延迟(ms)
    """
    if not isinstance(line, str) or not line.startswith("data:"):
        return
    payload = line[5:].strip()
    if not payload:
        return
    if payload == "[DONE]":
        usage_holder["terminal"] = True
        return
    try:
        o = json.loads(payload)
    except Exception:
        return
    if not isinstance(o, dict):
        return
    if o.get("usage"):
        usage_holder["usage"] = o["usage"]   # 覆盖式:取最近一次
    for ch in o.get("choices") or []:
        if ch.get("finish_reason"):
            usage_holder["terminal"] = True
        delta = ch.get("delta") or {}
        c = delta.get("content")
        if isinstance(c, str) and c:
            if "frt_ms" not in usage_holder and usage_holder.get("_t0"):
                usage_holder["frt_ms"] = int(
                    (time.time() - usage_holder["_t0"]) * 1000)
            usage_holder["text"] = usage_holder.get("text", "") + c


def _scrub_sse(line):
    """洗一条 OpenAI SSE 行里的 delta.content / reasoning_content(navos 真流式用)。"""
    if not scrubmod._ON or not isinstance(line, str) or not line.startswith("data:"):
        return line
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return line
    try:
        o = json.loads(payload)
        for ch in o.get("choices") or []:
            d = ch.get("delta") or {}
            for k in ("content", "reasoning_content"):
                if isinstance(d.get(k), str):
                    d[k] = scrubmod.scrub(d[k], tidy=False)
        return "data: %s\n\n" % json.dumps(o)
    except Exception:
        return line


def _stream_sse(adapter, channel, model, body, max_switch=6, usage_holder=None):
    """流式(透传型 adapter):取号 → stream_chat 原样转上游 SSE,洗词后转发。

    返回 True = 至少转出了一帧(流已交给这个渠道);False = 一帧没出(没号 / 换遍号
    仍失败),且什么都没 yield —— 调用方据此换下一个渠道。"""
    last_err = None
    for _ in range(max_switch):
        acct = POOL.get_valid_account(channel, adapter)
        if not acct:
            return False
        q = queue.Queue()
        err = {}

        def worker(acct=acct, q=q, err=err):
            try:
                adapter.stream_chat(acct, model, body, on_sse=lambda l: q.put(l))
            except Exception as e:
                err["e"] = e
            finally:
                q.put(_STREAM_END)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        got = False
        while True:
            item = q.get()
            if item is _STREAM_END:
                break
            got = True
            if usage_holder is not None:
                _sniff_usage(item, usage_holder)
            yield _scrub_sse(item)
        th.join(timeout=1)
        if got:
            return True
        # 一个 SSE 都没吐:上游异常时按异常性质落状态(超时/5xx 只冷却),
        # 无异常的空流才算额度耗尽。
        dead = getattr(adapter, "exhausted_is_dead", False)
        if err.get("e") is not None:
            print(f"[stream] {channel} 上游异常: {err['e']}", flush=True)
            POOL.mark_failure(acct["id"], err["e"], dead=dead, adapter=adapter)
            last_err = err["e"]
        else:
            POOL.mark_exhausted(acct["id"], dead=dead, adapter=adapter)
            last_err = "empty stream"
    _upstream_failed(channel, model, last_err)
    return False


def _stream(adapter, channel, messages, model, body=None, max_switch=6, usage_holder=None,
            cid=None, created=None):
    """流式(普通型 adapter):取号 → chat,on_token 逐段出字,自己拼 OpenAI SSE。

    role 帧不在这里发:_openai_stream 在取号之前先发了(首字节不等上游,也不会因为
    换渠道重发)。cid / created 从那边传进来,一条流里所有帧同一个 id。
    返回 True = 出过字;False = 一帧没出(没号 / 换遍号仍失败),且什么都没 yield。"""
    if getattr(adapter, "streaming", False):
        return (yield from _stream_sse(adapter, channel, model, body, max_switch, usage_holder))
    cid = cid or _cid()
    created = created or int(time.time())

    def chunk(delta=None, finish=None, usage=None):
        o = {"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": model,
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            o["usage"] = usage
        return f"data: {json.dumps(o)}\n\n"

    def tracked(line):
        if usage_holder is not None:
            _sniff_usage(line, usage_holder)
        return line

    completed = False
    final_usage = None
    last_err = None
    out_parts = []      # 客户端实际看到的文字(洗词后),给终止帧的估算 usage 用

    def say(delta):
        for k in ("content", "reasoning_content"):
            if isinstance(delta.get(k), str):
                out_parts.append(delta[k])
        return tracked(chunk(delta=delta))

    for _ in range(max_switch):
        acct = POOL.get_valid_account(channel, adapter)
        if not acct:
            return False
        q = queue.Queue()
        result = {"raw": None, "content": None, "reasoning": None,
                  "usage": None, "err": None}

        def worker(acct=acct, q=q, result=result):
            try:
                r = adapter.chat(acct, messages, stream=True, model=model, body=body,
                                 on_token=lambda t, k="content": q.put((k, t)))
                if isinstance(r, dict):
                    result["raw"] = r.get("raw")
                    result["content"] = r.get("content")
                    result["reasoning"] = r.get("reasoning")
                    result["usage"] = r.get("usage")
            except Exception as e:
                result["err"] = e
            finally:
                q.put(_STREAM_END)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        got = False
        emitted = {"content": False, "reasoning": False}
        # 流式洗词:content/reasoning 各一个 Scrubber(末尾留缓冲防品牌词跨 chunk 被切)
        scr = {"content": scrubmod.Scrubber(), "reasoning": scrubmod.Scrubber()}
        while True:
            item = q.get()
            if item is _STREAM_END:
                break
            kind, tok = item
            got = True
            if kind in emitted:
                emitted[kind] = True
            out = scr.get(kind, scr["content"]).feed(tok)
            if out:
                yield say({"reasoning_content" if kind == "reasoning" else "content": out})
        th.join(timeout=1)
        # flush 各 Scrubber 尾部缓冲
        for kind, sc in scr.items():
            rest = sc.flush()
            if rest:
                yield say({"reasoning_content" if kind == "reasoning" else "content": rest})

        # navos:无 on_token 流式,把完整 raw 转成 SSE(含 tool_calls)
        raw = result["raw"]
        if not got and raw:
            m = raw.get("choices", [{}])[0].get("message", {})
            if m.get("content"):
                yield say({"content": scrubmod.scrub(m["content"])}); got = True
            if m.get("tool_calls"):
                yield tracked(chunk(delta={"tool_calls": m["tool_calls"]})); got = True
            if isinstance(raw.get("usage"), dict) and result["usage"] is None:
                result["usage"] = raw["usage"]

        if result["reasoning"] and not emitted["reasoning"]:
            yield say({"reasoning_content": scrubmod.scrub(result["reasoning"])}); got = True
        if result["content"] and not emitted["content"]:
            yield say({"content": scrubmod.scrub(result["content"])}); got = True

        if got:
            # 已吐过部分内容后又抛错仍是截断流，不能被最后的协议收尾帧洗成成功。
            if result["err"] is None:
                completed = True
                final_usage = result["usage"]
            break
        # 什么都没吐出来:上游异常按性质落状态,无异常的空回复才算额度耗尽。
        dead = getattr(adapter, "exhausted_is_dead", False)
        if result["err"] is not None:
            print(f"[stream] {channel} 上游异常: {result['err']}", flush=True)
            POOL.mark_failure(acct["id"], result["err"], dead=dead, adapter=adapter)
            last_err = result["err"]
        else:
            POOL.mark_exhausted(acct["id"], dead=dead, adapter=adapter)
            last_err = "empty reply"
    else:
        # for 循环没被 break:换遍了号一个字都没出来。有号但全败才算上游失败,
        # 没号那条已经由 pool.empty 报过。一帧没出,交给调用方换渠道。
        _upstream_failed(channel, model, last_err)
        return False

    # 终止帧带 usage:上游给了真实值就用它;没给就按计量层同一套估算,让客户端看到
    # 的数和账单一致(Claude Code 之类靠这个显示花费与上下文占用,三个 0 等于告诉
    # 用户「没花钱」)。只有真实值才交给嗅探 —— 估算值进了嗅探会被记成上游真实值,
    # token_source 就撒谎了;计量层自己会算同一个估算数。
    # 只有适配器确实正常完成时才把这帧交给嗅探;空回复/半途异常仍应落 eof。
    real = final_usage if isinstance(final_usage, dict) and final_usage.get("total_tokens") else None
    shown = real or metering.estimate_usage(messages, "".join(out_parts))
    if completed:
        tracked(chunk(delta={}, finish="stop", usage=real))
    yield chunk(delta={}, finish="stop", usage=shown)
    yield "data: [DONE]\n\n"
    return True


def _proxy_stream(adapter, body, usage_holder=None):
    """转发型流式:把上游引擎的 SSE 原样透传(不改写,保留 tool_calls)。"""
    q = queue.Queue()

    def worker():
        try:
            adapter.proxy_chat(body, stream=True, on_sse=lambda line: q.put(line))
        except Exception as e:
            q.put(f"data: {json.dumps({'error': {'message': str(e)}})}\n\n")
        finally:
            q.put(_STREAM_END)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = q.get()
        if item is _STREAM_END:
            break
        if usage_holder is not None:
            _sniff_usage(item, usage_holder)
        yield item


# ---- OpenAI Responses API 适配 ----
#
# Codex CLI 与新版 OpenAI SDK 默认走 /v1/responses。这里把它翻译成 chat/completions
# 执行(与 /v1/messages 同一条路:归一到 OpenAI 执行层再翻回去),计费、路由、
# 换渠道全部复用。无状态:不存 response,previous_response_id 明确拒绝 ——
# 客户端每轮带全量 input(Codex 的 store=false 模式正是如此)。

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
        return {"type": "text", "text": part["text"]}
    if kind == "input_image":
        url = part.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if url:
            out = {"type": "image_url", "image_url": {"url": url}}
            if part.get("detail"):
                out["image_url"]["detail"] = part["detail"]
            return out
    return None


def _flatten_parts(parts):
    """全是文字就折成字符串(逆向渠道只认字符串),带图片才留数组。"""
    if all(p.get("type") == "text" for p in parts):
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
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                             "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)})
            continue
        if kind != "message":
            continue          # reasoning / item_reference 等历史项对上游没有意义
        role = item.get("role") or "user"
        if role == "developer":
            role = "system"
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        parts = [p for p in (_part_to_chat(x) for x in (content or [])) if p]
        if parts:
            messages.append({"role": role, "content": _flatten_parts(parts)})
    flush_calls()

    out = {"model": body.get("model", ""), "messages": messages,
           "stream": bool(body.get("stream", False))}
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls"):
        if body.get(k) is not None:
            out[k] = body[k]
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
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    reasoning = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    return {"input_tokens": it, "output_tokens": ot, "total_tokens": it + ot,
            "input_tokens_details": {"cached_tokens": cached},
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


@app.post("/v1/responses")
def responses_api(body: dict, request: Request, authorization: str = Header(None)):
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, _canonical_model(model), request=request)
    openai_body = _responses_to_openai(body)
    if not openai_body["messages"]:
        raise HTTPException(status_code=400, detail="input required")
    cmodel = _canonical_model(model)
    channel = model_to_channel().get(cmodel)
    adapter = get_adapter(channel) if channel else None
    req_messages = openai_body["messages"]
    started = time.time()
    request_id = _request_id()
    if openai_body["stream"]:
        usage_holder = {}

        def settle(end_reason):
            if user is None:
                return
            used = usage_holder.get("channel") or channel
            BILLING.record_usage(
                user, group, used, model, adapter=get_adapter(used) or adapter,
                upstream_usage=usage_holder.get("usage"),
                request_messages=req_messages, output_text=usage_holder.get("text"),
                stream=True, duration_ms=int((time.time() - started) * 1000),
                request_id=request_id, end_reason=end_reason,
                first_token_ms=usage_holder.get("frt_ms"))

        return StreamingResponse(
            _billed_stream(
                _responses_stream(_openai_stream(openai_body, usage_holder), body, model),
                usage_holder, settle),
            media_type="text/event-stream",
            headers={"X-Request-Id": request_id})
    served = {}
    chat = _openai_nonstream(openai_body, served=served)
    elapsed_ms = int((time.time() - started) * 1000)
    if user is not None:
        channel = served.get("channel") or channel
        BILLING.record_usage(
            user, group, channel, model, adapter=get_adapter(channel) or adapter,
            upstream_usage=served.get("upstream_usage"),
            request_messages=req_messages, output_text=_extract_output_text(chat),
            stream=False, duration_ms=elapsed_ms,
            request_id=request_id, end_reason="done", first_token_ms=elapsed_ms)
    return _openai_to_response(chat, body, model)


# ---- 向量 ----

def _embed_via(channel, adapter, body, max_switch=6):
    """在一个渠道上做一次 embeddings:取号 → adapter.embeddings,失败切号。
    与 _sync 同一套失败口径:没号 503、换遍号仍失败 502 / 429。"""
    if not callable(getattr(adapter, "embeddings", None)):
        raise HTTPException(status_code=400,
                            detail=f"channel {channel} does not support embeddings")
    last_err, last_kind = None, None
    for _ in range(max_switch):
        acct = POOL.get_valid_account(channel, adapter)
        if not acct:
            raise HTTPException(status_code=503, detail=f"no available account for {channel}")
        try:
            return adapter.embeddings(acct, body)
        except Exception as e:
            last_err = str(e)
            last_kind = POOL.mark_failure(
                acct["id"], e, dead=getattr(adapter, "exhausted_is_dead", False),
                adapter=adapter)
    _upstream_failed(channel, body.get("model"), last_err,
                     "ratelimit" if last_kind == "ratelimit" else "error")
    if last_kind == "ratelimit":
        raise HTTPException(status_code=429, detail=f"upstream rate limited: {last_err}")
    raise HTTPException(status_code=502, detail=f"all retries failed: {last_err}")


def _embedding_input_text(inp):
    """embeddings 的 input 可能是字符串、字符串数组、token 数组;估算只认文字。"""
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        return "\n".join(x for x in inp if isinstance(x, str))
    return ""


@app.post("/v1/embeddings")
def embeddings_api(body: dict, request: Request, authorization: str = Header(None)):
    """OpenAI 兼容 embeddings。按模型名路由到声明了它的渠道(只有 OpenAI 兼容型
    渠道实现了 embeddings),失败换渠道,计费走上游 usage(只有输入 token)。"""
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, _canonical_model(model), request=request)
    if body.get("input") in (None, "", []):
        raise HTTPException(status_code=400, detail="input required")
    _, _, routed, routes = _resolve_routes(body)
    started = time.time()
    last_exc = None
    for i, (channel, adapter) in enumerate(routes):
        try:
            resp = _embed_via(channel, adapter, routed)
        except HTTPException as e:
            if e.status_code in _FAILOVER_STATUS and i < len(routes) - 1:
                last_exc = e
                continue
            raise
        if user is not None:
            BILLING.record_usage(
                user, group, channel, model, adapter=adapter,
                upstream_usage=resp.get("usage") if isinstance(resp, dict) else None,
                request_messages=[{"role": "user",
                                   "content": _embedding_input_text(body.get("input"))}],
                output_text="", stream=False,
                duration_ms=int((time.time() - started) * 1000),
                request_id=_request_id(), end_reason="done")
        return resp
    raise last_exc


# ---- 管理 API ----

@app.get("/admin/stats")
def admin_stats(authorization: str = Header(None)):
    _check(authorization, config.ADMIN_KEY)
    # 声明了 sync_pool 的渠道以外部号源为准:出数前先对账一遍,否则面板会一直
    # 留着已经被删掉的外部账号。同样不写死渠道名。
    for _name, _ad in all_adapters().items():
        if not hasattr(_ad, "sync_pool"):
            continue
        try:
            _ad.sync_pool(DB)
            if hasattr(_ad, "sync_runtime"):
                _ad.sync_runtime(DB)
        except Exception as e:
            print(f"[bitapi] {_name} 状态同步失败: {e}", flush=True)
    channels = {}
    for name, ad in all_adapters().items():
        channels[name] = {"models": ad.models, "capabilities": ad.capabilities,
                          "columns": ad.columns}
    return {"channels": channels, "pool": DB.stats()}


@app.get("/admin/accounts")
def admin_accounts(channel: str = None, status: str = None,
                   offset: int = 0, limit: int = 50,
                   authorization: str = Header(None)):
    _check(authorization, config.ADMIN_KEY)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    adapter = get_adapter(channel) if channel else None
    if adapter and hasattr(adapter, "sync_runtime"):
        adapter.sync_runtime(DB)
    accts = DB.list_accounts(channel=channel, status=status, limit=limit, offset=offset)
    if adapter and hasattr(adapter, "decorate_accounts"):
        adapter.decorate_accounts(accts)
    # 脱敏:不返回 secret/token 明文
    for a in accts:
        a.pop("secret", None)
        if a.get("token"):
            a["token"] = a["token"][:12] + "..."
    total = DB.count_accounts(channel=channel, status=status)
    return {"accounts": accts, "count": len(accts),
            "total": total, "offset": offset, "limit": limit}


@app.post("/admin/accounts")
async def report_account(request: Request, authorization: str = Header(None)):
    """本地注册 worker 上报账号入库。body: {channel, identity, secret, meta, token, token_exp}"""
    _check(authorization, config.ADMIN_KEY)
    body = await request.json()
    channel = body.get("channel")
    identity = body.get("identity")
    if not channel or not identity:
        raise HTTPException(status_code=400, detail="channel & identity required")
    if not get_adapter(channel):
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    # 已有账号且服务器有 usage 快照时,保留服务器记账数据(避免注册端旧快照冲掉运行时消耗)。
    # 仅新增账号或服务器无快照时,接受客户端 meta。
    existing = DB.get_by_identity(channel, identity)
    have_usage = bool(((existing or {}).get("meta") or {}).get("usage"))
    meta = body.get("meta") if not have_usage else None
    acct_id = DB.upsert_account(channel, identity,
                                secret=body.get("secret"), meta=meta,
                                status=dbmod.ST_ACTIVE)
    if body.get("token"):
        # 已有 usage 快照的账号,保留服务器记账的 meta,但 balance 统一从 usage 快照重算:
        # limit - tokens。这样注册号(有快照但 balance=None)也能参与号池排序,不会被排到最后。
        upd = {"token": body["token"], "token_exp": body.get("token_exp", 0),
               "status": dbmod.ST_ACTIVE}
        # 服务器已有快照优先,避免客户端传来的旧快照冲掉运行时记账
        if not have_usage:
            upd["balance"] = body.get("balance")
        DB.update_account(acct_id, **upd)
    return {"ok": True, "id": acct_id}


@app.post("/admin/import-keys")
async def admin_import_keys(request: Request, authorization: str = Header(None)):
    """批量导入 API key 进 key 池型渠道。
    body: {channel, keys:[...]} 或 {channel, key:"多行/逗号分隔"}。
    每个 key 一个账号,identity 由 key 派生(脱敏),secret.api_key 存 key。
    已存在(同 identity)则跳过。返回 imported/skipped 计数。
    """
    _check(authorization, config.ADMIN_KEY)
    body = await request.json()
    channel = body.get("channel")
    ad = get_adapter(channel)
    if not ad:
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    keys = parse_keys(body)
    if not keys:
        raise HTTPException(status_code=400, detail="keys required")
    # identity 怎么派生由渠道自己说(core/pool_state.import_keys)—— 每加一个 key 池
    # 渠道都回来改 if/elif,就违背了 docs/adapters.md 承诺的「加渠道不需要改核心代码」。
    imported, skipped = import_keys(ad, keys)
    return {"ok": True, "channel": channel, "imported": imported,
            "skipped": skipped, "total": len(keys)}


@app.post("/admin/scan")
async def admin_scan(channel: str = None, authorization: str = Header(None)):
    """手动触发一轮巡检(刷 token / 查余额 / 测活)。channel=None 全渠道,否则仅该渠道。"""
    _check(authorization, config.ADMIN_KEY)
    if channel and not get_adapter(channel):
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    # 注:scan_once 内部已对文件型号池(grok)做增量同步 + 跳过 per-account 巡检
    tally = await scan_once(DB, config.SCAN_CONCURRENCY,
                            config.REFRESH_MARGIN, config.MIN_BALANCE, channel=channel)
    return {"ok": True, "result": tally}


@app.post("/admin/purge")
async def admin_purge(channel: str = None, authorization: str = Header(None)):
    """清除失效账号。指定 channel(面板手动点清除)时: dead + exhausted 一起清;
    全局不指定时: 清所有 dead,以及声明了 exhausted_is_dead 的渠道的 exhausted。"""
    _check(authorization, config.ADMIN_KEY)
    if channel and not get_adapter(channel):
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    if channel:
        # 面板手动清理:dead + exhausted 都算失效
        targets = (DB.list_accounts(channel=channel, status=dbmod.ST_DEAD, limit=100000)
                   + DB.list_accounts(channel=channel, status=dbmod.ST_EXHAUSTED,
                                      limit=100000))
    else:
        targets = DB.list_accounts(status=dbmod.ST_DEAD, limit=100000)
        # 「额度耗尽算不算永久失效」由渠道自己用 exhausted_is_dead 声明,这里不写死渠道名 ——
        # 写死了的话,后来新增一个同样声明的渠道,全局清理会一直漏掉它。
        for name, ad in all_adapters().items():
            if getattr(ad, "exhausted_is_dead", False):
                targets += DB.list_accounts(channel=name, status=dbmod.ST_EXHAUSTED,
                                            limit=100000)
    deleted = 0
    for account in targets:
        adapter = get_adapter(account["channel"])
        if adapter and hasattr(adapter, "delete_account"):
            adapter.delete_account(DB, account["identity"])
        else:
            DB.delete_account(account["channel"], account["identity"])
        deleted += 1
    return {"ok": True, "deleted": deleted, "channel": channel or "all"}


@app.post("/admin/channel/delete")
async def admin_delete_channel(channel: str, authorization: str = Header(None)):
    """删除某渠道全部账号(清空该渠道号池,不分状态)。adapter 仍在代码里注册,卡片会显示 0。"""
    _check(authorization, config.ADMIN_KEY)
    if not channel or not get_adapter(channel):
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    deleted = DB.delete_channel(channel)
    return {"ok": True, "deleted": deleted, "channel": channel}


@app.post("/admin/account/delete")
async def admin_delete_account(channel: str, identity: str, authorization: str = Header(None)):
    """删除单个账号。adapter 若有 delete_account 钩子(如 grok 删 auth 文件)则走它。"""
    _check(authorization, config.ADMIN_KEY)
    ad = get_adapter(channel)
    if not ad:
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    if hasattr(ad, "delete_account"):
        ad.delete_account(DB, identity)
    else:
        DB.delete_account(channel, identity)
    return {"ok": True, "channel": channel, "identity": identity}


@app.post("/admin/grok/import")
async def admin_grok_import(request: Request, authorization: str = Header(None)):
    """导入 grok OAuth 号(单个 dict / 列表 / {accounts:[...]}) → 写 auths 文件 + 入库。"""
    _check(authorization, config.ADMIN_KEY)
    ad = get_adapter("grok")
    if not ad:
        raise HTTPException(status_code=400, detail="grok adapter not loaded")
    body = await request.json()
    if isinstance(body, dict) and "accounts" in body:
        items = body["accounts"]
    elif isinstance(body, list):
        items = body
    else:
        items = [body]
    imported, skipped, errs = [], [], []
    for it in items:
        try:
            action, email = ad.import_account(DB, it)
            (imported if action == "imported" else skipped).append(email)
        except Exception as e:
            errs.append(str(e))
    return {"ok": True, "imported": imported, "skipped": skipped,
            "count": len(imported), "skipped_count": len(skipped), "errors": errs}


GEN_MAX_N = 4        # 一次生成的张数上限


def _gen_count(body):
    """本次要出几张。既是 adapter 的份数,也是 per_request 计费的份数 —— n 张只收
    一份的话,把 n 开大就等于白拿。上限挡住「一次请求抽干整个号池额度」。"""
    try:
        n = int(body.get("n", 1) or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, GEN_MAX_N))


# 生成渠道的请求限速。与 groups.rpm_limit 是两回事:那个按用户、护的是本站成本;
# 这个按渠道、全站合计,护的是**上游账号** —— 高频调用会被上游不可逆封号,
# 而这类账号往往没有密码可重登,废了就回不来。
# adapter 用 max_rpm 声明(0 / 缺省 = 不限)。
_gen_rpm = {}
_gen_rpm_lock = threading.Lock()


def _gen_rate_limit(channel, adapter):
    """按渠道限速,超了抛 429。"""
    rpm = int(getattr(adapter, "max_rpm", 0) or 0)
    if rpm <= 0:
        return
    with _gen_rpm_lock:
        win = _gen_rpm.get(channel)
        if win is None or win.limit != rpm:
            win = _gen_rpm[channel] = throttle.SlidingWindow(rpm, 60)
    if win.hit("all"):
        raise HTTPException(status_code=429, detail=(
            f"rate limit: {channel} 全站最多 {rpm} 次/分钟,"
            f"请 {win.retry_after('all')}s 后重试"))


def _rehost(urls, adapter, want_b64):
    """上游 CDN URL 列表 → OpenAI 风格的 data[]。

    上游返回的链接对调用方不可靠(带 referer 防盗链、内网地址、很快过期)——
    一律把字节抓到自家 /media/gen 下再给出去。
    want_b64=True 则内联 base64(response_format=b64_json,不落链接依赖)。

    抓取失败**不静默**:图已经出了、钱也扣了,回原始 URL 至少让调用方还有一线可取,
    而不是丢一个空 data。"""
    from core import media_cache
    referer = getattr(adapter, "media_referer", None)
    out = []
    for u in urls:
        try:
            name, path, _mime = media_cache.fetch_and_store(u, referer=referer)
        except Exception as e:
            print(f"[bitapi] media 抓取失败,回退原始 URL: {u} ({e})", flush=True)
            out.append({"url": u})
            continue
        if want_b64:
            import base64
            with open(path, "rb") as f:
                out.append({"b64_json": base64.b64encode(f.read()).decode("ascii")})
        else:
            out.append({"url": media_cache.public_url(name)})
    return out


def _generate(body, want_kind, max_switch=6):
    """图像/视频生成:取号 → adapter.generate,失败切号。"""
    model = body.get("model")
    channel = model_to_channel().get(model)
    adapter = get_adapter(channel) if channel else None
    if not adapter or getattr(adapter, "kind", None) != "generation":
        raise HTTPException(status_code=404, detail=f"unknown generation model: {model}")
    if adapter.model_kind(model) != want_kind:
        raise HTTPException(status_code=400, detail=f"model {model} is not a {want_kind} model")
    prompt = body.get("prompt") or ""
    size = body.get("size") or "1024x1024"
    n = _gen_count(body)
    # 默认自托管 URL;response_format=b64_json 才内联字节(与 OpenAI 一致)。
    want_b64 = (body.get("response_format") == "b64_json")
    # 渠道自定义透传参数(声明了 accepts_options 的渠道用它收单次调用的选项)。
    # 只给声明了 accepts_options 的渠道传 —— 别的渠道签名里没这个参数,硬传会 TypeError。
    opts = body.get("matchup_options")
    if not isinstance(opts, dict):
        opts = None
    # 限速放在取号之前:被限的请求不该占用账号,也不该留下任何副作用。
    _gen_rate_limit(channel, adapter)
    # 总时长预算:网关在 Cloudflare 后面,CF 的 524 线是「源站 100 秒无响应就切」。
    # 光把单次超时调短没用 —— 重试几次叠起来照样超(踩过:某个生图渠道报 524,
    # 报 524,用户拿到的是 CF 那张 HTML 页,看不出是哪一段慢)。这里只在预算内**开始**
    # 新的尝试:快的失败(额度不足一两秒就回)可以多试几个号,慢的尝试只跑得起一次,
    # 于是最坏耗时 ≈ 预算 + 单次上限,渠道把这两个数一起卡在 100 秒内。
    # gen_budget = 0 表示不限(默认,给视频那种本来就要跑几分钟的渠道)。
    budget = getattr(adapter, "gen_budget", 0) or 0
    started = time.time()
    last = None
    for _ in range(max_switch):
        if budget and time.time() - started > budget:
            break
        acct = POOL.get_valid_account(channel, adapter)
        if not acct:
            raise HTTPException(status_code=503, detail=f"no account for {channel}")
        try:
            if getattr(adapter, "accepts_options", False):
                urls = adapter.generate(acct, model, prompt, size, n, options=opts)
            else:
                urls = adapter.generate(acct, model, prompt, size, n)
            return {"created": int(time.time()),
                    "data": _rehost(urls, adapter, want_b64)}
        except GenerationSubmittedError as e:
            # 已扣上游额度但结果未知：冷却该号并立刻结束，绝不换号重复提交。
            POOL.mark_cooldown(acct["id"])
            raise HTTPException(status_code=502, detail={
                "code": "generation_result_unknown", "task_id": e.task_id,
                "message": "任务已提交，结果暂时未知，请勿重复提交",
            })
        except Exception as e:
            last = str(e)
            POOL.mark_failure(acct["id"], e,
                              dead=getattr(adapter, "exhausted_is_dead", False),
                              adapter=adapter)
    _upstream_failed(channel, model, last)
    raise HTTPException(status_code=502, detail=f"generation failed: {last}")


# ---- 生成型渠道走 chat 接口 ----
# 用户在普通客户端里选中一个生图模型,发出来的就是 /v1/chat/completions。这里把
# 那次对话翻译成一次生成:最后一条 user 消息当 prompt,出图后以 markdown 图片
# 回填。少了这段,请求会一路走到基类 Adapter.chat 抛空消息的 NotImplementedError,
# 而 SSE 头和第一帧已经发出去了 —— 客户端看到的就是「200 然后流断掉」。

GEN_KEEPALIVE_EVERY = 15   # 生成期间的 SSE 心跳间隔(秒)
GEN_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _gen_prompt(messages):
    """chat messages → prompt:取最后一条 user 消息的文字。

    多模态数组只收 text 段。图生图上游还没开,把图片 URL
    拼进 prompt 只会让模型照着念一遍,不如当没传。
    """
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            return "\n".join(p.get("text") or "" for p in c
                             if isinstance(p, dict)
                             and p.get("type") == "text").strip()
    return ""


def _gen_err_text(e):
    """异常 → 给用户看的一行。HTTPException 的 detail 可能是 dict
    (generation_result_unknown 就是),取里面的 message,别把整个 dict 抛出去。"""
    detail = e.detail if isinstance(e, HTTPException) else str(e)
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    return str(detail) or e.__class__.__name__


def _gen_chat_text(adapter, model, messages):
    """跑一次生成,结果写成 chat 里能显示的 markdown。"""
    prompt = _gen_prompt(messages)
    if not prompt:
        raise HTTPException(status_code=400,
                            detail="prompt required: 最后一条 user 消息没有文字")
    kind = adapter.model_kind(model) or "image"
    resp = _generate({"model": model, "prompt": prompt}, kind)
    urls = [d.get("url") for d in (resp.get("data") or []) if d.get("url")]
    if not urls:
        raise HTTPException(status_code=502, detail="generation returned no url")
    if kind == "video":
        return "\n\n".join(f"[视频 {i}]({u})" for i, u in enumerate(urls, 1))
    return "\n\n".join(f"![]({u})" for u in urls)


def _gen_chat_completion(adapter, body):
    """非流式:生成完一次性返回。失败直接抛 —— 抛在计费之前,不留白扣。"""
    model = body["model"]
    text = _gen_chat_text(adapter, model, body.get("messages") or [])
    return {
        "id": _cid(), "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": dict(GEN_ZERO_USAGE),
    }


def _gen_chat_stream(adapter, body, usage_holder=None):
    """流式:先出一帧占住连接,出图前每 GEN_KEEPALIVE_EVERY 秒补一个 SSE 心跳。

    心跳不是装饰:用户拿到的入口在 Cloudflare 后面,原点 100 秒不出字节就被掐断
    (生图实测能跑到 300 秒以上)。先发
    role 帧再定期心跳,把「等首字节」变成「有字节在流」,这条路才等得起几分钟。
    """
    cid, created, model = _cid(), int(time.time()), body["model"]

    def chunk(delta=None, finish=None, usage=None):
        o = {"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": model,
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            o["usage"] = usage
        return f"data: {json.dumps(o, ensure_ascii=False)}\n\n"

    yield chunk(delta={"role": "assistant"})

    result = {}

    def worker():
        try:
            result["text"] = _gen_chat_text(adapter, model,
                                            body.get("messages") or [])
        except Exception as e:
            result["err"] = e

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    while True:
        th.join(timeout=GEN_KEEPALIVE_EVERY)
        if not th.is_alive():
            break
        yield ": keepalive\n\n"      # SSE 注释,客户端忽略,连接上有字节在走

    if "err" in result:
        # 没出图不计费:靠 nobill 让 _stream_chat_billed 的 settle 跳过。
        if usage_holder is not None:
            usage_holder["nobill"] = True
        yield chunk(delta={"content": "[生成失败] " + _gen_err_text(result["err"])})
        yield chunk(delta={}, finish="stop", usage=dict(GEN_ZERO_USAGE))
        yield "data: [DONE]\n\n"
        return
    for line in (chunk(delta={"content": result["text"]}),
                 chunk(delta={}, finish="stop", usage=dict(GEN_ZERO_USAGE)),
                 "data: [DONE]\n\n"):
        if usage_holder is not None:
            _sniff_usage(line, usage_holder)
        yield line


@app.post("/v1/images/generations")
async def images_generations(request: Request, authorization: str = Header(None)):
    body = await request.json()
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, model, request=request)
    # 生成是同步阻塞的(提交 + 轮询,几十秒到几分钟)。留在事件循环里跑会把整个
    # 单 worker 的网关挂住 —— 别的请求连健康检查都排不上。
    resp = await asyncio.to_thread(_generate, body, "image")
    if user is not None:
        channel = model_to_channel().get(model)
        BILLING.record_usage(user, group, channel, model,
                             adapter=get_adapter(channel) if channel else None,
                             stream=False, units=_gen_count(body))
    return resp


@app.post("/v1/videos/generations")
async def videos_generations(request: Request, authorization: str = Header(None)):
    body = await request.json()
    model = body.get("model", "")
    user, group = _authorize_gateway(authorization, model, request=request)
    resp = await asyncio.to_thread(_generate, body, "video")
    if user is not None:
        channel = model_to_channel().get(model)
        BILLING.record_usage(user, group, channel, model,
                             adapter=get_adapter(channel) if channel else None,
                             stream=False, units=_gen_count(body))
    return resp


# 回调两种方法都要收(易支付用 GET 带 query,别的渠道 POST)。写成两个装饰器而不是
# api_route(methods=[...]):后者让 GET 与 POST 共用一个自动生成的 operationId,
# OpenAPI 要求 operationId 唯一,重了客户端代码生成器和 schema 校验都会报。
@app.get("/pay/notify/{provider}", operation_id="pay_notify_get")
@app.post("/pay/notify/{provider}", operation_id="pay_notify_post")
async def pay_notify(provider: str, request: Request):
    """支付回调。验签在 provider 内完成;不需要鉴权。
    始终返回 200 文本("success"/原因),避免渠道无限重试。"""
    import urllib.parse

    from fastapi.responses import PlainTextResponse

    from core import orders as orders_mod
    from core.portal_state import USER_DB
    raw = await request.body()
    params = dict(request.query_params)
    if not params and raw:
        params = {k: v[0] for k, v in
                  urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
    ok, msg = orders_mod.handle_notify(USER_DB, provider, dict(request.headers),
                                       raw, params=params)
    if not ok:
        print(f"[bitapi] 回调未受理 provider={provider} reason={msg}", flush=True)
    return PlainTextResponse("success" if ok else msg)


@app.get("/pay/return/{provider}", response_class=HTMLResponse)
def pay_return(provider: str, out_trade_no: str = None):
    """支付完成后的跳回页。

    只读不写:入账唯一入口是 /pay/notify 与后台对账。这个页面若也能触发到账,
    用户手动构造一次 return 就成了第二条入账路径。
    """
    from core.portal_state import USER_DB
    order = USER_DB.get_order_by_trade_no(out_trade_no) if out_trade_no else None
    if order is None:
        line = "如果已完成付款,额度通常几秒内到账。"
    elif order["status"] == "completed":
        line = f"已到账 ${order['amount']:.2f}。"
    elif order["status"] in ("paid", "recharging"):
        line = "已收到付款,正在入账。"
    else:
        line = ("这笔还没收到付款。若你已付款,后台每 10 分钟会主动核对一次,"
                "也可以联系站长立即查单。")
    return ('<!doctype html><meta charset="utf-8">'
            '<body style="font-family:-apple-system,Segoe UI,sans-serif;padding:40px">'
            f'<h2>{"支付完成" if order and order["status"] == "completed" else "支付已提交"}</h2>'
            f'<p>{line}</p>'
            '<p><a href="/portal#/wallet">返回控制台</a></p></body>')


@app.get("/", response_class=HTMLResponse)
def home_ui():
    """站点首页(公开落地页:定位、在册厂商、接入示例、计费口径)。

    页面本身不带数据:读数与厂商清单由前端拉 /api/models,和模型广场同一个
    免鉴权端点、同一份可见范围 —— 首页写的价与控制台里点开看到的价不会分叉。
    """
    import os
    from fastapi.responses import HTMLResponse as _HTML
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home.html")
    with open(path, encoding="utf-8") as f:
        return _HTML(content=f.read(), headers={"Cache-Control": "no-cache"})


@app.get("/portal", response_class=HTMLResponse)
@app.get("/portal/", response_class=HTMLResponse)
def portal_ui():
    """用户接入层前端(注册/登录/仪表盘/密钥/用量/计费/邀请)。"""
    import os
    from fastapi.responses import HTMLResponse as _HTML
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal.html")
    with open(path, encoding="utf-8") as f:
        return _HTML(content=f.read(), headers={"Cache-Control": "no-cache"})


@app.get("/portal-app.js")
def portal_app_js():
    """前端脚本(portal.html 引用)。"""
    import os
    from fastapi.responses import Response
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal-app.js")
    with open(path, encoding="utf-8") as f:
        # 与 /static/* 同一个理由:这几个文件没有版本号,浏览器一旦按启发式缓存
        # 缓住其中一个,新旧两份脚本就会配到一起,缺函数直接把页面打白。
        return Response(content=f.read(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/pricing-seed.json")
def pricing_seed():
    """内置阶梯定价种子(管理页一键导入用)。"""
    import os
    from fastapi.responses import Response
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "pricing_seed.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="seed not found")
    with open(path, encoding="utf-8") as f:
        return Response(content=f.read(), media_type="application/json")


@app.get("/media/gen/{name}")
def media_gen(name: str):
    """自托管的生成媒体(图片/视频)。字节由 _generate 从上游抓下缓存,token 即文件名。

    存活由 MEDIA_TTL 控制(默认 30 分钟),过期后 housekeeping 清掉,再取就是 404 ——
    这是短期分发,不是长期图床。"""
    from fastapi.responses import FileResponse

    from core import media_cache
    hit = media_cache.read(name)
    if not hit:
        raise HTTPException(status_code=404, detail="not found")
    path, mime = hit
    # 内容不可变(token 唯一),可放心让客户端/CDN 缓存到 TTL 结束。
    return FileResponse(path, media_type=mime,
                        headers={"Cache-Control": f"public, max-age={config.MEDIA_TTL}"})


@app.get("/static/{name:path}")
def static_asset(name: str):
    """自托管前端资产(vue / naive-ui UMD、模型图标等),不走 CDN。

    {name:path} 是为了让 model-icons/ 这种子目录也能取到;越权仍由下面的
    root 前缀校验拦住(normpath 先把 ../ 折平)。
    """
    import os
    from fastapi.responses import FileResponse
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    path = os.path.normpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    mime = {".js": "application/javascript", ".css": "text/css",
            ".html": "text/html; charset=utf-8", ".map": "application/json",
            ".svg": "image/svg+xml", ".png": "image/png",
            ".woff2": "font/woff2"}
    ext = os.path.splitext(path)[1].lower()
    # 必须每次回源校验(304 走 etag,开销只有一个空响应)。这些文件没有版本号,
    # 而 portal.html 与 /portal-app.js 不带 Last-Modified、浏览器每次都重取 ——
    # 少了这个头,启发式缓存会让新的 portal-app.js 配上旧的 portal-shared.js,
    # 缺函数直接抛异常把整个应用打白。
    return FileResponse(path, media_type=mime.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/admin/ui", response_class=HTMLResponse)
def admin_ui():
    """号池监控面板(读 /admin/stats + /admin/accounts 动态渲染)。"""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    return {"status": "ok", "adapters": list(all_adapters().keys())}
