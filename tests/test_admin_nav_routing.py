"""控制台管理页的路由分支门禁。

之前踩过：`<Codes v-else/>` 会兜住所有没匹配上的 key，于是新加的号池页
被兑换码页顶替，菜单点进去看到的是「兑换码与订单」。这类错误浏览器里只表现为
「点了没反应」，所以用静态断言钉住。
"""
import re
import unittest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _admin_nav_keys():
    shared = (ROOT / "static" / "portal-shared.js").read_text(encoding="utf-8")
    block = shared.split("const ADMIN_NAV = [", 1)[1].split("];", 1)[0]
    return re.findall(r'key:\s*"([a-z0-9_-]+)"', block)


def _admin_template():
    admin = (ROOT / "static" / "portal-admin.js").read_text(encoding="utf-8")
    start = admin.index("  const Admin = {")
    return admin[start:start + 3000]


class AdminNavRoutingTest(unittest.TestCase):
    def test_every_menu_key_has_a_branch(self):
        keys = _admin_nav_keys()
        self.assertIn("pool", keys, "号池管理菜单没注册")
        template = _admin_template()
        for key in keys:
            with self.subTest(key=key):
                self.assertRegex(
                    template, r"tab === '%s'" % re.escape(key),
                    f"菜单 {key} 没有对应的渲染分支，会被兜底页吞掉")

    def test_no_catch_all_else_branch(self):
        """兜底必须是显式的未知页提示，不能是某个功能页的 v-else。"""
        template = _admin_template()
        tail = template.split("Invites v-else-if", 1)[1]
        self.assertNotRegex(tail, r"<[A-Z]\w*\s+v-else\s*/>",
                            "管理页仍有一个 v-else 兜底，未知菜单会被它顶替")
        self.assertIn("未知的管理页", tail)

    def test_pool_component_is_wired(self):
        admin = (ROOT / "static" / "portal-admin.js").read_text(encoding="utf-8")
        self.assertIn("BitPortalPool", admin)
        self.assertIn("<Pool v-else-if=\"tab === 'pool'\"/>", admin)
        portal_page = (ROOT / "portal.html").read_text(encoding="utf-8")
        self.assertIn("portal-pool.js", portal_page)


class PortalPoolScriptTest(unittest.TestCase):
    """号池页脚本的门禁：在 setup() 之外出现的标识符必须先声明。

    线上踩过两次：一次是 `components: { Load }` 用了 setup 内部才解构的 Load，
    一次是状态值自造——两次都表现为整页空白，控制台才看得到 ReferenceError。
    """

    def setUp(self):
        self.source = (ROOT / "static" / "portal-pool.js").read_text(encoding="utf-8")

    def _module_scope_prefix(self):
        """components 声明与 setup 之前的那段模块级代码。"""
        end = self.source.index("  setup(")
        return self.source[:end]

    def test_components_reference_only_module_level_names(self):
        prefix = self._module_scope_prefix()
        line = next(l for l in prefix.splitlines() if "components:" in l)
        names = re.findall(r"[A-Za-z_$][\w$]*", line.split("components:", 1)[1])
        for name in names:
            if name in {"window", "BitPortalAdmin", "AdminIntro", "Load"}:
                declared = re.search(
                    r"^\s*(const|let|var) .*\b%s\b|\.\.\.|window\.%s" % (re.escape(name), re.escape(name)),
                    prefix, re.M)
                hint = ("（Load 必须来自 window.BitPortalPages，不能是 setup 内的解构）"
                        if name == "Load" else "")
                self.assertTrue(declared, f"components 引用了未在模块级声明的 {name}{hint}")

    def test_module_level_dependencies_declared_before_use(self):
        prefix = self._module_scope_prefix()
        for name in ("Load", "AdminIntro", "BarChart", "stack", "naive"):
            with self.subTest(name=name):
                self.assertRegex(prefix, r"(?m)^\s*const .*\b%s\b" % name,
                                 f"{name} 未在模块级声明，运行时会 ReferenceError")

    def test_component_compiles_and_renders_with_production_vue(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "portal_pool_runtime.cjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-3000:])

    def test_status_tags_use_naive_supported_types(self):
        allowed = {"success", "error", "warning", "info", "default"}
        block = self.source.split("const POOL_STATUS = {", 1)[1].split("};", 1)[0]
        found = set(re.findall(r'type:\s*"([a-z]+)"', block))
        self.assertTrue(found, "没找到状态标签的类型定义")
        self.assertTrue(found <= allowed, f"状态标签用了 Naive 不认的类型: {found - allowed}")

    def test_kpi_classes_exist_in_css(self):
        css = (ROOT / "static" / "portal-console.css").read_text(encoding="utf-8")
        used = set(re.findall(r"console-tone-[a-z]+", self.source))
        for cls in used:
            with self.subTest(cls=cls):
                self.assertIn(cls, css, f"{cls} 在样式表里不存在，KPI 不会有颜色")


if __name__ == "__main__":
    unittest.main()
