#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚拟工具桥:文本上游也能返回 OpenAI tool_calls。"""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "vt.db"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")
# 通道名由测试显式给定；生产用 adapter.virtual_tools 声明，不靠默认值。
os.environ["BITAPI_VIRTUAL_TOOL_CHANNELS"] = "vt-channel"

import server  # noqa: E402
from core.adapter import Adapter, get_adapter, register_adapter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a local file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]


def _fake_chat(acct, messages, stream=False, model=None, on_token=None, body=None):
    directive = ((body or {}).get("messages") or [{}])[0].get("content", "")
    marker = re.search(r"\[\[BITAPI-CALL-[0-9a-f]+\]\]", directive)
    if not marker:
        return {"content": "no marker", "reasoning": None}
    payload = [{"name": "read_file", "params": {"path": "a"}}]
    return {"content": marker.group(0) + "<<<BITAPI_TOOLS>>>"
                       + json.dumps(payload) + "<<<END_BITAPI_TOOLS>>>",
            "reasoning": None}


class _VtChannel(Adapter):
    """洼地通道：只探测"文本通道 + 声明能虚拟工具"这一条路。"""
    name = "vt-channel"
    models = ["vt-model"]
    capabilities = []
    virtual_tools = True

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        return _fake_chat(acct, messages, stream=stream, model=model,
                          on_token=on_token, body=body)


if get_adapter("vt-channel") is None:
    register_adapter(_VtChannel())


