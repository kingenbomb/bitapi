"""在解析 JSON/multipart 前限制请求字节，包括无 Content-Length 的分块上传。"""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
import config


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = config.MAX_AUDIO_BYTES if scope["path"] == "/v1/audio/transcriptions" else config.MAX_REQUEST_BYTES
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
            if length < 0:
                raise ValueError
        except ValueError:
            return await JSONResponse({"detail": "invalid Content-Length"}, 400)(scope, receive, send)
        if length > limit:
            return await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "request body too large")
            return message
        await self.app(scope, limited_receive, send)
