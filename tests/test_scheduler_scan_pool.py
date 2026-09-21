import unittest
from unittest import mock

from core import db as dbmod
from core.adapter import CAP_BALANCE
from core.scheduler import _check_one, scan_once


class _Adapter:
    proxy = True

    def sync_pool(self, _db):
        return {"total": 3, "changed": 0, "removed": 0}

    def scan_pool(self, _db):
        return {"active": 2, "dead": 1, "exhausted": 0, "transient": 0}


class SchedulerScanPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_pool_runs_batch_health_hook(self):
        logs = []
        with mock.patch("core.scheduler.all_adapters",
                        return_value={"grok": _Adapter()}):
            result = await scan_once(object(), 5, 600, 0.05,
                                     log=logs.append, channel="grok")

        self.assertEqual(result, {
            "active": 2, "dead": 1, "exhausted": 0, "transient": 0,
        })
        self.assertTrue(any("号池测活" in line for line in logs))

    def test_exhausted_balance_is_permanent_dead_for_channel_declaring_it(self):
        class BalanceAdapter:
            capabilities = [CAP_BALANCE]
            exhausted_is_dead = True

            def has(self, capability):
                return capability in self.capabilities

            def balance(self, _acct):
                return 0

        class DB:
            def __init__(self):
                self.status = None

            def update_account(self, *_args, **kwargs):
                self.status = kwargs.get("status")

            def set_status(self, _account_id, status):
                self.status = status

        db = DB()
        result = _check_one(db, BalanceAdapter(), {"id": 1}, 600, 0.05)
        self.assertEqual(result, dbmod.ST_DEAD)
        self.assertEqual(db.status, dbmod.ST_DEAD)

    def test_adapter_specific_balance_floor_overrides_global_floor(self):
        class BalanceAdapter:
            capabilities = [CAP_BALANCE]
            exhausted_is_dead = False
            min_balance = 2

            def has(self, capability):
                return capability in self.capabilities

            def balance(self, _acct):
                return 1

        class DB:
            def __init__(self):
                self.status = None

            def update_account(self, *_args, **kwargs):
                self.status = kwargs.get("status")

            def set_status(self, _account_id, status):
                self.status = status

        db = DB()
        result = _check_one(db, BalanceAdapter(), {"id": 1}, 600, 0.05)
        self.assertEqual(result, dbmod.ST_EXHAUSTED)
        self.assertEqual(db.status, dbmod.ST_EXHAUSTED)


class _FakeDB:
    def __init__(self):
        self.status = None

    def update_account(self, *_args, **kwargs):
        self.status = kwargs.get("status")

    def set_status(self, _account_id, status):
        self.status = status


class _Upstream(Exception):
    """带 HTTP 状态码的上游错误(classify_failure 按 code 判 terminal)。"""

    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg)


def _raising_adapter(exc):
    class Raising:
        capabilities = [CAP_BALANCE]
        exhausted_is_dead = False

        def has(self, capability):
            return capability in self.capabilities

        def balance(self, _acct):
            raise exc

    return Raising()


class SchedulerFailureVerdictTest(unittest.TestCase):
    """巡检里的失败判定 —— 2026-09-05 一轮扫描写死 1250 个好号那次的门禁。

    当时 fd 耗尽,进程连出站 socket 都开不出来(EMFILE),_check_one 的
    except Exception → dead 把一整轮扫到的上千个号全部
    写成死号,事后抽样验证它们都还能登录。dead 不再被巡检、不再被取号、没有恢复
    接口 —— 所以「判不准」必须落 cooldown,不能落 dead。
    """

    def _verdict(self, exc):
        db = _FakeDB()
        result = _check_one(db, _raising_adapter(exc), {"id": 1}, 600, 0.05)
        return result, db.status

    def test_fd_exhaustion_does_not_kill_account(self):
        """就是事故那个信号:Errno 24 Too many open files。"""
        _, status = self._verdict(OSError(24, "Too many open files"))
        self.assertEqual(status, dbmod.ST_COOLDOWN)

    def test_unknown_error_does_not_kill_account(self):
        _, status = self._verdict(Exception("connection reset by peer"))
        self.assertEqual(status, dbmod.ST_COOLDOWN)

    def test_upstream_5xx_does_not_kill_account(self):
        _, status = self._verdict(_Upstream(502, "Bad Gateway"))
        self.assertEqual(status, dbmod.ST_COOLDOWN)

    def test_credential_failure_still_retires_account(self):
        """豁免不包括凭据失效:401 说的就是这个号本身不行。"""
        _, status = self._verdict(_Upstream(401, "unauthorized"))
        self.assertEqual(status, dbmod.ST_DEAD)

    def test_unknown_balance_cools_down_instead_of_dying(self):
        """余额问不出来(快照缺失/上游不给数)不等于号废了。"""
        class NoBalance:
            capabilities = [CAP_BALANCE]
            exhausted_is_dead = True

            def has(self, capability):
                return capability in self.capabilities

            def balance(self, _acct):
                return None

        db = _FakeDB()
        result = _check_one(db, NoBalance(), {"id": 1}, 600, 0.05)
        self.assertEqual(result, dbmod.ST_COOLDOWN)
        self.assertEqual(db.status, dbmod.ST_COOLDOWN)


if __name__ == "__main__":
    unittest.main()
