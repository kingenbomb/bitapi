"""Single Web images through real pool/admin routes with fictional credentials."""
import base64
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tests.test_gpt_import_endpoint as fixture
from adapters.openai_compat import UpstreamError
from core import sse
from core.adapter import BILL_ESTIMATE, GenerationSubmittedError, get_adapter
from core.gpt_web import runtime as W
from core.gpt_web.client import WebImageClient
from core.gpt_web.protocol import ensure_ok

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1xkAAAAASUVORK5CYII=")


def response(data=None, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=json.dumps(data or {}),
                           json=lambda: data or {})


class ProtocolTest(unittest.TestCase):
    def client(self):
        client = WebImageClient({"id": 1, "token": "fictional-access-token", "secret": {}})
        self.addCleanup(client.close)
        return client

    def test_access_token_protocol_uses_http_bootstrap_without_browser_or_codex(self):
        client = self.client()
        wire = io.BytesIO(b'data: {"conversation_id":"fictional-conversation"}\n\ndata: [DONE]\n\n')
        wire.status_code, wire.headers = 200, {}
        replies = [response({"prepare_token": "prepare"}), response({"token": "requirements"}),
                   response({"conduit_token": "conduit"}), wire]
        conversation = {"mapping": {"image": {"message": {"author": {"role": "tool"},
            "metadata": {"async_task_type": "image_gen"}, "content": {"parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://fictional-file"}]}}}}}
        with mock.patch.object(client.session, "post", side_effect=replies) as post, \
                mock.patch.object(client.session, "get", side_effect=[SimpleNamespace(status_code=200, headers={}, text='<html data-build="fixture-build"><script src="/fixture.js"></script></html>'), response(conversation), response({"download_url": "https://cdn.example.test/image"})]) as get, \
                mock.patch.object(client, "_download", return_value=PNG) as download:
            self.assertEqual(client.generate("cat"), PNG)
        paths = [call.args[0].removeprefix("https://chatgpt.com") for call in post.call_args_list]
        self.assertEqual(paths, ["/backend-api/sentinel/chat-requirements/prepare",
                                "/backend-api/sentinel/chat-requirements/finalize",
                                "/backend-api/f/conversation/prepare", "/backend-api/f/conversation"])
        self.assertEqual(get.call_args_list[0].args[0], "https://chatgpt.com/")
        self.assertEqual(client.pow_script_sources, ["/fixture.js"])
        self.assertEqual(client.pow_data_build, "fixture-build")
        self.assertTrue(all("codex" not in call.args[0] for call in get.call_args_list))
        self.assertEqual(client.session.headers["Authorization"], "Bearer fictional-access-token")
        download.assert_called_once_with("https://cdn.example.test/image")

    def test_network_challenge_is_not_reported_as_invalid_token(self):
        with self.assertRaises(UpstreamError) as caught:
            ensure_ok(response(status=403, headers={"cf-mitigated": "challenge"}), "/backend-api/sentinel/chat-requirements/prepare")
        self.assertEqual(caught.exception.error_kind, "network_challenge")
        self.assertIn("尚不能据此判断 access_token 失效", str(caught.exception))

    def test_lost_submit_response_does_not_resubmit_generation(self):
        client = self.client()
        with mock.patch.object(client, "_bootstrap"), mock.patch.object(client, "_get_chat_requirements"), mock.patch.object(client, "_prepare_image_conversation"), \
                mock.patch.object(client, "_start_image_generation", side_effect=sse.StreamIdleTimeout("idle")) as submit:
            with self.assertRaises(GenerationSubmittedError):
                client.generate("cat")
        submit.assert_called_once()

    def test_idle_after_conversation_id_only_polls_same_conversation(self):
        client = self.client()
        def receive(_):
            client.conversation_id = "already-submitted"
            raise sse.StreamIdleTimeout("idle")
        with mock.patch.object(client, "_bootstrap"), mock.patch.object(client, "_get_chat_requirements"), mock.patch.object(client, "_prepare_image_conversation"), \
                mock.patch.object(client, "_start_image_generation") as submit, \
                mock.patch.object(client, "_receive", side_effect=receive), mock.patch.object(client, "_result", return_value=PNG):
            self.assertEqual(client.generate("cat"), PNG)
        submit.assert_called_once()
        self.assertEqual(client.conversation_id, "already-submitted")

    def test_sse_uses_same_fingerprint_auth_and_cookies_as_prepare(self):
        client = self.client()
        client.session.raw.cookies.set("__cf_bm", "fictional-cookie", domain="chatgpt.com")
        raw = SimpleNamespace(response=SimpleNamespace(status_code=200))
        with mock.patch("core.gpt_web.client.StreamResponse", return_value=raw) as stream:
            client.session.post("https://chatgpt.com/backend-api/f/conversation", json={"prompt": "cat"}, stream=True)
        request = stream.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fictional-access-token")
        self.assertIn("__cf_bm=fictional-cookie", request.get_header("Cookie"))
        self.assertIn("Chrome/152.", request.get_header("User-agent"))
        self.assertEqual(stream.call_args.kwargs["impersonate"], "chrome152")


