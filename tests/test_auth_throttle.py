#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录/注册限速的门禁。

守三条:
  1. 登录只记失败,成功不消耗额度,成功后该邮箱的失败计数清零
  2. 不存在的邮箱也记账 —— 否则「不限速 = 邮箱不存在」能枚举出注册过的邮箱
  3. 注册按 IP 计成功次数;被邀请码挡回去的不计,超限回 429 + Retry-After

套件级 conftest 把限速器阈值置 0,这里显式设回小阈值再恢复,避免与别的模块互相影响。
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "thr.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import throttle  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class SlidingWindowTest(unittest.TestCase):
    def test_hit_reports_over_limit_only_past_limit(self):
        w = throttle.SlidingWindow(3, 60)
        t = 1000.0
        self.assertFalse(w.hit("a", now=t))
        self.assertFalse(w.hit("a", now=t + 1))
        self.assertFalse(w.hit("a", now=t + 2))
        self.assertTrue(w.hit("a", now=t + 3))          # 第 4 次超限
        self.assertGreater(w.retry_after("a", now=t + 3), 0)
        self.assertEqual(w.retry_after("b", now=t + 3), 0)  # 别的 key 不受影响

    def test_window_slides(self):
        w = throttle.SlidingWindow(2, 10)
        t = 1000.0
        w.hit("a", now=t)
        w.hit("a", now=t + 1)
        self.assertEqual(w.retry_after("a", now=t + 5), 6)   # 最早那次 t 在 t+10 过期
        self.assertEqual(w.retry_after("a", now=t + 10), 0)

    def test_limit_zero_disables(self):
        w = throttle.SlidingWindow(0, 60)
        for _ in range(50):
            self.assertFalse(w.hit("a"))
        self.assertEqual(w.retry_after("a"), 0)

    def test_reset_clears_key(self):
        w = throttle.SlidingWindow(1, 60)
        w.hit("a")
        self.assertTrue(w.hit("a"))
        w.reset("a")
        self.assertEqual(w.retry_after("a"), 0)

    def test_sweep_drops_silent_keys(self):
        """静默的 key 不能永远留在字典里 —— 长跑几个月被扫过一遍的 IP 段每个都
        占一个槽,内存只增不减。"""
        w = throttle.SlidingWindow(5, 10)
        t = 1000.0
        for k in range(100):
            w.hit(f"ip{k}", now=t)
        self.assertEqual(len(w._events), 100)
        w.hit("fresh", now=t + 11)      # 过了一个窗口,触发全表清扫
        self.assertEqual(set(w._events), {"fresh"})


class ClientIpTest(unittest.TestCase):
    class _Req:
        def __init__(self, headers, host="10.0.0.9"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    def test_proxy_headers_trusted_by_default(self):
        saved = config.TRUST_PROXY_HEADERS
        try:
            config.TRUST_PROXY_HEADERS = True
            r = self._Req({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
            self.assertEqual(throttle.client_ip(r), "1.2.3.4")
            self.assertEqual(throttle.client_ip(self._Req({"x-real-ip": "5.6.7.8"})), "5.6.7.8")
            self.assertEqual(throttle.client_ip(self._Req({})), "10.0.0.9")
            config.TRUST_PROXY_HEADERS = False
            self.assertEqual(throttle.client_ip(r), "10.0.0.9")
        finally:
            config.TRUST_PROXY_HEADERS = saved

    def test_none_request(self):
        self.assertIsNone(throttle.client_ip(None))


class AuthEndpointThrottleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "thr-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        # 首个用户免码成为 admin
        r = cls.client.post("/api/register", json={
            "email": "root@example.com", "password": "secret123"})
        assert r.status_code == 200, r.text

    def setUp(self):
        throttle.reset_all()
        self._saved = (throttle.LOGIN_IP.limit, throttle.LOGIN_EMAIL.limit,
                       throttle.REGISTER_IP.limit)
        throttle.LOGIN_IP.limit = 6
        throttle.LOGIN_EMAIL.limit = 3
        throttle.REGISTER_IP.limit = 2

    def tearDown(self):
        (throttle.LOGIN_IP.limit, throttle.LOGIN_EMAIL.limit,
         throttle.REGISTER_IP.limit) = self._saved
        throttle.reset_all()

    def _login(self, email, pw, ip="9.9.9.9"):
        return self.client.post("/api/login", json={"email": email, "password": pw},
                                headers={"X-Forwarded-For": ip})

    def test_email_lockout_after_failures_and_reset_on_success(self):
        for _ in range(3):
            self.assertEqual(self._login("root@example.com", "nope").status_code, 401)
        r = self._login("root@example.com", "nope")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.json()["detail"]["code"], "AUTH_RATE_LIMITED")
        # 封的是这个邮箱:换个 IP 打同一邮箱照样 429(否则换 IP 就能继续撞)
        self.assertEqual(self._login("root@example.com", "nope", ip="8.8.8.8").status_code, 429)
        # 对的密码也被挡 —— 锁定期间不给任何反馈差异
        self.assertEqual(self._login("root@example.com", "secret123").status_code, 429)
        throttle.LOGIN_EMAIL.reset("root@example.com")
        ok = self._login("root@example.com", "secret123")
        self.assertEqual(ok.status_code, 200, ok.text)
        # 成功登录把该邮箱的失败计数清零:之后再错 3 次才会再封
        for _ in range(3):
            self.assertEqual(self._login("root@example.com", "nope").status_code, 401)
        self.assertEqual(self._login("root@example.com", "nope").status_code, 429)

    def test_success_does_not_consume(self):
        for _ in range(10):
            self.assertEqual(self._login("root@example.com", "secret123").status_code, 200)

    def test_ip_lockout_counts_unknown_emails_too(self):
        """一个 IP 扫一批不同邮箱:每个邮箱只错一次,邮箱那道拦不住,IP 那道要拦。
        且不存在的邮箱也计数 —— 否则响应差异能枚举出哪些邮箱注册过。"""
        for k in range(6):
            r = self._login(f"ghost{k}@example.com", "x", ip="7.7.7.7")
            self.assertEqual(r.status_code, 401)
        r = self._login("ghost99@example.com", "x", ip="7.7.7.7")
        self.assertEqual(r.status_code, 429)
        # 别的 IP 不受影响
        self.assertEqual(self._login("ghost99@example.com", "x", ip="7.7.7.8").status_code, 401)

    def test_register_per_ip_counts_successes_only(self):
        h = {"X-Forwarded-For": "6.6.6.6"}
        config.REQUIRE_INVITE = True
        portal_routes.USER_DB.set_setting("require_invite", True)
        try:
            # 被邀请码挡回去的不计数
            for k in range(5):
                r = self.client.post("/api/register", json={
                    "email": f"blocked{k}@example.com", "password": "secret123"}, headers=h)
                self.assertEqual(r.status_code, 403)
        finally:
            portal_routes.USER_DB.set_setting("require_invite", False)
        for k in range(2):
            r = self.client.post("/api/register", json={
                "email": f"ok{k}@example.com", "password": "secret123"}, headers=h)
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/register", json={
            "email": "third@example.com", "password": "secret123"}, headers=h)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIsNone(portal_routes.USER_DB.get_user_by_email("third@example.com"))
        # 换 IP 正常
        r = self.client.post("/api/register", json={
            "email": "third@example.com", "password": "secret123"},
            headers={"X-Forwarded-For": "6.6.6.7"})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
