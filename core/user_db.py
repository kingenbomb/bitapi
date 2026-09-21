#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户接入层存储 —— users / api_keys / groups / usage_logs / email_verifications。

与号池库(core/db.py 的 accounts 表)共用同一个 SQLite 文件,但表相互独立。
分组套餐模式(subscription 型):用户属于某 group,group 决定可用模型白名单、
倍率、日/周/月用量上限、RPM。不扣余额,usage_logs 仅记录用量供展示与限流聚合。
"""
import json
import sqlite3
import time

from core.db import ThreadConns

# 用户状态
US_ACTIVE = "active"
US_DISABLED = "disabled"

# API key 状态
KS_ACTIVE = "active"
KS_DISABLED = "disabled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    rate_multiplier   REAL DEFAULT 1.0,
    supported_models  TEXT,            -- JSON: ["kg-*","grok-4.5",...] 支持前缀通配
    billing_policy    TEXT DEFAULT 'free',  -- balance | quota | free
    rpm_limit         INTEGER DEFAULT 0,   -- 每分钟请求上限,0=不限
    daily_limit       INTEGER DEFAULT 0,   -- 日用量上限(单位 limit_unit),0=不限
    weekly_limit      INTEGER DEFAULT 0,
    monthly_limit     INTEGER DEFAULT 0,
    limit_unit        TEXT DEFAULT 'requests',  -- requests | tokens
    is_default        INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'active',
    -- 订阅套餐(时长卡)。listed=0 的分组只是管理员分配用的内部档位,
    -- 不出现在用户端可购列表里;duration_hours=0 表示不限时(买了永久有效)。
    listed            INTEGER DEFAULT 0,   -- 是否上架售卖
    price             REAL DEFAULT 0,      -- 售价(美元)
    duration_hours    INTEGER DEFAULT 0,   -- 有效时长(小时):1=小时卡 24=天卡 720=月卡
    notes             TEXT,                -- 卖点文案,展示在用户端套餐卡上
    created_at        INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT NOT NULL UNIQUE,
    password_hash     TEXT NOT NULL,     -- pbkdf2 格式串
    role              TEXT DEFAULT 'user',   -- user | admin
    status            TEXT DEFAULT 'active',
    group_id          INTEGER,               -- 当前套餐组
    balance           REAL DEFAULT 0,        -- 可用余额(美元),credit_ledger 的投影
    total_spent       REAL DEFAULT 0,        -- 累计消费(美元)
    email_verified_at INTEGER DEFAULT 0,
    display_name      TEXT,                  -- 昵称,空则回落邮箱前缀
    avatar            TEXT,                  -- data URI,上限 20KB(前端压,后端硬校验)
    inviter_id        INTEGER,               -- 邀请人(一次性终身绑定)
    aff_code          TEXT NOT NULL UNIQUE,  -- 专属永久返佣码,不作为邀请制通行证
    -- 当前订阅。为空 = 没买套餐,走 group_id(默认分组)按量计费。
    -- 到期不靠定时任务改库,读的时候判断(见 effective_group_id):定时任务
    -- 漏跑一次,用户就白用过期套餐,而且漏跑本身没人会发现。
    plan_group_id     INTEGER,               -- 订阅的套餐组
    plan_expires_at   INTEGER DEFAULT 0,     -- 到期 unix 秒,0=不限时
    created_at        INTEGER,
    FOREIGN KEY(group_id) REFERENCES groups(id),
    FOREIGN KEY(inviter_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    key           TEXT NOT NULL UNIQUE,   -- sk- 前缀明文
    name          TEXT,
    status        TEXT DEFAULT 'active',
    expires_at    INTEGER DEFAULT 0,      -- 0=永不过期
    last_used_at  INTEGER DEFAULT 0,
    quota         REAL DEFAULT 0,         -- 本密钥累计消费上限(美元),0=不限
    used_quota    REAL DEFAULT 0,         -- 已消费(结算时累加,只增不减)
    allowed_models TEXT,                  -- JSON:密钥级模型白名单,空=跟随分组
    allowed_ips   TEXT,                   -- JSON:IP/CIDR 白名单,空=不限
    created_at    INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_apikey_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS usage_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    api_key_id       INTEGER,
    channel          TEXT,
    model            TEXT,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    cost             REAL DEFAULT 0,            -- 原价(未乘倍率)
    actual_cost      REAL DEFAULT 0,            -- 实扣(乘倍率后)
    billing_mode     TEXT DEFAULT 'free',       -- token | per_request | free
    pricing_snapshot TEXT,                      -- JSON:冻结当次单价/档位/倍率,供改价后复算
    token_source     TEXT DEFAULT 'estimate',   -- upstream | estimate | per_request
    stream           INTEGER DEFAULT 0,
    duration_ms      INTEGER DEFAULT 0,
    created_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_key_time ON usage_logs(api_key_id, created_at);

CREATE TABLE IF NOT EXISTS email_verifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    email        TEXT NOT NULL,
    code         TEXT NOT NULL,
    expires_at   INTEGER,
    verified_at  INTEGER DEFAULT 0,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_emailverif_user ON email_verifications(user_id);

-- 找回密码的一次性令牌。只存 SHA-256:库泄露时拿不到能直接用的链接。
-- 用掉(used_at)或过期(expires_at)都作废;同一用户新发一张时旧的一并作废。
CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER DEFAULT 0,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_pwreset_user ON password_resets(user_id);

-- 白嫖社区身份与本地用户是一对一。subject 只存 Flarum users.id 的字符串；
-- username/name/email 都是展示快照，不能拿来认人或自动合并账号。
CREATE TABLE IF NOT EXISTS community_identities (
    subject       TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL UNIQUE,
    username      TEXT NOT NULL,
    name          TEXT,
    email         TEXT NOT NULL,
    bound_at      INTEGER NOT NULL,
    last_login_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- 浏览器 OAuth 短流程。state 与浏览器 nonce 只存 SHA-256；ready 后保存的
-- userinfo 是社区 access token 换回的快照，access token 本身绝不落库。
CREATE TABLE IF NOT EXISTS community_auth_flows (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    state_hash        TEXT NOT NULL UNIQUE,
    nonce_hash        TEXT NOT NULL UNIQUE,
    purpose           TEXT NOT NULL CHECK(purpose IN ('login','bind')),
    initiator_user_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'pending',
    subject           TEXT,
    username          TEXT,
    name              TEXT,
    email             TEXT,
    expires_at        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    used_at           INTEGER DEFAULT 0,
    FOREIGN KEY(initiator_user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_community_flow_expiry
    ON community_auth_flows(status, expires_at);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- 公告已读状态。公告正文存 settings 的 announcements 列表(个位数量级,不值得建表),
-- 但「谁读过哪条」是按用户增长的,且必须跨设备一致,所以单独落表。
-- 存 read_at 而不是布尔值:公告被编辑后 updated_at 会大于 read_at,自动重新算未读。
CREATE TABLE IF NOT EXISTS announcement_reads (
    announcement_id TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    read_at         INTEGER NOT NULL,
    PRIMARY KEY (announcement_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_annread_user ON announcement_reads(user_id);

-- 余额变动的唯一入口。充值/兑码/返佣/调额/消费扣费全部写这里,
-- users.balance 只是它的投影。idem_key 唯一索引保证幂等。
CREATE TABLE IF NOT EXISTS credit_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    amount        REAL NOT NULL,          -- 正=入账,负=扣费
    reason        TEXT NOT NULL,          -- recharge|redeem|affiliate|admin|usage|...
    idem_key      TEXT NOT NULL UNIQUE,   -- 幂等键,重复写入静默返回既有记录
    meta          TEXT,                   -- JSON 附加信息(订单号/来源用户等)
    balance_after REAL,
    created_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_time ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_reason ON credit_ledger(reason);

CREATE TABLE IF NOT EXISTS redeem_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    -- balance=余额兑换码;invitation=注册邀请码(不入账,面额恒 0,只在注册时消耗)
    type        TEXT DEFAULT 'balance',
    value       REAL NOT NULL,            -- 允许负数(扣款/纠错)
    status      TEXT DEFAULT 'unused',    -- unused | used | disabled
    used_by     INTEGER,
    used_at     INTEGER,
    expires_at  INTEGER DEFAULT 0,        -- 0=永不过期
    notes       TEXT,
    created_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_code_status ON redeem_codes(status);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    out_trade_no     TEXT NOT NULL UNIQUE,  -- 商户单号
    amount           REAL NOT NULL,         -- 应到账(美元)
    pay_amount       REAL,                  -- 实付
    -- 下单那一刻算出的应收人民币。冻在这里而不是回调时按汇率反算:汇率是可变配置,
    -- 乘一次再除回去,中途改一次两边就永久对不上,金额校验也就不可能成立。
    pay_amount_cny   REAL,
    provider         TEXT NOT NULL,
    payment_trade_no TEXT,                  -- 三方单号
    status           TEXT DEFAULT 'pending', -- pending|paid|recharging|completed|expired|failed
    recharge_code    TEXT,                  -- 到账用的内部兑换码
    lease_version    INTEGER DEFAULT 0,     -- 幂等租约版本号(乐观锁)
    expires_at       INTEGER,
    paid_at          INTEGER,
    completed_at     INTEGER,
    created_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_order_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_order_status ON orders(status);

-- 数据渠道:管理台建的 OpenAI 兼容上游。代码渠道(adapters/*.py)不在这张表里,
-- 两者在 core/adapter 注册表里平级;name 同时也是 accounts.channel。
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL DEFAULT 'openai',   -- 目前只有 openai(兼容协议)
    base_url    TEXT NOT NULL,                    -- 如 https://api.example.com/v1
    chat_path   TEXT DEFAULT '/chat/completions',
    models      TEXT,                             -- JSON 数组:对外模型名
    model_map   TEXT,                             -- JSON 对象:对外名 → 上游名
    headers     TEXT,                             -- JSON 对象:额外请求头
    timeout     INTEGER DEFAULT 0,                -- 秒,0 = 用 BITAPI_TIMEOUT
    notes       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);

-- 结对单价定价表。价格单位:token 类为「美元/1M token」,per_request 为「美元/次」。
-- long_* 为长上下文阶梯(整次跳档):total_ctx > long_threshold 时该项换用长档单价。
CREATE TABLE IF NOT EXISTS model_pricing (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern          TEXT NOT NULL,          -- 精确名或 'prefix*'
    group_id               INTEGER,                -- NULL=全局
    billing_mode           TEXT DEFAULT 'token',   -- token | per_request | free
    input_price            REAL DEFAULT 0,
    output_price           REAL DEFAULT 0,
    cache_read_price       REAL DEFAULT 0,
    cache_write_price      REAL DEFAULT 0,
    per_request_price      REAL DEFAULT 0,
    long_threshold         INTEGER DEFAULT 0,      -- 0/NULL=不启用阶梯
    long_input_price       REAL,
    long_output_price      REAL,
    long_cache_read_price  REAL,
    long_cache_write_price REAL,
    notes                  TEXT,
    updated_at             INTEGER
);
-- 用表达式索引而非 UNIQUE(model_pattern, group_id):SQLite 把 NULL 视为互不相同,
-- 全局定价(group_id IS NULL)会因此插入重复行,使 upsert 退化为 insert。
CREATE UNIQUE INDEX IF NOT EXISTS ux_pricing_pattern_group
    ON model_pricing(model_pattern, IFNULL(group_id, -1));
CREATE INDEX IF NOT EXISTS idx_pricing_group ON model_pricing(group_id);
"""

