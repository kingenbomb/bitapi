"""浏览器使用 HttpOnly 会话，脚本客户端保留 Bearer 协议。"""
import json
import hashlib
import hmac
from urllib.parse import urlsplit

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
from core import auth
from core.security import secret_equal

SESSION_COOKIE = "bitapi_session"
CSRF_COOKIE = "bitapi_csrf"


def _csrf_for(token):
    return hmac.new(config.JWT_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def _secure(request=None):
    return urlsplit(config.SITE_URL).scheme == "https" or request is not None and request.url.scheme == "https"


class SessionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            response = await handler(request)
            if request.headers.get("x-bitapi-session") == "cookie" and response.headers.get(
                    "content-type", "").startswith("application/json"):
                body = json.loads(response.body)
                if isinstance(body, dict) and body.get("token"):
                    token = body.pop("token")
                    body["authenticated"] = True
                    response.body = json.dumps(body, ensure_ascii=False).encode()
                    response.headers["content-length"] = str(len(response.body))
                    secure = _secure(request)
                    response.set_cookie(SESSION_COOKIE, token, max_age=config.JWT_TTL,
                                        httponly=True, secure=secure, samesite="lax", path="/")
                    response.set_cookie(CSRF_COOKIE, _csrf_for(token), max_age=config.JWT_TTL,
                                        secure=secure, samesite="lax", path="/")
                    response.headers["Cache-Control"] = "no-store"
            return response
        return wrapped


def clear_session(response, request=None):
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=_secure(request),
                               httponly=name == SESSION_COOKIE, samesite="lax")
    response.headers["Cache-Control"] = "no-store"


class SessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        protected = request.url.path.startswith(("/api/", "/admin/", "/mcp/"))
        cookie_auth = protected and not request.headers.get("authorization") and request.cookies.get(SESSION_COOKIE)
        if cookie_auth and not auth.decode_session_token(cookie_auth):
            cookie_auth = None  # 过期或密钥轮换后的旧 cookie 不能挡住重新登录与退出。
        if protected and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.url.netloc:
                return await JSONResponse({"detail": "cross-origin request denied"}, 403)(scope, receive, send)
            if cookie_auth and (not secret_equal(request.headers.get("x-csrf-token"), request.cookies.get(CSRF_COOKIE))
                    or not secret_equal(request.headers.get("x-csrf-token"), _csrf_for(cookie_auth))):
                return await JSONResponse({"detail": "CSRF token required"}, 403)(scope, receive, send)
        if cookie_auth:
            scope = dict(scope, headers=[*scope["headers"],
                (b"authorization", ("Bearer " + cookie_auth).encode())])
        await self.app(scope, receive, send)
