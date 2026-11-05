#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openai / claude 两个订阅号 adapter 的门禁 —— 协议转换、续期轮换、导入。

这里断言的都是「弄错了会静默出事」的点:
  - system 塞进 input 不报错但提示词失效(Responses 那边)
  - Claude Code 身份块掉了,上游按第三方应用计费或直接 403
  - 工具调用在流式里丢了,agent 客户端收到一条「说了但没做」的回答
  - refresh_token 轮换没写回,下次刷新 invalid_grant
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "sub.db"))

import adapters.claude  # noqa: E402,F401
import adapters.openai  # noqa: E402,F401
from adapters.claude import ClaudeAdapter, ClaudeError  # noqa: E402
from adapters.openai import OpenAIAdapter, OpenAIError  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.adapter import get_adapter  # noqa: E402
from core.pool import persist_runtime_meta, refresh_fields  # noqa: E402


class _Resp:
    """够用的 urlopen 返回值替身:非流式走 read(),流式走迭代。

    headers 必须有 —— 真实响应(http.client.HTTPResponse / HTTPError)一直带着它,
    而用量窗口就是从那里读的。
    """

    def __init__(self, payload=None, lines=None, headers=None):
        self._body = json.dumps(payload).encode() if payload is not None else b""
        self._lines = lines or []
        self.headers = headers or {}

    def read(self, *a):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sse(*events):
    return [("data: " + json.dumps(e) + "\n").encode() for e in events]


def _openai_acct(**over):
    a = {"id": 1, "channel": "openai", "identity": "a@example.com",
         "token": "tok", "token_exp": 0,
         "secret": {"refresh_token": "rt-old", "chatgpt_account_id": "acc-1"}}
    a.update(over)
    return a


def _claude_acct(**over):
    a = {"id": 2, "channel": "claude", "identity": "c@example.com",
         "token": "tok", "token_exp": 0,
         "secret": {"refresh_token": "rt-old", "account_uuid": "uuid-1"}}
    a.update(over)
    return a


