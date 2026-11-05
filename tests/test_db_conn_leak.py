"""短命线程用完的 DB 连接必须被关掉 —— 2026-09-05 站点打不开那次的门禁。

那次的形状:server.py 的流式路径每个请求起一个裸 threading.Thread,线程跑完结算
要写库,于是每条流留下一个没人关的 sqlite 连接(库文件 + -wal 两个 fd)。攒到
1024 就顶满 fd 上限,accept() 全线报 Errno 24,端口还在 LISTEN 但谁也连不进来。
现场:976 个 fd 指向同一个库,进程只剩 8 个活线程。

所以这里模拟的就是那条路:多批短命线程各碰一次库,退出;验证死线程的连接确实
关闭,句柄不随累计请求增长,全部连接关闭后句柄归零。

Linux SQLite 会把已关闭连接的 fd 放进 inode 的 pUnused 缓存,只要还有 WAL
连接持锁就延后关闭、供后续连接复用。不能把并发峰值留下的 fd 当成活连接:
https://sqlite.org/src/doc/trunk/src/os_unix.c (closePendingFds / findReusableFd)。
先同步建立一批并发连接,再比较同等并发下多轮的真实 fd;其它平台数连接注册表。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

from core import db as dbmod
from core import user_db as udbmod


def _open_handles(store, path):
    """Linux:数指向 path 的真 fd。其它平台:退回注册表里还开着的连接数。"""
    if sys.platform.startswith("linux"):
        n = 0
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(os.path.join("/proc/self/fd", fd))
            except OSError:
                continue        # fd 在我们读它的这一瞬间关掉了,不算
            if target.startswith(path):
                n += 1
        return n
    return store._conns.live()


def _hammer(store, work, threads=40, synchronize=False):
    """起 threads 个短命线程,各自碰一次库然后退出(线上流式请求那条路的形状)。"""
    errs = []
    connections = []
    barrier = threading.Barrier(threads) if synchronize else None

    def run():
        try:
            connections.append(store._conn())
            if barrier is not None:
                barrier.wait(10)
            work(store)
        except Exception as e:      # 线程里的异常不会冒到主线程,得自己收集
            errs.append(e)

    ths = [threading.Thread(target=run) for _ in range(threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    assert not any(t.is_alive() for t in ths), "工作线程未退出"
    assert not errs, f"工作线程报错: {errs[:3]}"
    return connections


class ThreadConnLeakTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bitapi-conn-test-")
        self.dir = temp.name
        self.addCleanup(temp.cleanup)

    @staticmethod
    def _close_connections(store):
        # 只关闭本用例拥有的库;此时工作线程均已退出。
        store._conns.close_current()
        with store._conns._lock:
            connections = list(store._conns._live.values())
            store._conns._live.clear()
        for connection in connections:
            connection.close()

    def _assert_closed(self, connections):
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def _assert_bounded(self, store, work):
        path = store.path
        self.addCleanup(self._close_connections, store)
        connections = _hammer(store, work, synchronize=True)
        # 清扫挂在「新建连接」上,再要一次连接把已结束线程的连接关掉。
        _hammer(store, work, threads=1)
        self._assert_closed(connections)
        base = _open_handles(store, path)
        for batch in range(3):
            connections = _hammer(store, work, synchronize=True)
            _hammer(store, work, threads=1)
            self._assert_closed(connections)
            self.assertLessEqual(store._conns.live(), 2)  # 主线程 + 最后一条清扫线程
            after = _open_handles(store, path)
            self.assertLessEqual(
                after - base, 6,
                f"第 {batch + 1} 批请求后句柄累积(基线 {base},现在 {after})")
        self._close_connections(store)
        self.assertEqual(_open_handles(store, path), 0, "全部连接关闭后仍有库句柄")

    def test_pool_db_closes_dead_thread_conns(self):
        db = dbmod.DB(os.path.join(self.dir, "pool.db"))
        self._assert_bounded(db, lambda s: s.upsert_account(
            "chan", "id-" + str(threading.get_ident()), secret={"k": "v"}))

    def test_user_db_closes_dead_thread_conns(self):
        udb = udbmod.UserDB(os.path.join(self.dir, "user.db"))
        self._assert_bounded(udb, lambda s: s.list_groups())

    def test_same_thread_reuses_one_conn(self):
        """同一个线程反复要连接只能拿到同一条 —— 清扫不该退化成每次重连。"""
        db = dbmod.DB(os.path.join(self.dir, "reuse.db"))
        self.addCleanup(self._close_connections, db)
        first = db._conn()
        for _ in range(20):
            self.assertIs(db._conn(), first)


if __name__ == "__main__":
    unittest.main()