class WebImageRouteTest(unittest.TestCase):
    setUpClass = classmethod(fixture.GPTEndpointTest.setUpClass.__func__)
    setUp = fixture.GPTEndpointTest.setUp
    value = fixture.GPTEndpointTest.value

    def add(self):
        result = self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        self.assertEqual(result.status_code, 200, result.text)
        return self.pool.list_accounts("gpt")[0]

    def post_image(self, **changes):
        return self.client.post("/v1/images/generations", headers={"Authorization": "Bearer " + fixture.config.API_KEY},
            json={"model": W.MODEL, "prompt": "one cat", **changes})

    def test_public_reuses_cooldown_account_and_ignores_only_codex_quota(self):
        row = self.add()
        self.pool.update_account(row["id"], status="cooldown", last_error="Codex unauthorized",
            upstream_until=int(time.time()) + 3600, meta={**row["meta"], "gpt_rate_limits": {"*": time.time() + 3600},
            "pool_upstream_usage": {"allowed": False, "fetched_at": int(time.time())}})
        with mock.patch.object(WebImageClient, "generate", return_value=PNG) as generate, \
                mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, None)), \
                mock.patch.object(fixture.server.BILLING, "record_usage") as bill, \
                mock.patch.object(get_adapter("gpt"), "_open_codex") as codex:
            result = self.post_image()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(base64.b64decode(result.json()["data"][0]["b64_json"]), PNG)
        generate.assert_called_once_with("one cat", None)
        codex.assert_not_called()
        self.assertEqual(bill.call_count, 1)
        self.assertEqual(bill.call_args.kwargs["units"], 1)
        self.assertEqual(bill.call_args.kwargs["upstream_account_id"], row["id"])
        self.assertEqual(get_adapter("gpt").billing_mode_for(W.MODEL), BILL_ESTIMATE)
        self.assertEqual(result.json()["usage"]["token_source"], "estimate")
        self.assertEqual(bill.call_args.kwargs["estimated_usage"]["completion_tokens"], result.json()["usage"]["output_tokens"])
        current = self.pool.get_account(row["id"])
        self.assertEqual(current["status"], "cooldown")
        self.assertEqual(current["last_error"], "Codex unauthorized")
        self.assertTrue(current["meta"]["gpt_web_image"]["ok"])

    def test_web_selection_keeps_pause_group_expiry_and_monetary_quota(self):
        row = self.add()
        self.pool.update_account(row["id"], status="cooldown")
        with mock.patch.object(WebImageClient, "generate", return_value=PNG) as generate:
            for fields in ({"meta": {"admin_paused": True}}, {"temp_until": int(time.time()) + 600},
                           {"account_expires_at": 1, "auto_pause_on_expired": 1}, {"status": "dead"}):
                with self.subTest(fields=fields):
                    original = self.pool.get_account(row["id"])
                    self.pool.update_account(row["id"], **fields)
                    self.assertEqual(self.post_image().status_code, 503)
                    self.pool.update_account(row["id"], **{key: original[key] for key in fields})
            with mock.patch("core.account_management.quota_exceeded", return_value=True):
                self.pool.update_account(row["id"], quota_daily_limit=1)
                self.assertEqual(self.post_image().status_code, 503)
            self.pool.update_account(row["id"], quota_daily_limit=0, group_ids=[991])
            with mock.patch.object(fixture.server, "_authorize_gateway", return_value=(None, {"id": 992})):
                self.assertEqual(self.post_image().status_code, 503)
        generate.assert_not_called()

    def test_admin_preview_exact_account_keeps_chat_state_and_no_user_bill(self):
        row = self.add()
        self.pool.update_account(row["id"], status="cooldown", last_error="Codex failed", meta={**row["meta"], "admin_paused": True})
        catalog = self.client.get(f'/api/admin/pool/accounts/{row["id"]}/models', headers=self.admin).json()
        self.assertEqual(catalog["image_models"][0]["id"], W.MODEL)
        self.assertNotIn(W.MODEL, [model["id"] for model in catalog["models"]])
        with mock.patch.object(WebImageClient, "generate", return_value=PNG), mock.patch.object(fixture.server.BILLING, "record_usage") as bill:
            response = self.client.post(f'/api/admin/pool/accounts/{row["id"]}/test-image', headers=self.admin,
                json={"model": W.MODEL, "prompt": "cat"})
        frames = [json.loads(frame.split("\ndata: ", 1)[1]) for frame in response.text.strip().split("\n\n") if frame.startswith("event: result")]
        self.assertTrue(frames[0]["ok"], frames)
        self.assertEqual(base64.b64decode(frames[0]["images"][0]["b64_json"]), PNG)
        bill.assert_not_called()
        current = self.pool.get_account(row["id"])
        self.assertEqual(current["status"], "cooldown")
        self.assertEqual(current["last_error"], "Codex failed")
        self.assertTrue(current["meta"]["admin_paused"])
        self.assertNotIn("pool_model_checks", current["meta"])
        history = self.client.get(f'/api/admin/pool/accounts/{row["id"]}/tests', headers=self.admin).json()["history"]
        self.assertNotIn("images", history[0])

    def test_challenge_error_records_web_only_and_is_not_billed(self):
        row = self.add()
        error = UpstreamError(403, "network challenge")
        error.error_kind = "network_challenge"
        with mock.patch.object(WebImageClient, "generate", side_effect=error) as generate, \
                mock.patch.object(fixture.server.POOL, "mark_failure") as penalize, \
                mock.patch.object(fixture.server, "_authorize_gateway", return_value=({"id": 1}, None)), \
                mock.patch.object(fixture.server.BILLING, "record_usage") as bill:
            result = self.post_image()
        self.assertEqual(result.status_code, 403, result.text)
        generate.assert_called_once()
        penalize.assert_not_called()
        self.assertEqual(bill.call_args.kwargs["end_reason"], "error")
        self.assertEqual(self.pool.get_account(row["id"])["meta"]["gpt_web_image"]["error_kind"], "network_challenge")

    def test_invalid_image_options_and_chat_route_do_not_submit(self):
        self.add()
        with mock.patch.object(WebImageClient, "generate") as generate:
            for body in ({"n": 2}, {"response_format": "url"}, {"quality": "high"}, {"prompt": ""}):
                self.assertEqual(self.post_image(**body).status_code, 400)
            response = self.client.post("/v1/responses", headers={"Authorization": "Bearer " + fixture.config.API_KEY},
                json={"model": W.MODEL, "input": "hi"})
            self.assertEqual(response.status_code, 400, response.text)
            for stream in (False, True):
                response = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + fixture.config.API_KEY},
                    json={"model": W.MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": stream})
                self.assertEqual(response.status_code, 400, response.text)
        generate.assert_not_called()

    def test_image_policy_and_non_web_credentials_are_rejected_before_submission(self):
        row = self.add()
        path = f'/api/admin/pool/accounts/{row["id"]}/test-image'
        with mock.patch.object(WebImageClient, "generate") as generate:
            for changes in ({"meta": {"gpt_settings": {"image_tool": "disabled"}}},
                            {"meta": {"gpt_settings": {"model_whitelist": ["other"]}}},
                            {"token": "at-fictional-pat"},
                            {"secret": {"auth_mode": "agentIdentity"}},
                            {"meta": {"parent_account_id": 991}}):
                with self.subTest(changes=changes):
                    self.pool.update_account(row["id"], **changes)
                    result = self.client.post(path, headers=self.admin, json={"model": W.MODEL, "prompt": "cat"})
                    self.assertEqual(result.status_code, 400, result.text)
                    self.pool.update_account(row["id"], **{key: row[key] for key in changes})
            self.assertEqual(self.client.post(path, headers=self.user, json={"model": W.MODEL, "prompt": "cat"}).status_code, 403)
            self.assertEqual(self.client.post(path, json={"model": W.MODEL, "prompt": "cat"}).status_code, 401)
        generate.assert_not_called()