class OpenAIPayloadTest(unittest.TestCase):
    """OpenAI messages → Responses 请求体。"""

    def setUp(self):
        self.ad = OpenAIAdapter()

    def test_system_goes_to_instructions_not_input(self):
        p = self.ad._payload([{"role": "system", "content": "be terse"},
                              {"role": "user", "content": "hi"}], "gpt-5.4", False)
        self.assertEqual(p["instructions"], "be terse")
        self.assertEqual(p["input"], [{"role": "user", "content": "hi"}])

    def test_tool_calls_flatten_to_function_call_items(self):
        """assistant 的工具调用在 Responses 里不是 message 的一种 role,
        要平铺成独立项 —— 不铺的话上游看不到上一轮调过什么。"""
        p = self.ad._payload([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get", "arguments": '{"a":1}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "42"}],
            "gpt-5.4", False)
        self.assertIn({"type": "function_call", "call_id": "call_1",
                       "name": "get", "arguments": '{"a":1}'}, p["input"])
        self.assertIn({"type": "function_call_output", "call_id": "call_1",
                       "output": "42"}, p["input"])

    def test_store_is_false_on_subscription_accounts(self):
        """订阅号这条路上游不替我们存对话。声明 store=true 会被拒。"""
        p = self.ad._payload([{"role": "user", "content": "hi"}], "gpt-5.4", False)
        self.assertFalse(p["store"])

    def test_response_maps_tool_calls_back(self):
        """上游的 function_call 项要翻回 OpenAI 的 tool_calls,否则 agent 断在半路。"""
        raw = {"id": "r1", "output": [
            {"type": "function_call", "call_id": "c1", "name": "get",
             "arguments": '{"a":1}'}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}
        chat = self.ad._to_chat(raw, "gpt-5.4")
        msg = chat["choices"][0]["message"]
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get")
        self.assertEqual(chat["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(chat["usage"]["prompt_tokens"], 3)

    def test_usage_details_are_carried(self):
        """缓存命中与思考 token 分开给, 计费层那两档价要靠它。"""
        u = self.ad._usage_of({"usage": {
            "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens_details": {"reasoning_tokens": 2}}})
        self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 7)
        self.assertEqual(u["completion_tokens_details"]["reasoning_tokens"], 2)

    def test_untrusted_base_url_is_refused(self):
        """凭据里的 base_url 被改写就把 Bearer token 送到别处了 —— 那是把号送人。"""
        acct = _openai_acct(secret={"refresh_token": "x",
                                    "base_url": "https://evil.example.com"})
        with self.assertRaises(OpenAIError):
            self.ad._base(acct)


class OpenAIStreamTest(unittest.TestCase):
    """Responses SSE → OpenAI SSE。"""

    def setUp(self):
        self.ad = OpenAIAdapter()

    def _run(self, events):
        out = []
        with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(*events))):
            self.ad.stream_chat(_openai_acct(), "gpt-5.4",
                                {"messages": [{"role": "user", "content": "hi"}]},
                                out.append)
        return [json.loads(l[6:]) for l in out if l.startswith("data: ") and
                l[6:].strip() != "[DONE]"]

    def test_text_delta_becomes_openai_content(self):
        frames = self._run([
            {"type": "response.output_text.delta", "delta": "he"},
            {"type": "response.output_text.delta", "delta": "llo"},
            {"type": "response.completed", "response": {"output": [], "usage": {}}}])
        self.assertEqual([f["choices"][0]["delta"].get("content") for f in frames
                          if "content" in f["choices"][0]["delta"]], ["he", "llo"])

    def test_usage_frame_and_done_are_emitted(self):
        """网关靠终止帧判「流正常收尾」,靠 usage 帧记账 —— 少了会被记成 eof。"""
        out = []
        with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(
                {"type": "response.completed",
                 "response": {"output": [], "usage": {"input_tokens": 1,
                                                      "output_tokens": 1,
                                                      "total_tokens": 2}}}))):
            self.ad.stream_chat(_openai_acct(), "gpt-5.4",
                                {"messages": [{"role": "user", "content": "hi"}]},
                                out.append)
        last = [l for l in out if l.startswith("data: ")][-1]
        self.assertEqual(last.strip(), "data: [DONE]")
        usage_frames = [json.loads(l[6:]) for l in out
                        if l.startswith("data: ") and '"usage"' in l]
        self.assertEqual(usage_frames[-1]["usage"]["total_tokens"], 2)

    def test_tool_call_arguments_stream_with_stable_index(self):
        """一轮里并行调两个工具时,index 串了就会把第二把的参数拼到第一把上。"""
        frames = self._run([
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "call_id": "c1", "name": "a"}},
            {"type": "response.output_item.added", "output_index": 1,
             "item": {"type": "function_call", "call_id": "c2", "name": "b"}},
            {"type": "response.function_call_arguments.delta", "output_index": 1,
             "delta": '{"x":'},
            {"type": "response.function_call_arguments.delta", "output_index": 0,
             "delta": '{"y":'},
            {"type": "response.completed", "response": {"output": []}}])
        args = {}
        for f in frames:
            for tc in f["choices"][0]["delta"].get("tool_calls") or []:
                args.setdefault(tc["index"], []).append(
                    (tc.get("function") or {}).get("arguments", ""))
        self.assertEqual("".join(args[0]), '{"y":')
        self.assertEqual("".join(args[1]), '{"x":')

    def test_upstream_failure_raises_with_code(self):
        """上游 failed 事件必须变成异常 —— 静默结束会被记成「空回复」而罚号。"""
        with self.assertRaises(OpenAIError):
            with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(
                    {"type": "response.failed",
                     "response": {"error": {"message": "boom"}}}))):
                self.ad.stream_chat(_openai_acct(), "gpt-5.4",
                                    {"messages": [{"role": "user", "content": "hi"}]},
                                    lambda l: None)