_JSON_COLS = {"supported_models", "meta", "pricing_snapshot",
              "allowed_models", "allowed_ips", "models", "model_map", "headers"}


class CommunityFlowError(RuntimeError):
    pass


class CommunityInviteError(RuntimeError):
    pass


class CommunityEmailConflict(RuntimeError):
    pass


class CommunityIdentityConflict(RuntimeError):
    pass


class CommunityUserDisabled(RuntimeError):
    pass


class CommunityAffCodeConflict(RuntimeError):
    pass


class UserDB:
    def __init__(self, path):
        self.path = path
        # 连接的开与关都交给 ThreadConns —— 短命线程漏 fd 那条见 core/db.py 的说明
        self._conns = ThreadConns(path, (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA foreign_keys=ON",
        ))
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c):
        """轻量列迁移:老库补新增列(SQLite 无 IF NOT EXISTS for ADD COLUMN)。"""
        def cols(table):
            return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

        added = cols("usage_logs")
        for name, ddl in (
                ("token_source", "TEXT DEFAULT 'estimate'"),
                ("actual_cost", "REAL DEFAULT 0"),
                ("billing_mode", "TEXT DEFAULT 'free'"),
                ("pricing_snapshot", "TEXT")):
            if name not in added:
                c.execute(f"ALTER TABLE usage_logs ADD COLUMN {name} {ddl}")

        ucols = cols("users")
        for name, ddl in (("balance", "REAL DEFAULT 0"),
                          ("total_spent", "REAL DEFAULT 0"),
                          ("display_name", "TEXT"),
                          ("avatar", "TEXT"),
                          # 改密时间:早于它签发的 JWT 一律作废。找回密码的意义之一
                          # 就是把偷走会话的人踢出去,不作废旧会话等于没改。
                          ("password_changed_at", "INTEGER DEFAULT 0"),
                          # TOTP:secret 非空 = 已开启;pending 是扫了码还没验证的候选;
                          # last_step 防同一个码 30 秒内重放
                          ("totp_secret", "TEXT"),
                          ("totp_pending", "TEXT"),
                          ("totp_last_step", "INTEGER DEFAULT 0")):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

        # 密钥级管控:默认值让老密钥行为不变(额度不限、模型跟随分组、IP 不限)
        kcols = cols("api_keys")
        for name, ddl in (("expires_at", "INTEGER DEFAULT 0"),
                          ("quota", "REAL DEFAULT 0"),
                          ("used_quota", "REAL DEFAULT 0"),
                          ("allowed_models", "TEXT"),
                          ("allowed_ips", "TEXT")):
            if name not in kcols:
                c.execute(f"ALTER TABLE api_keys ADD COLUMN {name} {ddl}")

        if "billing_policy" not in cols("groups"):
            c.execute("ALTER TABLE groups ADD COLUMN billing_policy TEXT DEFAULT 'free'")

        # 订阅套餐(时长卡)。默认值让所有老分组保持「不上架、不售卖」——
        # listed=0 的分组只能由管理员分配,不会出现在用户端的可购列表里,
        # 所以加这几列不会让现有分组突然变成商品。
        gcols = cols("groups")
        for name, ddl in (("listed", "INTEGER DEFAULT 0"),
                          ("price", "REAL DEFAULT 0"),
                          ("duration_hours", "INTEGER DEFAULT 0"),
                          ("notes", "TEXT")):
            if name not in gcols:
                c.execute(f"ALTER TABLE groups ADD COLUMN {name} {ddl}")

        # 用户当前订阅。plan_group_id 为空 = 没买套餐 = 走默认分组按量计费。
        # 到期不靠定时任务改库,而是在读的时候判断(见 effective_group_id):
        # 定时任务一旦漏跑,用户就会白用过期套餐,而且漏跑这件事没人会发现。
        for name, ddl in (("plan_group_id", "INTEGER"),
                          ("plan_expires_at", "INTEGER DEFAULT 0")):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

        # 应收人民币冻结列。老库的在途订单没有这个值,回调时按「没冻结就跳过金额比对」
        # 处理(见 core/orders.py),不能拿 NULL 当 0 去比,否则老单全被判少付。
        if "pay_amount_cny" not in cols("orders"):
            c.execute("ALTER TABLE orders ADD COLUMN pay_amount_cny REAL")

    def _conn(self):
        return self._conns.get()

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        for k in _JSON_COLS:
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        return d

    # ---- groups ----

    def create_group(self, name, rate_multiplier=1.0, supported_models=None,
                     rpm_limit=0, daily_limit=0, weekly_limit=0, monthly_limit=0,
                     limit_unit="requests", is_default=0, status="active",
                     billing_policy="free", listed=0, price=0.0,
                     duration_hours=0, notes=None):
        now = int(time.time())
        with self._conn() as c:
            if is_default:
                c.execute("UPDATE groups SET is_default=0")
            cur = c.execute(
                "INSERT INTO groups(name,rate_multiplier,supported_models,billing_policy,"
                "rpm_limit,daily_limit,weekly_limit,monthly_limit,limit_unit,"
                "is_default,status,listed,price,duration_hours,notes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, rate_multiplier,
                 json.dumps(supported_models or []), billing_policy,
                 rpm_limit, daily_limit, weekly_limit, monthly_limit,
                 limit_unit, is_default, status, listed, price,
                 duration_hours, notes, now))
            return cur.lastrowid

    def get_group(self, group_id):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM groups WHERE id=?", (group_id,)).fetchone())

    def get_group_by_name(self, name):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM groups WHERE name=?", (name,)).fetchone())

    def get_default_group(self):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM groups WHERE is_default=1 ORDER BY id LIMIT 1").fetchone())

    def list_groups(self):
        with self._conn() as c:
            return [self._row(r) for r in
                    c.execute("SELECT * FROM groups ORDER BY id").fetchall()]

    def list_listed_groups(self):
        """上架售卖的套餐(用户端可购列表)。停用的分组不卖。"""
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM groups WHERE listed=1 AND status='active' "
                "ORDER BY price, id").fetchall()]

    @staticmethod
    def effective_group_id(user, now=None):
        """生效分组 id:未过期的订阅优先,否则回落 group_id(默认分组,按量计费)。

        到期在**读的时候**判断,不靠定时任务把过期订阅写回默认组 —— 定时任务
        漏跑一次,用户就白用过期套餐,而且漏跑这件事没有任何症状,不会有人发现。
        判断一个时间戳很便宜,而这条路径每个请求都会走。
        """
        pid = user.get("plan_group_id")
        if pid:
            exp = user.get("plan_expires_at") or 0
            if exp == 0 or exp > (now if now is not None else time.time()):
                return pid
        return user.get("group_id")

    def effective_group(self, user):
        gid = self.effective_group_id(user)
        return self.get_group(gid) if gid else None

    def update_group(self, group_id, **fields):
        """改分组。is_default 在这里保证唯一 —— 表上没有唯一约束,而
        ensure_default_group() 只按名字找组、不看这个标记,所以两个组同时挂着
        「默认」不会报错也不会被纠正,只会让面板显示两个默认、而新用户实际进
        哪个组要看 DEFAULT_GROUP 那个名字。在写入口一处收敛,比让每个调用方
        自己记得清旧值可靠。"""
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in _JSON_COLS and not isinstance(v, str):
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(group_id)
        with self._conn() as c:
            if fields.get("is_default"):
                c.execute("UPDATE groups SET is_default=0 WHERE id!=?", (group_id,))
            c.execute(f"UPDATE groups SET {','.join(cols)} WHERE id=?", vals)

    def count_group_refs(self, group_id):
        """还有多少用户挂在这个组上。默认分组(group_id)与订阅(plan_group_id)都算 ——
        后者过期了也算:字段还指着它,清掉组就等于把那段历史指空。"""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM users WHERE group_id=? OR plan_group_id=?",
                (group_id, group_id)).fetchone()["n"]

    def delete_group(self, group_id):
        """删组,连带删掉挂在它名下的分组专属定价。

        留着那些定价行没有任何用处:解析链按 group_id 找价,组不存在就永远命中不到,
        而它们照旧出现在管理台定价列表里,显示一个查不出名字的分组。"""
        with self._conn() as c:
            c.execute("DELETE FROM model_pricing WHERE group_id=?", (group_id,))
            return c.execute("DELETE FROM groups WHERE id=?",
                             (group_id,)).rowcount > 0

    # ---- users ----

    def create_user(self, email, password_hash, aff_code, group_id=None,
                    inviter_id=None, role="user", status=US_ACTIVE):
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO users(email,password_hash,role,status,group_id,"
                "inviter_id,aff_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (email, password_hash, role, status, group_id,
                 inviter_id, aff_code, now))
            return cur.lastrowid

    def get_user(self, user_id):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def get_user_by_email(self, email):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM users WHERE email=?", (email,)).fetchone())

    def get_user_by_aff_code(self, aff_code):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM users WHERE aff_code=?", (aff_code,)).fetchone())

    def count_users(self):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

    def count_invitees(self, user_id):
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM users WHERE inviter_id=?",
                (user_id,)).fetchone()["n"]

    def update_user(self, user_id, **fields):
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [user_id]
        with self._conn() as c:
            c.execute(f"UPDATE users SET {cols} WHERE id=?", vals)

    # ---- 白嫖社区登录 ----

    def create_community_flow(self, state_hash, nonce_hash, purpose,
                              initiator_user_id, expires_at):
        now = int(time.time())
        with self._conn() as c:
            # 流程只有五分钟寿命；每次开新流程时顺手清掉过期行，不另起定时器。
            c.execute("DELETE FROM community_auth_flows WHERE expires_at<?", (now,))
            cur = c.execute(
                "INSERT INTO community_auth_flows("
                "state_hash,nonce_hash,purpose,initiator_user_id,status,"
                "expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (state_hash, nonce_hash, purpose, initiator_user_id,
                 "pending", expires_at, now))
            return cur.lastrowid

    def claim_community_flow(self, state_hash, nonce_hash, now=None):
        """callback 原子占用 state；同一个回调只能有一个请求进入换 token。"""
        now = int(time.time()) if now is None else int(now)
        with self._conn() as c:
            changed = c.execute(
                "UPDATE community_auth_flows SET status='exchanging' "
                "WHERE state_hash=? AND nonce_hash=? AND status='pending' "
                "AND expires_at>=?",
                (state_hash, nonce_hash, now)).rowcount
            if changed != 1:
                return None
            return self._row(c.execute(
                "SELECT * FROM community_auth_flows WHERE state_hash=?",
                (state_hash,)).fetchone())

    def ready_community_flow(self, flow_id, profile, expires_at):
        """把服务端换回的 userinfo 冻到 flow；上游 access token 不落库。"""
        with self._conn() as c:
            return c.execute(
                "UPDATE community_auth_flows SET status='ready',subject=?,"
                "username=?,name=?,email=?,expires_at=? "
                "WHERE id=? AND status='exchanging'",
                (profile["id"], profile["username"], profile.get("name"),
                 profile["email"], expires_at, flow_id)).rowcount == 1

    def get_ready_community_flow(self, nonce_hash, now=None):
        now = int(time.time()) if now is None else int(now)
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM community_auth_flows "
                "WHERE nonce_hash=? AND status='ready' AND expires_at>=?",
                (nonce_hash, now)).fetchone())

    def get_community_identity_by_user(self, user_id):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM community_identities WHERE user_id=?",
                (user_id,)).fetchone())

    def get_community_identity_by_subject(self, subject):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM community_identities WHERE subject=?",
                (str(subject),)).fetchone())

    @staticmethod
    def _consume_community_flow(c, flow_id, now):
        changed = c.execute(
            "UPDATE community_auth_flows SET status='used',used_at=? "
            "WHERE id=? AND status='ready' AND expires_at>=?",
            (now, flow_id, now)).rowcount
        if changed != 1:
            raise CommunityFlowError("community flow invalid or expired")

    def finish_community_login(self, flow_id, now=None):
        """已绑定身份登录并消费 flow；未知身份返回 None 且不消费。"""
        now = int(time.time()) if now is None else int(now)
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            flow = c.execute(
                "SELECT * FROM community_auth_flows WHERE id=? "
                "AND purpose='login' AND status='ready' AND expires_at>=?",
                (flow_id, now)).fetchone()
            if flow is None:
                raise CommunityFlowError("community flow invalid or expired")
            identity = c.execute(
                "SELECT * FROM community_identities WHERE subject=?",
                (flow["subject"],)).fetchone()
            if identity is None:
                c.rollback()
                return None
            user = c.execute("SELECT * FROM users WHERE id=?",
                             (identity["user_id"],)).fetchone()
            if user is None or user["status"] != US_ACTIVE:
                raise CommunityUserDisabled("community user is disabled")
            c.execute(
                "UPDATE community_identities SET username=?,name=?,email=?,"
                "last_login_at=? WHERE subject=?",
                (flow["username"], flow["name"], flow["email"], now,
                 flow["subject"]))
            self._consume_community_flow(c, flow_id, now)
            result = self._row(user)
            c.commit()
            return result
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise

    def bind_community_identity(self, flow_id, user_id, now=None):
        """把 ready flow 的社区身份绑给发起用户，并与 flow 消费同事务提交。"""
        now = int(time.time()) if now is None else int(now)
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            flow = c.execute(
                "SELECT * FROM community_auth_flows WHERE id=? "
                "AND purpose='bind' AND initiator_user_id=? "
                "AND status='ready' AND expires_at>=?",
                (flow_id, user_id, now)).fetchone()
            if flow is None:
                raise CommunityFlowError("community flow invalid or expired")
            by_subject = c.execute(
                "SELECT * FROM community_identities WHERE subject=?",
                (flow["subject"],)).fetchone()
            by_user = c.execute(
                "SELECT * FROM community_identities WHERE user_id=?",
                (user_id,)).fetchone()
            if ((by_subject and by_subject["user_id"] != user_id) or
                    (by_user and by_user["subject"] != flow["subject"])):
                raise CommunityIdentityConflict("community identity conflict")
            if by_subject:
                c.execute(
                    "UPDATE community_identities SET username=?,name=?,email=?,"
                    "last_login_at=? WHERE subject=?",
                    (flow["username"], flow["name"], flow["email"], now,
                     flow["subject"]))
            else:
                c.execute(
                    "INSERT INTO community_identities("
                    "subject,user_id,username,name,email,bound_at,last_login_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (flow["subject"], user_id, flow["username"], flow["name"],
                     flow["email"], now, now))
            self._consume_community_flow(c, flow_id, now)
            c.commit()
            return self.get_community_identity_by_user(user_id)
        except sqlite3.IntegrityError as e:
            if c.in_transaction:
                c.rollback()
            raise CommunityIdentityConflict("community identity conflict") from e
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise

    def register_community_user(self, flow_id, invite_code, aff_code,
                                group_id=None, now=None):
        """邀请码、用户、社区身份和 flow 在一个写事务里落地。

        社区首次建号永远是普通用户且永远要邀请码，不走本地注册的首用户免码。
        这里只接受 unused 且未过期的一次性 invitation 码；个人返佣码不能注册。
        """
        now = int(time.time()) if now is None else int(now)
        code = str(invite_code or "").strip().upper()
        if not code:
            raise CommunityInviteError("invite code required")
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            flow = c.execute(
                "SELECT * FROM community_auth_flows WHERE id=? "
                "AND purpose='login' AND status='ready' AND expires_at>=?",
                (flow_id, now)).fetchone()
            if flow is None:
                raise CommunityFlowError("community flow invalid or expired")
            if c.execute(
                    "SELECT 1 FROM community_identities WHERE subject=?",
                    (flow["subject"],)).fetchone():
                raise CommunityIdentityConflict("community identity conflict")

            email = str(flow["email"] or "").strip().lower()
            if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise CommunityEmailConflict("community email already registered")

            rec = c.execute(
                "SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
            if (rec is None or rec["type"] != "invitation" or
                    rec["status"] != "unused" or
                    (rec["expires_at"] and rec["expires_at"] < now)):
                raise CommunityInviteError("invalid invite code")

            cur = c.execute(
                "INSERT INTO users(email,password_hash,role,status,group_id,"
                "email_verified_at,inviter_id,aff_code,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (email, "oauth-only", "user", US_ACTIVE, group_id,
                 now, None, aff_code, now))
            user_id = cur.lastrowid
            c.execute(
                "INSERT INTO community_identities("
                "subject,user_id,username,name,email,bound_at,last_login_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (flow["subject"], user_id, flow["username"], flow["name"],
                 flow["email"], now, now))
            changed = c.execute(
                "UPDATE redeem_codes SET status='used',used_by=?,used_at=? "
                "WHERE code=? AND type='invitation' AND status='unused' "
                "AND (expires_at=0 OR expires_at>=?)",
                (user_id, now, code, now)).rowcount
            if changed != 1:
                raise CommunityInviteError("invalid invite code")
            self._consume_community_flow(c, flow_id, now)
            c.commit()
            return {"user_id": user_id, "inviter_id": None,
                    "aff_code": aff_code}
        except sqlite3.IntegrityError as e:
            if c.in_transaction:
                c.rollback()
            msg = str(e)
            if "users.aff_code" in msg:
                raise CommunityAffCodeConflict("invite allocation conflict") from e
            if "users.email" in msg:
                raise CommunityEmailConflict(
                    "community email already registered") from e
            raise CommunityIdentityConflict("community identity conflict") from e
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise

    def list_users(self, limit=100, offset=0):
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()]

    def search_users(self, q=None, group_id=None, status=None, role=None,
                     limit=100, offset=0):
        """管理台的用户检索。返回 (rows, total)。

        q 同时匹配邮箱与昵称的子串、返佣码的精确值,以及纯数字时的用户 id ——
        站长手里通常只有其中一样:用户报障给的是邮箱,流水里看到的是 id,
        邀请纠纷拿的是返佣码。三样都能直接贴进同一个框。
        LIKE 的 % 与 _ 要转义,否则输入一个 _ 会匹配所有人。"""
        where, args = [], []
        q = (q or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clause = ("(email LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' "
                      "OR aff_code = ?")
            args += [like, like, q.upper()]
            if q.isdigit():
                clause += " OR id = ?"
                args.append(int(q))
            where.append(clause + ")")
        if group_id is not None:
            where.append("group_id = ?")
            args.append(int(group_id))
        if status:
            where.append("status = ?")
            args.append(status)
        if role:
            where.append("role = ?")
            args.append(role)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM users" + sql_where, args).fetchone()["n"]
            rows = c.execute(
                "SELECT * FROM users" + sql_where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        return [self._row(r) for r in rows], total

    def count_users_since(self, since):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM users WHERE created_at>=?",
                             (since,)).fetchone()["n"]

    # ---- api_keys ----

    KEY_FIELDS = ("name", "status", "expires_at", "quota",
                  "allowed_models", "allowed_ips")

    def create_api_key(self, user_id, key, name=None, expires_at=0, **extra):
        now = int(time.time())
        allowed = {k: v for k, v in extra.items()
                   if k in self.KEY_FIELDS and k not in ("name", "expires_at")}
        allowed = {k: (json.dumps(v) if k in _JSON_COLS and not isinstance(v, str)
                       else v) for k, v in allowed.items()}
        cols = ["user_id", "key", "name", "status", "expires_at",
                "created_at"] + list(allowed)
        vals = [user_id, key, name, KS_ACTIVE, expires_at or 0,
                now] + list(allowed.values())
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO api_keys({','.join(cols)}) "
                f"VALUES({','.join('?' for _ in cols)})", vals)
            return cur.lastrowid

    def get_api_key(self, key):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM api_keys WHERE key=?", (key,)).fetchone())

    def get_api_key_by_id(self, key_id, user_id=None):
        q = "SELECT * FROM api_keys WHERE id=?"
        args = [key_id]
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        with self._conn() as c:
            return self._row(c.execute(q, args).fetchone())

    def list_api_keys(self, user_id):
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM api_keys WHERE user_id=? ORDER BY id DESC",
                (user_id,)).fetchall()]

    def update_api_key(self, key_id, user_id, **fields):
        allowed = {k: v for k, v in fields.items() if k in self.KEY_FIELDS}
        if not allowed:
            return 0
        for k in list(allowed):
            if k in _JSON_COLS and not isinstance(allowed[k], str):
                allowed[k] = json.dumps(allowed[k])
        cols = ",".join(f"{k}=?" for k in allowed)
        vals = list(allowed.values()) + [key_id, user_id]
        with self._conn() as c:
            return c.execute(
                f"UPDATE api_keys SET {cols} WHERE id=? AND user_id=?",
                vals).rowcount

    def delete_api_key(self, key_id, user_id):
        with self._conn() as c:
            return c.execute(
                "DELETE FROM api_keys WHERE id=? AND user_id=?",
                (key_id, user_id)).rowcount

    def touch_api_key(self, key_id, spent=0.0):
        """结算后回写:最后使用时间 + 累加已用额度。
        spent 用 SQL 里累加而非读改写,避免并发结算互相覆盖。"""
        with self._conn() as c:
            c.execute(
                "UPDATE api_keys SET last_used_at=?, "
                "used_quota=COALESCE(used_quota,0)+? WHERE id=?",
                (int(time.time()), float(spent or 0.0), key_id))

    def api_key_usage(self, key_id, since=0):
        """单个密钥的用量汇总(请求数/tokens/花费),供密钥列表展示。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
                "COALESCE(SUM(actual_cost),0) cost "
                "FROM usage_logs WHERE api_key_id=? AND created_at>=?",
                (key_id, since)).fetchone()
            return {"requests": r["reqs"], "tokens": r["tokens"], "cost": r["cost"]}

    def api_key_usage_map(self, user_id, since=0):
        """一次 GROUP BY 拿到该用户所有密钥的用量,替代逐把密钥查一次。
        返回 {api_key_id: {requests, tokens, cost}};没有用量的密钥不出现,
        调用方按缺省 0 处理。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT api_key_id, COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
                "COALESCE(SUM(actual_cost),0) cost "
                "FROM usage_logs WHERE user_id=? AND created_at>=? "
                "AND api_key_id IS NOT NULL GROUP BY api_key_id",
                (user_id, since)).fetchall()
        return {r["api_key_id"]: {"requests": r["reqs"], "tokens": r["tokens"],
                                  "cost": r["cost"]} for r in rows}

    # ---- usage_logs ----

    def add_usage(self, user_id, api_key_id=None, channel=None, model=None,
                  input_tokens=0, output_tokens=0, cost=0.0, actual_cost=0.0,
                  billing_mode="free", pricing_snapshot=None, stream=0,
                  duration_ms=0, token_source="estimate"):
        now = int(time.time())
        snap = (json.dumps(pricing_snapshot, ensure_ascii=False)
                if pricing_snapshot is not None else None)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO usage_logs(user_id,api_key_id,channel,model,"
                "input_tokens,output_tokens,cost,actual_cost,billing_mode,"
                "pricing_snapshot,token_source,stream,duration_ms,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, api_key_id, channel, model, input_tokens,
                 output_tokens, cost, actual_cost, billing_mode, snap,
                 token_source, 1 if stream else 0, duration_ms, now))
            return cur.lastrowid

    def usage_overview(self, user_id, since=0):
        """概览用的一次性聚合:请求数、输入/输出 token、原价与实扣、耗时。

        缓存 token 未落列,只能由 pricing_snapshot.total_ctx 反推
        (total_ctx = input + cache_write + cache_read,不含 output)。
        没有 snapshot 的行 json_extract 返回 NULL,max(NULL,0) 也是 NULL,
        被 SUM 跳过 —— 这正是想要的语义:承认不知道,而不是当成 0。
        cache_rows 报有多少行能算出缓存,前端据此判断该数是否完整。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens),0) in_tok, "
                "COALESCE(SUM(output_tokens),0) out_tok, "
                "COALESCE(SUM(MAX(json_extract(pricing_snapshot,'$.total_ctx') "
                "  - input_tokens, 0)),0) cache_tok, "
                "SUM(CASE WHEN json_extract(pricing_snapshot,'$.total_ctx') "
                "  IS NOT NULL THEN 1 ELSE 0 END) cache_rows, "
                "COALESCE(SUM(cost),0) cost, "
                "COALESCE(SUM(actual_cost),0) actual_cost, "
                "COALESCE(SUM(duration_ms),0) dur_ms, "
                "SUM(CASE WHEN duration_ms>0 THEN 1 ELSE 0 END) dur_rows, "
                "MIN(created_at) first_at, MAX(created_at) last_at "
                "FROM usage_logs WHERE user_id=? AND created_at>=?",
                (user_id, since)).fetchone()
        return {
            "requests": r["reqs"],
            "input_tokens": r["in_tok"],
            "output_tokens": r["out_tok"],
            "cache_tokens": r["cache_tok"],
            "cache_rows": r["cache_rows"] or 0,
            "cost": r["cost"],
            "actual_cost": r["actual_cost"],
            "duration_ms": r["dur_ms"],
            "duration_rows": r["dur_rows"] or 0,
            "first_at": r["first_at"],
            "last_at": r["last_at"],
        }

    def usage_daily(self, user_id, days=14):
        """按本地日历日分桶的近 N 天用量,供概览折线/柱状图。

        用 date(...,'localtime') 而不是「now - k*86400」滚动窗口:图表的 X 轴
        是日期标签,滚动窗口切出来的桶边界落在半天上,标签会对不上。
        只返回有记录的日子,补零交给前端(它才知道要画多少格)。

        tokens 与 usage_overview 口径一致:含由 total_ctx 反推的缓存 token,
        否则图表「合计」会小于卡片上的「累计 Token」,看着像有一处算错。"""
        since = int(time.time()) - max(1, days) * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT date(created_at,'unixepoch','localtime') d, "
                "COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens),0) in_tok, "
                "COALESCE(SUM(output_tokens),0) out_tok, "
                "COALESCE(SUM(MAX(json_extract(pricing_snapshot,'$.total_ctx') "
                "  - input_tokens, 0)),0) cache_tok, "
                "COALESCE(SUM(actual_cost),0) cost "
                "FROM usage_logs WHERE user_id=? AND created_at>=? "
                "GROUP BY d ORDER BY d", (user_id, since)).fetchall()
        return [{"date": r["d"], "requests": r["reqs"],
                 "input_tokens": r["in_tok"], "output_tokens": r["out_tok"],
                 "cache_tokens": r["cache_tok"],
                 "tokens": r["in_tok"] + r["out_tok"] + r["cache_tok"],
                 "cost": r["cost"]}
                for r in rows]

    def usage_by_model(self, user_id, since=0, limit=8):
        """按模型聚合(请求数、token、实扣),供概览的模型占比图。
        token 含反推的缓存,与 usage_overview / usage_daily 同口径。
        limit 之外的并进「其他」由调用方决定,这里只排序截断。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT model, COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
                "COALESCE(SUM(MAX(json_extract(pricing_snapshot,'$.total_ctx') "
                "  - input_tokens, 0)),0) cache_tok, "
                "COALESCE(SUM(actual_cost),0) cost "
                "FROM usage_logs WHERE user_id=? AND created_at>=? "
                "GROUP BY model ORDER BY tokens DESC", (user_id, since)).fetchall()
        out = [{"model": r["model"] or "—", "requests": r["reqs"],
                "tokens": r["tokens"] + r["cache_tok"], "cost": r["cost"]}
               for r in rows]
        out.sort(key=lambda x: x["tokens"], reverse=True)
        return out[:limit], out[limit:]

    def usage_count(self, user_id, since):
        """某用户自 since 起的请求数(用于日/周/月 requests 限额)。"""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM usage_logs WHERE user_id=? AND created_at>=?",
                (user_id, since)).fetchone()["n"]

    def usage_tokens(self, user_id, since):
        """某用户自 since 起的 token 用量(用于 tokens 限额)。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) t "
                "FROM usage_logs WHERE user_id=? AND created_at>=?",
                (user_id, since)).fetchone()
            return r["t"] or 0

    def usage_summary(self, user_id, since):
        """仪表盘聚合:总请求/总 token/总 cost + 按模型分布。"""
        with self._conn() as c:
            tot = c.execute(
                "SELECT COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens),0) in_tok, "
                "COALESCE(SUM(output_tokens),0) out_tok, "
                "COALESCE(SUM(cost),0) cost "
                "FROM usage_logs WHERE user_id=? AND created_at>=?",
                (user_id, since)).fetchone()
            by_model = c.execute(
                "SELECT model, COUNT(*) reqs, "
                "COALESCE(SUM(input_tokens+output_tokens),0) tokens "
                "FROM usage_logs WHERE user_id=? AND created_at>=? "
                "GROUP BY model ORDER BY reqs DESC",
                (user_id, since)).fetchall()
            return {
                "requests": tot["reqs"],
                "input_tokens": tot["in_tok"],
                "output_tokens": tot["out_tok"],
                "cost": tot["cost"],
                "by_model": [dict(r) for r in by_model],
            }

    def recent_usage(self, user_id, limit=50):
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM usage_logs WHERE user_id=? "
                "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()]

    # ---- usage_logs · 检索与全站聚合(管理台日志页 / 看板 / 用户侧分页) ----

    @staticmethod
    def _usage_where(user_id=None, api_key_id=None, model=None, channel=None,
                     since=0, until=None, end_reason=None):
        """过滤条件拼装,检索与计数共用同一份,否则「共 N 条」和列表对不上。

        model 支持 prefix* 通配(与定价、白名单同一写法);end_reason 存在
        pricing_snapshot 的 JSON 里,用 json_extract 过滤 —— 列上没有索引,
        这条只在管理员点了「只看异常」时才加。"""
        where, args = ["1=1"], []
        if user_id is not None:
            where.append("l.user_id=?")
            args.append(int(user_id))
        if api_key_id is not None:
            where.append("l.api_key_id=?")
            args.append(int(api_key_id))
        if model:
            if model.endswith("*"):
                where.append("l.model LIKE ? ESCAPE '\\'")
                args.append(model[:-1].replace("%", "\\%").replace("_", "\\_") + "%")
            else:
                where.append("l.model=?")
                args.append(model)
        if channel:
            where.append("l.channel=?")
            args.append(channel)
        if since:
            where.append("l.created_at>=?")
            args.append(int(since))
        if until:
            where.append("l.created_at<?")
            args.append(int(until))
        if end_reason == "failed":
            # 「只看异常」:done 之外的一切。没有快照的老行视同 done
            where.append("COALESCE(json_extract(l.pricing_snapshot,'$.end_reason'),'done')!='done'")
        elif end_reason:
            where.append("json_extract(l.pricing_snapshot,'$.end_reason')=?")
            args.append(end_reason)
        return " AND ".join(where), args

    def query_usage(self, limit=50, offset=0, with_user=False, **filters):
        """按条件检索用量日志,新→旧。返回 (rows, total)。

        with_user 时带上邮箱与密钥名:管理台的日志页每行都要显示「谁」,
        再让前端拿 user_id 去查一遍用户表就是 N+1。用户侧不需要,少两个 JOIN。"""
        where, args = self._usage_where(**filters)
        cols = "l.*"
        joins = ""
        if with_user:
            cols += ", u.email AS email, k.name AS key_name"
            joins = (" LEFT JOIN users u ON u.id=l.user_id"
                     " LEFT JOIN api_keys k ON k.id=l.api_key_id")
        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) n FROM usage_logs l WHERE {where}", args).fetchone()["n"]
            rows = c.execute(
                f"SELECT {cols} FROM usage_logs l{joins} WHERE {where} "
                "ORDER BY l.id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)]).fetchall()
        return [self._row(r) for r in rows], total

    def usage_totals(self, **filters):
        """一组过滤条件下的合计:请求、token(含由 total_ctx 反推的缓存)、原价/实扣、
        活跃用户数、平均耗时与首字、长上下文次数,以及按 end_reason 的分布
        (done 之外的每一种都是一类要盯的异常)。

        过滤条件与 query_usage 同一份 _usage_where:用户侧「使用记录」的统计卡
        与下面的分页列表必须是同一个集合,否则卡片说 40 次、翻页翻出 43 条。"""
        where, args = self._usage_where(**filters)
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) reqs, COUNT(DISTINCT l.user_id) users, "
                "COALESCE(SUM(l.input_tokens),0) in_tok, "
                "COALESCE(SUM(l.output_tokens),0) out_tok, "
                "COALESCE(SUM(MAX(json_extract(l.pricing_snapshot,'$.total_ctx') "
                "  - l.input_tokens, 0)),0) cache_tok, "
                "COALESCE(SUM(l.cost),0) cost, COALESCE(SUM(l.actual_cost),0) actual_cost, "
                "COALESCE(SUM(l.duration_ms),0) dur_ms, "
                "SUM(CASE WHEN l.duration_ms>0 THEN 1 ELSE 0 END) dur_rows, "
                "AVG(json_extract(l.pricing_snapshot,'$.frt_ms')) frt_ms, "
                "SUM(CASE WHEN json_extract(l.pricing_snapshot,'$.tier')='long' "
                "  THEN 1 ELSE 0 END) long_rows "
                f"FROM usage_logs l WHERE {where}", args).fetchone()
            reasons = c.execute(
                "SELECT COALESCE(json_extract(l.pricing_snapshot,'$.end_reason'),'done') er, "
                f"COUNT(*) n FROM usage_logs l WHERE {where} GROUP BY er", args).fetchall()
        ends = {x["er"]: x["n"] for x in reasons}
        return {
            "requests": r["reqs"], "users": r["users"],
            "input_tokens": r["in_tok"], "output_tokens": r["out_tok"],
            "cache_tokens": r["cache_tok"],
            "tokens": r["in_tok"] + r["out_tok"] + r["cache_tok"],
            "cost": r["cost"], "actual_cost": r["actual_cost"],
            "avg_ms": (r["dur_ms"] / r["dur_rows"]) if r["dur_rows"] else 0,
            "frt_ms": r["frt_ms"],
            "long": r["long_rows"] or 0,
            "failed": sum(n for k, n in ends.items() if k != "done"),
            "end_reasons": ends,
        }

    def usage_daily_all(self, days=14):
        """全站按本地日历日的日序列(请求、token、实扣、活跃用户)。补零交给前端。"""
        since = int(time.time()) - max(1, days) * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT date(created_at,'unixepoch','localtime') d, COUNT(*) reqs, "
                "COUNT(DISTINCT user_id) users, "
                "COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
                "COALESCE(SUM(actual_cost),0) cost "
                "FROM usage_logs WHERE created_at>=? GROUP BY d ORDER BY d",
                (since,)).fetchall()
        return [{"date": r["d"], "requests": r["reqs"], "users": r["users"],
                 "tokens": r["tokens"], "cost": r["cost"]} for r in rows]

    def usage_group_by(self, column, since=0, until=None, limit=20):
        """按 channel / model / user_id 聚合。column 只接受这三个字面量 ——
        它拼进 SQL,不能来自请求参数。"""
        assert column in ("channel", "model", "user_id")
        where, args = self._usage_where(since=since, until=until)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT l.{column} AS k, COUNT(*) reqs, "
                "COALESCE(SUM(l.input_tokens),0) in_tok, "
                "COALESCE(SUM(l.output_tokens),0) out_tok, "
                "COALESCE(SUM(l.cost),0) cost, COALESCE(SUM(l.actual_cost),0) actual_cost "
                f"FROM usage_logs l WHERE {where} GROUP BY l.{column} "
                "ORDER BY actual_cost DESC, reqs DESC LIMIT ?",
                args + [int(limit)]).fetchall()
        return [{"key": r["k"], "requests": r["reqs"],
                 "input_tokens": r["in_tok"], "output_tokens": r["out_tok"],
                 "tokens": r["in_tok"] + r["out_tok"],
                 "cost": r["cost"], "actual_cost": r["actual_cost"]} for r in rows]

    def users_brief(self, ids):
        """id → {email, display_name}。给聚合结果里的 user_id 配上能看的名字。"""
        ids = [int(i) for i in ids if i is not None]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id,email,display_name,status FROM users WHERE id IN ({marks})",
                ids).fetchall()
        return {r["id"]: {"email": r["email"], "display_name": r["display_name"],
                          "status": r["status"]} for r in rows}

    def ledger_by_reason(self, since=0, until=None):
        """全站流水按 reason 求和:recharge/redeem 是进账,usage 是消费(负),
        signup/checkin/affiliate/admin 是站长的营销支出。看板据此算「收了多少、
        送了多少、被消费了多少」。"""
        q = "SELECT reason, COALESCE(SUM(amount),0) s, COUNT(*) n FROM credit_ledger WHERE created_at>=?"
        args = [int(since)]
        if until:
            q += " AND created_at<?"
            args.append(int(until))
        with self._conn() as c:
            rows = c.execute(q + " GROUP BY reason", args).fetchall()
        return {r["reason"]: {"amount": r["s"], "count": r["n"]} for r in rows}

    def orders_completed(self, since=0, until=None):
        """真金白银:已完成订单的笔数、美元额度、人民币实收。"""
        q = ("SELECT COUNT(*) n, COALESCE(SUM(amount),0) usd, "
             "COALESCE(SUM(pay_amount_cny),0) cny FROM orders "
             "WHERE status='completed' AND completed_at>=?")
        args = [int(since)]
        if until:
            q += " AND completed_at<?"
            args.append(int(until))
        with self._conn() as c:
            r = c.execute(q, args).fetchone()
        return {"count": r["n"], "usd": r["usd"], "cny": r["cny"]}

    def balance_totals(self):
        """所有用户余额之和 —— 这是站长对用户的负债,看板上必须有这个数。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN balance>0 THEN balance ELSE 0 END),0) owed, "
                "COALESCE(SUM(CASE WHEN balance<0 THEN balance ELSE 0 END),0) overdrawn, "
                "COUNT(*) users FROM users").fetchone()
        return {"owed": r["owed"], "overdrawn": r["overdrawn"], "users": r["users"]}

    # ---- email_verifications ----

    def add_email_verification(self, user_id, email, code, ttl=900):
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO email_verifications(user_id,email,code,expires_at,created_at) "
                "VALUES(?,?,?,?,?)", (user_id, email, code, now + ttl, now))
            return cur.lastrowid

    def get_latest_verification(self, user_id, email):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM email_verifications WHERE user_id=? AND email=? "
                "ORDER BY id DESC LIMIT 1", (user_id, email)).fetchone())

    def get_latest_verification_by_user(self, user_id):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM email_verifications WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone())

    def mark_verification_used(self, verif_id):
        with self._conn() as c:
            c.execute("UPDATE email_verifications SET verified_at=? WHERE id=?",
                      (int(time.time()), verif_id))

    # ---- password_resets ----

    def create_password_reset(self, user_id, token_hash, ttl):
        """发一张新令牌,同时作废该用户之前没用掉的:用户连点两次「发送」,
        只有最后一封邮件里的链接有效,别让早先那封也能改密码。"""
        now = int(time.time())
        with self._conn() as c:
            c.execute("UPDATE password_resets SET used_at=? "
                      "WHERE user_id=? AND used_at=0", (now, user_id))
            c.execute("INSERT INTO password_resets(user_id,token_hash,expires_at,created_at) "
                      "VALUES(?,?,?,?)", (user_id, token_hash, now + int(ttl), now))

    def consume_password_reset(self, token_hash, now=None):
        """原子占用:只有「未用且未过期」的那一行能被翻成已用。返回 user_id 或 None。
        条件更新而不是先查再改 —— 同一链接被并发点两次,只能有一次成功。"""
        now = int(now if now is not None else time.time())
        with self._conn() as c:
            row = c.execute("SELECT id,user_id FROM password_resets WHERE token_hash=?",
                            (token_hash,)).fetchone()
            if row is None:
                return None
            ok = c.execute(
                "UPDATE password_resets SET used_at=? "
                "WHERE id=? AND used_at=0 AND expires_at>?",
                (now, row["id"], now)).rowcount
            return row["user_id"] if ok else None

    def set_password(self, user_id, password_hash, now=None):
        """改密的唯一入口:同时推进 password_changed_at,让旧会话作废。"""
        now = int(now if now is not None else time.time())
        with self._conn() as c:
            c.execute("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
                      (password_hash, now, user_id))

    # ---- settings(运行时可改配置,存库) ----

    def get_setting(self, key, default=None):
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except Exception:
            return r["value"]

    def set_setting(self, key, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def delete_setting(self, key):
        """删掉一条运行时设置,让它回落到环境变量默认值。

        没有这个,「未设置」在库里就无法表达 —— 管理员想恢复默认只能往框里敲空格,
        而空串在 present-wins 语义下是「明确设为空」,两个意图会被混成一个。
        """
        with self._conn() as c:
            return c.execute("DELETE FROM settings WHERE key=?", (key,)).rowcount

    def all_settings(self):
        with self._conn() as c:
            rows = c.execute("SELECT key,value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out

    # ---- 公告已读状态(正文在 settings,已读按用户落表) ----

    def announcement_reads(self, user_id):
        """返回 {announcement_id: read_at}。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT announcement_id, read_at FROM announcement_reads "
                "WHERE user_id=?", (user_id,)).fetchall()
        return {r["announcement_id"]: r["read_at"] for r in rows}

    def mark_announcements_read(self, user_id, ann_ids, when=None):
        """标记已读。重复标记刷新 read_at —— 公告改过之后再读一次要能重新盖住
        新的 updated_at,否则红点永远消不掉。返回写入条数。"""
        ids = [str(a) for a in (ann_ids or []) if a]
        if not ids:
            return 0
        now = int(when if when is not None else time.time())
        conn = self._conn()
        with conn:
            conn.executemany(
                "INSERT INTO announcement_reads(announcement_id,user_id,read_at) "
                "VALUES(?,?,?) ON CONFLICT(announcement_id,user_id) "
                "DO UPDATE SET read_at=excluded.read_at",
                [(i, user_id, now) for i in ids])
        return len(ids)

    def drop_announcement_reads(self, ann_id):
        """公告被删除时清掉它的已读记录,不留孤儿行。"""
        with self._conn() as c:
            return c.execute("DELETE FROM announcement_reads WHERE announcement_id=?",
                             (str(ann_id),)).rowcount

    def announcement_read_count(self, ann_id):
        """读过这条公告的人数(管理端列表用)。"""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM announcement_reads WHERE announcement_id=?",
                (str(ann_id),)).fetchone()["n"]

    # ---- credit_ledger(余额变动唯一入口,幂等由 idem_key 保证) ----

    def apply_credit(self, user_id, amount, reason, idem_key, meta=None):
        """单事务写流水 + 增量更新余额。返回 (ledger_row, created:bool)。
        idem_key 已存在则不重复记账,返回既有记录与 created=False。"""
        now = int(time.time())
        conn = self._conn()
        with conn:  # BEGIN..COMMIT,异常回滚
            # 先拿写锁再查幂等键。原先是 SELECT → UPDATE balance → INSERT ledger,
            # 两个并发请求都可能先查到不存在,输掉唯一键竞争的那次虽会回滚加钱,
            # 却把 IntegrityError 抛到 HTTP 层。签到按钮快速双击就会偶发 500。
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM credit_ledger WHERE idem_key=?", (idem_key,)).fetchone()
            if existing is not None:
                return self._row(existing), False
            # 余额与累计消费同步更新:负额计入 total_spent
            conn.execute(
                "UPDATE users SET balance = COALESCE(balance,0) + ?, "
                "total_spent = COALESCE(total_spent,0) + ? WHERE id=?",
                (amount, -amount if amount < 0 else 0, user_id))
            row = conn.execute("SELECT balance FROM users WHERE id=?",
                               (user_id,)).fetchone()
            balance_after = row["balance"] if row else None
            cur = conn.execute(
                "INSERT INTO credit_ledger(user_id,amount,reason,idem_key,meta,"
                "balance_after,created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, amount, reason, idem_key,
                 json.dumps(meta, ensure_ascii=False) if meta is not None else None,
                 balance_after, now))
            created = conn.execute("SELECT * FROM credit_ledger WHERE id=?",
                                   (cur.lastrowid,)).fetchone()
        return self._row(created), True

    def get_ledger_by_idem(self, idem_key):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM credit_ledger WHERE idem_key=?", (idem_key,)).fetchone()
        return self._row(row)

    def list_ledger(self, user_id, limit=100, offset=0, reason=None):
        q = "SELECT * FROM credit_ledger WHERE user_id=?"
        args = [user_id]
        if reason:
            q += " AND reason=?"
            args.append(reason)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def ledger_sum(self, user_id, reason=None, since=0):
        q = ("SELECT COALESCE(SUM(amount),0) s FROM credit_ledger "
             "WHERE user_id=? AND created_at>=?")
        args = [user_id, since]
        if reason:
            q += " AND reason=?"
            args.append(reason)
        with self._conn() as c:
            return c.execute(q, args).fetchone()["s"] or 0.0

    # ---- redeem_codes ----

    def create_code(self, code, value, type="balance", expires_at=0, notes=None):
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO redeem_codes(code,type,value,status,expires_at,notes,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, type, value, "unused", expires_at, notes, now))
            return cur.lastrowid

    def get_code(self, code):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone())

    def claim_code(self, code, user_id):
        """原子占用:仅当 status='unused' 时置为 used。返回是否抢到(rowcount==1)。

        user_id 允许为 None —— 注册用的邀请码必须先占用再建号(反过来的话并发的
        两个注册都能通过校验,一张一次性码进两个人),占用那一刻还没有用户 id,
        建号之后再用 bind_code_user 回填。
        """
        now = int(time.time())
        with self._conn() as c:
            return c.execute(
                "UPDATE redeem_codes SET status='used', used_by=?, used_at=? "
                "WHERE code=? AND status='unused'",
                (user_id, now, code)).rowcount == 1

    def bind_code_user(self, code, user_id):
        """给已占用但还没绑人的码补上使用者。见 claim_code 的两步说明。"""
        with self._conn() as c:
            return c.execute(
                "UPDATE redeem_codes SET used_by=? "
                "WHERE code=? AND used_by IS NULL", (user_id, code)).rowcount

    def list_codes(self, status=None, type=None, limit=100, offset=0):
        q, args = "SELECT * FROM redeem_codes WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if type:
            q += " AND type=?"
            args.append(type)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def disable_code(self, code):
        with self._conn() as c:
            return c.execute(
                "UPDATE redeem_codes SET status='disabled' "
                "WHERE code=? AND status='unused'", (code,)).rowcount

    # ---- orders ----

    def create_order(self, user_id, out_trade_no, amount, provider,
                     expires_at=0, recharge_code=None):
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders(user_id,out_trade_no,amount,provider,status,"
                "recharge_code,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (user_id, out_trade_no, amount, provider, "pending",
                 recharge_code, expires_at, now))
            return cur.lastrowid

    def fail_order(self, out_trade_no):
        """建单后渠道发起支付失败:标 failed,别留无主 pending 单。
        仍允许被迟到的合法回调救回(mark_order_paid 收 failed)。"""
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='failed' "
                "WHERE out_trade_no=? AND status='pending'",
                (out_trade_no,)).rowcount == 1

    def set_order_pay_amount_cny(self, out_trade_no, cny):
        """冻结应收人民币。渠道算出金额后回写一次,此后不再变 —— 汇率改了也不动在途单。"""
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET pay_amount_cny=? WHERE out_trade_no=?",
                (float(cny), out_trade_no)).rowcount == 1

    def get_order(self, order_id):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def get_order_by_trade_no(self, out_trade_no):
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM orders WHERE out_trade_no=?", (out_trade_no,)).fetchone())

    def mark_order_paid(self, out_trade_no, payment_trade_no=None, pay_amount=None):
        """条件更新:仅 pending/expired/failed → paid。rowcount==0 表示已处理过。"""
        now = int(time.time())
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='paid', payment_trade_no=?, pay_amount=?, "
                "paid_at=? WHERE out_trade_no=? AND status IN ('pending','expired','failed')",
                (payment_trade_no, pay_amount, now, out_trade_no)).rowcount == 1

    def acquire_order_lease(self, out_trade_no):
        """paid → recharging 并递增 lease_version(乐观锁)。返回新版本号或 None。"""
        with self._conn() as c:
            ok = c.execute(
                "UPDATE orders SET status='recharging', lease_version=lease_version+1 "
                "WHERE out_trade_no=? AND status='paid'", (out_trade_no,)).rowcount
            if not ok:
                return None
            r = c.execute("SELECT lease_version FROM orders WHERE out_trade_no=?",
                          (out_trade_no,)).fetchone()
            return r["lease_version"] if r else None

    def complete_order(self, out_trade_no, lease_version):
        """recharging + 版本匹配 → completed。防并发下重复完成。"""
        now = int(time.time())
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='completed', completed_at=? "
                "WHERE out_trade_no=? AND status='recharging' AND lease_version=?",
                (now, out_trade_no, lease_version)).rowcount == 1

    def list_orders(self, user_id=None, status=None, limit=100, offset=0):
        q, args = "SELECT * FROM orders WHERE 1=1", []
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def expire_orders(self, before_ts):
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='expired' "
                "WHERE status='pending' AND expires_at>0 AND expires_at<?",
                (before_ts,)).rowcount

    def release_stale_leases(self, before_ts):
        """recharging 是毫秒级的中间态。付款时间早于 before_ts 还停在这个状态,
        只能是 fulfil 跑到一半进程死了 —— 翻回 paid 让对账重做。lease_version 在
        重新抢占时会再加一,原先那个僵尸执行流即便还活着也过不了 complete 的版本校验。"""
        with self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='paid' "
                "WHERE status='recharging' AND paid_at IS NOT NULL AND paid_at<?",
                (before_ts,)).rowcount

    # ---- channels(数据渠道) ----

    CHANNEL_FIELDS = ("type", "base_url", "chat_path", "models", "model_map",
                      "headers", "timeout", "notes")

    def list_channels(self):
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM channels ORDER BY name").fetchall()]

    def get_channel(self, name):
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM channels WHERE name=?",
                                       (name,)).fetchone())

    def create_channel(self, name, **fields):
        now = int(time.time())
        data = {k: v for k, v in fields.items() if k in self.CHANNEL_FIELDS}
        for k in ("models", "model_map", "headers"):
            if k in data and data[k] is not None and not isinstance(data[k], str):
                data[k] = json.dumps(data[k], ensure_ascii=False)
        cols = ["name"] + list(data) + ["created_at", "updated_at"]
        vals = [name] + list(data.values()) + [now, now]
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO channels({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                vals)
            return cur.lastrowid

    def update_channel(self, name, **fields):
        data = {k: v for k, v in fields.items() if k in self.CHANNEL_FIELDS}
        if not data:
            return False
        for k in ("models", "model_map", "headers"):
            if k in data and data[k] is not None and not isinstance(data[k], str):
                data[k] = json.dumps(data[k], ensure_ascii=False)
        data["updated_at"] = int(time.time())
        sets = ",".join(f"{k}=?" for k in data)
        with self._conn() as c:
            return c.execute(f"UPDATE channels SET {sets} WHERE name=?",
                             list(data.values()) + [name]).rowcount == 1

    def delete_channel_config(self, name):
        with self._conn() as c:
            return c.execute("DELETE FROM channels WHERE name=?", (name,)).rowcount == 1

    # ---- model_pricing ----

    PRICING_FIELDS = (
        "model_pattern", "group_id", "billing_mode", "input_price", "output_price",
        "cache_read_price", "cache_write_price", "per_request_price",
        "long_threshold", "long_input_price", "long_output_price",
        "long_cache_read_price", "long_cache_write_price", "notes")

    def upsert_pricing(self, model_pattern, group_id=None, **fields):
        """按 (model_pattern, group_id) upsert。未给的字段保持原值(新建时取默认)。"""
        now = int(time.time())
        allowed = {k: v for k, v in fields.items() if k in self.PRICING_FIELDS}
        cols = ["model_pattern", "group_id"] + list(allowed)
        vals = [model_pattern, group_id] + list(allowed.values())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{k}=excluded.{k}" for k in allowed)
        updates = (updates + "," if updates else "") + "updated_at=excluded.updated_at"
        with self._conn() as c:
            c.execute(
                f"INSERT INTO model_pricing({','.join(cols)},updated_at) "
                f"VALUES({placeholders},?) "
                f"ON CONFLICT(model_pattern, IFNULL(group_id,-1)) DO UPDATE SET {updates}",
                vals + [now])
            r = c.execute(
                "SELECT id FROM model_pricing WHERE model_pattern=? "
                "AND IFNULL(group_id,-1)=IFNULL(?,-1)",
                (model_pattern, group_id)).fetchone()
            return r["id"] if r else None

    def list_pricing(self, group_id="__all__"):
        """group_id 传 None 只取全局;传具体 id 只取该组;默认取全部。"""
        if group_id == "__all__":
            q, args = "SELECT * FROM model_pricing ORDER BY model_pattern", []
        elif group_id is None:
            q, args = ("SELECT * FROM model_pricing WHERE group_id IS NULL "
                       "ORDER BY model_pattern"), []
        else:
            q, args = ("SELECT * FROM model_pricing WHERE group_id=? "
                       "ORDER BY model_pattern"), [group_id]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def delete_pricing(self, pricing_id):
        with self._conn() as c:
            return c.execute("DELETE FROM model_pricing WHERE id=?",
                             (pricing_id,)).rowcount

