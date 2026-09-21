"""短命线程用完的 DB 连接必须被关掉 —— 2026-09-05 站点打不开那次的门禁。

那次的形状:server.py 的流式路径每个请求起一个裸 threading.Thread,线程跑完结算
要写库,于是每条流留下一个没人关的 sqlite 连接(库文件 + -wal 两个 fd)。攒到
1024 就顶满 fd 上限,accept() 全线报 Errno 24,端口还在 LISTEN 但谁也连不进来。
现场:976 个 fd 指向同一个库,进程只剩 8 个活线程。

所以这里模拟的就是那条路:一批短命线程各写一次库,退出;然后断言连接数不随线程
数增长。Linux 上直接数真的 fd(事故本身的量纲),别的平台退回连接注册表长度 ——
这个仓在 Windows 上开发、在 Linux 上跑 CI,门禁得两边都能跑。
"""
import os
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


def _hammer(store, work, threads=40):
    """起 threads 个短命线程,各自碰一次库然后退出(线上流式请求那条路的形状)。"""
    errs = []

    def run():
        try:
            work(store)
        except Exception as e:      # 线程里的异常不会冒到主线程,得自己收集
            errs.append(e)

    ths = [threading.Thread(target=run) for _ in range(threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    assert not errs, f"工作线程报错: {errs[:3]}"


class ThreadConnLeakTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _assert_bounded(self, store, work):
        path = store.path
        base = _open_handles(store, path)
        _hammer(store, work)
        # 清扫挂在「新建连接」上,所以再要一次连接把已结束线程的那批关掉
        _hammer(store, work, threads=1)
        after = _open_handles(store, path)
        self.assertLessEqual(
            after - base, 6,
            f"40 个短命线程之后还多开着 {after - base} 个句柄(基线 {base},"
            f"现在 {after})—— 连接没被关,这就是 fd 撞 1024 的那条路")

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
        first = db._conn()
        for _ in range(20):
            self.assertIs(db._conn(), first)


if __name__ == "__main__":
    unittest.main()