class OpenAIRefreshTest(unittest.TestCase):
    def setUp(self):
        self.ad = OpenAIAdapter()

    def test_refresh_rotates_and_reports_new_secret(self):
        """上游刷新会轮换 refresh_token,新的那份必须回传。"""
        with mock.patch("urllib.request.urlopen", return_value=_Resp({
                "access_token": "new-tok", "refresh_token": "rt-new",
                "expires_in": 3600, "id_token": ""})):
            upd = self.ad.refresh_token(_openai_acct())
        self.assertEqual(upd["token"], "new-tok")
        self.assertEqual(upd["secret"]["refresh_token"], "rt-new")
        self.assertEqual(upd["secret"]["chatgpt_account_id"], "acc-1")
        self.assertGreater(upd["token_exp"], 0)

    def test_account_id_is_backfilled_from_id_token(self):
        import base64

        def jwt(payload):
            seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return "x." + seg + ".y"

        idt = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-9"}})
        acct = _openai_acct(secret={"refresh_token": "rt"})
        with mock.patch("urllib.request.urlopen", return_value=_Resp({
                "access_token": "t", "expires_in": 60, "id_token": idt})):
            upd = self.ad.refresh_token(acct)
        self.assertEqual(upd["secret"]["chatgpt_account_id"], "acc-9")

    def test_refresh_without_refresh_token_fails_loudly(self):
        with self.assertRaises(OpenAIError):
            self.ad.refresh_token(_openai_acct(secret={}))


class ClaudePayloadTest(unittest.TestCase):
    """OpenAI messages → Anthropic Messages。"""

    def setUp(self):
        self.ad = ClaudeAdapter()

    def test_identity_block_comes_first(self):
        """订阅号只认 Claude Code。身份块掉了上游按第三方应用计费或直接 403 ——
        两种都很难从报错里看出是这里的问题。"""
        p = self.ad._payload(_claude_acct(),
                             [{"role": "system", "content": "be terse"},
                              {"role": "user", "content": "hi"}],
                             "claude-sonnet-4-6", False)
        self.assertEqual(p["system"][0]["text"],
                         "You are Claude Code, Anthropic's official CLI for Claude.")
        self.assertEqual(p["system"][1]["text"], "be terse")
        self.assertEqual(p["messages"], [{"role": "user", "content": "hi"}])

    def test_tool_result_becomes_a_user_block(self):
        """OpenAI 的 role=tool 在 Anthropic 那边是 user 里的 tool_result 块。"""
        p = self.ad._payload(_claude_acct(), [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "42"}],
            "claude-sonnet-4-6", False)
        self.assertEqual(p["messages"][1], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "42"}]})

    def test_assistant_tool_calls_become_tool_use_blocks(self):
        p = self.ad._payload(_claude_acct(), [
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "c1", "function": {"name": "get", "arguments": '{"a":1}'}}]}],
            "claude-sonnet-4-6", False)
        blocks = p["messages"][0]["content"]
        self.assertEqual(blocks[1]["type"], "tool_use")
        self.assertEqual(blocks[1]["input"], {"a": 1})

    def test_openai_tools_become_input_schemas(self):
        p = self.ad._payload(_claude_acct(), [{"role": "user", "content": "hi"}],
                             "claude-sonnet-4-6", False,
                             body={"tools": [{"type": "function", "function": {
                                 "name": "get", "description": "d",
                                 "parameters": {"type": "object"}}}]})
        self.assertEqual(p["tools"][0]["name"], "get")
        self.assertEqual(p["tools"][0]["input_schema"], {"type": "object"})

    def test_response_maps_thinking_and_tools_back(self):
        raw = {"id": "m1", "stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "c1", "name": "get", "input": {"a": 1}}],
            "usage": {"input_tokens": 5, "output_tokens": 2,
                      "cache_read_input_tokens": 3}}
        chat = self.ad._to_chat(raw, "claude-sonnet-4-6")
        msg = chat["choices"][0]["message"]
        self.assertEqual(msg["reasoning_content"], "hmm")
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get")
        self.assertEqual(chat["choices"][0]["finish_reason"], "tool_calls")
        # 缓存读算进 prompt_tokens —— 否则按 input_tokens 计费会漏掉那一段
        self.assertEqual(chat["usage"]["prompt_tokens"], 8)

    def test_oauth_uses_bearer_and_beta_header(self):
        """OAuth 令牌走 x-api-key 上游一律 401;beta 头少了会被当第三方应用。"""
        h = self.ad._headers(_claude_acct(), False)
        self.assertTrue(h["Authorization"].startswith("Bearer "))
        self.assertNotIn("x-api-key", h)
        self.assertIn("oauth-2025-04-20", h["anthropic-beta"])

    def test_untrusted_base_url_is_refused(self):
        with self.assertRaises(ClaudeError):
            self.ad._url(_claude_acct(secret={"refresh_token": "x",
                                              "base_url": "https://evil.example.com"}))


