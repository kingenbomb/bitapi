#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asyncio 后台巡检器 —— 分级 + 限并发 + token 惰性验证。

抗压依据(万级实测 CPU 35%/内存 35MB):
  - 单进程 asyncio,同步 adapter 方法丢进 to_thread 池,不起海量线程
  - token 没过期不刷(看 exp),省掉大部分请求
  - Semaphore 限并发,不一次全打
  - 只巡检 active/unchecked/cooldown,dead 不再碰
    (cooldown 是网关热路径遇到超时/5xx 时落的临时状态,靠这里验活转回 active)
"""
import asyncio
import time

from core import db as dbmod
from core.adapter import CAP_BALANCE, CAP_HEALTH, CAP_REFRESH, all_adapters
from core.pool import classify_failure


def _check_one(db, adapter, acct, margin, min_balance):
    """同步检查单个账号,更新 DB 状态。跑在 to_thread 线程池里。"""
    aid = acct["id"]
    try:
        # 1) token 惰性刷新
        if adapter.has(CAP_REFRESH):
            exp = acct.get("token_exp") or 0
            if not acct.get("token") or exp - time.time() < margin:
                upd = adapter.refresh_token(acct)
                db.update_account(aid, token=upd["token"],
                                  token_exp=upd.get("token_exp", 0),
                                  meta=upd.get("meta", acct.get("meta")))
                acct["token"] = upd["token"]

        # 1.5) 强探活(可选):adapter 声明了 verify_alive 则先校验账号是否仍有效
        #      (有的上游删号后余额接口仍返回非 None,只有走一次真登录才认得出)。
        #      放 balance 之前,避免失效账号走完余额检查才暴露。
        if hasattr(adapter, "verify_alive") and not adapter.verify_alive(acct):
            db.set_status(aid, dbmod.ST_DEAD)
            return "dead"

        # 2) 余额(有余额概念的)否则 3) 存活
        if adapter.has(CAP_BALANCE):
            bal = adapter.balance(acct)
            if bal is None:
                # 「余额问不出来」不等于「这个号废了」:快照缺失、上游临时不给数都会
                # 落到这里。冷却一轮,下一轮巡检重新问 —— 判成 dead 就永久没了。
                db.set_status(aid, dbmod.ST_COOLDOWN)
                return dbmod.ST_COOLDOWN
            exhausted_status = (dbmod.ST_DEAD if getattr(adapter, "exhausted_is_dead", False)
                                else dbmod.ST_EXHAUSTED)
            # 门槛按渠道取:计量单位不是美元的渠道(比如按 credits 计价),
            # 拿全局那个按美元定的门槛判,额度不够的号会一直留在 active 里。
            floor = getattr(adapter, "min_balance", None)
            if floor is None:
                floor = min_balance
            new_status = exhausted_status if bal < floor else dbmod.ST_ACTIVE
            db.update_account(aid, balance=bal, status=new_status,
                              last_check=int(time.time()))
            return new_status
        elif adapter.has(CAP_HEALTH):
            ok = adapter.health(acct)
            st = dbmod.ST_ACTIVE if ok else dbmod.ST_DEAD
            db.set_status(aid, st)
            return st
        else:
            db.set_status(aid, dbmod.ST_ACTIVE)
            return "active"
    except Exception as e:
        # 巡检里的异常绝大多数是「这次问不到」,不是「这个号废了」,判定口径必须和
        # 网关热路径同一套(core/pool.classify_failure):判不准按 cooldown,下一轮
        # 巡检会纠正;判成 dead 就永久没了 —— dead 不再被巡检、不再被取号、没有
        # 恢复接口。踩过一次:进程 fd 耗尽(EMFILE),连出站 socket 都开不出来,
        # 原来这里的 except Exception → dead 把一整轮扫到的号全写成死号,事后抽样
        # 验证全都还能登录。见 tests/test_scheduler_scan_pool.py。
        st = (dbmod.ST_DEAD if classify_failure(e) == "terminal"
              else dbmod.ST_COOLDOWN)
        db.set_status(aid, st)
        return f"err:{e}" if st == dbmod.ST_DEAD else dbmod.ST_COOLDOWN


async def scan_once(db, concurrency, margin, min_balance, log=print, channel=None):
    """巡检一轮:active + unchecked + cooldown 账号(dead 不碰)。
    channel=None 全渠道,否则仅该渠道。返回统计。"""
    sem = asyncio.Semaphore(concurrency)
    tally = {}

    async def guarded(adapter, acct):
        async with sem:
            r = await asyncio.to_thread(_check_one, db, adapter, acct, margin, min_balance)
            tally[r] = tally.get(r, 0) + 1

    tasks = []
    adapters = all_adapters()
    if channel:
        ad = adapters.get(channel)
        adapters = {channel: ad} if ad else {}

    # 有外部号源的渠道:先增量同步一次,不进 per-account 巡检
    for name, adapter in adapters.items():
        if getattr(adapter, "exhausted_is_dead", False):
            exhausted = db.list_accounts(channel=name, status=dbmod.ST_EXHAUSTED,
                                         limit=100000)
            if exhausted:
                db.update_account_statuses(
                    name, [(acct["identity"], dbmod.ST_DEAD) for acct in exhausted])
        if hasattr(adapter, "sync_pool"):
            try:
                r = await asyncio.to_thread(adapter.sync_pool, db)
                if r.get("changed"):
                    log(f"[scan] {name} 号池同步 {r}")
            except Exception as e:
                log(f"[scan] {name} sync_pool error: {e}")

        if hasattr(adapter, "scan_pool"):
            try:
                result = await asyncio.to_thread(adapter.scan_pool, db)
                for status, count in result.items():
                    tally[status] = tally.get(status, 0) + count
                log(f"[scan] {name} 号池测活 {result}")
            except Exception as e:
                log(f"[scan] {name} scan_pool error: {e}")

    for channel, adapter in adapters.items():
        # 转发型 / 有外部号源的渠道:健康由 sync_pool 与上游自己负责,不逐号网络巡检
        # (否则万级号 = 万次 set_status,1 核直接打满)
        if getattr(adapter, "proxy", False) or hasattr(adapter, "sync_pool"):
            continue
        accts = (db.list_accounts(channel=channel, status=dbmod.ST_ACTIVE) +
                 db.list_accounts(channel=channel, status=dbmod.ST_UNCHECKED) +
                 db.list_accounts(channel=channel, status=dbmod.ST_COOLDOWN))
        for acct in accts:
            tasks.append(guarded(adapter, acct))

    if tasks:
        t0 = time.time()
        await asyncio.gather(*tasks)
        log(f"[scan] 巡检 {len(tasks)} 账号 {time.time()-t0:.1f}s -> {tally}")
    return tally


async def scanner_loop(db, interval, concurrency, margin, min_balance, log=print):
    """后台常驻:每 interval 秒巡检一轮。"""
    while True:
        try:
            await scan_once(db, concurrency, margin, min_balance, log=log)
        except Exception as e:
            log(f"[scan] error: {e}")
        await asyncio.sleep(interval)