class VirtualToolTests(unittest.TestCase):
    def setUp(self):
        if get_adapter("vt-channel") is None:
            register_adapter(_VtChannel())

    def test_parse_params_alias_and_fence(self):
        virtual = {"marker": "[[M]]", "allowed": {"read_file"}}
        text = ('[[M]]<<<BITAPI_TOOLS>>>```json\n'
                '[{"name":"read_file","arguments":"{\\"path\\":\\"/tmp/a\\"}"}]\n'
                '```<<<END_BITAPI_TOOLS>>>')
        content, calls = server._parse_virtual_tool_calls(text, virtual)
        self.assertEqual(content, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["path"], "/tmp/a")

    def test_parse_rejects_undeclared(self):
        virtual = {"marker": "[[M]]", "allowed": {"read_file"}}
        text = '[[M]]<<<BITAPI_TOOLS>>>[{"name":"rm_rf","params":{}}]<<<END_BITAPI_TOOLS>>>'
        _, calls = server._parse_virtual_tool_calls(text, virtual)
        self.assertEqual(calls, [])

    def test_parse_single_object(self):
        virtual = {"marker": "[[M]]", "allowed": {"write_file"}}
        text = ('[[M]]<<<BITAPI_TOOLS>>>'
                '{"name":"write_file","params":{"path":"a","content":"b"}}'
                '<<<END_BITAPI_TOOLS>>>')
        _, calls = server._parse_virtual_tool_calls(text, virtual)
        self.assertEqual(len(calls), 1)

    def test_prepare_injects_directive_and_strips_tools(self):
        body = {"messages": [{"role": "user", "content": "read a"}],
                "tools": [{"type": "function", "function": {
                    "name": "read_file", "description": "Read",
                    "parameters": {"type": "object"}}}],
                "tool_choice": "auto"}
        routed, virtual = server._prepare_virtual_tool_request("vt-channel", body)
        self.assertIsNotNone(virtual)
        self.assertNotIn("tools", routed)
        self.assertEqual(routed["messages"][0]["role"], "system")
        self.assertIn("read_file", routed["messages"][0]["content"])

    def test_prepare_accepts_online_channel_name(self):
        body = {"messages": [{"role": "user", "content": "read a"}],
                "tools": TOOLS, "tool_choice": "auto"}
        for channel in ("VT-Channel", "VT-CHANNEL", "vt-channel"):
            routed, virtual = server._prepare_virtual_tool_request(channel, body)
            self.assertIsNotNone(virtual, channel)
            self.assertNotIn("tools", routed)

    def test_prepare_uses_adapter_capability(self):
        class Renamed:
            virtual_tools = True

        body = {"messages": [{"role": "user", "content": "read a"}],
                "tools": TOOLS, "tool_choice": "auto"}
        routed, virtual = server._prepare_virtual_tool_request("frontdoor", body, Renamed())
        self.assertIsNotNone(virtual)
        self.assertNotIn("tools", routed)

    def test_model_names_are_case_insensitive(self):
        self.assertEqual(server._canonical_model("VT-CHANNEL"), "vt-channel")
        self.assertEqual(server._canonical_model("vt-channel[1M]"), "vt-channel")

    def _client_and_patches(self):
        adapter = get_adapter("vt-channel")
        acct = {"id": 1, "channel": "vt-channel", "token": "t"}
        return (TestClient(server.app), adapter, acct)

    def test_openai_nonstream_returns_tool_calls(self):
        client, adapter, acct = self._client_and_patches()
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct), \
                mock.patch.object(adapter, "chat", new=_fake_chat):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + server.config.API_KEY},
                json={"model": "vt-channel", "messages": [{"role": "user", "content": "read a"}],
                      "tools": TOOLS})
        self.assertEqual(resp.status_code, 200)
        choice = resp.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(json.loads(call["function"]["arguments"])["path"], "a")

    def test_sync_switches_account_and_keeps_conversation(self):
        """号池切号: 第一个号 402, 第二个号用同一份 messages 继续完成本轮。"""
        client, adapter, _ = self._client_and_patches()
        acct1 = {"id": 1, "channel": "vt-channel", "token": "t1"}
        acct2 = {"id": 2, "channel": "vt-channel", "token": "t2"}
        seen = []

        def fake_chat(acct, messages, stream=False, model=None, on_token=None, body=None):
            seen.append((acct["id"], [m.get("content") for m in messages]))
            if acct["id"] == 1:
                raise RuntimeError("HTTP 402: insufficient credits")
            return _fake_chat(acct, messages, stream=stream, model=model,
                              on_token=on_token, body=body)

        with mock.patch.object(server.POOL, "get_valid_account",
                               side_effect=[acct1, acct2]), \
                mock.patch.object(server.POOL, "mark_failure", return_value="terminal"), \
                mock.patch.object(adapter, "chat", new=fake_chat):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + server.config.API_KEY},
                json={"model": "vt-channel", "messages": [{"role": "user", "content": "read a"}],
                      "tools": TOOLS})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item[0] for item in seen], [1, 2])
        # 两次请求拿到同一份对话 + 同一个虚拟工具指令
        self.assertEqual(seen[0][1], seen[1][1])
        self.assertEqual(resp.json()["choices"][0]["finish_reason"], "tool_calls")

    def test_cross_turn_switch_preserves_tool_context(self):
        """长任务: 第 1 轮号 A 出 tool_call, 第 2 轮号 B 带 tool result 继续。"""
        client, adapter, _ = self._client_and_patches()
        acct1 = {"id": 1, "channel": "vt-channel", "token": "t1"}
        acct2 = {"id": 2, "channel": "vt-channel", "token": "t2"}
        seen = {}

        def fake_chat(acct, messages, stream=False, model=None, on_token=None, body=None):
            seen[acct["id"]] = messages
            return _fake_chat(acct, messages, stream=stream, model=model,
                              on_token=on_token, body=body)

        headers = {"Authorization": "Bearer " + server.config.API_KEY}
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct1), \
                mock.patch.object(adapter, "chat", new=fake_chat):
            first = client.post("/v1/chat/completions", headers=headers,
                                json={"model": "vt-channel",
                                      "messages": [{"role": "user", "content": "read a"}],
                                      "tools": TOOLS})
        call = first.json()["choices"][0]["message"]["tool_calls"][0]

        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct2), \
                mock.patch.object(adapter, "chat", new=fake_chat):
            second = client.post("/v1/chat/completions", headers=headers,
                                 json={"model": "vt-channel",
                                       "messages": [
                                           {"role": "user", "content": "read a"},
                                           {"role": "assistant", "content": None,
                                            "tool_calls": [call]},
                                           {"role": "tool", "tool_call_id": call["id"],
                                            "content": "hello"}],
                                       "tools": TOOLS})
        self.assertEqual(second.status_code, 200)
        routed = seen[2]
        # 文本协议渠道不认 tool_calls / role=tool:服务端把它们摊平成普通文本
        self.assertEqual(routed[-2]["role"], "assistant")
        self.assertIn("[called read_file(", routed[-2]["content"])
        self.assertEqual(routed[-1]["role"], "user")
        self.assertIn("[tool result", routed[-1]["content"])
        self.assertIn("hello", routed[-1]["content"])

    def test_openai_stream_returns_tool_calls(self):
        client, adapter, acct = self._client_and_patches()
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct), \
                mock.patch.object(adapter, "chat", new=_fake_chat):
            with client.stream(
                    "POST", "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + server.config.API_KEY},
                    json={"model": "vt-channel", "stream": True,
                          "messages": [{"role": "user", "content": "read a"}],
                          "tools": TOOLS}) as resp:
                body = "".join(resp.iter_text())
        self.assertIn('"tool_calls"', body)
        self.assertIn('"finish_reason": "tool_calls"', body)
        self.assertIn("[DONE]", body)

    def test_anthropic_messages_returns_tool_use(self):
        client, adapter, acct = self._client_and_patches()
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct), \
                mock.patch.object(adapter, "chat", new=_fake_chat):
            resp = client.post(
                "/v1/messages",
                headers={"x-api-key": server.config.API_KEY},
                json={"model": "vt-channel", "max_tokens": 64,
                      "messages": [{"role": "user", "content": "read a"}],
                      "tools": [{"name": "read_file", "description": "Read",
                                 "input_schema": {"type": "object",
                                                  "properties": {"path": {"type": "string"}}}}]})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["stop_reason"], "tool_use")
        self.assertTrue(any(b.get("type") == "tool_use" and b.get("name") == "read_file"
                            for b in data["content"]))

    def test_anthropic_stream_returns_tool_use(self):
        client, adapter, acct = self._client_and_patches()
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct), \
                mock.patch.object(adapter, "chat", new=_fake_chat):
            with client.stream(
                    "POST", "/v1/messages",
                    headers={"x-api-key": server.config.API_KEY},
                    json={"model": "vt-channel", "max_tokens": 64, "stream": True,
                          "messages": [{"role": "user", "content": "read a"}],
                          "tools": [{"name": "read_file", "description": "Read",
                                     "input_schema": {"type": "object",
                                                      "properties": {"path": {"type": "string"}}}}]}) as resp:
                body = "".join(resp.iter_text())
        self.assertIn('"type": "tool_use"', body)
        self.assertIn("read_file", body)
        self.assertIn('"stop_reason": "tool_use"', body)

    def test_responses_returns_function_call(self):
        client, adapter, acct = self._client_and_patches()
        with mock.patch.object(server.POOL, "get_valid_account", return_value=acct), \
                mock.patch.object(adapter, "chat", new=_fake_chat):
            resp = client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer " + server.config.API_KEY},
                json={"model": "vt-channel", "input": "read a",
                      "tools": [{"type": "function", "name": "read_file",
                                 "description": "Read",
                                 "parameters": {"type": "object",
                                                "properties": {"path": {"type": "string"}}}}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(item.get("type") == "function_call"
                            and item.get("name") == "read_file"
                            for item in resp.json()["output"]))


if __name__ == "__main__":
    unittest.main()
