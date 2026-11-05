#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 存储层 + 通用号池逻辑。

一张 accounts 表存所有网站的账号,channel 字段区分来源网站。
token 只是缓存,过期由 adapter.refresh_token 刷新(通用号池逻辑)。
"""
import json
import sqlite3
import threading
import time

# 账号状态
ST_ACTIVE = "active"        # 可用
ST_COOLDOWN = "cooldown"    # 临时冷却(权限/上游/限流,稍后自动恢复)
ST_EXHAUSTED = "exhausted"  # 余额耗尽
ST_DEAD = "dead"            # 失效(登录失败/被封)
ST_UNCHECKED = "unchecked"  # 新入库未验证

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,              -- 来源渠道 (adapter 的 name)
    identity    TEXT NOT NULL,              -- 唯一标识 (email 等)
    secret      TEXT,                       -- 凭证 json (password/vendor 等)
    token       TEXT,                       -- 缓存的 access token
    token_exp   INTEGER DEFAULT 0,          -- token 过期 unix 时间
    balance     REAL,                       -- 余额/额度
    status      TEXT DEFAULT 'unchecked',
    schedulable INTEGER DEFAULT 1,          -- 0 = 站长手动摘出号池
    meta        TEXT,                       -- 额外信息 json
    created_at  INTEGER,
    updated_at  INTEGER,
    last_check  INTEGER DEFAULT 0,          -- 最后一次巡检时间
    last_used   INTEGER DEFAULT 0,          -- 最后一次真实发出请求的时间
    UNIQUE(channel, identity)
);
CREATE INDEX IF NOT EXISTS idx_channel_status ON accounts(channel, status);
"""

# 面板可用的排序键 → 真实列名。白名单而不是把参数拼进 SQL:排序键来自 query
# string,拼进去就是一条注入口子。
SORTABLE = {
    "id": "id", "identity": "identity", "status": "status",
    "balance": "balance", "last_check": "last_check", "last_used": "last_used",
    "created_at": "created_at", "updated_at": "updated_at",
}


