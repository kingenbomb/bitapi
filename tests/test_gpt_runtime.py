import tempfile
import time
import unittest
import io
import config
from unittest import mock

from core.db import DB
from core import gpt_runtime as G


class GPTRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DB(self.temp.name + "/accounts.db")
        self.addCleanup(self.db.close)
        self.aid = self.db.upsert_account("gpt", "fixture@example.test", secret={"access_token": "fixture"},
            meta={"note": "keep", "admin_paused": True}, status="active")

    def row(self):
        return self.db.get_account(self.aid)

    def test_response_headers_map_windows_and_preserve_admin_state(self):
        G.capture_headers(self.db, self.row(), {
            "X-Codex-Primary-Used-Percent": "100", "X-Codex-Primary-Window-Minutes": "300",
            "X-Codex-Primary-Reset-After-Seconds": "60", "x-codex-secondary-used-percent": "18",
            "x-codex-secondary-window-minutes": "10080", "x-codex-secondary-reset-after-seconds": "600"})
        row = self.row()
        self.assertEqual(row["meta"]["pool_upstream_usage"]["five_hour"]["utilization"], 100)
        self.assertEqual(row["meta"]["pool_upstream_usage"]["seven_day"]["utilization"], 18)
        self.assertEqual(row["meta"]["note"], "keep")
        self.assertTrue(row["meta"]["admin_paused"])
        self.assertGreater(row["upstream_until"], time.time())
        self.assertFalse(G.eligible(self.db, row))
        self.assertEqual(G.quota_block(row, now=time.time() + 70), (0, ""))

    def test_query_snapshot_stops_selection_and_recovers_at_reset(self):
        now = int(time.time())
        self.db.update_account(self.aid, meta={"admin_paused": False})
        G.save_usage(self.db, self.row(), {"five_hour": {"utilization": 100, "reset_at": now + 60}, "fetched_at": now})
        self.assertFalse(G.eligible(self.db, self.row()))
        self.assertIsNone(self.db.pick_active("gpt"))
        G.edit_meta(self.db, self.aid, lambda meta: meta.update(admin_paused=True))
        G.save_usage(self.db, self.row(), {"five_hour": {"utilization": 10, "reset_at": now + 60}, "fetched_at": now})
        self.assertEqual(self.row()["upstream_until"], 0)
        self.assertTrue(self.row()["meta"]["admin_paused"], "quota recovery must never undo an administrator pause")

    def test_model_limit_does_not_disable_unrelated_models(self):
        class RateLimit(Exception):
            code = 429
            headers = {"Retry-After": "30"}
            details = {}
        G.record_limit(self.db, self.row(), RateLimit(), "model-a", final=True)
        self.assertFalse(G.eligible(self.db, self.row(), "model-a"))
        self.assertTrue(G.eligible(self.db, self.row(), "model-b"))

    def test_sticky_binding_is_scoped_to_caller_and_group(self):
        client = {"owner": "one", "session": "same"}
        G.bind_session(self.db, self.row(), client, 3)
        self.assertEqual(G.preferred_id(self.db, client, 3), self.aid)
        self.assertIsNone(G.preferred_id(self.db, {**client, "owner": "two"}, 3))
        self.assertIsNone(G.preferred_id(self.db, client, 4))

    def test_parent_pause_blocks_shadow_and_credentials_resolve_to_parent(self):
        child = {**self.row(), "meta": {"parent_account_id": self.aid}}
        self.assertEqual(G.credential_account(self.db, child)["id"], self.aid)
        self.assertFalse(G.eligible(self.db, child))

    def _probe_account(self):
        from adapters.gpt import GPTAdapter
        self.db.update_account(self.aid, token="fixture", token_exp=int(time.time()) + 7200,
                               meta={"admin_paused": False}, proxy_id=7)
        return GPTAdapter(db=self.db)

    def test_model_probe_fallback_keeps_account_available_and_uses_its_proxy(self):
        from adapters.openai_compat import UpstreamError
        from core import model_status, account_runtime
        from core.pool import Pool
        from tests.test_gpt_import import wire
        adapter = self._probe_account()
        self.db.update_account(self.aid, last_error="HTTP 502 from upstream: Our servers are currently overloaded")
        proxy = "http://proxy.example.test:8080"
        with mock.patch.object(self.db, "get_pool_proxy", return_value={"enabled": True, "url": proxy}), \
             mock.patch("core.gpt_transport.open_request", side_effect=[UpstreamError(502, "overloaded"), io.BytesIO(wire())]) as upstream, \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("probe bypassed account transport")):
            result = model_status.probe_one(adapter, Pool(self.db), self.db, adapter.models[0])
        self.assertEqual(result[0], model_status.OK)
        self.assertEqual(upstream.call_count, 2)
        self.assertTrue(all(call.args[2] == proxy for call in upstream.call_args_list))
        self.assertEqual(self.row()["status"], "active")
        self.assertEqual(self.row()["last_error"], "")
        self.assertIsNotNone(self.db.pick_active("gpt"))
        self.assertIsNone(account_runtime._NETWORK.get(), "probe account context must be restored")

    def test_model_probe_transient_gateway_failure_preserves_cause_without_parking_account(self):
        from adapters.openai_compat import UpstreamError
        from core import model_status
        from core.pool import Pool
        adapter = self._probe_account()
        with mock.patch.object(self.db, "get_pool_proxy", return_value={"enabled": True, "url": "http://proxy.example.test:8080"}), \
             mock.patch("core.gpt_transport.open_request", side_effect=UpstreamError(502, "Bad Gateway fixture")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("probe bypassed account transport")):
            result = model_status.probe_one(adapter, Pool(self.db), self.db, adapter.models[0])
        self.assertEqual(result[0], model_status.FAIL, "a failed attempt must not become an empty-pool skip")
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNotNone(self.db.pick_active("gpt"))
        self.assertIn("Bad Gateway", result[3])
        self.assertIn("Bad Gateway", self.row()["last_error"])
        self.assertNotIn("fixture", self.row()["last_error"], "upstream diagnostics must redact the account token")


    def test_model_probe_records_capacity_failure_but_keeps_healthy_gpt_account_in_pool(self):
        from adapters.openai_compat import UpstreamError
        from core import model_status
        from core.pool import Pool
        adapter = self._probe_account()
        with mock.patch.object(self.db, "get_pool_proxy", return_value={"enabled": True, "url": "http://proxy.example.test:8080"}), mock.patch(
                "core.gpt_transport.open_request", side_effect=UpstreamError(502, "Our servers are currently overloaded")) as upstream:
            result = model_status.probe_one(adapter, Pool(self.db), self.db, adapter.models[0])
        self.assertEqual(upstream.call_count, config.GPT_CAPACITY_RETRIES + 1, "do not multiply the exhausted request retry budget in the probe fallback")
        self.assertEqual(result[0], model_status.FAIL)
        self.assertIn("overloaded", result[3])
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNotNone(self.db.pick_active("gpt"))

    def test_test_budget_bounds_account_slot_and_refresh_lock_without_leaking_locks(self):
        import threading
        from core import account_runtime as runtime
        self._probe_account()
        self.db.update_account(self.aid, concurrency=1)
        row = self.row()
        with runtime.dispatch(self.db, row, record=False):
            with self.assertRaises(runtime.AccountTestTimeout), runtime.test_budget(0.03), runtime.dispatch(self.db, row, record=False):
                self.fail("a queued request cannot acquire the held account")
        self.assertIsNone(runtime._DEADLINE.get())
        self.assertEqual(runtime.capacity(self.aid), 0)
        with runtime.test_budget(0.1), runtime.dispatch(self.db, row, record=False):
            self.assertEqual(runtime.capacity(self.aid), 1)
        lock = threading.Lock()
        lock.acquire()
        try:
            with self.assertRaises(runtime.AccountTestTimeout), runtime.test_budget(0.03), runtime.refresh_lock(lock):
                self.fail("a held lock cannot be acquired")
        finally:
            lock.release()
        with runtime.refresh_lock(lock):
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())

    def test_real_curl_test_budget_stops_slow_headers_and_heartbeat_only_streams(self):
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from core import account_runtime as runtime, gpt_transport, codex
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path == "/headers":
                        time.sleep(0.35)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    until = time.monotonic() + 0.35
                    while time.monotonic() < until:
                        self.wfile.write(b": keep-alive heartbeat\n\n")
                        self.wfile.flush()
                        time.sleep(0.015)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for path in ("/headers", "/heartbeat"):
                with self.subTest(path=path):
                    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}")
                    started = time.monotonic()
                    with self.assertRaises(runtime.AccountTestTimeout), runtime.test_budget(0.15):
                        with gpt_transport.open_request(request, 120) as response:
                            list(codex.iter_events(response, 120))
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertIsNone(runtime._DEADLINE.get())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)
