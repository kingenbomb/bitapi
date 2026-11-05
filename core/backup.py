#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 在线备份 —— 用户、密钥、余额、订单、号池全在一个库文件里,它是唯一副本。

用 sqlite3 的 backup API(Connection.backup)而不是 copy 文件:WAL 模式下库由
主文件 + -wal 两个文件组成,直接拷主文件拿到的是不含最近写入的半份;backup API
在一个读事务里逐页复制,拿到的是某一时刻的完整一致快照,期间不阻塞写入。

产物是普通的 .db 文件,恢复 = 停服务、把文件改名放回去。不压缩、不加密:单机
场景下备份目录与库同盘,加密的意义是「盘丢了不泄露」,那时库本身也在同一块盘上。
真要异地,把 BITAPI_BACKUP_DIR 指到挂载的对象存储,或用 rclone 同步这个目录。

保留策略按份数不按天数:份数是确定的磁盘上界,天数遇到「服务停了三天」会一份
不留。清理只动本模块命名的文件(bitapi-*.db),目录里别的东西不碰。
"""
import os
import sqlite3
import time

import config

PREFIX = "bitapi-"
SUFFIX = ".db"


def default_dir(db_path):
    """默认放在库文件旁边的 backups/:deploy 是 tar 解压覆盖,不删多余文件,
    这个目录跟着库走而不是跟着代码走。"""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def resolve_dir():
    """当前生效的备份目录:环境变量指定的优先,否则库旁边。server 的定时任务与
    管理台的手动备份都从这里取,两处各算一份就会出现「手动备的在 A、自动备的在 B」。"""
    return config.BACKUP_DIR or default_dir(config.DB_PATH)


def run_if_due(now=None):
    """到期就做一份,返回结果;没到期返回 None。

    「到期」看备份目录里最新一份的年龄,不是进程启动以来的计时 ——
    服务重启不会把周期归零,停了几天再起来第一跳就补上。"""
    dest = resolve_dir()
    if not due(dest, config.BACKUP_INTERVAL, now=now):
        return None
    return backup_once(config.DB_PATH, dest, keep=config.BACKUP_KEEP, now=now)


def current_status():
    return status(resolve_dir(), config.BACKUP_INTERVAL, config.BACKUP_KEEP)


def list_backups(dest_dir):
    """已有备份,新→旧。只认本模块命名的文件。"""
    if not os.path.isdir(dest_dir):
        return []
    out = []
    for name in os.listdir(dest_dir):
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            continue
        path = os.path.join(dest_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "size": st.st_size,
                    "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def latest_mtime(dest_dir):
    items = list_backups(dest_dir)
    return items[0]["mtime"] if items else 0


def remove_sidecars(path):
    """删掉备份对应的 -shm/-wal。

    backup API 写出来的是完整一致的库,不该带 WAL 边车文件;目录里出现它们,
    说明有人把备份当库打开过(或旧版直接 cp 主文件)。留着既占空间,又会让
    下次「按 mtime 找最新一份」把边车误当备份。"""
    removed = 0
    for suffix in ("-shm", "-wal"):
        try:
            os.remove(path + suffix)
            removed += 1
        except OSError:
            pass
    return removed


def prune_orphan_sidecars(dest_dir):
    """清理没有对应 .db 的孤立边车文件(源备份早已被轮转删掉的那些)。"""
    if not os.path.isdir(dest_dir):
        return 0
    removed = 0
    for name in os.listdir(dest_dir):
        if not (name.startswith(PREFIX) and (name.endswith("-shm") or name.endswith("-wal"))):
            continue
        # "bitapi-20261027-024822.db-shm" -> "bitapi-20261027-024822.db"
        owner = os.path.join(dest_dir, name[:-4])
        if os.path.exists(owner):
            continue
        try:
            os.remove(os.path.join(dest_dir, name))
            removed += 1
        except OSError:
            pass
    return removed


def due(dest_dir, interval, now=None):
    """该不该备了:没有任何备份 → 该;最新一份的年龄 ≥ interval → 该。
    按「最新一份的 mtime」而不是进程内的上次时间判断:重启不会让周期归零,
    停了三天再起来第一跳就补一份,而不是再等一整个周期。"""
    if interval <= 0:
        return False
    now = time.time() if now is None else now
    return (now - latest_mtime(dest_dir)) >= interval


def backup_once(db_path, dest_dir=None, keep=7, now=None):
    """做一份备份并按 keep 清理旧份。返回 {"path","size","pruned"}。

    先写临时名再改名:备份跑到一半进程被杀,目录里留下的是 .tmp 而不是一个
    看着完整、其实缺页的 .db —— 恢复的人分不出后者,前者一眼就知道不能用。
    """
    dest_dir = dest_dir or default_dir(db_path)
    os.makedirs(dest_dir, exist_ok=True)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    final = os.path.join(dest_dir, f"{PREFIX}{stamp}{SUFFIX}")
    tmp = final + ".tmp"
    # 同一秒内连做两份(手动点两下)会撞名,加序号而不是覆盖
    n = 1
    while os.path.exists(final) or os.path.exists(tmp):
        final = os.path.join(dest_dir, f"{PREFIX}{stamp}-{n}{SUFFIX}")
        tmp = final + ".tmp"
        n += 1

    src = sqlite3.connect(db_path, timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, final)

    pruned = 0
    if keep and keep > 0:
        for item in list_backups(dest_dir)[keep:]:
            try:
                os.remove(item["path"])
                pruned += 1
            except OSError:
                pass
            remove_sidecars(item["path"])
    pruned += prune_orphan_sidecars(dest_dir)
    return {"path": final, "size": os.path.getsize(final), "pruned": pruned}


def status(dest_dir, interval, keep):
    """管理台展示用:最新一份、总份数、总大小、下一次预计时间。"""
    items = list_backups(dest_dir)
    latest = items[0] if items else None
    return {
        "dir": dest_dir,
        "interval": interval,
        "keep": keep,
        "count": len(items),
        "total_size": sum(i["size"] for i in items),
        "latest": ({"name": latest["name"], "size": latest["size"],
                    "mtime": latest["mtime"]} if latest else None),
        "next_at": (latest["mtime"] + interval) if (latest and interval > 0) else None,
    }
