#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库备份的门禁。

守的不是「函数会不会跑」,是三件会在恢复那天才暴露的事:
  1. 备份必须是完整一致的库(WAL 里未 checkpoint 的写入也在),不是半份文件
  2. 到期判断按目录里最新一份的年龄,重启不归零、停机后第一跳补上
  3. 清理只动自己命名的文件,且按份数保留 —— 份数是确定的磁盘上界
"""
import os
import sqlite3
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "bk.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

import config  # noqa: E402
from core import backup as B  # noqa: E402
from core.user_db import UserDB  # noqa: E402


class BackupOnceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "live.db")
        self.dest = os.path.join(self.dir, "backups")
        self.db = UserDB(self.db_path)

    def tearDown(self):
        self.db._conns.close_current()

    def _rows(self, path):
        c = sqlite3.connect(path)
        try:
            return c.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
        finally:
            c.close()

    def test_backup_contains_uncheckpointed_wal_writes(self):
        """WAL 模式下刚写的行还在 -wal 文件里,直接拷主文件会丢它。
        backup API 走页级复制,必须能把这行带上。"""
        self.db.set_setting("k1", "v1")
        self.db.set_setting("k2", {"nested": True})
        self.assertTrue(os.path.exists(self.db_path + "-wal"))
        done = B.backup_once(self.db_path, self.dest, keep=7)
        self.assertTrue(os.path.exists(done["path"]))
        self.assertEqual(done["size"], os.path.getsize(done["path"]))
        rows = self._rows(done["path"])
        self.assertEqual([r[0] for r in rows], ["k1", "k2"])
        # 备份是独立文件:之后改原库不影响它
        self.db.set_setting("k1", "changed")
        self.assertEqual(self._rows(done["path"])[0][1], '"v1"')

    def test_no_tmp_left_behind_and_name_has_prefix(self):
        done = B.backup_once(self.db_path, self.dest, keep=7)
        names = os.listdir(self.dest)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith(B.PREFIX) and names[0].endswith(B.SUFFIX))
        self.assertFalse(any(n.endswith(".tmp") for n in names))
        self.assertEqual(os.path.basename(done["path"]), names[0])

    def test_same_second_twice_does_not_overwrite(self):
        """手动连点两下落在同一秒:第二份加序号,不能覆盖第一份。"""
        t = time.time()
        a = B.backup_once(self.db_path, self.dest, keep=7, now=t)
        b = B.backup_once(self.db_path, self.dest, keep=7, now=t)
        self.assertNotEqual(a["path"], b["path"])
        self.assertEqual(len(B.list_backups(self.dest)), 2)

    def test_keep_prunes_oldest_and_only_own_files(self):
        os.makedirs(self.dest)
        stranger = os.path.join(self.dest, "notes.txt")
        with open(stranger, "w") as f:
            f.write("keep me")
        base = time.time() - 10 * 86400
        for k in range(5):
            r = B.backup_once(self.db_path, self.dest, keep=0, now=base + k * 86400)
            # 用 mtime 排新旧,得把文件时间戳也拨到对应那天
            os.utime(r["path"], (base + k * 86400, base + k * 86400))
        self.assertEqual(len(B.list_backups(self.dest)), 5)
        r = B.backup_once(self.db_path, self.dest, keep=3)
        self.assertEqual(r["pruned"], 3)
        left = B.list_backups(self.dest)
        self.assertEqual(len(left), 3)
        self.assertEqual(left[0]["path"], r["path"])   # 最新的在最前
        self.assertTrue(os.path.exists(stranger))       # 别人的文件不碰

    def test_status_reports_latest_and_next(self):
        st = B.status(self.dest, 3600, 7)
        self.assertEqual(st["count"], 0)
        self.assertIsNone(st["latest"])
        self.assertIsNone(st["next_at"])
        done = B.backup_once(self.db_path, self.dest, keep=7)
        st = B.status(self.dest, 3600, 7)
        self.assertEqual(st["count"], 1)
        self.assertEqual(st["latest"]["name"], os.path.basename(done["path"]))
        self.assertEqual(st["next_at"], st["latest"]["mtime"] + 3600)


class DueTest(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def test_interval_zero_is_off(self):
        self.assertFalse(B.due(self.dest, 0))

    def test_empty_dir_is_due(self):
        self.assertTrue(B.due(self.dest, 86400))

    def test_fresh_backup_not_due_until_interval_passes(self):
        p = os.path.join(self.dest, B.PREFIX + "x" + B.SUFFIX)
        with open(p, "wb"):
            pass
        now = time.time()
        os.utime(p, (now, now))
        self.assertFalse(B.due(self.dest, 3600, now=now + 3599))
        # 「重启不归零」:判断只看文件年龄,进程什么时候起的无关
        self.assertTrue(B.due(self.dest, 3600, now=now + 3600))


class RunIfDueTest(unittest.TestCase):
    """接 config 的那层:目录解析与到期跳过。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "cfg.db")
        UserDB(self.db_path)._conns.close_current()
        self._saved = (config.DB_PATH, config.BACKUP_DIR,
                       config.BACKUP_INTERVAL, config.BACKUP_KEEP)
        config.DB_PATH = self.db_path
        config.BACKUP_DIR = ""
        config.BACKUP_INTERVAL = 3600
        config.BACKUP_KEEP = 2

    def tearDown(self):
        (config.DB_PATH, config.BACKUP_DIR,
         config.BACKUP_INTERVAL, config.BACKUP_KEEP) = self._saved

    def test_default_dir_sits_beside_db(self):
        self.assertEqual(B.resolve_dir(), os.path.join(self.dir, "backups"))
        config.BACKUP_DIR = os.path.join(self.dir, "elsewhere")
        self.assertEqual(B.resolve_dir(), config.BACKUP_DIR)

    def test_first_run_backs_up_then_skips_until_due(self):
        first = B.run_if_due()
        self.assertIsNotNone(first)
        self.assertTrue(first["path"].startswith(os.path.join(self.dir, "backups")))
        self.assertIsNone(B.run_if_due())                       # 刚备过
        self.assertIsNotNone(B.run_if_due(now=time.time() + 3601))  # 到期
        self.assertEqual(B.current_status()["count"], 2)


if __name__ == "__main__":
    unittest.main()
