"""每用户并发契约:迁移、管理设置、真实 ASGI 请求与流式断连的名额生命周期。"""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import httpx
from starlette.requests import ClientDisconnect

import core.portal_state as portal_state
import routers.portal as portal_routes
import server
from core import adapter as adapter_mod, auth
from core.adapter import Adapter, CAP_CHAT, register_adapter
from core.billing import Billing, RateLimitError
from core.user_db import UserDB


class MigrationTest(unittest.TestCase):
    def test_old_group_values_and_user_references_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            db = UserDB(path)
            ids = [db.create_group(name=str(n), concurrency_limit=n)
                   for n in (20, 10, 0)]
            uid = db.create_user("migration@qq.com", "hash", "MIGRATION", group_id=ids[1])
            db._conns.close_current()
            # 构造上一个版本的表结构,包含已经分组的用户。
            # 注意:`with sqlite3.connect()` 只提交事务、**不关连接** ——
            # Windows 上不关会锁住 old.db,TemporaryDirectory 清理时 PermissionError。
            conn = sqlite3.connect(path)
            try:
                conn.execute("ALTER TABLE groups RENAME COLUMN concurrency_limit TO rpm_limit")
                conn.commit()
            finally:
                conn.close()
            for _ in range(2):
                db = UserDB(path)
                self.assertEqual([db.get_group(i)["concurrency_limit"] for i in ids], [20, 10, 0])
                self.assertNotIn("rpm_limit", db.get_group(ids[1]))
                self.assertEqual(db.effective_group(db.get_user(uid))["id"], ids[1])
                db._conns.close_current()


class _Stub(Adapter):
    name = "concurrency-stub"
    models = ["concurrency-model"]
    capabilities = [CAP_CHAT]
    proxy = True


class RequestConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = UserDB(os.path.join(self.tmp.name, "users.db"))
        self.addCleanup(self.close_db)
        self.billing = Billing(self.db, cache_ttl=0)
        self.gid = self.db.create_group(name="ten", concurrency_limit=10,
                                       supported_models=["*"], billing_policy="free")
        self.uid = self.db.create_user("concurrency@qq.com", "hash", "CONCURRENCY", group_id=self.gid)
        self.other = self.db.create_user("other@qq.com", "hash", "OTHER", group_id=self.gid)
        self.keys = ["sk-concurrency-first", "sk-concurrency-second", "sk-concurrency-other"]
        for uid, key in zip((self.uid, self.uid, self.other), self.keys):
            self.db.create_api_key(uid, key)
        for mod, attr, value in ((server, "BILLING", self.billing),
                                 (portal_state, "USER_DB", self.db),
                                 (portal_routes, "USER_DB", self.db),
                                 (portal_routes, "BILLING", self.billing)):
            patch = mock.patch.object(mod, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        register_adapter(_Stub())
        self.addCleanup(adapter_mod._REGISTRY.pop, _Stub.name)
        self.body = {"model": "concurrency-model", "messages": [{"role": "user", "content": "hi"}]}
        self.headers = {"Authorization": "Bearer " + self.keys[0]}

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")

    def close_db(self):
        # 请求线程已退出,释放本测试拥有的全部连接,让 Windows 也能删除临时库。
        with self.db._conns._lock:
            connections = list(self.db._conns._live.values())
            self.db._conns._live.clear()
        for connection in connections:
            connection.close()

    @staticmethod
    def response(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "ok"}}]}

    async def wait_event(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 8), "request did not reach upstream")

    def test_ten_inflight_share_keys_and_playground_then_release_immediately(self):
        async def run():
            entered, finish = threading.Event(), threading.Event()
            lock = threading.Lock()
            active = 0

            def blocked(*args, **kwargs):
                nonlocal active
                with lock:
                    active += 1
                    if active == 10:
                        entered.set()
                if not finish.wait(10):
                    raise RuntimeError("test upstream timed out")
                return self.response(*args, **kwargs)

            async with self.client() as client:
                with mock.patch.object(server, "_openai_nonstream", side_effect=blocked):
                    tasks = [asyncio.create_task(client.post("/v1/chat/completions", json=self.body,
                             headers={"Authorization": "Bearer " + self.keys[i % 2]})) for i in range(10)]
                    try:
                        await self.wait_event(entered)
                        r = await client.post("/v1/chat/completions", json=self.body, headers=self.headers)
                        self.assertEqual(r.status_code, 429, r.text)
                        self.assertEqual(r.json()["detail"]["code"], "CONCURRENCY_LIMIT_EXCEEDED")
                        token = auth.issue_token(self.uid, role="user")
                        r = await client.post("/api/playground/chat", json=self.body,
                                              headers={"Authorization": "Bearer " + token})
                        self.assertEqual(r.status_code, 429, r.text)
                        # 另一个用户有独立名额。
                        with mock.patch.object(server, "_openai_nonstream", side_effect=self.response):
                            r = await client.post("/v1/chat/completions", json=self.body,
                                                  headers={"Authorization": "Bearer " + self.keys[2]})
                            self.assertEqual(r.status_code, 200, r.text)
                    finally:
                        finish.set()
                        responses = await asyncio.gather(*tasks)
                    self.assertEqual([r.status_code for r in responses], [200] * 10)
                with mock.patch.object(server, "_openai_nonstream", side_effect=self.response):
                    for _ in range(12):
                        r = await client.post("/v1/chat/completions", json=self.body, headers=self.headers)
                        self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.billing._inflight, {})
        asyncio.run(run())

    def test_streams_hold_until_upstream_finishes_on_all_chat_protocols(self):
        async def run():
            self.db.update_group(self.gid, concurrency_limit=1)
            cases = [("/v1/chat/completions", self.body, self.headers),
                     ("/v1/messages", dict(self.body, max_tokens=16), self.headers),
                     ("/v1/responses", {"model": "concurrency-model", "input": "hi"}, self.headers),
                     ("/api/playground/chat", self.body,
                      {"Authorization": "Bearer " + auth.issue_token(self.uid, role="user")})]
            async with self.client() as client:
                for path, body, headers in cases:
                    entered, finish = threading.Event(), threading.Event()

                    def stream(_body, holder):
                        yield 'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
                        entered.set()
                        if not finish.wait(10):
                            raise RuntimeError("test stream timed out")
                        holder["terminal"] = True
                        yield "data: [DONE]\n\n"

                    with mock.patch.object(server, "_openai_stream", side_effect=stream):
                        task = asyncio.create_task(client.post(path, json=dict(body, stream=True), headers=headers))
                        try:
                            await self.wait_event(entered)
                            r = await client.post("/v1/chat/completions", json=self.body, headers=self.headers)
                            self.assertEqual(r.status_code, 429, (path, r.text))
                        finally:
                            finish.set()
                            r = await task
                        self.assertEqual(r.status_code, 200, (path, r.text))
                        self.assertEqual(self.billing._inflight, {})
        asyncio.run(run())

    def test_disconnect_drains_and_settles_before_releasing_slot(self):
        async def run(spec):
            self.db.update_group(self.gid, concurrency_limit=1)
            gone, finish, drained = threading.Event(), threading.Event(), threading.Event()

            def stream(_body, holder):
                yield 'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
                if not finish.wait(10):
                    raise RuntimeError("drain timed out")
                holder["usage"] = {"prompt_tokens": 3, "completion_tokens": 2}
                # 模拟 _openai_stream 的完整契约：真实实现会先嗅探终止帧。
                server._sniff_usage("data: [DONE]\n\n", holder)
                drained.set()
                yield "data: [DONE]\n\n"

            payload = json.dumps(dict(self.body, stream=True)).encode()
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
                     "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                     "scheme": "http", "query_string": b"", "server": ("test", 80),
                     "client": ("127.0.0.1", 1234), "http_version": "1.1",
                     "headers": [(b"content-type", b"application/json"),
                                 (b"authorization", self.headers["Authorization"].encode())]}

            request_read = False

            async def receive():
                nonlocal request_read
                if not request_read:
                    request_read = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                while not gone.is_set():
                    await asyncio.sleep(0.005)
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    gone.set()
                    if spec == "2.4":
                        raise OSError("client disconnected")
                    # ASGI 2.3 由 http.disconnect 取消发送任务,保持生成器停在首帧。
                    await asyncio.Event().wait()

            with mock.patch.object(server, "_openai_stream", side_effect=stream):
                task = asyncio.create_task(server.app(scope, receive, send))
                try:
                    await self.wait_event(gone)
                    async with self.client() as client:
                        r = await client.post("/v1/chat/completions", json=self.body, headers=self.headers)
                        self.assertEqual(r.status_code, 429, r.text)
                finally:
                    finish.set()
                    if spec == "2.4":
                        with self.assertRaises(ClientDisconnect):
                            await task
                    else:
                        await task
            self.assertTrue(drained.is_set())
            self.assertEqual(self.billing._inflight, {})
            log = self.db.recent_usage(self.uid, limit=1)[0]
            self.assertEqual(log["pricing_snapshot"]["end_reason"], "client_gone")
            self.assertEqual((log["input_tokens"], log["output_tokens"]), (3, 2))
        for spec in ("2.3", "2.4"):
            with self.subTest(asgi=spec):
                asyncio.run(run(spec))

    def test_invalid_requests_and_upstream_errors_do_not_leak_slots(self):
        async def run():
            self.db.update_group(self.gid, concurrency_limit=1)
            cases = [("/v1/chat/completions", {}), ("/v1/messages", {}),
                     ("/v1/responses", {}), ("/v1/embeddings", {}),
                     ("/v1/structured-data/nvidia/kumo-relational/predictions", {}),
                     ("/v1/images/generations", {}), ("/v1/videos/generations", {})]
            async with self.client() as client:
                for path, body in cases:
                    r = await client.post(path, json=body, headers=self.headers)
                    self.assertGreaterEqual(r.status_code, 400, (path, r.text))
                    self.assertNotEqual(r.status_code, 429, (path, r.text))
                    self.assertEqual(self.billing._inflight, {})
                with mock.patch.object(server, "_openai_nonstream",
                                       side_effect=server.HTTPException(502, "stub upstream failure")):
                    for _ in range(3):
                        r = await client.post("/v1/chat/completions", json=self.body, headers=self.headers)
                        self.assertEqual(r.status_code, 502, r.text)
                self.assertEqual(self.billing._inflight, {})
        asyncio.run(run())

    def test_admin_setting_and_user_views_use_nonnegative_concurrency(self):
        async def run():
            self.db.update_user(self.uid, role="admin")
            headers = {"Authorization": "Bearer " + auth.issue_token(self.uid, role="admin")}
            async with self.client() as client:
                r = await client.patch(f"/api/admin/groups/{self.gid}",
                                       json={"concurrency_limit": 7}, headers=headers)
                self.assertEqual(r.status_code, 200, r.text)
                for path in ("/api/me", "/api/billing"):
                    r = await client.get(path, headers=headers)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["concurrency_limit"], 7)
                    self.assertNotIn("rpm_limit", r.json())
                r = await client.patch(f"/api/admin/groups/{self.gid}",
                                       json={"concurrency_limit": -1}, headers=headers)
                self.assertEqual(r.status_code, 422, r.text)
        asyncio.run(run())

    def test_unlimited_requests_count_for_later_limit_changes(self):
        user = self.db.get_user(self.uid)
        releases = [self.billing.acquire_concurrency(user, {"concurrency_limit": 0}) for _ in range(12)]
        try:
            with self.assertRaises(RateLimitError):
                self.billing.acquire_concurrency(user, {"concurrency_limit": 10})
        finally:
            for release in releases:
                release()
        self.assertEqual(self.billing._inflight, {})
