"""记录管理写操作的操作者、目标、结果和有限的业务字段，不存凭据。"""
import asyncio
import json
import math
import re

from starlette.requests import Request
import config
from core import auth, throttle
from core.security import secret_equal

_FIELDS = {"role", "group_id", "status", "email", "balance", "email_verified_at",
           "password_changed_at", "totp_secret"}
_INPUTS = {"channel", "identity", "name", "amount", "value", "count", "role", "group_id",
           "status", "action", "ids", "paused", "enabled", "notes", "user_id"}


def _actor(db, headers):
    token = (headers.get("x-bitapi-admin-key") or headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if secret_equal(token, config.ADMIN_KEY):
        return "admin_key"
    if secret_equal(token, config.API_KEY):
        return "api_key"
    payload = auth.decode_session_token(token)
    if payload:
        user = db.get_user(int(payload.get("sub") or 0))
        if (user and user.get("role") == "admin" and user.get("status") == "active"
                and int(payload.get("iat") or 0) >= int(user.get("password_changed_at") or 0)):
            return "user:" + str(user["id"])


def _snapshot(db, path):
    match = re.match(r"/api/admin/users/(\d+)(?:/|$)", path)
    if match:
        user = db.get_user(int(match[1])) or {}
        return {key: (bool(value) if key == "totp_secret" else value)
                for key, value in user.items() if key in _FIELDS}
    return None


class AdminAuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}
                or not path.startswith(("/api/admin/", "/admin/", "/mcp/"))):
            return await self.app(scope, receive, send)
        from core import portal_state
        db = portal_state.USER_DB
        request = Request(scope)
        actor = await asyncio.to_thread(_actor, db, request.headers)
        if not actor:
            return await self.app(scope, receive, send)
        before = await asyncio.to_thread(_snapshot, db, path)
        target = {key: value for key, value in request.query_params.items() if key in _INPUTS}
        audit_id = await asyncio.to_thread(db.start_admin_audit, actor,
            scope["method"] + " " + path, target, before, throttle.client_ip(request))
        captured, status = bytearray(), 500

        async def capture_receive():
            message = await receive()
            if len(captured) <= 16384:
                captured.extend(message.get("body", b"")[:16385 - len(captured)])
            return message

        async def capture_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, capture_receive, capture_send)
        finally:
            changes = {}
            if captured and len(captured) <= 16384:
                try:
                    body = json.loads(captured)
                    if isinstance(body, dict):
                        changes = {k: (str(v) if isinstance(v, float) and not math.isfinite(v) else v)
                                   for k, v in body.items() if k in _INPUTS and
                                   (isinstance(v, (str, int, float, bool)) or v is None)}
                        if isinstance(body.get("ids"), list):
                            changes["ids"] = [value for value in body["ids"]
                                              if isinstance(value, int) and not isinstance(value, bool)]
                        # 只记录敏感配置的字段名，绝不记录它们的值。
                        changes["fields"] = sorted(body)
                except (ValueError, UnicodeError):
                    pass
            after = await asyncio.to_thread(_snapshot, db, path)
            await asyncio.to_thread(db.finish_admin_audit, audit_id, status, changes, after)
