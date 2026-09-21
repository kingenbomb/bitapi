#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时站点设置的门禁 —— 三态优先级 + 写入校验。

这里的每一条校验都对应一条「管理员在网页上填一个值就丢钱」的路径,
所以断言的重点不是「函数返回什么」,而是「坏值进不进得去库」。
"""
import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "ss.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))

import config  # noqa: E402
import adapters.grok  # noqa: E402,F401  (注册一个渠道,好验开关的生效面)
from core import adapter as adapter_mod  # noqa: E402
from core import site_settings as S  # noqa: E402
from core.user_db import UserDB  # noqa: E402


class SettingsResolveTest(unittest.TestCase):
    """三态:键不存在回落 env / 键存在库里说话 / 删除回到第一种。"""

    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, f"r{id(self)}.db"))
        S.bind(self.db)
        config.EPAY_KEY = "env-key"
        config.EPAY_USD_RATE = 7.2
        config.MIN_TOPUP = 1.0
        config.PAYMENT_PROVIDERS = ["epay"]
        config.CHECKIN_MIN = 0.01
        config.CHECKIN_MAX = 0.10

    def test_missing_key_falls_back_to_env(self):
        self.assertEqual(S.epay_key(), "env-key")
        self.assertFalse(S.is_from_db("epay_key"))

    def test_db_value_wins(self):
        self.db.set_setting("epay_key", "db-key")
        self.assertEqual(S.epay_key(), "db-key")
        self.assertTrue(S.is_from_db("epay_key"))

    def test_empty_string_means_stop_not_fallback(self):
        """清空 KEY 的意图是「先把收款停掉」。

        若空串被当成「未设置」而回落 env,验签会继续用界面上看不见的旧 key ——
        入账口敞开着,而管理员以为已经关了。
        """
        self.db.set_setting("epay_key", "")
        self.assertEqual(S.epay_key(), "")
        self.assertTrue(S.is_from_db("epay_key"))

    def test_delete_restores_env(self):
        self.db.set_setting("epay_key", "db-key")
        self.db.delete_setting("epay_key")
        self.assertEqual(S.epay_key(), "env-key")
        self.assertFalse(S.is_from_db("epay_key"))

    def test_text_is_stripped(self):
        """商户 PID/KEY 从后台粘过来常带尾部空白。带空格的 PID 进签名后上游拒,
        回调侧比对又不符 —— 表现是「验签失败」,排查方向全歪。"""
        self.db.set_setting("epay_pid", "  1001\n")
        self.assertEqual(S.epay_pid(), "1001")

    def test_providers_accepts_comma_string(self):
        """手写进库的可能是 "epay" 而不是 ["epay"]。逐字符迭代会把渠道闸门
        从成员判断退化成子串判断。"""
        self.db.set_setting("payment_providers", "epay, mock")
        self.assertEqual(S.payment_providers(), ["epay", "mock"])

    def test_providers_garbage_is_empty_not_iterated(self):
        self.db.set_setting("payment_providers", 42)
        self.assertEqual(S.payment_providers(), [])

    def test_number_falls_back_when_db_value_unusable(self):
        """有人手改了库写进非数字时不要崩 —— 但写入侧已经挡过一次。"""
        self.db.set_setting("epay_usd_rate", "7.2元")
        self.assertAlmostEqual(S.epay_usd_rate(), 7.2)

    def test_nan_in_db_does_not_leak_through(self):
        self.db.set_setting("epay_usd_rate", float("nan"))
        self.assertAlmostEqual(S.epay_usd_rate(), 7.2)

    def test_checkin_range_uses_db_over_env(self):
        self.db.set_setting("checkin_min", 0.1234)
        self.db.set_setting("checkin_max", 0.5678)
        self.assertAlmostEqual(S.checkin_min(), 0.1234)
        self.assertAlmostEqual(S.checkin_max(), 0.5678)


class ValidateTest(unittest.TestCase):
    """写入校验。每条都是一次「填错就丢钱」。"""

    def test_rate_zero_rejected(self):
        """汇率 0 → 应收 ¥0.00 → 金额闸门比 0 >= 0 通过 → 零成本拿全额额度。"""
        with self.assertRaises(S.SettingError):
            S.validate("epay_usd_rate", 0)

    def test_rate_negative_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("epay_usd_rate", -7.2)

    def test_rate_too_low_rejected(self):
        """0.72 比 0 更危险:不需要上游配合,用户真付 0.72 元拿全额额度,
        而应收与实付自洽,一行日志都不会报警。"""
        with self.assertRaises(S.SettingError):
            S.validate("epay_usd_rate", 0.72)

    def test_rate_nan_and_inf_rejected(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(S.SettingError):
                    S.validate("epay_usd_rate", bad)

    def test_rate_non_numeric_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("epay_usd_rate", "7.2元")

    def test_rate_normal_accepted(self):
        self.assertAlmostEqual(S.validate("epay_usd_rate", "7.25"), 7.25)

    def test_min_topup_zero_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("min_topup", 0)

    def test_min_topup_absurdly_high_rejected(self):
        """填 1000000 等于把站点收款通道整体关掉,用户只看到一句英文错误。"""
        with self.assertRaises(S.SettingError):
            S.validate("min_topup", 1_000_000)

    def test_checkin_amount_is_positive_bounded_and_four_decimals(self):
        for bad in (0, -1, float("nan"), 100.0001):
            with self.subTest(bad=bad):
                with self.assertRaises(S.SettingError):
                    S.validate("checkin_min", bad)
        self.assertEqual(S.validate("checkin_max", 0.123456), 0.1235)

    def test_mock_cannot_be_enabled_from_admin_ui(self):
        """mock 不验签也不比金额,网页上能勾就等于给站点开一个免费充值口。"""
        with self.assertRaises(S.SettingError):
            S.validate("payment_providers", ["epay", "mock"])

    def test_unknown_provider_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("payment_providers", ["epya"], known_providers={"epay"})

    def test_providers_comma_string_normalized(self):
        self.assertEqual(
            S.validate("payment_providers", "epay , ", known_providers={"epay"}),
            ["epay"])

    def test_providers_empty_allowed(self):
        """清空渠道 = 关掉支付,这是唯一正当的关闭入口。"""
        self.assertEqual(S.validate("payment_providers", []), [])

    def test_epay_url_requires_https(self):
        """商户密钥会随查单请求发到这个地址,明文不行。"""
        with self.assertRaises(S.SettingError):
            S.validate("epay_api_url", "http://pay.example.com")

    def test_epay_url_requires_scheme(self):
        """无 scheme 时 pay_url 变成相对路径(用户看到本站 404),
        而查单的 Request 抛 ValueError 被吞掉 —— 兜底静默失效。"""
        with self.assertRaises(S.SettingError):
            S.validate("epay_api_url", "pay.example.com")

    def test_epay_url_rejects_subpath(self):
        """拼接是 url + "/api.php",带子路径会拼出 404。"""
        with self.assertRaises(S.SettingError):
            S.validate("epay_api_url", "https://pay.example.com/shop")

    def test_epay_url_trailing_slash_normalized(self):
        self.assertEqual(S.validate("epay_api_url", "https://pay.example.com/"),
                         "https://pay.example.com")

    def test_site_url_allows_http_for_local_dev(self):
        self.assertEqual(S.validate("site_url", "http://127.0.0.1:8080"),
                         "http://127.0.0.1:8080")

    def test_site_url_rejects_subpath(self):
        """SITE_URL 要拼 /pay/notify/epay;带子路径回调打不进来,
        表现是「用户付了钱不到账」。"""
        with self.assertRaises(S.SettingError):
            S.validate("site_url", "https://x.com/portal")

    def test_empty_url_allowed_as_stop(self):
        self.assertEqual(S.validate("site_url", ""), "")

    def test_unknown_key_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("admin_key", "x")




class GroupDefaultUniqueTest(unittest.TestCase):
    """「默认」只能有一个。表上没有唯一约束,而 ensure_default_group() 只按名字
    找组、不看这个标记 —— 两个组同挂默认不会报错,只会让面板显示两个默认,
    而新用户实际进哪个组取决于 DEFAULT_GROUP 那个名字,对不上就很难查。"""

    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, f"g{id(self)}.db"))
        self.a = self.db.create_group(name="a", is_default=1)
        self.b = self.db.create_group(name="b")
        self.c = self.db.create_group(name="c")

    def _defaults(self):
        return sorted(g["id"] for g in self.db.list_groups() if g["is_default"])

    def test_only_one_default_after_switch(self):
        self.assertEqual(self._defaults(), [self.a])
        self.db.update_group(self.b, is_default=1)
        self.assertEqual(self._defaults(), [self.b])
        self.db.update_group(self.c, is_default=1)
        self.assertEqual(self._defaults(), [self.c])

    def test_other_fields_do_not_touch_default(self):
        self.db.update_group(self.b, status="disabled")
        self.assertEqual(self._defaults(), [self.a])

    def test_clearing_default_leaves_none(self):
        self.db.update_group(self.a, is_default=0)
        self.assertEqual(self._defaults(), [])




class PlanSubscriptionTest(unittest.TestCase):
    """时长卡的到期语义。到期在读的时候判,不靠定时任务改库 —— 定时任务漏跑
    一次用户就白用过期套餐,而漏跑本身没有任何症状。"""

    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, f"p{id(self)}.db"))
        self.base = self.db.create_group(name="base", is_default=1)
        self.day = self.db.create_group(name="day", listed=1, price=2.0,
                                        duration_hours=24,
                                        billing_policy="balance")
        self.uid = self.db.create_user(email="u@example.com",
                                       password_hash="x", aff_code="AFF1",
                                       group_id=self.base)

    def _u(self):
        return self.db.get_user(self.uid)

    def test_no_plan_falls_back_to_base_group(self):
        self.assertEqual(self.db.effective_group_id(self._u()), self.base)

    def test_active_plan_wins(self):
        self.db.update_user(self.uid, plan_group_id=self.day,
                            plan_expires_at=int(time.time()) + 3600)
        self.assertEqual(self.db.effective_group_id(self._u()), self.day)

    def test_expired_plan_falls_back_without_any_cleanup(self):
        """过期后立刻回落,库里那两个字段仍然留着(便于查历史),不需要谁去清。"""
        self.db.update_user(self.uid, plan_group_id=self.day,
                            plan_expires_at=int(time.time()) - 1)
        self.assertEqual(self.db.effective_group_id(self._u()), self.base)
        self.assertEqual(self._u()["plan_group_id"], self.day)

    def test_zero_expiry_means_unlimited(self):
        self.db.update_user(self.uid, plan_group_id=self.day, plan_expires_at=0)
        self.assertEqual(self.db.effective_group_id(self._u()), self.day)

    def test_only_listed_active_groups_are_sellable(self):
        self.assertEqual([g["name"] for g in self.db.list_listed_groups()], ["day"])
        self.db.update_group(self.day, status="disabled")
        self.assertEqual(self.db.list_listed_groups(), [])


class ChannelSwitchTest(unittest.TestCase):
    """渠道上下线 —— 校验 + 注册表生效面。

    最要紧的一条是「拼错的渠道名必须被拦住」:静默不生效意味着界面上写着已下线,
    而那个渠道照旧在卖,谁都不会去核对。
    """

    def setUp(self):
        self.db = UserDB(os.path.join(_TMP, f"ch{id(self)}.db"))
        S.bind(self.db)
        config.DISABLED_CHANNELS = []
        self.known = ("grok", "acme", "beta")
        # _DISABLED 是模块级状态,漏了这一步会污染同一进程里后面的用例
        self.addCleanup(adapter_mod.set_disabled_channels, [])

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(S.SettingError):
            S.validate("disabled_channels", ["clipfy"], known_channels=self.known)

    def test_list_is_normalized_and_deduped(self):
        got = S.validate("disabled_channels", [" grok ", "grok", "acme"],
                         known_channels=self.known)
        self.assertEqual(got, ["acme", "grok"])

    def test_comma_string_is_accepted(self):
        """手写进库的值可能是字符串(get_setting 解析不出 json 就原样返回)。"""
        self.assertEqual(
            S.validate("disabled_channels", "acme,grok", known_channels=self.known),
            ["acme", "grok"])

    def test_empty_means_all_online(self):
        self.assertEqual(S.validate("disabled_channels", [],
                                    known_channels=self.known), [])

    def test_db_value_wins_over_env(self):
        config.DISABLED_CHANNELS = ["grok"]
        self.assertEqual(S.disabled_channels(), ["grok"])
        self.db.set_setting("disabled_channels", ["grok"])
        self.assertEqual(S.disabled_channels(), ["grok"])
        self.db.delete_setting("disabled_channels")
        self.assertEqual(S.disabled_channels(), ["grok"])

    def test_registry_hides_disabled_from_users_but_not_from_maintenance(self):
        """下线是对用户下线。号池巡检与管理面板走 all_adapters(),必须照旧看得到 ——
        否则一下线就再也维护不了那批号,也没法把它开回来。"""
        every = set(adapter_mod.all_adapters())
        if not every:
            self.skipTest("no adapter registered in this run")
        victim = sorted(every)[0]
        adapter_mod.set_disabled_channels([victim])
        self.assertNotIn(victim, adapter_mod.enabled_adapters())
        self.assertIn(victim, adapter_mod.all_adapters())
        self.assertEqual(adapter_mod.disabled_channels(), [victim])

    def test_disabled_models_leave_the_routing_table(self):
        """看不到但调得通是最难查的那种不一致,所以路由表也要跟着少。"""
        ad = next((a for a in adapter_mod.all_adapters().values() if a.models), None)
        if ad is None:
            self.skipTest("no adapter with models")
        adapter_mod.set_disabled_channels([ad.name])
        routes = adapter_mod.model_to_channel()
        for m in ad.models:
            self.assertNotIn(m, routes)
        self.assertNotIn(ad.name, routes)


if __name__ == "__main__":
    unittest.main()
