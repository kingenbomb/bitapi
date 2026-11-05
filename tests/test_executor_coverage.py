#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行器覆盖不变量 —— 守 gates.txt 这份清单本身。

元规则一:没有执行器的检查不是检查,是收据。最危险的形态不是没做检查,是
「配了没跑」—— 配置躺在仓里、零执行器,看起来跟装好了一模一样。

所以这里断言三件事,而不是断言「清单存在」:
  1. 登记的路径真存在(无幽灵登记)
  2. 盘上每个 scripts/check_*.py 都被登记(无孤儿检查器)
  3. 每条非豁免登记都被 CI workflow 真调到(登记了但没人跑 == 收据)

刻意不逐个登记 tests/test_*.py:它们被 `pytest tests/` 整目录带走,新增一个不需要
接线,强行登记只会让清单变噪音。清单只管「需要自己接线」的东西。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "gates.txt")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "assurance.yml")


def _parse():
    """返回 (登记项, 豁免项),各为 [(路径, 说明)]。"""
    registered, exempt = [], []
    with open(MANIFEST, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                body = line.lstrip("#").strip()
                if body.startswith("exempt:"):
                    body = body[len("exempt:"):].strip()
                    if "|" in body:
                        path, _, who = body.partition("|")
                        exempt.append((path.strip(), who.strip()))
                continue
            if "|" not in line:
                continue
            path, _, desc = line.partition("|")
            registered.append((path.strip(), desc.strip()))
    return registered, exempt


class ExecutorCoverageTest(unittest.TestCase):
    def setUp(self):
        self.registered, self.exempt = _parse()

    def test_manifest_is_not_empty(self):
        self.assertTrue(self.registered, "gates.txt 一条登记都没有")

    def test_no_ghost_entries(self):
        """登记的路径必须真存在 —— 删掉检查器却留着登记,清单就开始骗人。"""
        for path, _desc in self.registered + self.exempt:
            with self.subTest(path=path):
                self.assertTrue(os.path.exists(os.path.join(ROOT, path)),
                                f"gates.txt 登记了不存在的路径:{path}")

    def test_every_check_script_is_registered(self):
        """盘上每个 scripts/check_*.py 都要登记 —— 否则就是写了检查器没接执行器。"""
        scripts_dir = os.path.join(ROOT, "scripts")
        on_disk = set()
        if os.path.isdir(scripts_dir):
            on_disk = {f"scripts/{n}" for n in os.listdir(scripts_dir)
                       if n.startswith("check_") and n.endswith(".py")}
        listed = {p for p, _ in self.registered} | {p for p, _ in self.exempt}
        self.assertEqual(on_disk - listed, set(),
                         "这些检查器在盘上但没登记进 gates.txt")

    def test_every_registered_entry_is_reachable_from_ci(self):
        """登记了但 CI 里没人调 == 收据。这条是整个不变量的重点。"""
        with open(WORKFLOW, encoding="utf-8") as f:
            # 整行注释不算调用 —— 注释掉的步骤不该让格子变绿
            body = "\n".join(l for l in f.read().splitlines()
                             if not l.strip().startswith("#"))
        for path, _desc in self.registered:
            if os.path.abspath(os.path.join(ROOT, path)) == os.path.abspath(WORKFLOW):
                continue  # workflow 自己就是执行器,不用自己调自己
            with self.subTest(path=path):
                trimmed = path.rstrip("/")
                hit = (trimmed in body
                       or os.path.basename(trimmed) in body
                       # pytest 跑整个目录时,目录下的文件算被覆盖
                       or any(re.search(r"pytest\s+[^\n]*\b" + re.escape(d) + r"\b", body)
                              for d in _parents(trimmed)))
                self.assertTrue(hit, f"{path} 登记了,但 CI workflow 里没有调它的步骤")

    def test_exempt_entries_name_their_executor(self):
        """豁免的判据是「下一个人能不能核对」,所以必须写出谁执行它。"""
        for path, who in self.exempt:
            with self.subTest(path=path):
                self.assertTrue(len(who) >= 10,
                                f"{path} 的豁免没写清谁执行它:{who!r}")


def _parents(path):
    """'tests/e2e/x.py' -> ['tests/e2e/x.py', 'tests/e2e', 'tests']"""
    out, cur = [], path
    while cur:
        out.append(cur)
        cur = cur.rpartition("/")[0]
    return out


if __name__ == "__main__":
    unittest.main()
