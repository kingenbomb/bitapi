"""Native protocol and account policies, using generated credentials and mock upstreams."""
import base64
import io
import json
import time
import unittest
from unittest import mock

import tests.test_gpt_import_endpoint as fixture
from tests.test_gpt_import import wire, jwt, complete_response
from core import account_runtime as R, gpt_runtime as G, gpt_models, gpt_auth
from core import adapter as registry
from adapters.gpt import GPTAdapter


class NativeProtocolTest(unittest.TestCase):
    setUpClass = classmethod(fixture.GPTEndpointTest.setUpClass.__func__)
    setUp = fixture.GPTEndpointTest.setUp
    value = fixture.GPTEndpointTest.value

    def add(self, **policy):
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        row = self.pool.list_accounts("gpt")[0]
        self.pool.update_account(row["id"], meta={**row["meta"], "gpt_settings": policy})
        return self.pool.get_account(row["id"])

    def post(self, **body):
        return self.client.post("/v1/responses", headers={"Authorization": "Bearer " + fixture.config.API_KEY}, json={"model": "gpt-5.6-luna", "input": "hi", **body})

    def test_native_tools_context_ownership_and_replay(self):
        self.add()
        calls = []
        def upstream(request, timeout):
            calls.append(json.loads(request.data))
            return io.BytesIO(wire())
        with mock.patch("core.account_runtime.urlopen", side_effect=upstream):
            first = self.post(tools=[{"type": "web_search"}, {"type": "namespace", "name": "workspace", "tools": [{"type": "function", "name": "read", "parameters": {}}]}])
            self.assertEqual(first.status_code, 200, first.text)
            second = self.post(input="next", previous_response_id=first.json()["id"])
            self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(calls[0]["tools"][0]["type"], "web_search")
        self.assertEqual(calls[0]["tools"][1]["type"], "namespace")
        self.assertFalse(calls[0]["store"])
        self.assertEqual(calls[1]["input"][-1]["content"], "next")
        self.assertTrue(any(item.get("role") == "assistant" for item in calls[1]["input"]))
        with mock.patch.object(fixture.server, "_authorize_gateway", return_value=(None, {"id": 99})):
            denied = self.post(previous_response_id=first.json()["id"])
        self.assertEqual(denied.status_code, 400)

    def test_native_billing_stream_and_missing_terminal(self):
        row = self.add()
        with mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, {"id": 2})), mock.patch.object(fixture.server.BILLING, "record_usage") as bill, mock.patch("core.account_runtime.urlopen", side_effect=lambda *a, **k: io.BytesIO(wire())):
            response = self.post(stream=True)
            self.assertIn('response.completed', response.text)
            self.assertEqual(bill.call_count, 1)
            self.assertEqual(bill.call_args.kwargs["end_reason"], "done", response.text)
            self.assertEqual(bill.call_args.kwargs["upstream_usage"]["prompt_tokens"], 11)
            self.assertEqual(self.pool.get_account(row["id"])["last_error"], "")
        with mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, {"id": 2})), mock.patch.object(fixture.server.BILLING, "record_usage") as bill, mock.patch("core.account_runtime.urlopen", side_effect=lambda *a, **k: io.BytesIO(wire(completed=False))):
            response = self.post(stream=True)
            self.assertIn('event: error', response.text)
            self.assertNotIn('event: response.completed', response.text)
            self.assertEqual(bill.call_args.kwargs["end_reason"], "scanner_error")

    def test_model_manifest_mapping_whitelist_and_etag(self):
        row = self.add(model_mapping={"fixture-alias": "upstream-id"}, model_whitelist=["fixture-alias"])
        adapter = registry.get_adapter("gpt")
        gpt_models.cache_manifest(adapter, row, {"models": [{"slug": "upstream-id", "visibility": "list", "supported_in_api": True, "context_window": 12345}, {"slug": "hidden", "visibility": "hide"}]}, "fixture")
        try:
            headers = {"Authorization": "Bearer " + fixture.config.API_KEY}
            response = self.client.get("/backend-api/codex/models?client_version=fixture", headers=headers)
            self.assertEqual([m["slug"] for m in response.json()["models"]], ["fixture-alias"])
            self.assertEqual(response.json()["models"][0]["context_window"], 12345)
            self.assertEqual(self.client.get("/backend-api/codex/models", headers={**headers, "If-None-Match": response.headers["etag"]}).status_code, 304)
            with mock.patch("core.account_runtime.urlopen", side_effect=lambda *a, **k: io.BytesIO(wire())) as upstream:
                self.assertEqual(self.post(model="fixture-alias").status_code, 200)
                self.assertEqual(json.loads(upstream.call_args.args[0].data)["model"], "upstream-id")
            self.assertEqual(self.post(model="gpt-5.6-luna").status_code, 404)
        finally:
            self.pool.delete_channel("gpt")
            gpt_models.rebuild(adapter)

    def test_temporary_probe_failures_keep_account_group_catalog_and_routing(self):
        models = ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
        row = self.add()
        adapter = registry.get_adapter("gpt")
        gpt_models.cache_manifest(adapter, row, {"models": [
            {"slug": model, "visibility": "list", "supported_in_api": True} for model in models
        ]}, fixture.config.GPT_CLIENT_VERSION)
        for model in models:
            # One advertised model has never completed a probe; a temporary
            # failure must not revoke the upstream manifest's support either.
            if model != "gpt-6-luna":
                gpt_models.record_test(adapter, self.pool.get_account(row["id"]),
                    {"model": model, "ok": True, "elapsed_ms": 10})
            gpt_models.record_test(adapter, self.pool.get_account(row["id"]),
                {"model": model, "ok": False, "elapsed_ms": 20, "error_code": 503,
                 "error_kind": "capacity", "error": "Our servers are currently overloaded. Please try again later."})
        row = self.pool.get_account(row["id"])
        self.assertTrue(set(models).issubset({m["slug"] for m in gpt_models.entries(row, [])}))
        channels = self.client.get("/api/admin/channels", headers=self.admin).json()["channels"]
        self.assertTrue(set(models).issubset(next(c["models"] for c in channels if c["name"] == "gpt")))
        headers = {"Authorization": "Bearer " + fixture.config.API_KEY}
        public = self.client.get("/v1/models", headers=headers).json()["data"]
        self.assertTrue(set(models).issubset({m["id"] for m in public}))
        manifest = self.client.get("/backend-api/codex/models", headers=headers).json()["models"]
        self.assertTrue(set(models).issubset({m["slug"] for m in manifest}))
        for model in models:
            with self.subTest(model=model):
                self.assertTrue(G.eligible(self.pool, row, model, defaults=adapter.configured_models))
                with mock.patch("core.account_runtime.urlopen", side_effect=lambda *a, **k: io.BytesIO(wire())):
                    response = self.post(model=model)
                    self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.pool.get_account(row["id"])["meta"]["pool_model_checks"]["gpt-6-astra"]["ok"])

    def test_verified_only_models_survive_transient_errors_but_not_explicit_rejections(self):
        row = self.add()
        adapter = registry.get_adapter("gpt")
        gpt_models.record_test(adapter, row, {"model": "fixture-verified", "ok": True, "elapsed_ms": 10})
        gpt_models.record_test(adapter, self.pool.get_account(row["id"]),
            {"model": "fixture-verified", "ok": False, "elapsed_ms": 20, "error": "upstream stream idle timeout"})
        gpt_models.record_test(adapter, self.pool.get_account(row["id"]),
            {"model": "fixture-never-verified", "ok": False, "elapsed_ms": 20, "error": "HTTP 502: overloaded"})
        names = {m["slug"] for m in gpt_models.entries(self.pool.get_account(row["id"]), [])}
        self.assertIn("fixture-verified", names)
        self.assertNotIn("fixture-never-verified", names)
        gpt_models.record_test(adapter, self.pool.get_account(row["id"]),
            {"model": "fixture-verified", "ok": False, "elapsed_ms": 20,
             "error_code": 400, "error": "model_not_found: model is not supported"})
        row = self.pool.get_account(row["id"])
        self.assertNotIn("fixture-verified", {m["slug"] for m in gpt_models.entries(row, [])})
        self.assertFalse(G.eligible(self.pool, row, "fixture-verified", defaults=[]))

    def test_spark_parent_ownership_capacity_and_no_secret_copy(self):
        row = self.add()
        self.pool.update_account(row["id"], concurrency=1)
        row = self.pool.get_account(row["id"])
        child = gpt_models.create_shadow(self.pool, row, ["fixture-spark"])
        self.assertEqual(child["secret"], {})
        self.assertEqual(G.credential_account(self.pool, child)["id"], row["id"])
        self.assertFalse(G.eligible(self.pool, row, "fixture-spark", defaults=["fixture-spark"]))
        with R.dispatch(self.pool, child):
            with self.assertRaises(R.AccountBusyError):
                with R.dispatch(self.pool, row):
                    pass
        with self.assertRaises(ValueError):
            gpt_models.create_shadow(self.pool, row, ["fixture-spark"])
        G.edit_meta(self.pool, row["id"], lambda meta: meta.update(admin_paused=True))
        self.assertFalse(G.eligible(self.pool, child, "fixture-spark"))

    def test_spark_export_import_references_parent_without_copying_credentials(self):
        from core.account_transfer import export_data, import_data
        row = self.add(ws_mode="passthrough")
        self.pool.update_account(row["id"], concurrency=3, priority=7)
        row = self.pool.get_account(row["id"])
        child = gpt_models.create_shadow(self.pool, row, ["fixture-spark"])
        data = export_data([child, row])
        self.assertEqual(data["accounts"][0]["credentials"], {})
        self.assertEqual(data["accounts"][0]["parent_identity"], row["identity"])
        self.pool.delete_channel("gpt")
        preview = import_data(json.dumps(data), preview=True)
        self.assertEqual(preview["failed"], 0, preview)
        self.assertEqual(self.pool.list_accounts("gpt"), [])
        result = import_data(json.dumps(data))
        self.assertEqual(result["failed"], 0, result)
        rows = self.pool.list_accounts("gpt")
        parent = next(r for r in rows if not (r.get("meta") or {}).get("parent_account_id"))
        shadow = next(r for r in rows if (r.get("meta") or {}).get("parent_account_id"))
        self.assertEqual(shadow["meta"]["parent_account_id"], parent["id"])
        self.assertEqual(shadow["concurrency"], 3)
        self.assertEqual(shadow["priority"], 7)
        self.assertEqual(parent["meta"]["gpt_settings"]["ws_mode"], "passthrough")

    def test_compact_uses_model_mapping_and_policy(self):
        row = self.add(compact_model_mapping={"gpt-5.6-luna": "compact-fixture"})
        headers = {"Authorization": "Bearer " + fixture.config.API_KEY}
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(json.dumps({"id": "compact", "output": [{"type": "compaction", "encrypted_content": "opaque"}]}).encode())) as upstream:
            response = self.client.post("/v1/responses/compact", headers=headers, json={"model": "gpt-5.6-luna", "input": "hi"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(upstream.call_args.args[0].full_url.endswith("/responses/compact"))
        self.assertEqual(json.loads(upstream.call_args.args[0].data)["model"], "compact-fixture")
        G.edit_meta(self.pool, row["id"], lambda meta: meta.update(gpt_settings={"compact_mode": "disabled"}))
        response = self.client.post("/v1/responses/compact", headers=headers, json={"model": "gpt-5.6-luna", "input": "hi"})
        self.assertEqual(response.status_code, 400)

    def test_websocket_v2_multiple_turns_context_and_billing(self):
        self.add(ws_mode="passthrough")
        class Socket:
            def __init__(self):
                self.sent, self.events, self.closed = [], [], False
            def send(self, data):
                self.sent.append(json.loads(data))
                self.events = [json.dumps({"type": "response.completed", "response": complete_response()})]
            def settimeout(self, timeout):
                self.timeout = timeout
            def recv_data(self, control_frame=False):
                del control_frame  # 签名由 websocket-client 基类定死,刻意不用
                return 1, self.events.pop(0)
            def close(self):
                self.closed = True
        upstream = Socket()
        with mock.patch("core.gpt_native.open_ws", return_value=upstream) as connect, mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, {"id": 2})), mock.patch.object(fixture.server.BILLING, "record_usage") as bill:
            with self.client.websocket_connect("/v1/responses", headers={"Authorization": "Bearer " + fixture.config.API_KEY}) as ws:
                ws.send_json({"type": "response.create", "model": "gpt-5.6-luna", "input": "hi"})
                one = ws.receive_json()
                self.assertEqual(one["type"], "response.completed")
                ws.send_json({"type": "response.create", "model": "gpt-5.6-luna", "input": "next", "previous_response_id": one["response"]["id"]})
                self.assertEqual(ws.receive_json()["type"], "response.completed")
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(len(upstream.sent), 2)
            self.assertEqual(upstream.sent[1]["input"][-1]["content"], "next")
            self.assertEqual(bill.call_count, 2)
        self.assertTrue(upstream.closed)

    def test_websocket_active_output_outlives_total_timeout_and_heartbeats_expire(self):
        import websocket
        from core import gpt_native as native, sse
        row = self.add(ws_mode="ctx_pool")
        adapter = registry.get_adapter("gpt")
        clock = [0.0]
        class Socket:
            closed = False
            def __init__(self, frames):
                self.frames = iter(frames)
            def settimeout(self, timeout):
                self.timeout = timeout
            def send(self, data):
                pass
            def recv_data(self, control_frame=False):
                del control_frame  # 签名由 websocket-client 基类定死,刻意不用
                at, opcode, value = next(self.frames)
                clock[0] = at
                return opcode, json.dumps(value) if isinstance(value, dict) else value
            def close(self):
                self.closed = True
        active = Socket([(60, 1, {"type": "response.output_text.delta", "delta": "a"}),
                         (120, 1, {"type": "response.function_call_arguments.delta", "delta": "{"}),
                         (180, 1, {"type": "response.completed", "response": complete_response()})])
        holder = {}
        with mock.patch("core.gpt_native.open_ws", return_value=active), mock.patch("core.gpt_native.time.monotonic", side_effect=lambda: clock[0]), mock.patch.object(fixture.config, "GPT_STREAM_IDLE_TIMEOUT", 90):
            events = list(native.events(adapter, self.pool, row, {"model": "gpt-5.6-luna", "input": "hi"}, holder, socket_state={}))
        self.assertTrue(holder["terminal"])
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertGreater(clock[0], fixture.config.SEND_TIMEOUT)
        self.assertTrue(active.closed)
        clock[0] = 0
        ping = websocket.ABNF.OPCODE_PING
        idle = Socket([(50, ping, b"ping"), (100, 1, {"type": "response.in_progress"}), (150, ping, b"ping")])
        state = {}
        with mock.patch("core.gpt_native.open_ws", return_value=idle), mock.patch("core.gpt_native.time.monotonic", side_effect=lambda: clock[0]), mock.patch.object(fixture.config, "GPT_STREAM_IDLE_TIMEOUT", 120):
            with self.assertRaises(sse.StreamIdleTimeout):
                list(native.events(adapter, self.pool, row, {"model": "gpt-5.6-luna", "input": "hi"}, {}, socket_state=state))
        self.assertTrue(idle.closed)
        self.assertNotIn("connection", state)

    def test_websocket_capacity_reopens_before_output_and_keeps_one_result(self):
        from core import gpt_native as native, gpt_request_policy as policy
        from types import SimpleNamespace
        row = self.add(ws_mode="ctx_pool")
        class Socket:
            closed = False
            def __init__(self, frames):
                self.frames = iter(frames)
            def settimeout(self, timeout):
                pass
            def send(self, data):
                pass
            def recv_data(self, control_frame=False):
                del control_frame  # 签名由 websocket-client 基类定死,刻意不用
                return 1, json.dumps(next(self.frames))
            def close(self):
                self.closed = True
        rejected = Socket([{"type": "response.created", "response": {"id": "abandoned"}},
                           {"type": "error", "error": {"code": "server_is_overloaded"}}])
        completed = Socket([{"type": "response.completed", "response": complete_response()}])
        holder, state = {}, {}
        with mock.patch("core.gpt_native.open_ws", side_effect=[rejected, completed]) as connect, mock.patch.object(policy, "time", SimpleNamespace(sleep=mock.Mock())):
            result = list(native.events(registry.get_adapter("gpt"), self.pool, row,
                {"model": "gpt-5.6-luna", "input": "hi"}, holder, socket_state=state))
        self.assertEqual(connect.call_count, 2)
        self.assertEqual([event["type"] for event in result], ["response.completed"])
        self.assertTrue(holder["terminal"])
        self.assertTrue(rejected.closed and completed.closed)
        self.assertNotIn("connection", state)

    def test_native_idle_timeout_has_504_and_correct_billing_reason(self):
        from core.sse import StreamIdleTimeout
        self.add()
        with mock.patch("core.account_runtime.urlopen", side_effect=StreamIdleTimeout("upstream stream idle timeout: no model output")), mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, {"id": 2})), mock.patch.object(fixture.server.BILLING, "record_usage") as bill:
            response = self.post(stream=False)
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(bill.call_args.kwargs["end_reason"], "idle_timeout")

    def test_flat_namespace_history_and_output_are_reversible(self):
        self.add(flatten_namespaces=True)
        tools = [{"type": "namespace", "name": "files", "tools": [{"type": "function", "name": "read", "parameters": {}}]}]
        response = complete_response()
        response["output"] = [{"type": "function_call", "name": "files__read", "call_id": "call-new", "arguments": "{}"}]
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode())) as upstream:
            result = self.post(tools=tools, input=[{"type": "function_call", "namespace": "files", "name": "read", "call_id": "call-old", "arguments": "{}"}, {"type": "function_call_output", "call_id": "call-old", "output": "ok"}])
        self.assertEqual(result.status_code, 200, result.text)
        payload = json.loads(upstream.call_args.args[0].data)
        self.assertEqual(payload["input"][0]["name"], "files__read")
        self.assertNotIn("namespace", payload["input"][0])
        self.assertEqual(payload["tools"][0]["name"], "files__read")
        self.assertEqual(result.json()["output"][0]["name"], "read")
        self.assertEqual(result.json()["output"][0]["namespace"], "files")

    def test_pat_online_validation_preview_never_returns_token(self):
        token = "at-fixture-token"
        whoami = {"email": "pat@example.test", "chatgpt_account_id": "pat-account", "chatgpt_user_id": "pat-user", "chatgpt_plan_type": "plus"}
        with mock.patch("core.account_runtime.urlopen", side_effect=lambda *a, **k: io.BytesIO(json.dumps(whoami).encode())):
            preview = self.client.post("/api/admin/pool/pat/gpt", headers=self.admin, json={"token": token})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertNotIn(token, preview.text)
            self.assertEqual(self.pool.list_accounts("gpt"), [])
            result = self.client.post("/api/admin/pool/pat/gpt", headers=self.admin, json={"token": token, "confirmed": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.pool.list_accounts("gpt")[0]["secret"]["auth_mode"], "personal_access_token")

    def test_image_tool_results_are_returned(self):
        self.add()
        response = complete_response()
        response["output"] = [{"type": "image_generation_call", "result": "fixture-pixels"}]
        upstream = io.BytesIO(("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode())
        with mock.patch("core.account_runtime.urlopen", return_value=upstream):
            result = self.client.post("/v1/images/generations", headers={"Authorization": "Bearer " + fixture.config.API_KEY}, json={"model": "gpt-5.6-luna", "prompt": "a square"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["data"][0]["b64_json"], "fixture-pixels")

    def test_missing_image_result_settles_as_failure(self):
        self.add()
        with mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, {"id": 2})), mock.patch.object(fixture.server.BILLING, "record_usage") as bill, mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire())):
            result = self.client.post("/v1/images/generations", headers={"Authorization": "Bearer " + fixture.config.API_KEY}, json={"model": "gpt-5.6-luna", "prompt": "a square"})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(bill.call_args.kwargs["end_reason"], "scanner_error")

    def test_websocket_rejects_unauthenticated_handshake(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/v1/responses"):
                self.fail("unauthenticated connection accepted")


class AgentIdentityTest(unittest.TestCase):
    def test_assertion_signature_and_encrypted_task_registration(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from nacl.signing import SigningKey
        from nacl.public import SealedBox
        key = Ed25519PrivateKey.generate()
        encoded = base64.b64encode(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()
        secret = {"agent_runtime_id": "runtime-fixture", "agent_private_key": encoded, "task_id": "task-fixture"}
        assertion = gpt_auth.assertion(secret).split(" ")[1]
        envelope = json.loads(base64.urlsafe_b64decode(assertion + "=" * (-len(assertion) % 4)))
        key.public_key().verify(base64.b64decode(envelope["signature"]), ("runtime-fixture:task-fixture:" + envelope["timestamp"]).encode())
        seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        encrypted = base64.b64encode(SealedBox(SigningKey(seed).verify_key.to_curve25519_public_key()).encrypt(b"new-task")).decode()
        with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(json.dumps({"encrypted_task_id": encrypted}).encode())):
            self.assertEqual(gpt_auth.register_task(secret), "new-task")
        from core.gpt_accounts import normalize_entry
        self.assertEqual(normalize_entry(secret)["auth_mode"], "agentIdentity")
        pat = normalize_entry({"access_token": "at-fixture", "refresh_token": "must-drop", "id_token": "must-drop", "client_id": "must-drop"})
        self.assertFalse(pat["refresh_token"] or pat["id_token"] or pat["client_id"])
