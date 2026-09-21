"""流式请求的计费生命周期 —— 不做预占,允许透支,结算幂等靠 request_id。

覆盖需求里的 7 项测试要点。
"""
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "s.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "nx")
os.environ["BITAPI_PRICING_PATH"] = os.path.join(_TMP, "nopricing.json")
os.environ["BITAPI_PRICING_URL"] = ""

import config  # noqa: E402
import server  # noqa: E402
from core import credit as C  # noqa: E402
from core.adapter import BILL_UPSTREAM, Adapter  # noqa: E402
from core.billing import Billing, InsufficientBalanceError  # noqa: E402
from core.pricing import Pricing  # noqa: E402
from core.user_db import UserDB  # noqa: E402


class _Upstream(Adapter):
    billing_mode = BILL_UPSTREAM


def _setup(name, policy="balance"):
    db = UserDB(os.path.join(_TMP, name))
    C.bind(db)
    gid = db.create_group(name="g-" + name, supported_models=["*"],
                          billing_policy=policy)
    uid = db.create_user(f"u-{name}@x.com", "h", "AFF" + name[:4].upper(),
                         group_id=gid)
    pricing = Pricing(db, catalog=None, cache_ttl=0)
    billing = Billing(db, pricing=pricing)
    # $1/1M in, $2/1M out —— 好算
    db.upsert_pricing("m1", None, billing_mode="token",
                      input_price=1, output_price=2)
    pricing.invalidate()
    return db, billing, dict(db.get_user(uid), _api_key_id=None), db.get_group(gid)


def _sse(obj):
    import json
    return "data: " + json.dumps(obj) + "\n\n"


def _chunk(content=None, finish=None, usage=None):
    o = {"id": "x", "object": "chat.completion.chunk", "model": "m1",
         "choices": [{"index": 0, "delta": {"content": content} if content else {},
                      "finish_reason": finish}]}
    if usage is not None:
        o["usage"] = usage
    return _sse(o)


class SniffTest(unittest.TestCase):
    def test_terminal_and_frt_and_text(self):
        import time
        h = {"_t0": time.time() - 0.05}
        server._sniff_usage(_chunk(content="he"), h)
        server._sniff_usage(_chunk(content="llo"), h)
        server._sniff_usage(_chunk(finish="stop"), h)
        server._sniff_usage("data: [DONE]\n\n", h)
        self.assertEqual(h["text"], "hello")
        self.assertTrue(h["terminal"])
        self.assertGreaterEqual(h["frt_ms"], 40)

    def test_repeated_usage_takes_latest_not_sum(self):
        h = {}
        server._sniff_usage(_chunk(usage={"prompt_tokens": 10, "completion_tokens": 1}), h)
        server._sniff_usage(_chunk(usage={"prompt_tokens": 10, "completion_tokens": 7}), h)
        self.assertEqual(h["usage"]["completion_tokens"], 7)   # 取最近,不是 8

    def test_no_terminal_event(self):
        h = {}
        server._sniff_usage(_chunk(content="hi"), h)
        self.assertFalse(h.get("terminal"))


class ForceIncludeUsageTest(unittest.TestCase):
    def test_forces_include_usage_on_stream(self):
        out = server._force_include_usage({"stream": True, "model": "m"})
        self.assertTrue(out["stream_options"]["include_usage"])

    def test_preserves_other_stream_options(self):
        out = server._force_include_usage(
            {"stream": True, "stream_options": {"foo": 1}})
        self.assertEqual(out["stream_options"]["foo"], 1)
        self.assertTrue(out["stream_options"]["include_usage"])

    def test_non_stream_untouched(self):
        body = {"stream": False}
        self.assertIs(server._force_include_usage(body), body)


