#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清号语义 —— 哪些状态算「失效」,以及全局清理怎么决定 exhausted 去留。

这块踩过:把「额度耗尽」当永久失效写死成某个渠道名,后来新增一个同样声明的渠道,
全局清理就一直漏它。所以判据必须是渠道自己声明的 exhausted_is_dead,不是名字。

「清不准」的代价不对称:错删一个还能用的号不可逆(凭据一起没了),
错留一个死号只是占一行。
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "purge.db"))

import config  # noqa: E402
import server  # noqa: E402
from core import adapter as adapter_mod  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.adapter import Adapter  # noqa: E402


class _Stub(Adapter):
    """最小渠道替身:只声明 exhausted_is_dead 与 delete_account 这两个被用到的点。"""

    def __init__(self, name, exhausted_is_dead=False):
        self.name = name
        self.exhausted_is_dead = exhausted_is_dead
        self.deleted = []

    def delete_account(self, db, identity):
        """契约:有 delete_account 的渠道,收尾责任在它自己(删库里的行 + 外部文件)。"""
        self.deleted.append(identity)
        db.delete_account(self.name, identity)


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _db(self, name):
        db = dbmod.DB(os.path.join(self.tmp.name, name))
        self.addCleanup(db.close)
        return db

    def test_default_purge_keeps_exhausted_and_cooldown_accounts(self):
        """DB 层的默认清理只清 dead —— 耗尽与冷却都还会回来。"""
        db = self._db("default.db")
        db.upsert_account("grok", "dead@example.com", status=dbmod.ST_DEAD)
        db.upsert_account("grok", "quota@example.com", status=dbmod.ST_EXHAUSTED)
        db.upsert_account("grok", "cool@example.com", status=dbmod.ST_COOLDOWN)
        self.assertEqual(db.purge(channel="grok"), 1)
        self.assertIsNone(db.get_by_identity("grok", "dead@example.com"))
        self.assertIsNotNone(db.get_by_identity("grok", "quota@example.com"))
        self.assertIsNotNone(db.get_by_identity("grok", "cool@example.com"))

    def test_named_channel_purge_clears_dead_and_exhausted_keeps_cooldown(self):
        """端点带 channel 时(面板点「清除」)按失效清:dead + exhausted 一起走,冷却留。

        与上一条刻意不同:DB.purge 只清 dead,端点范围更大。dashboard.html 的确认框
        原文也是这么写的(「权限拒绝和额度耗尽账号也会删除，冷却账号不会删除」)。
        """
        db = self._db("named.db")
        db.upsert_account("grok", "dead@example.com", status=dbmod.ST_DEAD)
        db.upsert_account("grok", "quota@example.com", status=dbmod.ST_EXHAUSTED)
        db.upsert_account("grok", "cool@example.com", status=dbmod.ST_COOLDOWN)
        stub = _Stub("grok")
        with (mock.patch.object(server, "DB", db),
              mock.patch.object(server, "get_adapter", return_value=stub)):
            result = asyncio.run(server.admin_purge(
                channel="grok", authorization="Bearer " + config.ADMIN_KEY))
        self.assertEqual(result["deleted"], 2)
        self.assertIsNone(db.get_by_identity("grok", "dead@example.com"))
        self.assertIsNone(db.get_by_identity("grok", "quota@example.com"))
        self.assertIsNotNone(db.get_by_identity("grok", "cool@example.com"))
        # 外部状态(号文件等)由渠道自己收尾,不能只删库里的行
        self.assertEqual(sorted(stub.deleted),
                         ["dead@example.com", "quota@example.com"])

    def test_global_purge_clears_exhausted_only_for_channels_declaring_it_dead(self):
        """不带 channel 的全局清理:exhausted 只清「自己声明 exhausted_is_dead」的渠道。

        判据必须来自渠道声明,不能写死渠道名 —— 写死的实现过不了这一条。
        """
        db = self._db("global.db")
        db.upsert_account("grok", "dead@example.com", status=dbmod.ST_DEAD)
        db.upsert_account("grok", "quota@example.com", status=dbmod.ST_EXHAUSTED)
        db.upsert_account("metered", "quota@example.com", status=dbmod.ST_EXHAUSTED)
        db.upsert_account("metered", "cool@example.com", status=dbmod.ST_COOLDOWN)
        # 只给 metered 声明「耗尽即永久失效」;grok 不声明
        adapter_mod.register_adapter(_Stub("metered", exhausted_is_dead=True))
        self.addCleanup(adapter_mod.unregister_adapter, "metered")
        with (mock.patch.object(server, "DB", db),
              mock.patch.object(server, "get_adapter", return_value=None)):
            result = asyncio.run(server.admin_purge(
                authorization="Bearer " + config.ADMIN_KEY))
        self.assertEqual(result["deleted"], 2)
        self.assertIsNone(db.get_by_identity("grok", "dead@example.com"))
        # grok 没声明 —— 额度耗尽会恢复,不能删
        self.assertIsNotNone(db.get_by_identity("grok", "quota@example.com"))
        # metered 声明了 —— 耗尽即永久失效,该清
        self.assertIsNone(db.get_by_identity("metered", "quota@example.com"))
        self.assertIsNotNone(db.get_by_identity("metered", "cool@example.com"))


if __name__ == "__main__":
    unittest.main()
