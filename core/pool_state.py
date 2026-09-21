#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
号池单例 —— accounts 表的 DB 与取号器 Pool。

原先建在 server.py 顶层,routers/portal.py 拿不到(它不能 import server,那是
循环依赖)。管理台要给数据渠道导 key、看号池计数、测试渠道,都得碰这两个对象,
所以挪到这里;server.py 从这里 import,别处引用 server.DB / server.POOL 的地方不变。

与 core/portal_state.py 的 USER_DB 同一个文件、不同的表集合。
"""
import config
from core import db as dbmod
from core.pool import Pool

DB = dbmod.DB(config.DB_PATH)
POOL = Pool(DB, min_balance=config.MIN_BALANCE, refresh_margin=config.REFRESH_MARGIN)


def import_keys(adapter, keys):
    """把一批 API key 导进某渠道的号池,每把 key 一个账号,identity 由渠道派生
    (脱敏),同 identity 跳过。返回 (imported, skipped)。

    server.py 的 /admin/import-keys(管理密钥)与 routers/portal 的
    /api/admin/channels/{name}/keys(管理员 JWT)共用这一份 —— 两条入口的去重
    口径必须相同,否则同一把 key 从两边导会进两个账号。"""
    kid = getattr(adapter, "key_identity", None)
    if not callable(kid):
        import hashlib

        def kid(k):
            return "manual-" + hashlib.sha1(k.encode()).hexdigest()[:16]
    imported, skipped = 0, 0
    for key in keys:
        identity = kid(key)
        if DB.get_by_identity(adapter.name, identity):
            skipped += 1
            continue
        aid = DB.upsert_account(adapter.name, identity,
                                secret={"api_key": key}, status=dbmod.ST_ACTIVE)
        DB.update_account(aid, status=dbmod.ST_ACTIVE)
        imported += 1
    return imported, skipped


def parse_keys(body):
    """请求体里的 key 列表:keys=[...] 或 key="多行 / 逗号分隔"。去空白、去空项。"""
    keys = body.get("keys")
    if not keys:
        raw = body.get("key") or ""
        keys = [k for chunk in str(raw).split("\n") for k in chunk.split(",")]
    return [str(k).strip() for k in (keys or []) if k and str(k).strip()]
