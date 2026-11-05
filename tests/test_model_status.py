#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型状态巡检:探测分支、TTFT、历史与状态条、管理/用户端点门禁。

探测全用假 adapter / 假 pool,不出网;API 只验证读数形状与权限,不真跑巡检。
"""
import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ms.db"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from core import adapter as adapter_mod  # noqa: E402
from core import model_status as MS  # noqa: E402
from core.adapter import register_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.hooks import on  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient  # noqa: E402


class FakeAcct(dict):
    pass


class FakePoolDb:
    """假号池库:只提供 pick_active(生成类兜底分支用)。"""

    def __init__(self, fail_get=False):
        self.fail_get = fail_get

    def pick_active(self, channel, prefer=None, group_id=None, exclude=None):
        return None if self.fail_get else {"id": 1, "channel": channel}


class FakePool:
    """假号池:默认每次给一个自增 id 的号;失败计入计数。"""

    def __init__(self, fail_get=False, seq=False):
        self.fail_get = fail_get
        self.seq = seq
        self.failures = []
        self._n = 0

    def get_valid_account(self, channel, adapter, max_tries=20):
        if self.fail_get:
            return None
        if self.seq:
            self._n += 1
            return FakeAcct(id=self._n, channel=channel, token="t%d" % self._n)
        return FakeAcct(id=1, channel=channel, token="t")

    def pick_active(self, channel, prefer=None, group_id=None, exclude=None):
        return None if self.fail_get else {"id": 1, "channel": channel}

    def mark_failure(self, acct_id, exc, dead=False, adapter=None):
        self.failures.append((acct_id, str(exc)))


class ChatAdapter:
    name = "chat-ok"
    kind = "chat"
    models = ["m1", "m2"]
    capabilities = ["chat"]
    streaming = True

    def __init__(self, reply=None, error=None, fail_ids=None, stream_error=None):
        self.reply = reply if reply is not None else {
            "raw": {"choices": [{"message": {"content": "pong"}}], "usage": {}}}
        self.error = error
        self.stream_error = stream_error
        self.fail_ids = fail_ids      # 只有这些账号 id 会失败(模拟单 key 被上游限制)
        self.calls = 0

    def has(self, cap):
        return cap in self.capabilities

    def supports_chat(self, model):
        return True

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        self.calls += 1
        time.sleep(0.002)   # 让 latency_ms 至少跳过 0,断言才有意义
        if self.error and (not self.fail_ids or acct["id"] in self.fail_ids):
            raise RuntimeError(self.error)
        return dict(self.reply)

    def stream_chat(self, acct, model, body, on_sse):
        self.calls += 1
        time.sleep(0.002)
        err = self.stream_error or self.error
        if err and (not self.fail_ids or acct["id"] in self.fail_ids):
            raise RuntimeError(err)
        on_sse('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
        time.sleep(0.002)
        on_sse('data: {"choices":[{"delta":{"content":"pong"}}]}\n\n')
        on_sse("data: [DONE]\n\n")


class EmbedAdapter:
    name = "embed"
    kind = "chat"
    models = ["e1"]
    capabilities = []

    def has(self, cap):
        return False

    def embeddings(self, acct, body):
        assert body["model"] == "e1"
        return {"data": [{"embedding": [0.0]}]}


class GenAdapter:
    """生成类:整个渠道不巡检。"""

    name = "gen"
    kind = "generation"
    models = ["img-1"]
    capabilities = ["balance"]

    def has(self, cap):
        return cap in self.capabilities

    def supports_chat(self, model):
        return False


class PredictAdapter:
    name = "pred"
    kind = "chat"
    models = ["kumo"]
    prediction_models = ["kumo"]
    capabilities = ["chat"]

    def has(self, cap):
        return cap in self.capabilities

    def supports_chat(self, model):
        return model not in self.prediction_models


class ModelStatusCoreTest(unittest.TestCase):
    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, "ms-core-%d.db" % id(self)))
        self.events = []
        self._subs = [
            on("model.failed")(lambda **kw: self.events.append(("failed", kw))),
            on("model.recovered")(lambda **kw: self.events.append(("recovered", kw))),
        ]

    def tearDown(self):
        # hooks.on 会一直累积订阅,测试结束摘掉自己注册的
        from core import hooks
        for kind in ("model.failed", "model.recovered"):
            hooks._HANDLERS[kind] = [f for f in hooks._HANDLERS.get(kind, [])
                                     if f not in self._subs]

    def test_gpt_monitoring_only_probes_and_reports_terra_without_changing_routing_models(self):
        gpt = ChatAdapter()
        gpt.name, gpt.models = "gpt", ["gpt-6.1-sol", "gpt-5.6-luna"]
        before = list(gpt.models)
        with mock.patch.object(MS, "probe_one", return_value=(MS.OK, 100, 50, None)) as probe:
            result = asyncio.run(MS.check_all(self.db, FakePool(), FakePoolDb(), {"gpt": gpt}, log=lambda _: None))
        self.assertEqual(result, {"ok": 1, "fail": 0, "skip": 0})
        self.assertEqual([call.args[3] for call in probe.call_args_list], ["gpt-6.1-terra"])
        self.assertEqual(gpt.models, before)
        stored = {("gpt", "gpt-6.1-sol"): {"ok": MS.OK, "checked_at": 1}}
        self.assertEqual(list(MS.user_status_map({"gpt": gpt}, stored, {})), ["gpt-6.1-terra"])
        rows = MS.aggregate_for_user({"gpt": gpt}, stored)
        self.assertEqual([row["model"] for row in rows], ["gpt-6.1-terra"])
        self.assertIsNone(rows[0]["ok"], "keep the selected monitoring target even before its first result")

    def test_chat_stream_probe_records_ttft(self):
        good = ChatAdapter()
        bad = ChatAdapter(error="HTTP 502 from upstream: boom",
                          stream_error="HTTP 502 from upstream: boom")
        bad.name = "chat-bad"
        pool = FakePool()
        tally = asyncio.run(MS.check_all(
            self.db, pool, FakePoolDb(), {good.name: good, bad.name: bad}))
        self.assertEqual(tally, {"ok": 2, "fail": 2, "skip": 0})
        ok_row = self.db.get_model_status("chat-ok", "m1")
        self.assertEqual(ok_row["ok"], MS.OK)
        self.assertIsNotNone(ok_row["ttft_ms"])       # 流式探测量到了首字
        self.assertGreaterEqual(ok_row["ttft_ms"], 2)
        self.assertGreaterEqual(ok_row["latency_ms"], ok_row["ttft_ms"])
        bad_row = self.db.get_model_status("chat-bad", "m1")
        self.assertEqual(bad_row["ok"], MS.FAIL)
        self.assertIn("boom", bad_row["error"])
        # Each account is penalized once, after both transports have failed.
        self.assertEqual(len(pool.failures), 4)

    def test_stream_failure_falls_back_to_nonstream(self):
        """流式这条路坏(上游不支持/超时),原地回退非流式:模型活着,无 TTFT。"""
        ad = ChatAdapter(stream_error="HTTP 400: stream not supported")
        pool = FakePool()
        ok, ms, ttft, err = MS._probe_chat(ad, pool, "m1")
        self.assertEqual((ok, ttft, err), (MS.OK, None, None))
        self.assertEqual(ad.calls, 2)                 # 流式 1 次 + 非流式 1 次
        self.assertEqual(pool.failures, [], "successful fallback must not penalize the account")

    def test_single_bad_key_retried_with_next_account(self):
        """第一把 key 被上游单独限制、第二把是好的:换号重试后应判 OK。

        探测路径:流式失败 → 原地回退非流式也失败 → 换一把号 → 流式成功。"""
        ad = ChatAdapter(error="HTTP 403 from upstream: inference blocked",
                         fail_ids={1})
        pool = FakePool(seq=True)
        ok, probe_ms, ttft, err = MS._probe_chat(ad, pool, "m1")
        self.assertEqual(ok, MS.OK)
        self.assertIsNotNone(ttft)              # 第二把号走流式,量到首字
        self.assertEqual(ad.calls, 3)           # 流式失败+回退失败 + 第二把成功
        self.assertEqual(len(pool.failures), 1)  # Only the account that failed both transports is penalized.

    def test_generation_channel_excluded_entirely(self):
        ad = GenAdapter()
        self.assertIsNone(MS.probe_one(ad, FakePool(), FakePoolDb(), "img-1"))
        tally = asyncio.run(MS.check_all(
            self.db, FakePool(), FakePoolDb(), {ad.name: ad}))
        self.assertEqual(tally, {"ok": 0, "fail": 0, "skip": 0})
        self.assertIsNone(self.db.get_model_status("gen", "img-1"))

    def test_prediction_not_recorded(self):
        ad = PredictAdapter()
        self.assertIsNone(MS.probe_one(ad, FakePool(), FakePoolDb(), "kumo"))
        tally = asyncio.run(MS.check_all(
            self.db, FakePool(), FakePoolDb(), {ad.name: ad}))
        self.assertEqual(tally, {"ok": 0, "fail": 0, "skip": 0})

    def test_pool_empty_is_skip_not_fail(self):
        ad = ChatAdapter()
        pool = FakePool(fail_get=True)
        result = MS.probe_one(ad, pool, FakePoolDb(), "m1")
        self.assertEqual(result[0], MS.SKIP)
        self.assertIn("号池", result[3])
        self.assertEqual(pool.failures, [])

    def test_embed_probe(self):
        ok, ms, ttft, err = MS._probe_embed(EmbedAdapter(), FakePool(), "e1")
        self.assertEqual((ok, ttft, err), (MS.OK, None, None))

    def test_transition_events_fire_once_each(self):
        ad = ChatAdapter()
        MS._record(self.db, ad, "m1", (MS.OK, 10, 5, None))
        self.assertEqual(self.events, [])          # 首次无翻转
        MS._record(self.db, ad, "m1", (MS.FAIL, 10, None, "x"))
        self.assertEqual([e[0] for e in self.events], ["failed"])
        self.assertEqual(self.events[0][1]["model"], "m1")
        MS._record(self.db, ad, "m1", (MS.FAIL, 10, None, "x"))
        self.assertEqual(len(self.events), 1)      # 持续失败不重复报
        MS._record(self.db, ad, "m1", (MS.OK, 10, 5, None))
        self.assertEqual([e[0] for e in self.events], ["failed", "recovered"])

    def test_run_checked_guard_and_prune(self):
        ad = ChatAdapter()

        def adapters_fn():
            return {ad.name: ad}

        first = asyncio.run(MS.run_checked(self.db, FakePool(), FakePoolDb(),
                                           adapters_fn))
        self.assertTrue(first)

        async def two_at_once():
            task = asyncio.create_task(MS.run_checked(
                self.db, FakePool(), FakePoolDb(), adapters_fn))
            await asyncio.sleep(0)
            second = await MS.run_checked(self.db, FakePool(), FakePoolDb(),
                                          adapters_fn)
            return await task, second
        task_result, second = asyncio.run(two_at_once())
        self.assertTrue(task_result)
        self.assertFalse(second)
        # 历史保留期:刚写入的行不被清,老行被清
        self.db.add_model_status("chat-ok", "old", MS.OK,
                                 checked_at=time.time() - MS.RETENTION - 60)
        asyncio.run(MS.run_checked(self.db, FakePool(), FakePoolDb(), adapters_fn))
        self.assertIsNone(self.db.get_model_status("chat-ok", "old"))
        self.assertIsNotNone(self.db.get_model_status("chat-ok", "m1"))


class DayBarsTest(unittest.TestCase):
    def _row(self, ok, checked_at, ttft=None, latency=0):
        return {"ok": ok, "checked_at": checked_at,
                "ttft_ms": ttft, "latency_ms": latency}

    def test_hour_bars_buckets_each_heartbeat_hour(self):
        now = 1793300000  # 固定时刻,避免跨小时抖动
        slot = int(now // 3600) * 3600
        rows = [
            self._row(MS.OK, slot - 3600, ttft=15000),
            self._row(MS.FAIL, slot),
        ]
        bars = MS.hour_bars(rows, hours=3, now=now, worst=True)
        self.assertEqual(len(bars), 3)
        self.assertIsNone(bars[0]["grade"])                # 更早的槽没测
        self.assertEqual(bars[1]["grade"], MS.BAR_YELLOW)  # 1 小时前:通过但慢
        self.assertEqual(bars[2]["grade"], MS.BAR_RED)     # 当前槽:失败 = 红
        self.assertEqual(bars[2]["slot"], slot)
        self.assertTrue(bars[2]["label"])

    def test_hour_bars_user_view_takes_best_across_channels(self):
        now = 1793300000
        slot = int(now // 3600) * 3600
        rows = [self._row(MS.FAIL, slot), self._row(MS.OK, slot)]
        self.assertEqual(MS.hour_bars(rows, 1, now=now, worst=False)[-1]["grade"],
                         MS.BAR_GREEN)
        self.assertEqual(MS.hour_bars(rows, 1, now=now, worst=True)[-1]["grade"],
                         MS.BAR_RED)

    def test_grade_of(self):
        # 绿:通过且首字 <= 10s
        self.assertEqual(MS.grade_of(MS.OK, ttft_ms=9000, latency_ms=9000),
                         MS.BAR_GREEN)
        # 黄:通过但首字超 10s;无首字按总耗时
        self.assertEqual(MS.grade_of(MS.OK, ttft_ms=10001), MS.BAR_YELLOW)
        self.assertEqual(MS.grade_of(MS.OK, ttft_ms=None, latency_ms=45000),
                         MS.BAR_YELLOW)
        # 红:失败与号池空对用户都是「不能用」
        self.assertEqual(MS.grade_of(MS.FAIL), MS.BAR_RED)
        self.assertEqual(MS.grade_of(MS.SKIP), MS.BAR_RED)

    def test_worst_per_day_channel_view(self):
        now = time.time()
        today = now
        yesterday = now - 86400
        rows = [self._row(MS.OK, today, ttft=9000),
                self._row(MS.FAIL, today - 3600),      # 同一天有失败 → 红
                self._row(MS.OK, yesterday, ttft=9000)]
        bars = MS.day_bars(rows, days=7, now=now, worst=True)
        self.assertEqual(len(bars), 7)
        self.assertEqual(bars[-1]["grade"], MS.BAR_RED)   # 今天:有失败就是红
        self.assertEqual(bars[-2]["grade"], MS.BAR_GREEN)
        self.assertIsNone(bars[0]["grade"])               # 更早的几天没测 = 灰
        self.assertEqual(bars[-1]["date"],
                         time.strftime("%m-%d", time.localtime(now)))

    def test_slow_day_is_yellow(self):
        now = time.time()
        # 渠道视角:当天全部通过但都慢 → 黄
        rows = [self._row(MS.OK, now, ttft=65000)]
        bars = MS.day_bars(rows, days=3, now=now, worst=True)
        self.assertEqual(bars[-1]["grade"], MS.BAR_YELLOW)
        # 用户视角同理(单渠道)
        bars = MS.day_bars(rows, days=3, now=now, worst=False)
        self.assertEqual(bars[-1]["grade"], MS.BAR_YELLOW)

    def test_best_per_day_user_view(self):
        now = time.time()
        rows = [self._row(MS.FAIL, now - 3600), self._row(MS.OK, now, ttft=900)]
        bars = MS.day_bars(rows, days=7, now=now, worst=False)
        self.assertEqual(bars[-1]["grade"], MS.BAR_GREEN)  # 当天任一渠道能用 = 绿

    def test_skip_is_red_for_users(self):
        """号池空对用户就是调用 503 = 不能用 = 红(不再是黄)。"""
        now = time.time()
        bars = MS.day_bars([self._row(MS.SKIP, now)], days=3, now=now, worst=True)
        self.assertEqual(bars[-1]["grade"], MS.BAR_RED)
        bars = MS.day_bars([self._row(MS.SKIP, now)], days=3, now=now, worst=False)
        self.assertEqual(bars[-1]["grade"], MS.BAR_RED)


class HeartbeatMapTest(unittest.TestCase):
    def _row(self, ok, checked_at, ttft=None, latency=0):
        return {"ok": ok, "checked_at": checked_at,
                "ttft_ms": ttft, "latency_ms": latency}

    def test_user_status_map_exposes_24_heartbeats_without_channels(self):
        now = time.time()
        slot = int(now // 3600) * 3600
        adapter = ChatAdapter()
        stored = {("chat-ok", "m1"): {"ok": MS.OK, "latency_ms": 900,
                                      "ttft_ms": 800, "checked_at": slot}}
        hist = {("chat-ok", "m1"): [self._row(MS.OK, slot, ttft=800)]}
        smap = MS.user_status_map({"chat-ok": adapter}, stored, hist, now=now)
        entry = smap["m1"]
        self.assertEqual(len(entry["heartbeats"]), 24)
        self.assertEqual(entry["heartbeats"][-1]["grade"], MS.BAR_GREEN)
        self.assertNotIn("channels", entry)


class AggregateForUserTest(unittest.TestCase):
    def _ad(self, name, models, kind="chat"):
        class A:
            pass
        a = A()
        a.name, a.models, a.kind = name, list(models), kind
        return a

    def test_multi_channel_takes_best(self):
        adapters = {"a": self._ad("a", ["m"]), "b": self._ad("b", ["m"])}
        stored = {("a", "m"): {"ok": 0, "latency_ms": 5000, "ttft_ms": None,
                               "checked_at": 111},
                  ("b", "m"): {"ok": 1, "latency_ms": 300, "ttft_ms": 200,
                               "checked_at": 222}}
        out = MS.aggregate_for_user(adapters, stored)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["model"], "m")
        self.assertEqual(out[0]["ok"], MS.OK)          # 主渠道挂了,备胎活着 = 能用
        self.assertEqual(out[0]["latency_ms"], 300)    # 取可用渠道的最好耗时
        self.assertEqual(out[0]["ttft_ms"], 200)
        self.assertEqual(out[0]["checked_at"], 222)

    def test_generation_excluded(self):
        adapters = {"a": self._ad("a", ["m"]), "g": self._ad("g", ["img"], "generation")}
        out = MS.aggregate_for_user(adapters, {})
        self.assertEqual([r["model"] for r in out], ["m"])

    def test_fail_first_ordering_and_bucket(self):
        adapters = {"a": self._ad("a", ["ok1", "bad", "empty", "new"])}
        stored = {("a", "ok1"): {"ok": 1, "latency_ms": 120, "ttft_ms": 60,
                                 "checked_at": 1},
                  ("a", "bad"): {"ok": 0, "latency_ms": 900, "ttft_ms": None,
                                 "checked_at": 2},
                  ("a", "empty"): {"ok": -1, "latency_ms": 0, "ttft_ms": None,
                                   "checked_at": 3}}
        out = MS.aggregate_for_user(adapters, stored)
        by_model = {r["model"]: r for r in out}
        self.assertEqual([r["model"] for r in out], ["bad", "empty", "new", "ok1"])
        self.assertEqual(by_model["bad"]["ok"], MS.FAIL)
        self.assertEqual(by_model["empty"]["ok"], MS.SKIP)
        self.assertIsNone(by_model["new"]["ok"])       # 从没查过 = 未检测
        self.assertEqual(by_model["ok1"]["ok"], MS.OK)

    def test_no_channel_or_error_leaks(self):
        adapters = {"secret-ch": self._ad("secret-ch", ["m"])}
        stored = {("secret-ch", "m"): {"ok": 0, "latency_ms": 10, "ttft_ms": None,
                                       "checked_at": 1,
                                       "error": "HTTP 403 upstream secret stuff"}}
        out = MS.aggregate_for_user(adapters, stored)
        dump = repr(out)
        self.assertNotIn("secret-ch", dump)
        self.assertNotIn("HTTP 403", dump)
        self.assertNotIn("error", dump)


class ModelStatusApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "ms-api.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@gmail.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.admin_h = {"Authorization": "Bearer " + r.json()["token"]}
        r2 = cls.client.post("/api/register", json={"email": "pleb@gmail.com",
                                                    "password": "secret123"})
        cls.user_h = {"Authorization": "Bearer " + r2.json()["token"]}

    def test_admin_and_user_monitoring_only_keep_gpt_terra(self):
        gpt = ChatAdapter()
        gpt.name, gpt.models = "gpt", ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-reserve"]
        from core import adapter as adapter_mod
        with mock.patch.object(adapter_mod, "enabled_adapters", return_value={"gpt": gpt}):
            for path, headers in (("/api/admin/model-status", self.admin_h), ("/api/model-status", self.user_h)):
                with self.subTest(path=path):
                    response = self.client.get(path, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual([row["model"] for row in response.json()["rows"]], ["gpt-6.1-terra"])
        self.assertEqual(gpt.models, ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-reserve"])

    def test_requires_admin(self):
        r = self.client.get("/api/admin/model-status", headers=self.user_h)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/admin/model-status/run", headers=self.user_h)
        self.assertEqual(r.status_code, 403)

    def test_shape_and_run_endpoint(self):
        fresh = portal_state.USER_DB
        fresh.add_model_status("gpt", "gpt-6.1-terra", MS.OK,
                               latency_ms=64000, ttft_ms=900)
        fresh.add_model_status("gpt", "gpt-6.1-terra", MS.FAIL,
                               latency_ms=5000, error="HTTP 410 gone",
                               checked_at=int(time.time()) - 3600)
        r = self.client.get("/api/admin/model-status", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["interval"], config.MODEL_STATUS_INTERVAL)
        self.assertFalse(body["running"])
        self.assertTrue(body["rows"])
        row = next(x for x in body["rows"]
                   if x["channel"] == "gpt" and x["model"] == "gpt-6.1-terra")
        self.assertEqual(row["ok"], MS.OK)             # 最新一行是成功
        self.assertEqual(row["ttft_ms"], 900)
        self.assertEqual(len(row["bars"]), 7)
        self.assertEqual(row["bars"][-1]["grade"], MS.BAR_RED)  # 渠道视角:当天有失败 = 红
        self.assertIsNone(row["bars"][0]["grade"])
        self.assertNotIn("probe", row)
        # run:立刻返回、不把整轮巡检挂在 HTTP 请求上(mock 掉真正的一轮)
        with mock.patch.object(MS, "run_checked", mock.AsyncMock(return_value=True)):
            r = self.client.post("/api/admin/model-status/run", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["started"])
        # 已经在跑时再点:started=False,不重复起轮
        with mock.patch.object(MS, "is_running", return_value=True):
            r = self.client.post("/api/admin/model-status/run", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["started"])

    def test_channel_delete_cleans_status_rows(self):
        fresh = portal_state.USER_DB
        fresh.add_model_status("ghost", "m", MS.OK)
        self.assertIsNotNone(fresh.get_model_status("ghost", "m"))
        fresh.create_channel("ghost", base_url="https://x.example/v1")
        fresh.delete_channel_config("ghost")
        self.assertIsNone(fresh.get_model_status("ghost", "m"))


class ModelStatusMigrationTest(unittest.TestCase):
    def test_old_latest_shape_table_is_rebuilt(self):
        """旧版 (channel,model) 主键 + probe 列的表,打开库时重建为历史表。"""
        path = os.path.join(_TMP, "ms-old.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE model_status (channel TEXT NOT NULL,"
                    "model TEXT NOT NULL, ok INTEGER NOT NULL, probe TEXT,"
                    "latency_ms INTEGER DEFAULT 0, error TEXT, checked_at INTEGER,"
                    "PRIMARY KEY(channel, model))")
        con.execute("INSERT INTO model_status VALUES('x','y',1,'chat',5,NULL,1)")
        con.commit()
        con.close()
        db = UserDB(path)
        cols = {r[1] for r in sqlite3.connect(path).execute(
            "PRAGMA table_info(model_status)")}
        self.assertIn("id", cols)
        self.assertNotIn("probe", cols)
        self.assertIn("ttft_ms", cols)
        db.add_model_status("x", "y", MS.OK, latency_ms=7, ttft_ms=3)
        row = db.get_model_status("x", "y")
        self.assertEqual(row["latency_ms"], 7)


class ModelStatusUserApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "ms-user.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@gmail.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.user_h = {"Authorization": "Bearer " + r.json()["token"]}

    def test_requires_login(self):
        r = self.client.get("/api/model-status")
        self.assertEqual(r.status_code, 401)

    def test_plaza_cards_carry_status(self):
        """/api/models(游客可看):对话卡带状态条与 TTFT。"""
        fresh = portal_state.USER_DB
        # 卡片来自 adapter.models,而 status 取自 user_status_map —— 后者只遍历
        # monitored_models(ad)。原来这条用例靠 nv(两者一致)成立;gpt 的
        # monitored_models 固定返回 gpt-6.1-terra,与 adapter.models 无交集,
        # 所以这里临时注册一个两者一致的通用渠道来验证「卡片带状态」这个机制。
        plaza = "plaza"
        ad = OpenAICompatAdapter(name=plaza, base_url="https://plaza.example.com/v1",
                                 models=["plaza-a", "plaza-b"])
        register_adapter(ad)
        self.addCleanup(adapter_mod._REGISTRY.pop, plaza, None)
        model = "plaza-a"
        # 本用例验证状态卡，不依赖其他模块留下的定价目录。
        for name in ad.models:
            fresh.upsert_pricing(name, billing_mode="free")
        portal_state.PRICING.invalidate()
        fresh.add_model_status(plaza, model, MS.OK,
                               latency_ms=64000, ttft_ms=900)
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200, r.text)
        data = {x["model"]: x for x in r.json()["data"]}
        st = data[model]["status"]
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["ttft_ms"], 900)
        self.assertEqual(len(st["bars"]), 7)
        self.assertNotIn("channel", repr(st))
        self.assertNotIn("error", repr(st))

    def test_user_view_is_sanitized(self):
        fresh = portal_state.USER_DB
        # 同一模型两条记录:先失败(带上游地址与凭据)后成功,最新一行必须是成功。
        fresh.add_model_status("gpt", "gpt-6.1-terra", MS.FAIL,
                               error="HTTP 410 from https://upstream.test/v1 "
                                     "from upstream: gone sk-abcdef123456",
                               checked_at=int(time.time()) - 3600)
        fresh.add_model_status("gpt", "gpt-6.1-terra", MS.OK,
                               latency_ms=64000, ttft_ms=900)
        r = self.client.get("/api/model-status", headers=self.user_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["interval"], config.MODEL_STATUS_INTERVAL)
        self.assertIn("summary", body)
        rows = {x["model"]: x for x in body["rows"]}
        self.assertEqual(rows["gpt-6.1-terra"]["ok"], 1)
        self.assertEqual(rows["gpt-6.1-terra"]["ttft_ms"], 900)
        self.assertNotIn("channel", rows["gpt-6.1-terra"])
        self.assertEqual(len(rows["gpt-6.1-terra"]["bars"]), 7)
        # 失败原因给状态页看,但上游地址与凭据必须抹掉 —— 库里那条失败记录
        # 经脱敏后才出网关(见 core/model_status.public_error)。
        hist = fresh.history_model_status(int(time.time()) - MS.RETENTION)
        errs = [r0.get("error") for r0 in hist if r0.get("error")]
        self.assertTrue(errs, hist)
        self.assertIn("HTTP 410", errs[0])
        clean = MS.public_error(errs[0])
        self.assertNotIn("https://", clean)
        self.assertNotIn("sk-", clean)
        # 排序:故障排最前,正常(1)必须排在全部 故障(0)/暂不可用(-1)/未检测(None) 之后。
        # 这里只有一个受巡检模型,断言写成不变量而不是「首项必须是故障」:
        # 有故障时故障必须全在正常之前,没有故障时这条自然成立。
        flags = [x["ok"] for x in body["rows"]]
        first_ok = next((i for i, v in enumerate(flags) if v == 1), len(flags))
        self.assertTrue(all(v in (0, -1, None) for v in flags[:first_ok]),
                        flags)
        self.assertTrue(all(v == 1 for v in flags[first_ok:]), flags)


class ModelStatusPageStaticTest(unittest.TestCase):
    """状态页前端的小守卫:组件里用到的局部辅助函数必须就地定义。

    hbSlots 曾在 Plaza 的 setup 里定义、被 ModelStatus 直接引用 —— 作用是
    组件内局部作用域,拿不到就 ReferenceError,整张表连「最近 24 小时」格子
    一起渲染不出来(线上无报错提示,只是空白)。"""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "portal-pages.js")
        with open(path, encoding="utf-8") as fh:
            self.src = fh.read()
        start = self.src.index("const ModelStatus = {")
        self.block = self.src[start:start + 6000]

    def test_hb_slots_defined_in_component(self):
        self.assertIn("hbSlots(", self.block)
        self.assertIn("const hbSlots", self.block)

    def test_heartbeat_column_renders_via_bars_cell(self):
        self.assertIn("最近 24 小时", self.block)
        self.assertIn("const barsCell", self.block)


class PublicErrorTest(unittest.TestCase):
    """给用户看的失败原因:保留可操作信息,抹掉凭证与上游地址。"""

    def test_keeps_actionable_status(self):
        self.assertEqual(MS.public_error("HTTP 429 Too Many Requests"),
                         "HTTP 429 Too Many Requests")

    def test_strips_credentials(self):
        out = MS.public_error("401 invalid api_key=sk-abcdef123456")
        self.assertNotIn("sk-abcdef123456", out)
        self.assertIn("401", out)

    def test_strips_bearer_and_url(self):
        out = MS.public_error(
            "upstream https://relay.internal.example.com/v1 failed, "
            "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        self.assertNotIn("https://", out)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", out)

    def test_strips_private_ip(self):
        out = MS.public_error("connect timeout to 10.0.0.12:8080")
        self.assertNotIn("10.0.0.12", out)

    def test_truncates_and_handles_empty(self):
        self.assertIsNone(MS.public_error(None))
        self.assertIsNone(MS.public_error(""))
        self.assertLessEqual(len(MS.public_error("x" * 500)), 91)


class UserStatusMapErrorTest(unittest.TestCase):
    """error 只跟随 with_channels(登录状态页),游客广场不泄露。"""

    class _Ad:
        def __init__(self, name, models, kind="chat"):
            self.name, self.models, self.kind = name, list(models), kind

    def _call(self, with_channels):
        now = int(time.time())
        adapters = {"ch": self._Ad("ch", ["m"])}
        stored = {("ch", "m"): {"ok": MS.FAIL, "latency_ms": 0, "ttft_ms": None,
                                "error": "HTTP 500 https://up.example/v1 Bearer t0k3n123456",
                                "checked_at": now}}
        hist = {("ch", "m"): [dict(stored[("ch", "m")])]}
        return MS.user_status_map(adapters, stored, hist, now=now,
                                  with_channels=with_channels)["m"]

    def test_error_present_and_scrubbed_for_logged_in(self):
        entry = self._call(True)
        self.assertIn("channels", entry)
        self.assertIn("HTTP 500", entry["error"])
        self.assertNotIn("https://", entry["error"])
        self.assertNotIn("t0k3n123456", entry["error"])

    def test_error_absent_for_public_square(self):
        entry = self._call(False)
        self.assertNotIn("error", entry)
        self.assertNotIn("channels", entry)


if __name__ == "__main__":
    unittest.main()
