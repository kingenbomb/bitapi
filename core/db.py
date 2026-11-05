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

# Account-management fields. Existing identities, credentials and health states
# remain authoritative; migrations only add fields with the old behavior as default.
ACCOUNT_COLUMNS = {
    "name": "TEXT DEFAULT ''", "account_type": "TEXT DEFAULT ''",
    "priority": "INTEGER DEFAULT 50", "concurrency": "INTEGER DEFAULT 0",
    "rate_multiplier": "REAL DEFAULT 1", "group_ids": "TEXT DEFAULT '[]'",
    "account_expires_at": "INTEGER DEFAULT 0", "auto_pause_on_expired": "INTEGER DEFAULT 1",
    "temp_until": "INTEGER DEFAULT 0", "temp_reason": "TEXT DEFAULT ''",
    "last_used_at": "INTEGER DEFAULT 0", "last_error": "TEXT DEFAULT ''",
    "proxy_id": "INTEGER", "quota_limit": "REAL DEFAULT 0",
    "quota_daily_limit": "REAL DEFAULT 0", "quota_weekly_limit": "REAL DEFAULT 0",
    "quota_reset_at": "INTEGER DEFAULT 0", "quota_reset_log_id": "INTEGER DEFAULT 0",
    "base_rpm": "INTEGER DEFAULT 0", "window_cost_limit": "REAL DEFAULT 0",
    "upstream_until": "INTEGER DEFAULT 0", "upstream_reason": "TEXT DEFAULT ''",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,              -- 来源渠道名
    identity    TEXT NOT NULL,              -- 唯一标识 (email 等)
    secret      TEXT,                       -- 凭证 json (password/vendor 等)
    token       TEXT,                       -- 缓存的 access token
    token_exp   INTEGER DEFAULT 0,          -- token 过期 unix 时间
    balance     REAL,                       -- 余额/额度
    status      TEXT DEFAULT 'unchecked',
    meta        TEXT,                       -- 额外信息 json
    created_at  INTEGER,
    updated_at  INTEGER,
    last_check  INTEGER DEFAULT 0,
    UNIQUE(channel, identity)
);
CREATE INDEX IF NOT EXISTS idx_channel_status ON accounts(channel, status);
"""


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

    def close_all(self):
        """服务停止后关闭所有线程连接，最后一个连接负责 SQLite WAL 收尾。"""
        with self._lock:
            for conn in self._live.values():
                conn.close()
            self._live.clear()
        self._local.conn = None


class DB:
    def __init__(self, path):
        self.path = path
        self._conns = ThreadConns(path, (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
        ))
        with self._conn() as c:
            c.executescript(SCHEMA)
            existing = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
            for name, ddl in ACCOUNT_COLUMNS.items():
                if name not in existing:
                    c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS pool_proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS pool_test_plans (
                    account_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    next_run INTEGER NOT NULL, last_run INTEGER, last_result TEXT
                );
                CREATE TABLE IF NOT EXISTS pool_test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, model TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pool_test_account ON pool_test_results(account_id,id);
                CREATE TABLE IF NOT EXISTS gpt_actions (
                    request_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                    created_at INTEGER NOT NULL, intent TEXT NOT NULL, result TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_gpt_actions_account ON gpt_actions(account_id,created_at);
            """)

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

    def insert_account(self, channel, identity, secret, meta, status=ST_UNCHECKED):
        """Create only; a concurrent create must never overwrite credentials."""
        now = int(time.time())
        with self._conn() as c:
            return c.execute(
                "INSERT INTO accounts(channel,identity,secret,meta,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (channel, identity, json.dumps(secret), json.dumps(meta), status, now, now)).lastrowid

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
            if k in ("secret", "meta", "group_ids") and not isinstance(v, str):
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
        for k in ("secret", "meta", "group_ids"):
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

    def list_accounts(self, channel=None, status=None, limit=1000, offset=0, q=None):
        sql, args = "SELECT * FROM accounts WHERE 1=1", []
        if channel:
            sql += " AND channel=?"; args.append(channel)
        if status:
            sql += " AND status=?"; args.append(status)
        if q:
            sql += " AND identity LIKE ?"; args.append(f"%{q}%")
        sql += " ORDER BY id LIMIT ? OFFSET ?"; args += [limit, offset]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql, args).fetchall()]

    def count_accounts(self, channel=None, status=None, q=None):
        sql, args = "SELECT COUNT(*) n FROM accounts WHERE 1=1", []
        if channel:
            sql += " AND channel=?"; args.append(channel)
        if status:
            sql += " AND status=?"; args.append(status)
        if q:
            sql += " AND identity LIKE ?"; args.append(f"%{q}%")
        with self._conn() as c:
            return c.execute(sql, args).fetchone()["n"]

    def count_by_status(self, channel=None, q=None):
        """某渠道(可加 identity 搜索)下各状态的账号数,给筛选条用。"""
        sql = "SELECT status, COUNT(*) n FROM accounts WHERE 1=1"
        args = []
        if channel:
            sql += " AND channel=?"; args.append(channel)
        if q:
            sql += " AND identity LIKE ?"; args.append(f"%{q}%")
        sql += " GROUP BY status"
        with self._conn() as c:
            return {r["status"]: r["n"] for r in c.execute(sql, args).fetchall()}

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

    def list_shared(self, user_id=None, limit=200):
        """用户共享进来的账号(meta.shared_by 有值),新→旧。

        走 SQLite 的 JSON1(json_extract)。万一运行环境的 SQLite 没编 JSON1,
        退回「读一批再在 Python 里筛」—— 一个可选视图不该把接口整个打挂。
        """
        try:
            sql = ("SELECT * FROM accounts "
                   "WHERE json_extract(meta,'$.shared_by') IS NOT NULL")
            args = []
            if user_id is not None:
                sql += " AND json_extract(meta,'$.shared_by')=?"
                args.append(int(user_id))
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            with self._conn() as c:
                return [self._row(r) for r in c.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            out = []
            for row in self.list_accounts(limit=5000):
                meta = row.get("meta") or {}
                if not isinstance(meta, dict) or not meta.get("shared_by"):
                    continue
                if user_id is not None and int(meta["shared_by"]) != int(user_id):
                    continue
                out.append(row)
                if len(out) >= limit:
                    break
            return out

    def pick_active(self, channel, prefer=None, group_id=None, exclude=None, preferred_id=None, web_image=False):
        """取一个可用账号(active,按余额降序),供轮换取号。

        prefer: 可选的 SQL **布尔表达式**。adapter 用它把「更该先用」的号排到最前
        —— 例如会员期内的号不消耗额度(余额恒 0),必须先吃流量,否则会被
        余额降序的排序永远排到最后,等于没用上。
        """
        order = ("CASE WHEN %s THEN 0 ELSE 1 END, " % prefer) if prefer else ""
        now = int(time.time())
        where = ""
        if web_image and channel != "gpt":
            raise ValueError("Web image selection is only supported for GPT accounts")
        status_filter = "status IN ('active','cooldown','exhausted','unchecked')" if web_image else "status=?"
        args = [channel, *([] if web_image else [ST_ACTIVE]), now, now]
        if group_id is not None:
            where += " AND (json_array_length(group_ids)=0 OR ? IN (SELECT value FROM json_each(group_ids)))"
            args.append(group_id)
        if exclude:
            where += " AND id NOT IN (" + ",".join("?" for _ in exclude) + ")"
            args.extend(exclude)
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM accounts WHERE channel=? AND " + status_filter + " "
                # 管理台显式停用的号（meta.admin_paused）不参与取号：
                # 只靠 status 翻不动它，因为巡检器会把"能用"的号放回 active。
                "AND COALESCE(json_extract(meta,'$.admin_paused'),0)=0 "
                "AND (auto_pause_on_expired=0 OR account_expires_at=0 OR account_expires_at>?) "
                "AND temp_until<=? " + ("" if web_image else "AND upstream_until<=strftime('%s','now') ") + where +
                " ORDER BY " + ("CASE WHEN id=" + str(int(preferred_id)) + " THEN 0 ELSE 1 END," if preferred_id else "") + "priority ASC, " + order +
                "(balance IS NULL) ASC, balance DESC, last_check ASC LIMIT 1",
                args).fetchone()
            return self._row(r)

    def get_pool_proxy(self, proxy_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM pool_proxies WHERE id=?", (proxy_id,)).fetchone()
            return dict(r) if r else None

    def stats(self, channel=None):
        """各渠道统计:总数/各状态数/余额合计。"""
        q = ("SELECT channel, status, COUNT(*) n, "
             "COALESCE(SUM(balance),0) bal FROM accounts ")
        args = []
        if channel:
            q += "WHERE channel=? "; args.append(channel)
        q += "GROUP BY channel, status"
        out = {}
        with self._conn() as c:
            for r in c.execute(q, args):
                ch = out.setdefault(r["channel"], {"total": 0, "balance": 0.0, "by_status": {}})
                ch["by_status"][r["status"]] = r["n"]
                ch["total"] += r["n"]
                ch["balance"] += r["bal"] or 0
        return out