class SyntheticStreamTest(unittest.TestCase):
    class _Pool:
        def get_valid_account(self, _channel, _adapter):
            return {"id": 1}

    def test_nonproxy_stream_marks_done_and_keeps_real_usage(self):
        class FakeAdapter:
            streaming = False

            def chat(self, _acct, _messages, stream=False, model=None,
                     body=None, on_token=None):
                on_token("hello", "content")
                return {"content": "hello", "reasoning": None,
                        "usage": {"prompt_tokens": 3,
                                  "completion_tokens": 2,
                                  "total_tokens": 5}}

        holder, settled = {}, {}
        with mock.patch.object(server, "POOL", self._Pool()):
            out = "".join(server._billed_stream(
                server._stream(FakeAdapter(), "fake", [{"role": "user",
                                                         "content": "hi"}],
                               "m1", {"stream": True}, usage_holder=holder),
                holder, lambda reason: settled.setdefault("reason", reason)))
        self.assertIn("data: [DONE]", out)
        self.assertEqual(settled["reason"], "done")
        self.assertEqual(holder["text"], "hello")
        self.assertEqual(holder["usage"]["completion_tokens"], 2)
        self.assertIn("frt_ms", holder)

    def test_partial_output_followed_by_error_stays_eof(self):
        class BrokenAdapter:
            streaming = False

            def chat(self, _acct, _messages, stream=False, model=None,
                     body=None, on_token=None):
                on_token("partial", "content")
                raise RuntimeError("upstream broke")

        holder, settled = {}, {}
        with mock.patch.object(server, "POOL", self._Pool()):
            list(server._billed_stream(
                server._stream(BrokenAdapter(), "fake", [], "m1",
                               {"stream": True}, usage_holder=holder),
                holder, lambda reason: settled.setdefault("reason", reason)))
        self.assertEqual(settled["reason"], "eof")
        self.assertFalse(holder.get("terminal"))

    def test_virtual_tool_stream_is_sniffed(self):
        routed = {"model": "m1", "messages": [], "stream": True}
        response = {
            "id": "chatcmpl-test", "created": 1, "model": "m1",
            "choices": [{"message": {"role": "assistant", "content": "answer"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2,
                      "total_tokens": 6},
        }
        holder = {}

        def fake_nonstream(_body, served=None):
            # 与真实 _openai_nonstream 同一契约:served 里报实际渠道与上游真实 usage
            if served is not None:
                served.update({"channel": "grok",
                               "upstream_usage": response["usage"]})
            return response

        with mock.patch.object(server, "_resolve_routes",
                               return_value=(None, None, routed,
                                             [("grok", object())])), \
             mock.patch.object(server, "_virtual_tooling_requested",
                               return_value=True), \
             mock.patch.object(server, "_openai_nonstream",
                               side_effect=fake_nonstream):
            out = "".join(server._openai_stream(routed, holder))
        self.assertIn("answer", out)
        self.assertTrue(holder["terminal"])
        self.assertEqual(holder["text"], "answer")
        self.assertEqual(holder["usage"]["completion_tokens"], 2)
        self.assertEqual(holder["channel"], "grok")

    def test_virtual_tool_stream_does_not_pass_estimate_off_as_upstream(self):
        """合成终止帧里的 usage 是估算值:客户端要看到,计量层不能当真实值。"""
        routed = {"model": "m1", "messages": [], "stream": True}
        response = {"id": "c", "created": 1, "model": "m1",
                    "choices": [{"message": {"role": "assistant", "content": "answer"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
        holder = {}

        def fake_nonstream(_body, served=None):
            if served is not None:
                served.update({"channel": "xai", "upstream_usage": None})
            return response

        with mock.patch.object(server, "_resolve_routes",
                               return_value=(None, None, routed, [("xai", object())])), \
             mock.patch.object(server, "_virtual_tooling_requested", return_value=True), \
             mock.patch.object(server, "_openai_nonstream", side_effect=fake_nonstream):
            out = "".join(server._openai_stream(routed, holder))
        self.assertIn('"total_tokens": 6', out)      # 客户端看得到
        self.assertNotIn("usage", holder)            # 计量层不拿它当上游真实值
        self.assertTrue(holder["terminal"])


class BilledStreamTest(unittest.TestCase):
    """_billed_stream 的 end_reason 判定与断连排空。"""

    def test_done_when_terminal_seen(self):
        seen = {}
        h = {}

        def inner():
            yield _chunk(content="a")
            yield _chunk(finish="stop")
            yield "data: [DONE]\n\n"
            server._sniff_usage("data: [DONE]\n\n", h)

        # 手动喂 sniff(生产里由 _proxy_stream/_stream 调用)
        def wrapped():
            for c in inner():
                server._sniff_usage(c, h)
                yield c

        list(server._billed_stream(wrapped(), h, lambda r: seen.setdefault("r", r)))
        self.assertEqual(seen["r"], "done")

    def test_eof_when_no_terminal(self):
        seen = {}
        h = {}

        def wrapped():
            for c in [_chunk(content="a")]:
                server._sniff_usage(c, h)
                yield c

        list(server._billed_stream(wrapped(), h, lambda r: seen.setdefault("r", r)))
        self.assertEqual(seen["r"], "eof")

    def test_client_gone_drains_upstream_and_settles(self):
        seen = {}
        h = {}
        produced = []

        def upstream():
            for i in range(5):
                c = _chunk(content="tok%d" % i)
                produced.append(i)
                server._sniff_usage(c, h)
                yield c
            tail = _chunk(usage={"prompt_tokens": 100, "completion_tokens": 50},
                          finish="stop")
            server._sniff_usage(tail, h)
            produced.append("usage")
            yield tail

        gen = server._billed_stream(upstream(), h, lambda r: seen.setdefault("r", r))
        next(gen)          # 只读一个 chunk
        gen.close()        # 模拟客户端断连
        self.assertEqual(seen["r"], "client_gone")
        # 断连后仍排空了上游 → usage 已合并
        self.assertIn("usage", produced)
        self.assertEqual(h["usage"]["completion_tokens"], 50)

    def test_scanner_error(self):
        seen = {}
        h = {}

        def broken():
            yield _chunk(content="a")
            raise RuntimeError("upstream blew up")

        with self.assertRaises(RuntimeError):
            list(server._billed_stream(broken(), h, lambda r: seen.setdefault("r", r)))
        self.assertEqual(seen["r"], "scanner_error")

    def test_settle_failure_does_not_break_stream(self):
        h = {}

        def boom(_r):
            raise RuntimeError("settle failed")

        def inner():
            yield _chunk(content="a")

        out = list(server._billed_stream(inner(), h, boom))
        self.assertEqual(len(out), 1)   # 流照常输出


class SettlementTest(unittest.TestCase):
    def test_normal_stream_records_and_deducts(self):
        db, billing, user, group = _setup("st1.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        log_id = billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 1_000_000,
                            "completion_tokens": 1_000_000},
            stream=True, request_id="req-1", end_reason="done",
            first_token_ms=42)
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertEqual(log["id"], log_id)
        snap = log["pricing_snapshot"]
        self.assertEqual(snap["end_reason"], "done")
        self.assertEqual(snap["frt_ms"], 42)
        self.assertEqual(snap["request_id"], "req-1")
        self.assertAlmostEqual(log["actual_cost"], 3.0)   # 1 + 2
        entries = [e for e in db.list_ledger(user["id"]) if e["reason"] == "usage"]
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["amount"], -3.0)
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 7.0)

    def test_client_gone_still_charges_produced_tokens(self):
        db, billing, user, group = _setup("st2.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 500_000, "completion_tokens": 250_000},
            stream=True, request_id="req-2", end_reason="client_gone")
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertEqual(log["pricing_snapshot"]["end_reason"], "client_gone")
        expected = 0.5 * 1 + 0.25 * 2   # 0.5 + 0.5 = 1.0
        self.assertAlmostEqual(log["actual_cost"], expected)
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10.0 - expected)

    def test_zero_output_no_charge_no_ledger(self):
        db, billing, user, group = _setup("st3.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 0, "completion_tokens": 0},
            request_messages=[], output_text="",
            stream=True, request_id="req-3", end_reason="scanner_error")
        log = db.recent_usage(user["id"], limit=1)[0]
        self.assertEqual(log["actual_cost"], 0)
        self.assertEqual([e for e in db.list_ledger(user["id"])
                          if e["reason"] == "usage"], [])
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 10.0)

    def test_duplicate_settlement_charges_once(self):
        db, billing, user, group = _setup("st4.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        ids = [billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(), upstream_usage=usage,
            stream=True, request_id="req-dup", end_reason="done")
            for _ in range(5)]
        self.assertEqual(len(set(ids)), 1)              # 同一条日志
        logs = [l for l in db.recent_usage(user["id"], limit=10)]
        self.assertEqual(len(logs), 1)                  # 不重复写日志
        entries = [e for e in db.list_ledger(user["id"]) if e["reason"] == "usage"]
        self.assertEqual(len(entries), 1)               # 只扣一次
        self.assertAlmostEqual(db.get_user(user["id"])["balance"], 9.0)

    def test_balance_boundary_zero_blocked_tiny_allowed_then_overdraft(self):
        db, billing, user, group = _setup("st5.db")
        # 余额恰好 0 → 预检 402
        with self.assertRaises(InsufficientBalanceError):
            billing.precheck(user, group)
        # 余额 0.001 → 放行
        C.credit(user["id"], 0.001, "recharge", "r1")
        user = dict(db.get_user(user["id"]), _api_key_id=None)
        billing.precheck(user, group)
        # 本次请求扣成负数(允许透支,保证进行中的请求完成)
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 1_000_000},
            stream=True, request_id="req-5", end_reason="done")
        bal = db.get_user(user["id"])["balance"]
        self.assertLess(bal, 0)
        self.assertAlmostEqual(bal, 0.001 - 1.0)
        # 下次请求被拦
        user = dict(db.get_user(user["id"]), _api_key_id=None)
        with self.assertRaises(InsufficientBalanceError):
            billing.precheck(user, group)

    def test_snapshot_contains_end_reason_and_frt(self):
        db, billing, user, group = _setup("st6.db", policy="free")
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 10},
            stream=True, request_id="req-6", end_reason="eof",
            first_token_ms=137)
        snap = db.recent_usage(user["id"], limit=1)[0]["pricing_snapshot"]
        self.assertEqual(snap["end_reason"], "eof")
        self.assertEqual(snap["frt_ms"], 137)
        self.assertEqual(snap["request_id"], "req-6")

    def test_no_request_id_falls_back_to_log_id_key(self):
        db, billing, user, group = _setup("st7.db")
        C.credit(user["id"], 10.0, "recharge", "r1")
        billing.record_usage(
            user, group, "ch", "m1", adapter=_Upstream(),
            upstream_usage={"prompt_tokens": 1_000_000})
        entries = [e for e in db.list_ledger(user["id"]) if e["reason"] == "usage"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["idem_key"].startswith("usage:log:"))


class AnthropicStreamGuardTest(unittest.TestCase):
    def test_no_terminal_events_when_message_never_started(self):
        # 上游一行有效数据都没有 → 不补任何结束事件(宁可空流也不给残缺流)
        out = list(server._anthropic_stream(iter(["data: [DONE]\n\n"]), "m1"))
        self.assertEqual(out, [])

    def test_completes_protocol_when_started(self):
        lines = [_chunk(content="hi"), _chunk(finish="stop"), "data: [DONE]\n\n"]
        out = "".join(server._anthropic_stream(iter(lines), "m1"))
        self.assertIn("message_start", out)
        self.assertIn("content_block_stop", out)
        self.assertIn("message_delta", out)
        self.assertIn("message_stop", out)

    def test_completes_protocol_even_without_terminal(self):
        # 上游断流未见终止事件,但已 start → 仍补齐,避免客户端挂死
        out = "".join(server._anthropic_stream(iter([_chunk(content="hi")]), "m1"))
        self.assertIn("message_start", out)
        self.assertIn("message_stop", out)


if __name__ == "__main__":
    unittest.main()
