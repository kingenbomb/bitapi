#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 反向代理 —— 把站内用户的 MCP 客户端接到 TinyFish MCP，用号池里的 key。

TinyFish MCP 是 streamable-HTTP（`POST/GET/DELETE https://agent.tinyfish.ai/mcp`），
认 `Authorization: Bearer sk-tinyfish-...`。本模块把它透明中继，注入号池里的一把
TinyFish key，并原样透传 `mcp-session-id` / SSE 流；上游 401/402/403 视为「这把号
不能用」，冷却后换下一把。

用户侧鉴权：默认要求带上本站的 key（网关同款 sk- key），可用
  - `Authorization: Bearer <本站 key>`  或
  - URL 查询参数 `?key=<本站 key>`（给不方便加请求头的客户端）
master key（config.API_KEY）同样放行。设 `BITAPI_MCP_OPEN=1` 可放开为公开。

配置（环境变量）：
  BITAPI_MCP_UPSTREAM  上游 MCP 端点，默认 https://agent.tinyfish.ai/mcp
  BITAPI_MCP_KEY_GLOB  号池文件 glob（相对仓根），默认 data/tf_*.txt，逗号分隔
  BITAPI_MCP_OPEN      1 = 不校验本站 key（公开）
"""
import concurrent.futures
import glob
import json
import os
import threading
import time

import httpx
from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

import config
from core.portal_state import BILLING, USER_DB

UPSTREAM = os.environ.get("BITAPI_MCP_UPSTREAM", "https://agent.tinyfish.ai/mcp")
KEY_GLOB = os.environ.get("BITAPI_MCP_KEY_GLOB", "data/tf_*.txt")
OPEN = (os.environ.get("BITAPI_MCP_OPEN", "") or "").lower() not in ("", "0", "false", "no")
RPM = int(os.environ.get("BITAPI_MCP_RPM", "60"))  # 每用户每分钟 MCP 请求上限
_FORWARD_REQ_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "user-agent")
_FORWARD_RES_HEADERS = ("content-type", "mcp-session-id", "cache-control")

router = APIRouter()

# ---- 号池 ----
_lock = threading.Lock()
_keys = []
_idx = 0
_cooldown = {}  # key -> 解冻时间戳
_session_key = {}  # mcp-session-id -> key（MCP 会话必须粘在同一把号上）
_rpm = {}  # user_id -> [请求时间戳]
_stats = {"metered": 0}  # 累计计量次数（近似，供 /mcp/status）


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reload_keys():
    """从号池文件重新载入 TinyFish key（幂等，可热更）。"""
    global _keys
    root = _repo_root()
    found = []
    for pat in [p.strip() for p in KEY_GLOB.split(",") if p.strip()]:
        for path in glob.glob(os.path.join(root, pat)):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        for part in line.strip().split("\t"):
                            part = part.strip()
                            if part.startswith("sk-tinyfish-"):
                                found.append(part)
            except Exception:
                pass
    with _lock:
        _keys = list(dict.fromkeys(found))
    return len(_keys)


def _next_key():
    with _lock:
        now = time.time()
        avail = [k for k in _keys if _cooldown.get(k, 0) < now]
        if not avail:
            raise HTTPException(status_code=503, detail="no available TinyFish key")
        global _idx
        k = avail[_idx % len(avail)]
        _idx += 1
        return k


def _cooldown_key(k, secs):
    with _lock:
        _cooldown[k] = time.time() + secs


def _authorize(request: Request):
    """返回 (user, key_id)。master key / 开放模式 → (None, None)。"""
    if OPEN:
        return None, None
    tok = (request.headers.get("authorization") or "").replace("Bearer", "").strip()
    if not tok:
        tok = (request.query_params.get("key") or "").strip()
    if not tok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: pass your site key as `Authorization: Bearer <key>` or `?key=<key>`")
    from core.security import secret_equal
    if secret_equal(tok, config.API_KEY):
        return None, None
    resolved = BILLING.resolve_user_by_key(tok)
    if not resolved:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: pass your site key as `Authorization: Bearer <key>` or `?key=<key>`")
    user, _group = resolved
    return user, user.get("_api_key")


def _ratelimit(user):
    if not user or RPM <= 0:
        return
    now = time.time()
    uid = user.get("id")
    with _lock:
        w = [t for t in _rpm.get(uid, []) if now - t < 60]
        if len(w) >= RPM:
            raise HTTPException(status_code=429, detail=f"MCP rate limit {RPM}/min exceeded")
        w.append(now)
        _rpm[uid] = w


def _meter(user, key_id, body):
    """把一次 tools/call 记进 usage_logs（channel=mcp，成本 0，免费）。"""
    if not user:
        return
    try:
        msg = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return
    if msg.get("method") != "tools/call":
        return
    tool = (msg.get("params") or {}).get("name")
    try:
        USER_DB.add_usage(user.get("id"), api_key_id=key_id, channel="mcp",
                          model=tool or "mcp", cost=0.0, actual_cost=0.0,
                          billing_mode="free", token_source="estimate")
        with _lock:
            _stats["metered"] += 1
    except Exception:
        pass


class _MCPStreamingResponse(StreamingResponse):
    """上游连接随下游响应存活，结束、断连或异常时统一释放。"""

    def __init__(self, upstream, client, headers):
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code,
                         headers=headers)
        self._upstream = upstream
        self._client = client

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 下游断连会取消响应任务；清理不能被同一个取消信号打断。
            with CancelScope(shield=True):
                try:
                    await self._upstream.aclose()
                finally:
                    await self._client.aclose()


async def _proxy(request: Request):
    user, key_id = _authorize(request)
    _ratelimit(user)
    body = await request.body()
    _meter(user, key_id, body)
    fwd = {h: request.headers[h] for h in _FORWARD_REQ_HEADERS if request.headers.get(h)}
    sid = request.headers.get("mcp-session-id") or ""

    # 会话粘性：同一 mcp-session-id 必须复用同一把 key，否则上游「Session not found」
    with _lock:
        sticky_key = _session_key.get(sid) if sid else None
    if sticky_key and _cooldown.get(sticky_key, 0) > time.time():
        sticky_key = None  # 绑定的号已冷却，重新选

    timeout = httpx.Timeout(300.0, connect=20.0)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(4):
            key = sticky_key if (sticky_key and attempt == 0) else _next_key()
            headers = dict(fwd)
            headers["authorization"] = "Bearer " + key
            req = client.build_request(request.method, UPSTREAM, headers=headers, content=body)
            try:
                resp = await client.send(req, stream=True)
            except Exception:
                _cooldown_key(key, 60)
                if key == sticky_key:
                    sticky_key = None
                continue
            if resp.status_code in (401, 402, 403):
                await resp.aclose()
                _cooldown_key(key, 600)
                if key == sticky_key:
                    sticky_key = None
                continue
            # initialize 的响应带新的 session id -> 绑定到本次用的 key
            resp_sid = resp.headers.get("mcp-session-id")
            if resp_sid:
                with _lock:
                    _session_key[resp_sid] = key
            if request.method == "DELETE" and sid:
                with _lock:
                    _session_key.pop(sid, None)
            out = {h: resp.headers[h] for h in _FORWARD_RES_HEADERS if resp.headers.get(h)}
            return _MCPStreamingResponse(resp, client, out)
        raise HTTPException(status_code=502, detail="all TinyFish keys failed upstream")
    except BaseException:
        # 尚未交给响应对象的失败路径（含取消、无可用 key）由这里关连接。
        with CancelScope(shield=True):
            await client.aclose()
        raise


@router.get("/mcp/status")
async def mcp_status(request: Request):
    """运维自检：号池大小、冷却数、会话数、计量次数。（注册在 /mcp/{client} 通配之前）"""
    _require_admin(request)
    with _lock:
        now = time.time()
        return {"keys": len(_keys), "cooling": sum(1 for v in _cooldown.values() if v > now),
                "sessions": len(_session_key), "metered": _stats["metered"], "rpm": RPM,
                "wallet_known": len(_wallets), "scan": dict(_scan),
                "upstream": UPSTREAM, "open": OPEN}


def _require_admin(request: Request):
    tok = (request.headers.get("authorization") or "").replace("Bearer", "").strip()
    from core.security import secret_equal
    if secret_equal(tok, config.ADMIN_KEY) or secret_equal(tok, config.API_KEY):
        return
    raise HTTPException(status_code=401, detail="admin key required")


@router.post("/mcp/reload")
async def mcp_reload(request: Request):
    """热更号池：把新 key 文件推上来后调用即可，无需重启。需管理密钥。"""
    _require_admin(request)
    return {"ok": True, "keys": reload_keys()}


@router.post("/mcp/scan")
async def mcp_scan(request: Request):
    """后台扫描每把 key 的 TinyFish 钱包额度（MCP get_wallet），结果进内存缓存。"""
    _require_admin(request)
    started = start_scan()
    with _lock:
        return {"ok": True, "started": started, "total": _scan["total"], "done": _scan["done"]}


def _mask(k):
    return (k[:20] + "…" + k[-4:]) if len(k) > 30 else k


# ---- 每号额度（TinyFish 钱包，走 MCP get_wallet 查） ----
_wallets = {}                                    # key -> {"balance": float, "at": ts}
_scan = {"running": False, "done": 0, "total": 0}


def _mcp_post(key, body, sid=None, timeout=30):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + key}
    if sid:
        headers["Mcp-Session-Id"] = sid
    with httpx.Client(timeout=timeout) as c:
        r = c.post(UPSTREAM, headers=headers, json=body)
        return r.headers.get("mcp-session-id"), r.text


def _extract_wallet(body):
    try:
        j = json.loads(body)
    except Exception:
        j = None
        for line in reversed(body.splitlines()):
            if line.startswith("data:"):
                try:
                    j = json.loads(line[5:].strip())
                except Exception:
                    j = None
                break
    if not isinstance(j, dict):
        return None
    content = (j.get("result") or {}).get("content") or []
    if not content:
        return None
    try:
        return float(json.loads(content[0].get("text", "")).get("available_balance"))
    except Exception:
        return None


def _scan_one(key):
    try:
        sid, _ = _mcp_post(key, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "scan", "version": "1"}}})
        _, body = _mcp_post(key, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "get_wallet", "arguments": {}}}, sid)
    except Exception:
        return
    bal = _extract_wallet(body)
    if bal is None:
        return
    with _lock:
        _wallets[key] = {"balance": bal, "at": int(time.time())}
        _scan["done"] += 1


def _scan_worker(keys, concurrency=8):
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(_scan_one, keys))
    finally:
        with _lock:
            _scan["running"] = False


def start_scan():
    with _lock:
        if _scan["running"]:
            return False
        keys = list(_keys)
        _scan.update(running=True, done=0, total=len(keys))
    threading.Thread(target=_scan_worker, args=(keys,), daemon=True).start()
    return True


def snapshot():
    """MCP 文件池快照（脱敏 + 每号额度）。供 /admin/stats 与 /admin/mcp/keys 共用。"""
    now = time.time()
    with _lock:
        keys = list(_keys)
        cd = dict(_cooldown)
        wallets = {k: dict(v) for k, v in _wallets.items()}
        scan = dict(_scan)
    items = [{"id": _mask(k),
              "status": "cooldown" if cd.get(k, 0) > now else "active",
              "cooldown_until": int(cd.get(k, 0)) if cd.get(k, 0) > now else 0,
              "balance": (wallets.get(k) or {}).get("balance"),
              "wallet_at": (wallets.get(k) or {}).get("at")}
             for k in keys]
    cooling = sum(1 for it in items if it["status"] == "cooldown")
    balances = [it["balance"] for it in items if it["balance"] is not None]
    return {"total": len(items), "cooling": cooling,
            "by_status": {"active": len(items) - cooling, "cooldown": cooling},
            "keys": items, "scan": scan,
            "wallet_known": len(balances),
            "wallet_total": round(sum(balances), 2) if balances else 0}


@router.get("/admin/mcp/keys")
async def admin_mcp_keys(request: Request):
    """号池监控面板用：列出 MCP（TinyFish）文件池的 key（脱敏）与状态。"""
    _require_admin(request)
    return snapshot()


@router.post("/mcp")
@router.get("/mcp")
@router.delete("/mcp")
@router.post("/mcp/{client}")
@router.get("/mcp/{client}")
@router.delete("/mcp/{client}")
async def mcp_proxy(request: Request, client: str = ""):
    """透明中继到 TinyFish MCP（/mcp、/mcp/claude、/mcp/chatgpt 均可）。"""
    return await _proxy(request)


# 首次导入即载入号池
try:
    reload_keys()
except Exception:
    pass
