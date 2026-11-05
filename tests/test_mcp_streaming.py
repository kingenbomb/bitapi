"""MCP 转发必须读完整个响应，并在完成、断连及失败时释放上游连接。"""
import asyncio
import unittest
from unittest import mock

import anyio
import httpx
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request

from routers import mcp


class _UpstreamBody(httpx.AsyncByteStream):
    def __init__(self, chunks, client, *, error=None, hold=False):
        self.chunks = chunks
        self.client = client
        self.error = error
        self.hold = hold
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await anyio.sleep(0)
            if self.client.is_closed:
                raise httpx.ReadError("upstream connection closed before body was read")
            yield chunk
            if self.hold:
                await anyio.sleep_forever()
        if self.error:
            raise self.error

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True


class MCPStreamingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("_keys", ["upstream-one", "upstream-two"]),
                            ("_cooldown", {}), ("_session_key", {}), ("_idx", 0)):
            patcher = mock.patch.object(mcp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        auth = mock.patch.object(mcp, "_authorize", return_value=(None, None))
        auth.start()
        self.addCleanup(auth.stop)

    async def proxy(self, client, *, spec="2.4"):
        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(b"accept", b"application/json, text/event-stream")],
                 "asgi": {"version": "3.0", "spec_version": spec}}

        async def receive():
            return {"type": "http.request", "body": b'{"method":"initialize"}'}

        with mock.patch.object(mcp.httpx, "AsyncClient", return_value=client):
            response = await mcp._proxy(Request(scope, receive))
        return response, scope

    async def consume(self, response, scope, *, disconnect=False):
        messages = []
        first_body = asyncio.Event()

        async def receive():
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()
                if disconnect and scope["asgi"]["spec_version"] == "2.4":
                    raise OSError("downstream disconnected")

        await response(scope, receive, send)
        return messages

    async def test_complete_sse_json_and_empty_responses(self):
        for content_type, status, chunks in (
            ("text/event-stream", 200,
             [b'event: message\ndata: {"jsonrpc":"2.0",',
              b'"id":1,"result":{"ok":true}}\n\n']),
            ("application/json", 200, [b'{"jsonrpc":"2.0","id":1,"result":{}}']),
            ("application/json", 202, []),
        ):
            with self.subTest(content_type=content_type, status=status):
                def handler(_request):
                    return httpx.Response(status, stream=body, headers={
                        "content-type": content_type, "mcp-session-id": "session-one",
                        "cache-control": "no-cache"})

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                body = _UpstreamBody(chunks, client)
                response, scope = await self.proxy(client)
                messages = await self.consume(response, scope)
                self.assertEqual(messages[0]["status"], status)
                self.assertEqual(b"".join(m.get("body", b"") for m in messages),
                                 b"".join(chunks))
                self.assertEqual(dict(messages[0]["headers"])[b"mcp-session-id"],
                                 b"session-one")
                self.assertFalse(messages[-1]["more_body"])
                self.assertTrue(body.closed)
                self.assertTrue(client.is_closed)

    async def test_downstream_disconnect_closes_response_and_client(self):
        for spec in ("2.3", "2.4"):
            with self.subTest(asgi=spec):
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, stream=body)))
                body = _UpstreamBody([b"data: first\n\n"], client, hold=True)
                response, scope = await self.proxy(client, spec=spec)
                if spec == "2.4":
                    with self.assertRaises(ClientDisconnect):
                        await self.consume(response, scope, disconnect=True)
                else:
                    await self.consume(response, scope, disconnect=True)
                self.assertTrue(body.closed)
                self.assertTrue(client.is_closed)

    async def test_upstream_read_failure_closes_response_and_client(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=body)))
        body = _UpstreamBody([b"data: first\n\n"], client,
                             error=httpx.ReadError("upstream failed"))
        response, scope = await self.proxy(client)
        with self.assertRaises(httpx.ReadError):
            await self.consume(response, scope)
        self.assertTrue(body.closed)
        self.assertTrue(client.is_closed)

    async def test_rejected_key_is_closed_before_retry(self):
        calls = []

        def handler(request):
            calls.append(request.headers["authorization"])
            if len(calls) == 1:
                return httpx.Response(401, stream=rejected)
            self.assertTrue(rejected.closed)
            return httpx.Response(200, stream=accepted)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rejected = _UpstreamBody([b"unauthorized"], client)
        accepted = _UpstreamBody([b'{"result":{}}'], client)
        response, scope = await self.proxy(client)
        messages = await self.consume(response, scope)
        self.assertEqual(calls, ["Bearer upstream-one", "Bearer upstream-two"])
        self.assertEqual(b"".join(m.get("body", b"") for m in messages),
                         b'{"result":{}}')
        self.assertTrue(accepted.closed)
        self.assertTrue(client.is_closed)

    async def test_no_available_key_closes_client(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(mock.Mock()))
        with mock.patch.object(mcp, "_keys", []), self.assertRaises(HTTPException) as ctx:
            await self.proxy(client)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(client.is_closed)

    async def test_connect_failures_close_client(self):
        handler = mock.Mock(side_effect=httpx.ConnectError("upstream unreachable"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # 四次连接失败，覆盖所有重试耗尽后尚未移交响应对象的清理路径。
        with mock.patch.object(mcp, "_next_key", return_value="upstream-one"), \
                self.assertRaises(HTTPException) as ctx:
            await self.proxy(client)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(handler.call_count, 4)
        self.assertTrue(client.is_closed)

    async def test_cancellation_before_response_closes_client(self):
        async def handler(_request):
            raise asyncio.CancelledError()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(asyncio.CancelledError):
            await self.proxy(client)
        self.assertTrue(client.is_closed)
