"""Codex PKCE authorization, using sub2api's authorization/callback workflow.

Single-worker sessions expire after 30 minutes, are administrator-bound, and are
consumed once. Token material never leaves the account-management backend.
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from core import account_management as M, account_runtime, gpt_accounts as G, pool_state

REDIRECT_URI = "http://localhost:1455/auth/callback"
_FLOWS = {}
_LOCK = threading.Lock()


def start(admin_id, account_id=None, proxy_id=None):
    if account_id is not None:
        row = M.account_or_404(account_id)
        if row["channel"] != "gpt":
            raise ValueError("此授权流程适用于 GPT 原生账号")
        if proxy_id is None:
            proxy_id = row.get("proxy_id")
    if proxy_id is not None:
        proxy = pool_state.DB.get_pool_proxy(proxy_id)
        if not proxy or not proxy["enabled"]:
            raise ValueError("所选代理不存在或已停用")
    now = int(time.time())
    session_id, state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with _LOCK:
        for key in [k for k, v in _FLOWS.items() if v["expires_at"] <= now]:
            del _FLOWS[key]
        _FLOWS[session_id] = dict(admin_id=admin_id, account_id=account_id, proxy_id=proxy_id,
                                  state=state, verifier=verifier, expires_at=now + 1800)
    query = urllib.parse.urlencode({"client_id": G.CLIENT_ID, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": "openid profile email offline_access",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"})
    return {"session_id": session_id, "auth_url": "https://auth.openai.com/oauth/authorize?" + query,
            "expires_at": now + 1800}


def finish(admin_id, session_id, callback_url):
    parsed = urllib.parse.urlsplit(callback_url.strip())
    if (parsed.scheme, parsed.hostname, parsed.port, parsed.path) != ("http", "localhost", 1455, "/auth/callback"):
        raise ValueError("请粘贴浏览器跳转后的完整 localhost:1455/auth/callback 地址")
    query = urllib.parse.parse_qs(parsed.query)
    state, code = (query.get("state") or [""])[0], (query.get("code") or [""])[0]
    if not state or not code:
        raise ValueError("回调地址缺少 code 或 state，请重新授权")
    with _LOCK:
        flow = _FLOWS.get(session_id)
        if not flow or flow["expires_at"] <= time.time() or flow["admin_id"] != admin_id:
            raise ValueError("授权会话不存在、已过期或已使用，请重新开始")
        if not hmac.compare_digest(flow["state"], state):
            raise ValueError("授权 state 不匹配，请使用本次授权的回调地址")
        del _FLOWS[session_id]
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": G.CLIENT_ID,
        "code": code, "code_verifier": flow["verifier"], "redirect_uri": REDIRECT_URI}).encode()
    request = urllib.request.Request(config.GPT_TOKEN_URL, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with account_runtime.network(pool_state.DB, {"proxy_id": flow["proxy_id"]}):
            with account_runtime.urlopen(request, timeout=30) as response:
                credentials = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f"OAuth 换取凭据失败（HTTP {exc.code}），请重新开始授权") from None
    except (OSError, ValueError):
        raise ValueError("OAuth 换取凭据失败，请检查代理或网络并重新授权") from None
    normalized = G.normalize_entry(credentials)
    if flow["account_id"] is not None:
        row = M.account_or_404(flow["account_id"])
        previous = G.normalize_entry(row.get("secret") or {}, validate_expiry=False)
        if G.conflicts(previous, normalized):
            raise ValueError("授权账号与正在编辑的账号不一致；如需添加新账号，请从新建授权入口开始")
        M.patch_account(row["id"], {"credentials": credentials, "proxy_id": flow["proxy_id"]})
        aid, action = row["id"], "updated"
    else:
        result = G.import_accounts(pool_state.DB, [credentials])
        outcome = result["items"][0]
        if outcome["action"] == "failed":
            raise ValueError(outcome["message"])
        aid, action = outcome["account_id"], outcome["action"]
        if aid and action == "imported" and flow["proxy_id"] is not None:
            M.patch_account(aid, {"proxy_id": flow["proxy_id"]})
    return {"ok": True, "action": action, "account": M.view(M.account_or_404(aid))}
