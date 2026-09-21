#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""死面扫描 —— 未使用的导入、变量、不可达分支。零容忍。

为什么门槛钉在 min-confidence 80 而不是 vulture 默认的 60:
60 会把「未使用的函数/方法/属性」也报出来,在这个仓是 125 条,其中绝大多数是
框架回调与接口实现 —— HTMLParser 的 handle_* 、socketio 的 on_* 、Adapter 子类
覆盖的 refresh_interval 、被平铺导入触发的注册函数。要用 60 就得背
一份 120 行的白名单,而一条报了没人修的门禁会让人整个不看输出,连带跳过其余全部。
收窄规则集,不是调低门槛。

80 这一档留下的是确定性判据:未使用的导入、未使用的变量、不可满足的条件。
这个仓已经栽过一次 —— 「清掉三处未使用导入」是靠人工审出来的。
门槛要往 60 走的话,先建白名单文件并逐条写明为什么是误报,再改这里。

参数刻意写死在脚本里,不放配置文件:配置文件躺着而没人调用,读数会从「缺」
变成「配了没跑」—— 那个状态更危险,因为它看起来像装了。

用法:python scripts/check_dead_code.py     (退出码非 0 即有发现)
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGETS = ["adapters", "core", "payments", "plugins", "routers", "tests",
           "config.py", "main.py", "server.py"]
MIN_CONFIDENCE = "80"


def main():
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "vulture", *TARGETS,
             "--min-confidence", MIN_CONFIDENCE],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace")
    except FileNotFoundError:
        print("[dead-code] 跑不起来:装一下 pip install vulture", file=sys.stderr)
        return 2

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if err and "No module named" in err:
        print("[dead-code] vulture 没装:pip install vulture", file=sys.stderr)
        return 2
    if out:
        print(out)
        n = len(out.splitlines())
        print(f"\n[dead-code] {n} 处死面。零容忍:删掉,或改名加下划线前缀"
              f"说明是刻意不用的参数。", file=sys.stderr)
        return 1
    if err:
        print(err, file=sys.stderr)
        return proc.returncode or 1
    print(f"[dead-code] 干净(min-confidence {MIN_CONFIDENCE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