class ThreadConns:
    """每线程一个 sqlite 连接,并且负责在线程结束后把它关掉。

    只用 threading.local 缓存会漏 fd。server.py 的流式路径每个请求起一个裸
    threading.Thread,那个线程跑完结算要写库 —— 于是每条流留下一个没人关的连接,
    两个 fd(库文件 + -wal)。2026-09-05 线上就是这么顶满 1024 的 fd 上限的:
    976 个 fd 是同一个库的连接,进程只剩 8 个活线程,accept() 全线报
    Errno 24「Too many open files」,端口还在 LISTEN 但谁也连不进来,
    nginx 那头全是 upstream timed out。见 tests/test_db_conn_leak.py。

    所以连接除了进 threading.local,还记一份 registry;每次新建连接时顺手把已经
    结束的线程留下的连接关掉。连接数只在「有新线程」时增长,清扫也就正好发生在
    需要的时候,不必另起定时器,也不必让调用方改成 with 拿连接。

    不靠引用计数自动回收:线上那 481 个连接就是等自动回收没等到的结果,原因没查
    清。显式 close() 是确定的,不确定的东西不该守在这条路上。
    """

    def __init__(self, path, pragmas=()):
        self.path = path
        self._pragmas = tuple(pragmas)
        self._local = threading.local()
        self._live = {}                 # Thread -> Connection
        self._lock = threading.Lock()

    def get(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        # check_same_thread=False:连接照旧只被自己那个线程使用,放开这个检查只是
        # 为了让清扫方能对「线程已结束」的连接调 close()(否则 sqlite3 会拒绝)。
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in self._pragmas:
            conn.execute(pragma)
        self._local.conn = conn
        with self._lock:
            self._live[threading.current_thread()] = conn
            for th in [t for t in self._live if not t.is_alive()]:
                try:
                    self._live.pop(th).close()
                except Exception:
                    pass        # 关不掉就丢掉引用,不能让清扫本身把调用打断
        return conn

    def close_current(self):
        """关掉本线程的连接并忘掉它,下次 get() 重开。

        用在「要松开文件句柄」的地方:Windows 上句柄还开着就删不掉临时目录,
        测试的 teardown 靠它。别让调用方去戳 threading.local 内部。
        """
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        with self._lock:
            self._live.pop(threading.current_thread(), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def live(self):
        """当前还开着的连接数。门禁用它断言「不随线程数增长」。"""
        with self._lock:
            return len(self._live)


class DB:
    def __init__(self, path):
        self.path = path
        self._conns = ThreadConns(path, (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
        ))
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c):
        """轻量列迁移:老库补新增列(SQLite 的 ADD COLUMN 没有 IF NOT EXISTS)。

        schedulable 默认 1 —— 老库里的号全都还在池子里,升级不该悄悄摘掉谁。
        """
        have = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        for name, ddl in (("schedulable", "INTEGER DEFAULT 1"),
                          ("last_used", "INTEGER DEFAULT 0")):
            if name not in have:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")

    def _conn(self):
        # 每线程一个连接(FastAPI 同步路由跑在线程池),线程结束后由 ThreadConns 关掉
        return self._conns.get()

    def close(self):
        """松开本线程的连接(释放文件句柄)。下次用会自动重开。"""
        self._conns.close_current()

    # ---- 写 ----

    def upsert_account(self, channel, identity, secret=None, meta=None, status=ST_UNCHECKED):
        """插入或更新账号(按 channel+identity 唯一)。返回 id。"""
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "SELECT id FROM accounts WHERE channel=? AND identity=?",
                (channel, identity))
            row = cur.fetchone()
            if row:
                c.execute(
                    "UPDATE accounts SET secret=COALESCE(?,secret), meta=COALESCE(?,meta), updated_at=? WHERE id=?",
                    (json.dumps(secret) if secret else None,
                     json.dumps(meta) if meta else None, now, row["id"]))
                return row["id"]
            cur = c.execute(
                "INSERT INTO accounts(channel,identity,secret,meta,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (channel, identity,
                 json.dumps(secret) if secret else None,
                 json.dumps(meta) if meta else None,
                 status, now, now))
            return cur.lastrowid

    def upsert_accounts_bulk(self, channel, items):
        """批量 upsert(一个事务)。items=[(identity, secret, meta, status, token_exp)]。
        用于万级文件型号池同步,避免逐号一个事务把 CPU/IO 打满。"""
        if not items:
            return 0
        now = int(time.time())
        rows = []
        for identity, secret, meta, status, token_exp in items:
            rows.append((
                channel, identity,
                json.dumps(secret) if not isinstance(secret, str) and secret is not None else secret,
                json.dumps(meta) if not isinstance(meta, str) and meta is not None else meta,
                status, token_exp, now, now, 0))
        with self._conn() as c:
            c.executemany(
                "INSERT INTO accounts(channel,identity,secret,meta,status,token_exp,"
                "created_at,updated_at,last_check) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(channel,identity) DO UPDATE SET "
                "secret=excluded.secret, meta=excluded.meta, status=excluded.status, "
                "token_exp=excluded.token_exp, updated_at=excluded.updated_at", rows)
        return len(rows)

    def list_identities(self, channel):
        """只取某渠道的 identity 列表(轻量,用于同步对账)。"""
        with self._conn() as c:
            return [r["identity"] for r in
                    c.execute("SELECT identity FROM accounts WHERE channel=?", (channel,)).fetchall()]

    def list_statuses(self, channel):
        """轻量读取某渠道各账号的现有状态。"""
        with self._conn() as c:
            return {r["identity"]: r["status"] for r in c.execute(
                "SELECT identity,status FROM accounts WHERE channel=?", (channel,)).fetchall()}

    def delete_accounts(self, channel, identities):
        """批量删除指定账号,分块避免 SQLite 参数上限。"""
        if not identities:
            return 0
        deleted = 0
        with self._conn() as c:
            for start in range(0, len(identities), 500):
                batch = identities[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = c.execute(
                    f"DELETE FROM accounts WHERE channel=? AND identity IN ({placeholders})",
                    [channel, *batch],
                )
                deleted += cur.rowcount
        return deleted

    def update_account_statuses(self, channel, statuses):
        """批量写入运行状态,只更新状态实际发生变化的账号。"""
        if not statuses:
            return 0
        now = int(time.time())
        rows = [(status, now, now, channel, identity, status)
                for identity, status in statuses]
        with self._conn() as c:
            c.executemany(
                "UPDATE accounts SET status=?, updated_at=?, last_check=? "
                "WHERE channel=? AND identity=? AND status<>?", rows)
        return len(rows)

    def record_account_checks(self, channel, statuses):
        """批量记录真实测活结果；状态未变化时也更新 last_check。"""
        if not statuses:
            return 0
        now = int(time.time())
        rows = [(status, now, now, channel, identity)
                for identity, status in statuses]
        with self._conn() as c:
            c.executemany(
                "UPDATE accounts SET status=?, updated_at=?, last_check=? "
                "WHERE channel=? AND identity=?", rows)
        return len(rows)

    def update_account(self, acct_id, **fields):
        """更新账号任意字段。secret/meta 自动 json 序列化。"""
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("secret", "meta") and not isinstance(v, str):
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(acct_id)
        with self._conn() as c:
            c.execute(f"UPDATE accounts SET {','.join(cols)} WHERE id=?", vals)

    def set_status(self, acct_id, status):
        self.update_account(acct_id, status=status, last_check=int(time.time()))

    def purge(self, channel=None, statuses=(ST_DEAD,)):
        """删除指定状态的账号(默认只清永久失效)。channel=None 时全渠道。返回删除数。"""
        if not statuses:
            return 0
        ph = ",".join("?" for _ in statuses)
        q = f"DELETE FROM accounts WHERE status IN ({ph})"
        args = list(statuses)
        if channel:
            q += " AND channel=?"; args.append(channel)
        with self._conn() as c:
            cur = c.execute(q, args)
            return cur.rowcount

    # ---- 读 ----

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("secret", "meta"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        return d

    def get_account(self, acct_id):
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone())

    def get_by_identity(self, channel, identity):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM accounts WHERE channel=? AND identity=?",
                (channel, identity)).fetchone())

    @staticmethod
    def _filters(channel=None, status=None, q=None, schedulable=None, ids=None):
        """面板筛选 → (WHERE 子句, 参数)。列表与计数共用一份,两处口径不会歪。

        q 是关键词:identity 或 meta 里出现即可。面板上搜的是一个号,而号的关键
        信息一半在 identity(email)、一半在 meta(套餐、订阅),只搜 identity 会让
        「搜 k12 套餐」这类筛选落空。
        """
        where, args = ["1=1"], []
        if channel:
            where.append("channel=?"); args.append(channel)
        if status:
            where.append("status=?"); args.append(status)
        if schedulable is not None:
            where.append("schedulable=?"); args.append(1 if schedulable else 0)
        if ids:
            ids = [int(i) for i in ids]
            where.append(f"id IN ({','.join('?' for _ in ids)})"); args += ids
        if q:
            like = f"%{q}%"
            where.append("(identity LIKE ? OR meta LIKE ?)"); args += [like, like]
        return " AND ".join(where), args

    def list_accounts(self, channel=None, status=None, limit=1000, offset=0,
                      q=None, schedulable=None, sort=None, order="desc", ids=None):
        where, args = self._filters(channel, status, q, schedulable, ids)
        col = SORTABLE.get(sort or "")
        if col:
            direction = "ASC" if str(order).lower() == "asc" else "DESC"
            # id 兜底排序:同值时没有稳定的第二排序键,翻页会重复或漏行
            tail = f" ORDER BY {col} {direction}, id DESC"
        else:
            tail = " ORDER BY id"
        q_ = f"SELECT * FROM accounts WHERE {where}{tail} LIMIT ? OFFSET ?"
        with self._conn() as c:
            return [self._row(r) for r in
                    c.execute(q_, [*args, limit, offset]).fetchall()]

    def count_accounts(self, channel=None, status=None, q=None, schedulable=None,
                       ids=None):
        where, args = self._filters(channel, status, q, schedulable, ids)
        with self._conn() as c:
            return c.execute(
                f"SELECT COUNT(*) n FROM accounts WHERE {where}", args).fetchone()["n"]

    def delete_channel(self, channel):
        """删除某渠道的全部账号(不分状态)。返回删除数。"""
        if not channel:
            return 0
        with self._conn() as c:
            return c.execute("DELETE FROM accounts WHERE channel=?", (channel,)).rowcount

    def delete_account(self, channel, identity):
        """删除单个账号(channel+identity)。返回删除数。"""
        with self._conn() as c:
            return c.execute("DELETE FROM accounts WHERE channel=? AND identity=?",
                             (channel, identity)).rowcount

    def pick_active(self, channel):
        """挑一个可用账号(active,余额降序优先)。用于网关取号。

        schedulable=0 的号在 SQL 这一层就排除掉:那是站长手动摘出号池的号,
        网关、巡检、批量操作三条路都要认这一位,漏了任何一条都会出现
        「面板上明明停用了,却还在被调用」。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM accounts WHERE channel=? AND status=? AND schedulable=1 "
                "ORDER BY (balance IS NULL) ASC, balance DESC, last_check ASC LIMIT 1",
                (channel, ST_ACTIVE)).fetchone()
            return self._row(r)

    def touch_used(self, acct_id):
        """记一次「这个号真的发出去过请求」。面板的「最近使用」读它。

        单独一条 UPDATE 而不是并进别的写:取号成功后并不总有别的字段要写,
        而 last_check 是「什么时候被检查过」,和「什么时候被用过」是两件事 ——
        混成一个字段的话,面板上就分不出「这号一直能用」和「这号一直没人用」。"""
        with self._conn() as c:
            c.execute("UPDATE accounts SET last_used=? WHERE id=?",
                      (int(time.time()), acct_id))

    # ---- 批量(面板选中多行后一次做完) ----

    def set_schedulable(self, ids, on):
        """批量摘出/放回号池。返回受影响的 id 列表。

        只认传进来的 id:没有「按条件批量」这条路 —— 那等于给一个不带边界的写操作,
        而面板上每次选中的是什么,操作的人自己看得见。"""
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        with self._conn() as c:
            c.execute(f"UPDATE accounts SET schedulable=?, updated_at=? "
                      f"WHERE id IN ({ph})",
                      [1 if on else 0, int(time.time()), *ids])
            return [r["id"] for r in c.execute(
                f"SELECT id FROM accounts WHERE id IN ({ph})", ids).fetchall()]

    def set_statuses(self, ids, status):
        """批量改状态(面板的「重置状态」)。"""
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return 0
        now = int(time.time())
        ph = ",".join("?" for _ in ids)
        with self._conn() as c:
            return c.execute(
                f"UPDATE accounts SET status=?, updated_at=?, last_check=? "
                f"WHERE id IN ({ph})", [status, now, now, *ids]).rowcount

    def delete_ids(self, ids):
        """按主键批量删除。面板的「批量删除」用它 —— 单条那条入口按
        (channel, identity) 定位,与面板选中行的 id 对不上。"""
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return 0
        deleted = 0
        with self._conn() as c:
            for start in range(0, len(ids), 500):     # 分块避开 SQLite 参数上限
                batch = ids[start:start + 500]
                ph = ",".join("?" for _ in batch)
                deleted += c.execute(
                    f"DELETE FROM accounts WHERE id IN ({ph})", batch).rowcount
        return deleted

    def stats(self, channel=None):
        """各渠道统计:总数/各状态数/余额合计/被摘出的号。

        parked 单独出一档而不是并进某个状态:站长手动摘出的号状态照旧是 active
        (它本身没坏),只是不参与取号 —— 面板上「可用 12」和「其中 3 个已摘出」
        要能同时看见,否则摘出一个号之后账就对不上了。"""
        q = ("SELECT channel, status, schedulable, COUNT(*) n, "
             "COALESCE(SUM(balance),0) bal FROM accounts ")
        args = []
        if channel:
            q += "WHERE channel=? "; args.append(channel)
        q += "GROUP BY channel, status, schedulable"
        out = {}
        with self._conn() as c:
            for r in c.execute(q, args):
                ch = out.setdefault(r["channel"], {"total": 0, "balance": 0.0,
                                                   "parked": 0, "by_status": {}})
                ch["by_status"][r["status"]] = ch["by_status"].get(r["status"], 0) + r["n"]
                ch["total"] += r["n"]
                ch["balance"] += r["bal"] or 0
                if not r["schedulable"]:
                    ch["parked"] += r["n"]
        return out