class ClaudeStreamTest(unittest.TestCase):
    """Anthropic SSE → OpenAI SSE。"""

    def setUp(self):
        self.ad = ClaudeAdapter()

    def _run(self, events):
        out = []
        with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(*events))):
            self.ad.stream_chat(_claude_acct(), "claude-sonnet-4-6",
                                {"messages": [{"role": "user", "content": "hi"}]},
                                out.append)
        return [json.loads(l[6:]) for l in out if l.startswith("data: ") and
                l[6:].strip() != "[DONE]"]

    def test_text_and_thinking_deltas(self):
        frames = self._run([
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 2}}])
        deltas = [f["choices"][0]["delta"] for f in frames]
        self.assertIn({"reasoning_content": "hmm"}, deltas)
        self.assertIn({"content": "hi"}, deltas)

    def test_usage_and_done(self):
        out = []
        with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(
                {"type": "message_start", "message": {"usage": {}}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 9}}))):
            self.ad.stream_chat(_claude_acct(), "claude-sonnet-4-6",
                                {"messages": [{"role": "user", "content": "hi"}]},
                                out.append)
        self.assertEqual([l for l in out if l.startswith("data: ")][-1].strip(),
                         "data: [DONE]")
        usage_frames = [json.loads(l[6:]) for l in out
                        if l.startswith("data: ") and '"usage"' in l]
        self.assertEqual(usage_frames[-1]["usage"]["completion_tokens"], 9)

    def test_tool_arguments_stream_with_stable_index(self):
        frames = self._run([
            {"type": "message_start", "message": {"usage": {}}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "c1", "name": "a"}},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "c2", "name": "b"}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '{"x":'}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '{"y":'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}])
        args = {}
        for f in frames:
            for tc in f["choices"][0]["delta"].get("tool_calls") or []:
                args.setdefault(tc["index"], []).append(
                    (tc.get("function") or {}).get("arguments", ""))
        self.assertEqual("".join(args[0]), '{"y":')
        self.assertEqual("".join(args[1]), '{"x":')

    def test_upstream_error_event_raises(self):
        with self.assertRaises(ClaudeError):
            with mock.patch("urllib.request.urlopen", return_value=_Resp(lines=_sse(
                    {"type": "error", "error": {"message": "overloaded"}}))):
                self.ad.stream_chat(_claude_acct(), "claude-sonnet-4-6",
                                    {"messages": [{"role": "user", "content": "hi"}]},
                                    lambda l: None)


