#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池失败分类的门禁 —— 守 docs/incidents/2026-08-30-上游异常把账号判死.md。

这条事故的形状是:一次上游超时就让账号永久离开号池(dead 不再被取号,还是 purge
的默认清理目标)。所以断言的重点不是「分类函数返回什么字符串」,而是
「transient 必须压过调用方传进来的 dead=True」—— 事故里正是那个 dead 参数
把临时故障写成了终态。
"""
import itertools
import unittest
from unittest import mock

from core import db as dbmod
from core.adapter import CAP_HEALTH, CAP_REFRESH
from core.pool import Pool, classify_failure


class _StatusDB:
    """只记 set_status / update_account 的假库:门禁关心状态落点,不关心 SQLite。"""

    def __init__(self):
        self.status = None
        self.updates = []

    def set_status(self, _acct_id, status):
        self.status = status

    def update_account(self, _acct_id, **fields):
        self.updates.append(fields)


class ClassifyFailureTest(unittest.TestCase):
    def test_unknown_and_network_errors_are_transient(self):
        for exc in (TimeoutError("Read timed out"),
                    ConnectionResetError("upstream connection reset"),
                    Exception("502 Bad Gateway"),
                    Exception("说不清的报错")):
            with self.subTest(exc=exc):
                self.assertEqual(classify_failure(exc), "transient")

    def test_rate_limit_is_its_own_class(self):
        """429 说的是上游此刻满了,跟这把 key 好不好无关 —— 不能和 transient 混在
        一起,因为 transient 会冷却账号,而共享并发额度的上游满载时所有 key 一起
        429,冷却等于一次把整个渠道打黑。"""
        by_code = Exception("Too Many Requests")
        by_code.code = 429
        for exc in (by_code,
                    Exception("rate limited, try again"),
                    Exception("HTTP Error 429: Too Many Requests"),
                    Exception("model_concurrency_rate_limit_exceeded")):
            with self.subTest(exc=exc):
                self.assertEqual(classify_failure(exc), "ratelimit")

    def test_terminal_markers_win_over_429(self):
        """有的上游用 429 报「额度用尽」,那种号该退场,不能当限流一直重试。"""
        exc = Exception("insufficient_quota")
        exc.code = 429
        self.assertEqual(classify_failure(exc), "terminal")

    def test_credential_and_quota_errors_are_terminal(self):
        for exc in (Exception("invalid_grant"),
                    Exception("Account blocked"),
                    Exception("permission-denied"),
                    Exception("insufficient_quota")):
            with self.subTest(exc=exc):
                self.assertEqual(classify_failure(exc), "terminal")

    def test_http_status_401_402_403_are_terminal(self):
        for code in (401, 402, 403):
            exc = Exception("nope")
            exc.code = code
            with self.subTest(code=code):
                self.assertEqual(classify_failure(exc), "terminal")

    def test_http_status_500_is_not_terminal(self):
        exc = Exception("nope")
        exc.code = 500
        self.assertEqual(classify_failure(exc), "transient")


class MarkFailureTest(unittest.TestCase):
    def test_transient_lands_cooldown_even_when_caller_says_dead(self):
        """事故的核心断言:dead=True 也不许把临时故障写成 dead。

        dead=True 是真实调用形状(server.py 传的是
        getattr(adapter, "exhausted_is_dead", False),grok 之外的渠道也可能为 True)。
        """
        db = _StatusDB()
        landed = Pool(db).mark_failure(1, TimeoutError("Read timed out"), dead=True)
        self.assertEqual(landed, dbmod.ST_COOLDOWN)
        self.assertEqual(db.status, dbmod.ST_COOLDOWN)

    def test_terminal_with_dead_flag_lands_dead(self):
        db = _StatusDB()
        landed = Pool(db).mark_failure(1, Exception("invalid_grant"), dead=True)
        self.assertEqual(landed, dbmod.ST_DEAD)
        self.assertEqual(db.status, dbmod.ST_DEAD)

    def test_terminal_without_dead_flag_lands_exhausted(self):
        db = _StatusDB()
        landed = Pool(db).mark_failure(1, Exception("insufficient_quota"))
        self.assertEqual(landed, dbmod.ST_EXHAUSTED)
        self.assertEqual(db.status, dbmod.ST_EXHAUSTED)

    def test_ratelimit_does_not_touch_status(self):
        """限流不罚号:pick_active 只挑 active,冷却一下就要等巡检(默认 30 分钟)
        才回场。共享并发额度的上游满载时所有 key 一起 429,罚号会把整个渠道一次
        打黑,而号本身一个都没坏。"""
        db = _StatusDB()
        exc = Exception("Too Many Requests")
        exc.code = 429
        landed = Pool(db).mark_failure(1, exc, dead=True)
        self.assertEqual(landed, "ratelimit")
        self.assertIsNone(db.status, "限流不该改状态")

    def test_ratelimit_advances_last_check_so_next_pick_rotates(self):
        """pick_active 是 last_check ASC 排的。不推进这个时间戳,下一次取号会
        原地挑回同一把 key,六次重试全打在同一个号上,轮询就白设了。"""
        db = _StatusDB()
        exc = Exception("rate limit exceeded")
        Pool(db).mark_failure(1, exc)
        self.assertEqual(len(db.updates), 1)
        self.assertIn("last_check", db.updates[0])
        self.assertGreater(db.updates[0]["last_check"], 0)


class _StatelessAdapter:
    stateless_keys = True


class StatelessKeysTest(unittest.TestCase):
    """透传型 key 池(AMD GPU Cloud):key 没有本地状态,上游抖动不许罚号。

    这里断言的是「状态没被改」而不是「返回了某个字符串」—— 罚号的代价是账号离场:
    cooldown 要等下一轮巡检(默认 30 分钟)才回来,exhausted 根本不在巡检范围里
    (core/scheduler.py 只扫 active/unchecked/cooldown),等于这把好 key 永久没了。
    """

    def test_transient_only_rotates(self):
        """实测上游会对同一把好 key 回 502 / 400「Unsupported model」,一分钟后自愈。
        那种抖动落 cooldown,一次就能把整个渠道 park 半小时。"""
        db = _StatusDB()
        landed = Pool(db).mark_failure(1, TimeoutError("Read timed out"),
                                       adapter=_StatelessAdapter())
        self.assertEqual(landed, "rotate")
        self.assertIsNone(db.status, "透传型 key 池的临时故障不该改状态")
        self.assertIn("last_check", db.updates[0])

    def test_transient_ignores_caller_dead_flag(self):
        db = _StatusDB()
        landed = Pool(db).mark_failure(1, Exception("502 Bad Gateway"), dead=True,
                                       adapter=_StatelessAdapter())
        self.assertEqual(landed, "rotate")
        self.assertIsNone(db.status)

    def test_empty_reply_only_rotates(self):
        """空回复走的是 mark_exhausted。透传型 key 没有额度概念,上游回了个空
        不代表这把 key 用尽了 —— 而 exhausted 比 cooldown 更狠,巡检不会碰它。"""
        db = _StatusDB()
        landed = Pool(db).mark_exhausted(1, adapter=_StatelessAdapter())
        self.assertEqual(landed, "rotate")
        self.assertIsNone(db.status)
        self.assertIn("last_check", db.updates[0])

    def test_terminal_still_retires_the_key(self):
        """豁免只给「说不清的上游故障」。401/402/403 说的就是这把 key 本身不行,
        再留在池里每次取到它都白跑一趟。"""
        for code, dead, want in ((401, False, dbmod.ST_EXHAUSTED),
                                 (403, True, dbmod.ST_DEAD)):
            with self.subTest(code=code):
                db = _StatusDB()
                exc = Exception("nope")
                exc.code = code
                landed = Pool(db).mark_failure(1, exc, dead=dead,
                                               adapter=_StatelessAdapter())
                self.assertEqual(landed, want)
                self.assertEqual(db.status, want)

    def test_default_channels_keep_cooldown(self):
        """豁免必须是渠道显式声明的:没声明的渠道(adapter 缺省/未传)行为不变。"""
        for adapter in (None, object()):
            with self.subTest(adapter=adapter):
                db = _StatusDB()
                landed = Pool(db).mark_failure(1, TimeoutError("x"), adapter=adapter)
                self.assertEqual(landed, dbmod.ST_COOLDOWN)
                self.assertEqual(db.status, dbmod.ST_COOLDOWN)


class _RotatingDB:
    """模拟 accounts 表,关键是 pick_active 的 last_check ASC 排序。"""

    def __init__(self, names):
        self.rows = [{"id": i + 1, "identity": n, "last_check": 0}
                     for i, n in enumerate(names)]

    def pick_active(self, _channel):
        if not self.rows:
            return None
        return min(self.rows, key=lambda r: r["last_check"])

    def update_account(self, acct_id, **fields):
        for r in self.rows:
            if r["id"] == acct_id:
                r.update(fields)

    def set_status(self, _acct_id, _status):
        pass


class _BareAdapter:
    """没有任何 capability:不刷 token,也没有余额/存活检查。"""

    def has(self, _cap):
        return False


class _RefreshOnlyAdapter(_BareAdapter):
    """只声明 CAP_REFRESH 的渠道形状。刷 token 只写 token/token_exp,
    不碰 last_check —— 取号路径里没有任何东西会推进它,轮换就是这么断的。"""

    def has(self, cap):
        return cap == CAP_REFRESH

    def refresh_token(self, _acct):
        return {"token": "tok", "token_exp": 9_999_999_999}


class _HealthOnlyAdapter:
    """声明 CAP_HEALTH 但不声明 CAP_BALANCE 的渠道形状。"""

    def has(self, cap):
        return cap == CAP_HEALTH


class PickRotationTest(unittest.TestCase):
    """取号必须轮换。

    没有余额/存活检查的渠道在取号路径里不会推进 last_check,而 pick_active 是
    last_check ASC 排序 —— 不推就等于每次都返回同一个号,请求全堆在一个号上。
    高频调用被上游封号是不可逆的,所以这条是门禁。
    """

    def test_refresh_only_channel_rotates_across_accounts(self):
        db = _RotatingDB(["a", "b", "c"])
        pool = Pool(db)
        # 固化时间:last_check 是秒级精度,同一秒内的推进会被 min() 判成平局,
        # 测试就测不出轮换了。
        with mock.patch("time.time", side_effect=itertools.count(1000, 1)):
            seen = [pool.get_valid_account("ch", _RefreshOnlyAdapter())["identity"]
                    for _ in range(6)]
        self.assertEqual(seen, ["a", "b", "c", "a", "b", "c"])

    def test_bare_channel_rotates_too(self):
        db = _RotatingDB(["a", "b"])
        pool = Pool(db)
        with mock.patch("time.time", side_effect=itertools.count(1000, 1)):
            seen = [pool.get_valid_account("ch", _BareAdapter())["identity"]
                    for _ in range(4)]
        self.assertEqual(seen, ["a", "b", "a", "b"])

    def test_health_channel_is_left_to_its_own_path(self):
        """有存活检查的渠道由自己那条路径负责推进,这里不重复插手。"""
        db = _RotatingDB(["a", "b"])
        pool = Pool(db)
        with mock.patch("time.time", side_effect=itertools.count(1000, 1)):
            pool.get_valid_account("ch", _HealthOnlyAdapter())
        self.assertEqual([r["last_check"] for r in db.rows], [0, 0])


if __name__ == "__main__":
    unittest.main()
