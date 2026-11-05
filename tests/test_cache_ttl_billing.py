"""Cache TTLs survive protocol boundaries and settle once at their frozen rates."""
import copy
import io
import json
import unittest
import urllib.error
from unittest import mock

import server
from core import codex, gpt_native
from core.metering import cache_tokens, cache_write_ttls
from core.pricing import Pricing
from tests import test_gpt_import_endpoint as fixture
from tests.test_gpt_import import complete_response


SPLIT = {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000}
PRICE = {"billing_mode": "token", "input_price": 1, "output_price": 2,
         "cache_read_price": .1, "cache_write_price": 1.25}


class CacheTTLUnitTest(unittest.TestCase):
    def test_tier_prices_multiplier_and_frozen_snapshot(self):
        for long in (False, True):
            with self.subTest(long=long):
                price = {**PRICE, "long_threshold": 10000 if long else 12000,
                         "long_input_price": 2, "long_output_price": 4,
                         "long_cache_read_price": .2, "long_cache_write_price": 2.5}
                cost, actual, snap = Pricing.compute(price, 1000, 20, 4000, 7000,
                    rate_multiplier=.4, cache_write_ttls=SPLIT)
                self.assertAlmostEqual(cost, .01519 * (2 if long else 1))
                self.assertAlmostEqual(actual, cost * .4)
                self.assertEqual(snap["total_ctx"], 12000)
                self.assertEqual(snap["tier"], "long" if long else "official")
                self.assertEqual(snap["cache_write_12h_price"], 5 if long else 2.5)
                self.assertEqual(snap["cache_write_30m_tokens"], 3000)
                price["cache_write_price"] = 99
                self.assertEqual(snap["cache_write_30m_price"], 2.5 if long else 1.25)

    def test_aggregate_missing_zero_and_invalid_splits(self):
        for details in ("prompt_tokens_details", "input_tokens_details"):
            usage = {details: {"cache_write_tokens_details": SPLIT}}
            self.assertEqual(cache_tokens(usage), (None, 7000))
        self.assertIsNone(cache_write_ttls({"input_tokens_details": {"cache_write_tokens": 0}}))
        for split in ({"ttl_30m_tokens": 1}, {"ttl_30m_tokens": -1, "ttl_12h_tokens": 4},
                      {"ttl_30m_tokens": True, "ttl_12h_tokens": 4},
                      {"ttl_30m_tokens": 1.5, "ttl_12h_tokens": 4},
                      {"ttl_30m_tokens": "bad", "ttl_12h_tokens": 4},
                      {"ttl_30m_tokens": 5000, "ttl_12h_tokens": 4000}):
            with self.subTest(split=split):
                parsed = cache_write_ttls({"cache_write_tokens_details": split})
                cost, _, snap = Pricing.compute(PRICE, cache_write=7000, cache_write_ttls=parsed)
                self.assertAlmostEqual(cost, .00875)
                self.assertFalse(snap["cache_write_ttl_valid"])
                self.assertNotIn("cache_write_12h_tokens", snap)
        cost, _, snap = Pricing.compute(PRICE, cache_write=0,
            cache_write_ttls={"ttl_30m_tokens": 0, "ttl_12h_tokens": 0})
        self.assertEqual(cost, 0)
        self.assertEqual(snap["cache_write_12h_tokens"], 0)
        self.assertTrue(snap["cache_write_ttl_valid"])
        # An unclassified remainder is charged once at the base write price.
        cost, _, snap = Pricing.compute(PRICE, cache_write=8000, cache_write_ttls=SPLIT)
        self.assertAlmostEqual(cost, .015)
        self.assertEqual(snap["cache_write_unclassified_tokens"], 1000)

    def test_marked_instructions_images_tools_roundtrip_without_mutation(self):
        marker = {"mode": "explicit", "ttl": "12h"}
        body = {"model": "gpt-5.6-luna", "prompt_cache_options": {"mode": "explicit", "ttl": "12h"},
                "prompt_cache_key": "prefix", "prompt_cache_retention": "24h", "input": [
                    {"role": "developer", "content": [{"type": "input_text", "text": "rules", "prompt_cache_breakpoint": marker}]},
                    {"role": "user", "content": [{"type": "input_image", "image_url": "https://example.com/a.png", "detail": "low", "prompt_cache_breakpoint": marker}]},
                    {"type": "function_call_output", "call_id": "c", "output": [
                        {"type": "input_text", "text": "tool result", "prompt_cache_breakpoint": marker}]}]}
        before = copy.deepcopy(body)
        chat = server._responses_to_openai(body)
        output = codex.chat_to_responses(chat, body["model"])
        self.assertEqual(body, before)
        self.assertEqual(output["prompt_cache_options"], body["prompt_cache_options"])
        self.assertEqual(output["prompt_cache_key"], "prefix")
        self.assertEqual(output["prompt_cache_retention"], "24h")
        for item in output["input"]:
            self.assertEqual((item.get("content") or item["output"])[0]["prompt_cache_breakpoint"], marker)
        output["prompt_cache_options"]["ttl"] = "30m"
        self.assertEqual(body, before)
        adapter = mock.Mock()
        adapter.upstream_account_model.return_value = body["model"]
        policy = {**gpt_native.G.settings({}), "passthrough": False}
        with mock.patch.object(gpt_native.G, "settings", return_value=policy):
            native = gpt_native.payload_for(adapter, {}, body)
        self.assertEqual(native["input"], body["input"])
        self.assertEqual(native["prompt_cache_options"], body["prompt_cache_options"])

    def test_anthropic_markers_and_usage_roundtrip(self):
        part = {"type": "text", "text": "cached", "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "12h"}}
        body = {"model": "gpt-5.6-luna", "system": [part], "messages": [
                {"role": "user", "content": [part]}, {"role": "assistant", "content": [part]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": [part]}]}],
                "prompt_cache_options": {"mode": "explicit", "ttl": "12h"}}
        before = copy.deepcopy(body)
        output = codex.chat_to_responses(server._anthropic_to_openai(body), body["model"])
        self.assertEqual(body, before)
        self.assertEqual(output["prompt_cache_options"], body["prompt_cache_options"])
        self.assertEqual(len(output["input"]), 4)
        for item in output["input"]:
            self.assertEqual((item.get("content") or item["output"])[0]["prompt_cache_breakpoint"], part["prompt_cache_breakpoint"])
        usage = {"input_tokens": 12000, "output_tokens": 20, "input_tokens_details": {
                 "cached_tokens": 4000, "cache_write_tokens": 7000, "cache_write_tokens_details": SPLIT}}
        chat = codex.usage_to_chat(usage)
        self.assertEqual(server._responses_usage(chat)["input_tokens_details"], usage["input_tokens_details"])
        anthropic = server._anthropic_usage(chat)
        self.assertEqual(anthropic["input_tokens"], 1000)
        self.assertEqual(cache_tokens(anthropic), (4000, 7000))
        self.assertEqual(cache_write_ttls(anthropic), SPLIT)


class CacheTTLEndpointTest(unittest.TestCase):
    setUpClass = classmethod(fixture.GPTEndpointTest.setUpClass.__func__)
    setUp = fixture.GPTEndpointTest.setUp
    value = fixture.GPTEndpointTest.value

    def prepare(self):
        from core.credit import credit
        self.client.post("/api/admin/gpt/import", headers=self.admin, json=self.value())
        user = self.users.get_user_by_email("user@gmail.com")
        self.users.update_group(user["group_id"], billing_policy="balance", rate_multiplier=.4)
        credit(user["id"], 1, "admin", self.id())
        self.users.upsert_pricing("gpt-5.6-luna", None, **PRICE)
        key = self.client.post("/api/keys", headers=self.user, json={"name": "cache-ttl"}).json()["key"]
        return user, {"Authorization": "Bearer " + key}

    @staticmethod
    def requests():
        for path, body in (("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
                           ("/v1/responses", {"input": "hi"}),
                           ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32})):
            for stream in (False, True):
                yield path, {"model": "gpt-5.6-luna", "stream": stream, **body}

    def test_mixed_ttl_settles_once_all_protocols_stream_and_nonstream(self):
        user, headers = self.prepare()
        result = complete_response()
        result["usage"] = {"input_tokens": 12000, "output_tokens": 20, "total_tokens": 12020,
            "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000,
                                     "cache_write_tokens_details": SPLIT}}
        wire = b'data: {"type":"response.output_text.delta","delta":"pong"}\n\n'
        wire += ("data: " + json.dumps({"type": "response.completed", "response": result}) + "\n\n").encode()
        for path, body in self.requests():
            with self.subTest(path=path, stream=body["stream"]):
                before = self.users.get_user(user["id"])["balance"]
                old_count = len(self.users.recent_usage(user["id"], limit=1000))
                with mock.patch("core.account_runtime.urlopen", return_value=io.BytesIO(wire)) as upstream:
                    response = self.client.post(path, headers=headers, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('"ttl_12h_tokens":4000', response.text.replace(" ", ""))
                self.assertEqual(upstream.call_count, 1)
                logs = self.users.recent_usage(user["id"], limit=1000)
                self.assertEqual(len(logs), old_count + 1)
                log = logs[0]
                self.assertEqual((log["input_tokens"], log["output_tokens"]), (1000, 20))
                self.assertAlmostEqual(log["cost"], .01519)
                self.assertAlmostEqual(before - self.users.get_user(user["id"])["balance"], .01519 * .4)
                snap = log["pricing_snapshot"]
                self.assertEqual((snap["cache_usage_version"], snap["cache_write_30m_tokens"], snap["cache_write_12h_tokens"]), (3, 3000, 4000))
                self.assertEqual((snap["cache_write_30m_price"], snap["cache_write_12h_price"]), (1.25, 2.5))

    def test_unsupported_cache_options_not_stripped_retried_charged_or_cooled(self):
        user, headers = self.prepare()
        options = {"mode": "explicit", "ttl": "12h"}
        error = {"error": {"message": "prompt_cache_options is not supported on this model",
                           "type": "invalid_request_error", "param": "prompt_cache_options"}}
        for path, body in self.requests():
            with self.subTest(path=path, stream=body["stream"]):
                before = self.users.get_user(user["id"])["balance"]
                def reject(request, **kwargs):
                    self.assertEqual(json.loads(request.data)["prompt_cache_options"], options)
                    raise urllib.error.HTTPError(request.full_url, 400, "bad request", {}, io.BytesIO(json.dumps(error).encode()))
                with mock.patch("core.account_runtime.urlopen", side_effect=reject) as upstream:
                    response = self.client.post(path, headers=headers, json={**body, "prompt_cache_options": options})
                if body["stream"]:
                    self.assertEqual(response.status_code, 200, response.text)
                    packets = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
                    self.assertEqual(len(packets), 1, response.text)
                    self.assertEqual(packets[0]["error"]["status_code"], 400, response.text)
                    self.assertNotIn("choices", packets[0])
                    self.assertNotIn("message_stop", response.text)
                else:
                    self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("prompt_cache_options is not supported", response.text)
                self.assertEqual(upstream.call_count, 1)
                self.assertEqual(before, self.users.get_user(user["id"])["balance"])
                self.assertEqual(self.pool.list_accounts("gpt")[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
