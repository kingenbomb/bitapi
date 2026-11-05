"""deploy 那条入口的两条门禁 —— 都只在 Windows 手点时才犯，CI 永远抓不到。

deploy.ps1 是照 pwsh 7 写的，但 deploy.cmd 原先点的是 powershell.exe(5.1)，两处
不兼容叠在一起：

1. 5.1 把字符串当原生命令参数传时不转义里面的双引号。脚本给 `bash.exe -lc` 传的
   命令自带双引号，bash 收到的是被截断的命令**却仍然 exit 0** —— 上传报成功、
   远端一个文件都没有，再往下卡在完整性校验。所以脚本头上有一道版本闸门。
2. 5.1 读不带 BOM 的文件按系统 ANSI 解码，中文注释被 GBK 误读会吃掉行尾、把紧跟
   的一行代码并进注释。原先并掉的正好是 `$verifyFiles = ...`，Set-StrictMode 抛
   「变量未设置」；没有 BOM 的话连那道闸门自己都可能被吞掉。

CI 在 Linux 上跑 pwsh，这两条都碰不到 —— 那正是需要测试来守的理由。
"""
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BOM = b"\xef\xbb\xbf"


class PowerShellEncodingTest(unittest.TestCase):
    def test_non_ascii_ps1_files_have_utf8_bom(self):
        found = list(_ROOT.glob("*.ps1")) + list(_ROOT.glob("scripts/*.ps1"))
        self.assertTrue(found, "仓里应当有 .ps1;glob 空了说明这条门禁在空转")
        for path in found:
            raw = path.read_bytes()
            if all(b < 0x80 for b in raw):
                continue          # 纯 ASCII 的脚本两种引擎读法一致,不需要 BOM
            with self.subTest(path=path.name):
                self.assertTrue(
                    raw.startswith(_BOM),
                    f"{path.name} 含非 ASCII 字符但没有 UTF-8 BOM:"
                    "Windows PowerShell 5.1 会按 ANSI 读它,注释会吞掉后面的代码行")

    def test_deploy_script_refuses_windows_powershell(self):
        """闸门必须在脚本里,不能只在 deploy.cmd 里 —— 谁直接 powershell -File
        点它都得看到一句说得清的报错,而不是「上传成功但远端没文件」。"""
        src = (_ROOT / "deploy.ps1").read_bytes().decode("utf-8")
        self.assertIn("$PSVersionTable.PSVersion.Major -lt 6", src)
        self.assertIn("verifyFiles", src, "盯的还是那个被 ANSI 误读吞掉的赋值")

    def test_deploy_cmd_uses_pwsh_only(self):
        """deploy.cmd 是用户手点的那条入口。回落到 powershell.exe 不是兜底,
        那就是这个 bug 本身 —— 所以断言它压根不提 powershell.exe。"""
        cmd = (_ROOT / "deploy.cmd").read_text(encoding="utf-8")
        self.assertIn("deploy.ps1", cmd)
        self.assertRegex(cmd, r"(?m)^\s*pwsh\s")
        self.assertNotIn("powershell.exe -NoProfile", cmd)


class DirtyTreeGateTest(unittest.TestCase):
    """未提交的改动必须**拦住**部署,不能只警告。

    deploy 打包的是 `git archive HEAD`,工作区改动根本上不去。而旧代码照样能启动、
    照样通过 /health,于是脚本打印 "[done] Deployment is healthy" —— 人以为改的东西
    上线了,实际一个都没有。「报成功但啥也没生效」比直接失败难查得多。
    """

    def _deploy_src(self):
        return (_ROOT / "deploy.ps1").read_bytes().decode("utf-8")

    def test_dirty_tree_throws_instead_of_warning(self):
        src = self._deploy_src()
        self.assertIn("Refusing to deploy", src)
        # 光警告就是原来那个坑,所以不允许再出现裸的 Write-Warning 放过流程
        start = src.find("$dirtyFiles = @(")
        self.assertNotEqual(start, -1, "找不到脏文件检查,门禁在空转")
        block = src[start:src.find("$gitBash", start)]
        self.assertIn("throw", block)
        self.assertNotIn("Write-Warning", block)

    def test_refusal_message_reports_count(self):
        """{0} 必须被 -f 真正替换掉 —— 用 + 拼字符串时 -f 只作用于紧邻的那个字面量,
        会原样打印 "{0}"。"""
        src = self._deploy_src()
        self.assertRegex(src, r'"\)\s*-f\s+\$dirtyFiles\.Count')


if __name__ == "__main__":
    unittest.main()

