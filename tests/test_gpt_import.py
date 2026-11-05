"""原生 GPT 导入、OAuth 续期与 Codex 协议门禁；上游只用本地模拟。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "default.db"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config
from adapters.gpt import GPTAdapter, decode_jwt_claims, normalize_entry, parse_import_body, parse_import_text
from adapters.openai_compat import UpstreamError
from core import codex, db as dbmod, gpt_accounts
from core.adapter import CAP_CHAT, CAP_REFRESH
from core.pool import Pool
from core.scheduler import _check_one


def jwt(exp=None, email="user@example.com", account_id="acct-1", user_id="user-1", plan="plus", poid="org-1"):
    claims = {"exp": int(time.time()) + 3600 if exp is None else exp, "email": email,
              "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_user_id": user_id,
                                             "chatgpt_plan_type": plan, "poid": poid}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + encoded + ".sig"


def complete_response(tool=False):
    output = [{"type": "message", "id": "msg", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "pong"}]}]
    if tool:
        output.append({"id": "fc", "type": "function_call", "call_id": "call_weather",
                       "name": "weather", "arguments": '{"city":"SH"}'})
    return {"id": "resp_native", "object": "response", "status": "completed", "output": output,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                      "input_tokens_details": {"cached_tokens": 6},
                      "output_tokens_details": {"reasoning_tokens": 2}}}


def wire(tool=False, completed=True):
    response = complete_response(tool)
    events = [{"type": "response.created", "response": {"id": response["id"], "created_at": 123}},
              {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
              {"type": "response.output_text.delta", "delta": "pong"}]
    if tool:
        events.extend([
            {"type": "response.output_item.added", "output_index": 3,
             "item": {"type": "function_call", "id": "fc", "call_id": "call_weather", "name": "weather"}},
            {"type": "response.function_call_arguments.delta", "output_index": 3, "delta": '{"city":'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc", "delta": '"SH"}'},
            {"type": "response.output_item.done", "output_index": 3, "item": response["output"][1]},
        ])
    if completed:
        events.append({"type": "response.completed", "response": response})
    return (": ping\n\n" + "".join("data: " + json.dumps(e) + "\n\n" for e in events)).encode()


def frames(lines):
    return [json.loads(line[5:].strip()) for line in lines
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


class ParseTest(unittest.TestCase):
    def test_supported_input_formats(self):
        item = {"access_token": jwt(), "refresh_token": "rt"}
        examples = [json.dumps(item), json.dumps([item]), json.dumps({"accounts": [item]}),
                    "\ufeff" + json.dumps(item), json.dumps(item) + "\n" + json.dumps(item)]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(normalize_entry(parse_import_text(text)[0])["refresh_token"], "rt")
        self.assertEqual(parse_import_text("a\n\n b \n"), ["a", "b"])
        self.assertEqual(len(parse_import_text(json.dumps(item) + "\nopaque")), 2)
        self.assertEqual(len(parse_import_body({"content": "a", "contents": ["b", "c"]})), 3)

    def test_auth_json_and_sub2api_export(self):
        item = normalize_entry({"tokens": {"access_token": jwt(account_id=""), "refresh_token": "rt",
                                           "account_id": "nested-account", "client_id": "custom-client"}})
        self.assertEqual(item["account_id"], "nested-account")
        self.assertEqual(item["client_id"], "custom-client")
        bundle = {"accounts": [{"platform": "openai", "type": "oauth",
                                 "credentials": {"access_token": jwt(), "refresh_token": "rt"}}]}
        self.assertEqual(normalize_entry(parse_import_text(json.dumps(bundle))[0])["plan_type"], "plus")
        with self.assertRaises(ValueError):
            normalize_entry({"platform": "anthropic", "type": "oauth", "credentials": {"access_token": "x"}})

    def test_access_expiry_not_id_expiry_and_expired_rt_can_refresh(self):
        exp = int(time.time()) + 7200
        item = normalize_entry({"access_token": jwt(exp=exp), "id_token": jwt(exp=1)})
        self.assertEqual(item["expires_at"], exp)
        with self.assertRaisesRegex(ValueError, "已过期"):
            normalize_entry({"access_token": jwt(exp=1), "id_token": jwt(), "expires_at": exp})
        item = normalize_entry({"access_token": jwt(exp=1), "refresh_token": "rt"})
        self.assertTrue(any("bit-api" in w for w in item["warnings"]))
        self.assertEqual(decode_jwt_claims(jwt())["email"], "user@example.com")

    def test_session_token_is_not_refresh_token(self):
        item = normalize_entry({"accessToken": jwt(), "sessionToken": "web-session"})
        self.assertFalse(item["refresh_token"])
        self.assertTrue(any("sessionToken" in w for w in item["warnings"]))

    def test_bad_values_fail_with_readable_errors(self):
        for value in (True, 4, {}, {"access_token": "not a token"}, {"access_token": "sk-key"},
                      {"access_token": "opaque", "expires_at": float("inf")},
                      {"access_token": "opaque", "expires_at": 10**100}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_entry(value)
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_import_text("{broken}")


class NativeGPTTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = dbmod.DB(os.path.join(self.tmp.name, "pool.db"))
        self.addCleanup(self.db.close)
        self.adapter = GPTAdapter(db=self.db)

    def import_one(self, email="native@example.com", expired=False, rt="refresh-old"):
        value = {"access_token": jwt(email=email, exp=1 if expired else None)}
        if rt:
            value["refresh_token"] = rt
        result = self.adapter.import_accounts(self.db, [value])
        self.assertEqual(result["imported"], 1)
        return self.db.get_by_identity("gpt", email)

    def test_database_is_the_only_account_source(self):
        row = self.import_one()
        self.assertEqual(row["secret"]["refresh_token"], "refresh-old")
        self.assertEqual(row["token_exp"], decode_jwt_claims(row["token"])["exp"])
        self.assertFalse(self.adapter.proxy)
        self.assertEqual(set(self.adapter.capabilities), {CAP_CHAT, CAP_REFRESH})
        self.assertFalse(hasattr(self.adapter, "sync_pool"))
        self.assertFalse(any(name.endswith(".json") for name in os.listdir(self.tmp.name)))

    def test_import_partial_failure_dedup_and_update_preserves_refresh(self):
        value = {"access_token": jwt(email="dup@example.com"), "refresh_token": "rt"}
        result = self.adapter.import_accounts(self.db, [value, {}, value])
        self.assertEqual((result["imported"], result["failed"], result["skipped"]), (1, 1, 1))
        old = self.db.get_by_identity("gpt", "dup@example.com")
        self.db.update_account(old["id"], meta={**old["meta"], "usage": {"used": 7}})
        result = self.adapter.import_accounts(self.db, [{"access_token": value["access_token"]}], force=True)
        self.assertEqual(result["updated"], 1)
        updated = self.db.get_account(old["id"])
        self.assertEqual(updated["secret"]["refresh_token"], "rt")
        self.assertEqual(updated["meta"]["usage"], {"used": 7})

    def test_personal_team_members_and_email_less_rotations_do_not_merge(self):
        values = [{"access_token": jwt(email="same@example.com", account_id=a, user_id=u)}
                  for a, u in (("personal", "u"), ("team", "u"), ("team", "u2"))]
        self.assertEqual(self.adapter.import_accounts(self.db, values)["imported"], 3)
        self.assertEqual(self.adapter.import_accounts(self.db, values)["skipped"], 3)
        items = [{"access_token": jwt(email="", account_id="other", user_id=u)} for u in ("u1", "u2")]
        self.assertEqual(self.adapter.import_accounts(self.db, items)["imported"], 2)
        rotated = [{"access_token": jwt(email="", account_id="other", user_id=u, exp=int(time.time()) + 7200)} for u in ("u1", "u2")]
        self.assertEqual(self.adapter.import_accounts(self.db, rotated, force=True)["updated"], 2)
        self.assertEqual(self.db.count_accounts("gpt"), 5)
        self.assertNotEqual(gpt_accounts.build_identity(normalize_entry("opaque-one", validate_expiry=False)),
                            gpt_accounts.build_identity(normalize_entry("opaque-two", validate_expiry=False)))

    def test_pick_and_scheduler_both_persist_rotated_refresh_tokens(self):
        row = self.import_one(expired=True)
        renewed = {"access_token": jwt(email=row["identity"], exp=int(time.time()) + 7200),
                   "refresh_token": "refresh-new", "expires_in": 7200}
        response = io.BytesIO(json.dumps(renewed).encode())
        with mock.patch("core.account_runtime.urlopen", return_value=response) as http:
            picked = Pool(self.db).get_valid_account("gpt", self.adapter)
        self.assertEqual(picked["secret"]["refresh_token"], "refresh-new")
        self.assertEqual(self.db.get_account(row["id"])["secret"]["refresh_token"], "refresh-new")
        request = http.call_args.args[0]
        form = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(form["client_id"], [gpt_accounts.CLIENT_ID])
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["scope"], ["openid profile email"])
        self.assertTrue(response.closed)
        self.db.update_account(row["id"], token_exp=1)
        renewed["refresh_token"] = "refresh-scheduler"
        with mock.patch.object(self.adapter, "_request", return_value=io.BytesIO(json.dumps(renewed).encode())):
            self.assertEqual(_check_one(self.db, self.adapter, self.db.get_account(row["id"]), 600, 0.05), "active")
        self.assertEqual(self.db.get_account(row["id"])["secret"]["refresh_token"], "refresh-scheduler")

    def test_concurrent_refresh_consumes_old_rt_only_once(self):
        row = self.import_one(expired=True)
        renewed = {"access_token": jwt(email=row["identity"], exp=int(time.time()) + 7200),
                   "refresh_token": "new", "expires_in": 7200}
        def refresh(_):
            try:
                return self.adapter.refresh_token(dict(row))
            finally:
                self.db.close()
        with mock.patch.object(self.adapter, "_request", side_effect=lambda *a: io.BytesIO(json.dumps(renewed).encode())) as http:
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(refresh, range(4)))
        self.assertEqual(http.call_count, 1)
        self.assertTrue(all(r["secret"]["refresh_token"] == "new" for r in results))

    def test_access_only_not_refreshed_before_expiry_and_dies_after(self):
        row = self.import_one(rt=None)
        self.db.update_account(row["id"], token_exp=int(time.time()) + 60)
        with mock.patch.object(self.adapter, "_request") as http:
            self.assertIsNotNone(Pool(self.db).get_valid_account("gpt", self.adapter))
        http.assert_not_called()
        self.db.update_account(row["id"], token_exp=1)
        with self.assertRaises(UpstreamError) as error:
            self.adapter.refresh_token(self.db.get_account(row["id"]))
        self.assertEqual(error.exception.code, 401)

    def test_native_request_headers_tools_and_nonstream_usage(self):
        row = self.import_one(rt=None)
        body = {"store": True, "max_tokens": 32, "temperature": 0,
                "reasoning_effort": "high", "prompt_cache_key": "cache",
                "tools": [{"type": "function", "function": {"name": "weather", "parameters": {}}}]}
        response = io.BytesIO(wire(tool=True))
        with mock.patch("core.account_runtime.urlopen", return_value=response) as http:
            result = self.adapter.chat(row, [{"role": "system", "content": "Follow instructions"},
                                           {"role": "user", "content": "weather"}], model="gpt-5.6-luna", body=body)["raw"]
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, config.GPT_BASE_URL + "/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + row["token"])
        self.assertEqual(request.get_header("Chatgpt-account-id"), "acct-1")
        self.assertEqual(request.get_header("Version"), config.GPT_CLIENT_VERSION)
        payload = json.loads(request.data)
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(payload["instructions"], "Follow instructions")
        self.assertEqual(payload["tools"][0]["name"], "weather")
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call_weather")
        self.assertEqual(result["usage"]["prompt_tokens_details"]["cached_tokens"], 6)
        self.assertTrue(response.closed)

    def test_streaming_arguments_reasoning_usage_and_terminal(self):
        row, lines = self.import_one(rt=None), []
        with mock.patch.object(self.adapter, "_request", return_value=io.BytesIO(wire(tool=True))):
            self.adapter.stream_chat(row, "gpt-5.6-luna", {"messages": [{"role": "user", "content": "weather"}]}, lines.append)
        decoded = frames(lines)
        calls = [c for f in decoded for ch in f["choices"] for c in (ch["delta"].get("tool_calls") or [])]
        self.assertEqual("".join(c.get("function", {}).get("arguments", "") for c in calls), '{"city":"SH"}')
        self.assertTrue(any(ch["delta"].get("reasoning_content") == "thinking" for f in decoded for ch in f["choices"]))
        self.assertEqual([f["usage"]["total_tokens"] for f in decoded if f.get("usage")], [18])
        self.assertEqual(lines[-1], "data: [DONE]\n\n")

    def test_capacity_events_are_service_unavailable_and_keep_native_error_code(self):
        cases = [({"code": "server_is_overloaded", "message": "Our servers are currently overloaded"}, 503),
                 ({"code": "slow_down", "message": "Please try again"}, 503),
                 ({"type": "server_error", "message": "Our servers are currently overloaded"}, 503),
                 ({"code": "rate_limit_exceeded", "message": "slow"}, 429),
                 ({"code": "invalid_api_key", "message": "no"}, 401),
                 ({"code": "server_is_overloaded", "status_code": 401}, 401),
                 ({"message": "bad gateway"}, 502)]
        for value, expected in cases:
            with self.subTest(value=value):
                error = codex.error_from_event({"type": "error", "error": value})
                self.assertEqual(error.code, expected)
                self.assertEqual(error.details["error"], value)
                if expected == 503:
                    self.assertTrue(codex.is_capacity_error(error))
        response = io.BytesIO(b'data: {"type":"response.failed","response":{"error":{"code":"server_is_overloaded","message":"overloaded"}}}\n\n')
        with self.assertRaises(UpstreamError) as failure:
            list(codex.iter_events(response, 1))
        self.assertEqual(failure.exception.code, 503)
        self.assertEqual(failure.exception.upstream_code, "server_is_overloaded")

    def test_eof_and_stream_errors_never_fake_completed_response(self):
        row = self.import_one(rt=None)
        for raw in (wire(completed=False), b'data: {"type":"error","error":{"code":"rate_limit_exceeded","message":"slow"}}\n\n'):
            with self.subTest(raw=raw):
                lines = []
                with mock.patch.object(self.adapter, "_request", side_effect=lambda *a, **kw: io.BytesIO(raw)), mock.patch("core.gpt_request_policy.OAUTH_429_RETRY_WINDOW", 0):
                    with self.assertRaises(UpstreamError):
                        self.adapter.stream_chat(row, "gpt-5.6-luna", {"messages": []}, lines.append)
                self.assertNotIn("data: [DONE]\n\n", lines)

    def test_401_refreshes_and_retries_once(self):
        row = self.import_one()
        renewed = {"access_token": jwt(email=row["identity"], exp=int(time.time()) + 7200), "refresh_token": "new", "expires_in": 7200}
        with mock.patch.object(self.adapter, "_request", side_effect=[UpstreamError(401, "expired"),
                               io.BytesIO(json.dumps(renewed).encode()), io.BytesIO(wire())]) as http:
            self.assertEqual(self.adapter.chat(row, [], model="gpt-5.6-luna")["raw"]["choices"][0]["message"]["content"], "pong")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(self.db.get_account(row["id"])["secret"]["refresh_token"], "new")

    def test_tool_history_and_images_map_to_responses(self):
        body = {"messages": [{"role": "assistant", "content": None,
                              "tool_calls": [{"id": "call", "function": {"name": "weather", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": "call", "content": "sunny"},
                             {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA", "detail": "low"}}]}]}
        payload = codex.chat_to_responses(body, "gpt-5.6-luna")
        self.assertEqual(payload["input"][0]["type"], "function_call")
        self.assertEqual(payload["input"][1]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["content"][0]["type"], "input_image")


if __name__ == "__main__":
    unittest.main()