class ClaudeRefreshTest(unittest.TestCase):
    def setUp(self):
        self.ad = ClaudeAdapter()

    def test_refresh_is_json_and_rotates(self):
        """这个端点的请求体是 JSON 不是表单 —— 发表单会 400。"""
        captured = {}

        def fake(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["ct"] = req.headers["Content-type"]
            return _Resp({"access_token": "new", "refresh_token": "rt-new",
                          "expires_in": 3600})

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            upd = self.ad.refresh_token(_claude_acct())
        self.assertEqual(captured["ct"], "application/json")
        self.assertEqual(captured["body"]["grant_type"], "refresh_token")
        self.assertEqual(upd["secret"]["refresh_token"], "rt-new")
        self.assertEqual(upd["token"], "new")

    def test_refresh_without_refresh_token_fails_loudly(self):
        with self.assertRaises(ClaudeError):
            self.ad.refresh_token(_claude_acct(secret={}))


class RefreshWriteBackTest(unittest.TestCase):
    """续期轮换必须落盘 —— 这条原先在 pool 和 scheduler 两边都漏了。"""

    def test_rotated_secret_is_carried_into_the_update(self):
        acct = {"meta": {"keep": 1}, "secret": {"refresh_token": "old"}}
        upd = {"token": "t", "token_exp": 5,
               "secret": {"refresh_token": "new"}}
        fields = refresh_fields(upd, acct)
        self.assertEqual(fields["secret"], {"refresh_token": "new"})
        self.assertEqual(fields["token"], "t")

    def test_adapter_that_does_not_rotate_leaves_secret_alone(self):
        """没返回 secret 的 adapter(以及没轮的号)不该把库里那份覆盖成空。"""
        acct = {"meta": {}, "secret": {"refresh_token": "old"}}
        fields = refresh_fields({"token": "t", "token_exp": 5}, acct)
        self.assertNotIn("secret", fields)


class ImportTest(unittest.TestCase):
    def setUp(self):
        # 每个用例一个库:几个用例都往 claude 这个渠道里塞号,共用一个库的话
        # 「导了两个还是两个」这种计数断言会被别的用例的残留污染。
        self.db = dbmod.DB(os.path.join(_TMP, f"import-{self._testMethodName}.db"))

    def test_openai_import_reads_account_id_from_id_token(self):
        import base64

        def jwt(payload):
            seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return "x." + seg + ".y"

        idt = jwt({"email": "a@x.com",
                   "https://api.openai.com/auth": {"chatgpt_account_id": "acc-7",
                                                   "chatgpt_plan_type": "plus"}})
        ad = get_adapter("openai")
        action, identity = ad.import_account(self.db, {
            "refresh_token": "rt", "id_token": idt})
        self.assertEqual(action, "imported")
        self.assertEqual(identity, "a@x.com")
        row = self.db.get_by_identity("openai", "a@x.com")
        self.assertEqual(row["secret"]["chatgpt_account_id"], "acc-7")
        self.assertEqual(row["meta"]["plan"], "plus")

    def test_openai_import_requires_refresh_token(self):
        """只有 access_token 的号几小时后就失效,而且这个渠道没有别的续期途径。"""
        ad = get_adapter("openai")
        with self.assertRaises(ValueError):
            ad.import_account(self.db, {"email": "a@x.com", "access_token": "t"})

    def test_claude_import_keeps_uuid(self):
        ad = get_adapter("claude")
        action, identity = ad.import_account(self.db, {
            "refresh_token": "rt", "account_uuid": "uuid-3",
            "access_token": "at", "expires_at": 1893456000})
        self.assertEqual(action, "imported")
        row = self.db.get_by_identity("claude", identity)
        self.assertEqual(row["secret"]["account_uuid"], "uuid-3")
        self.assertEqual(row["token_exp"], 1893456000)

    def test_claude_import_requires_refresh_token(self):
        ad = get_adapter("claude")
        with self.assertRaises(ValueError):
            ad.import_account(self.db, {"account_uuid": "u", "access_token": "t"})

    def test_reimport_is_skipped_not_duplicated(self):
        ad = get_adapter("claude")
        ad.import_account(self.db, {"refresh_token": "r1", "account_uuid": "u9"})
        action, _ = ad.import_account(self.db, {"refresh_token": "r2",
                                                "account_uuid": "u9"})
        self.assertEqual(action, "skipped")
        self.assertEqual(self.db.count_accounts(channel="claude"), 1)


class WindowMetaTest(unittest.TestCase):
    """用量窗口的归一 —— 两边上游字段名完全不同,面板只该认一种形状。"""

    def setUp(self):
        self.oa = OpenAIAdapter()
        self.cl = ClaudeAdapter()

    # ---- OpenAI: x-codex-* ----

    def test_openai_percent_becomes_fraction(self):
        """上游给 0-100 的百分数,归一成 0..1 的分数(面板只乘 100)。"""
        m = self.oa.window_meta({
            "x-codex-primary-used-percent": "42",
            "x-codex-primary-window-minutes": "300",
            "x-codex-primary-reset-after-seconds": "3600",
        })
        self.assertAlmostEqual(m["usage_5h"]["pct"], 0.42)
        self.assertGreater(m["usage_5h"]["reset_at"], time.time())

    def test_openai_slots_are_classified_by_window_length(self):
        """primary/secondary 哪个是 5 小时是**按窗口时长判**的,不是固定 primary。
        实测两种分配都出现过,写死会让两条进度条对调。"""
        m = self.oa.window_meta({
            "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "10080",     # primary 反而是 7 天
            "x-codex-secondary-used-percent": "90",
            "x-codex-secondary-window-minutes": "300",
        })
        self.assertAlmostEqual(m["usage_5h"]["pct"], 0.90)
        self.assertAlmostEqual(m["usage_7d"]["pct"], 0.10)

    def test_openai_without_window_minutes_falls_back(self):
        """两边都没有 window-minutes 时按 primary=5h 退化 —— 有依据的猜测,
        猜错的代价只是面板上两条对调,不影响取号。"""
        m = self.oa.window_meta({
            "x-codex-primary-used-percent": "5",
            "x-codex-secondary-used-percent": "60"})
        self.assertAlmostEqual(m["usage_5h"]["pct"], 0.05)
        self.assertAlmostEqual(m["usage_7d"]["pct"], 0.60)

    def test_openai_headers_are_case_insensitive(self):
        """头名大小写是上游定的,不该让调用方去猜。"""
        m = self.oa.window_meta({"X-Codex-Primary-Used-Percent": "7"})
        self.assertAlmostEqual(m["usage_5h"]["pct"], 0.07)

    def test_openai_without_headers_is_empty(self):
        """上游没给窗口信息就什么都不返回 —— 面板上不会多出两列空进度条。"""
        self.assertEqual(self.oa.window_meta({}), {})
        self.assertEqual(self.oa.window_meta({"content-type": "application/json"}), {})

    # ---- Claude: anthropic-ratelimit-unified-* ----

    def test_claude_already_gives_a_fraction(self):
        m = self.cl.window_meta({
            "anthropic-ratelimit-unified-5h-utilization": "0.83",
            "anthropic-ratelimit-unified-7d-utilization": "0.11",
        })
        self.assertAlmostEqual(m["usage_5h"]["pct"], 0.83)
        self.assertAlmostEqual(m["usage_7d"]["pct"], 0.11)

    def test_claude_surpassed_threshold_pins_to_full(self):
        """打穿了还显示 97% 是撒谎 —— 那一刻号已经调不动了。"""
        m = self.cl.window_meta({
            "anthropic-ratelimit-unified-5h-utilization": "0.97",
            "anthropic-ratelimit-unified-5h-surpassed-threshold": "true"})
        self.assertEqual(m["usage_5h"]["pct"], 1.0)

    def test_claude_reset_accepts_both_timestamp_shapes(self):
        unix = self.cl.window_meta({
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-5h-reset": "1789999999"})
        iso = self.cl.window_meta({
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-5h-reset": "2026-09-21T12:00:00Z"})
        self.assertEqual(unix["usage_5h"]["reset_at"], 1789999999)
        self.assertGreater(iso["usage_5h"]["reset_at"], 0)

    def test_claude_unparseable_reset_is_omitted_not_guessed(self):
        """猜一个时间会显示成「刚重置过」,比不显示更糟。"""
        m = self.cl.window_meta({
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-5h-reset": "not-a-time"})
        self.assertNotIn("reset_at", m["usage_5h"])

    # ---- 两条路都要带上 meta ----

    def test_nonstream_chat_returns_window_meta(self):
        headers = {"x-codex-primary-used-percent": "33",
                   "x-codex-primary-window-minutes": "300"}
        with mock.patch("urllib.request.urlopen",
                        return_value=_Resp({"output": [], "usage": {}},
                                           headers=headers)):
            r = self.oa.chat(_openai_acct(),
                             [{"role": "user", "content": "hi"}], model="gpt-5.4")
        self.assertAlmostEqual(r["meta"]["usage_5h"]["pct"], 0.33)

    def test_stream_chat_returns_window_meta(self):
        headers = {"anthropic-ratelimit-unified-7d-utilization": "0.44"}
        with mock.patch("urllib.request.urlopen", return_value=_Resp(
                lines=_sse({"type": "message_start", "message": {"usage": {}}}),
                headers=headers)):
            r = self.cl.stream_chat(_claude_acct(), "claude-sonnet-4-6",
                                    {"messages": [{"role": "user", "content": "hi"}]},
                                    lambda l: None)
        self.assertAlmostEqual(r["meta"]["usage_7d"]["pct"], 0.44)

    def test_error_carries_headers_for_the_rate_limited_case(self):
        """窗口打满时上游回 429,最新的百分比就在 429 的响应头里 ——
        不从失败响应里取,面板会停在旧数上撒谎。"""
        import email.message
        import urllib.error

        hdrs = email.message.Message()
        hdrs["anthropic-ratelimit-unified-5h-utilization"] = "1.0"
        err = urllib.error.HTTPError("http://x", 429, "Too Many Requests",
                                     hdrs, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ClaudeError) as ctx:
                self.cl.chat(_claude_acct(), [{"role": "user", "content": "hi"}],
                             model="claude-sonnet-4-6")
        self.assertEqual(ctx.exception.code, 429)
        self.assertAlmostEqual(
            self.cl.window_meta(ctx.exception.headers)["usage_5h"]["pct"], 1.0)


class PersistRuntimeMetaTest(unittest.TestCase):
    """窗口数据的落盘与节流。"""

    def setUp(self):
        self.db = dbmod.DB(os.path.join(_TMP, f"meta-{id(self)}.db"))
        self.aid = self.db.upsert_account("openai", "w@x.com",
                                          secret={"refresh_token": "r"})

    def test_patch_is_merged_not_replaced(self):
        """meta 里还有别的键(套餐、账号 id),写窗口不能把它们抹掉。"""
        self.db.update_account(self.aid, meta={"plan": "plus"})
        persist_runtime_meta(self.db, self.aid,
                             {"usage_5h": {"pct": 0.1}, "usage_updated_at": 1000})
        meta = self.db.get_account(self.aid)["meta"]
        self.assertEqual(meta["plan"], "plus")
        self.assertAlmostEqual(meta["usage_5h"]["pct"], 0.1)

    def test_repeat_writes_inside_the_window_are_skipped(self):
        """上游每几秒就抬一点窗口数,每次请求都写等于把库当日志用。"""
        self.assertTrue(persist_runtime_meta(
            self.db, self.aid, {"usage_5h": {"pct": 0.1}, "usage_updated_at": 1000}))
        self.assertFalse(persist_runtime_meta(
            self.db, self.aid, {"usage_5h": {"pct": 0.2}, "usage_updated_at": 1030}))
        self.assertAlmostEqual(
            self.db.get_account(self.aid)["meta"]["usage_5h"]["pct"], 0.1)
        # 过了节流窗口就该写进去
        self.assertTrue(persist_runtime_meta(
            self.db, self.aid, {"usage_5h": {"pct": 0.3}, "usage_updated_at": 1100}))
        self.assertAlmostEqual(
            self.db.get_account(self.aid)["meta"]["usage_5h"]["pct"], 0.3)

    def test_empty_patch_and_unknown_account_are_noops(self):
        self.assertFalse(persist_runtime_meta(self.db, self.aid, {}))
        self.assertFalse(persist_runtime_meta(self.db, 999999,
                                              {"usage_updated_at": 1}))


class TestHookTest(unittest.TestCase):
    """管理台「测试账号」走 adapter.test_account,默认实现复用 chat()。"""

    def test_default_implementation_reuses_chat(self):
        ad = OpenAIAdapter()
        called = {}

        def fake_chat(acct, messages, stream=False, model=None, on_token=None, body=None):
            called["model"] = model
            called["prompt"] = messages[0]["content"]
            return {"raw": {"choices": [{"message": {"content": "hi"}}]},
                    "content": "hi", "usage": {"total_tokens": 3}}

        with mock.patch.object(ad, "chat", side_effect=fake_chat):
            out = ad.test_account(_openai_acct(), model="gpt-5.4")
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "hi")
        self.assertEqual(called["model"], "gpt-5.4")
        self.assertTrue(called["prompt"])

    def test_upstream_error_propagates_with_code(self):
        """测试失败要让端点拿到 .code,界面才能分清 401(号坏了)和 503(上游忙)。"""
        ad = ClaudeAdapter()

        def boom(*a, **k):
            raise ClaudeError(401, "invalid api key")

        with mock.patch.object(ad, "chat", side_effect=boom):
            with self.assertRaises(ClaudeError) as ctx:
                ad.test_account(_claude_acct())
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
