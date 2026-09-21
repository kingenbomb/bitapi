#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bit-api 启动入口。"""
import os
import sys

import uvicorn

import config
from core import preflight

if __name__ == "__main__":
    ok, problems = preflight.check()
    if not ok:
        print("[bitapi] 拒绝启动:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(2)
    # 默认密钥只在回环地址上才允许,所以能走到这里就说明:要么已经改了,要么只有本机能连。
    # 密钥仍是默认值时打出来提醒;改过的不打 —— 生产日志里不该出现密钥明文。
    still_default = set(preflight.default_secrets_in_use())
    print("=" * 55)
    print("  bit-api — 极简 LLM API 网关")
    print("=" * 55)
    print(f"  监听:    http://{config.HOST}:{config.PORT}")
    print("  网关 Key: " + (config.API_KEY if "BITAPI_API_KEY" in still_default
                            else "(已设置)") + "  (/v1/*, new-api 侧填)")
    print("  管理 Key: " + (config.ADMIN_KEY if "BITAPI_ADMIN_KEY" in still_default
                            else "(已设置)") + "  (/admin/*)")
    print(f"  DB:      {config.DB_PATH}")
    if still_default:
        print("  提醒:    " + ", ".join(sorted(still_default)) + " 仍是默认值,上线前必须改")
    print("=" * 55, flush=True)
    # timeout_graceful_shutdown:uvicorn 默认会**无限期**等在途请求跑完再退出。
    # 出图请求动辄 40-95 秒,于是每次部署(重启服务)systemd 都要等满
    # TimeoutStopUSec=90s 才 SIGKILL —— 停 90 秒 + 起 2 秒,期间网关整个不可用,
    # 用户拿到 Cloudflare 的 502(2026-09-15 踩过:一天三次部署累计约 3.7 分钟空窗)。
    # 收到停止信号后最多再等 10 秒,在途的生图请求会被切断 —— 但它们本来也在
    # CF 的 100 秒线附近,拿"少一次部署空窗"换"偶尔断一个请求"划算。
    try:
        uvicorn.run("server:app", host=config.HOST, port=config.PORT, workers=1,
                    timeout_graceful_shutdown=10)
    finally:
        # 光调 uvicorn 的优雅关闭还不够:生图与巡检都跑在 asyncio.to_thread 里,
        # 而 concurrent.futures 在解释器退出时会 atexit **join** 住那些非守护线程 ——
        # 一个在途的生图调用就能把停止拖到分钟级(实测 90s 被 systemd SIGKILL,
        # 改完 timeout_graceful_shutdown 仍有 60s)。uvicorn 已经关完了该关的,
        # 这里直接退,不等线程池。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
