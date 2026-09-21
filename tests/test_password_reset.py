#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找回密码 + SMTP 发信的门禁。

守的是钱包门口的锁:
  1. 令牌一次性、有时效,重发即作废旧的;库里只有哈希
  2. 改密(自助 / 找回 / 管理员)之后,旧会话一律 401
  3. 邮箱不存在也回 ok(不可枚举);邮件没配时明说 503,不假装发了
  4. 发信失败把 SMTP 原话给管理员;测试发信端点可用
SMTP 全程 mock,不出网。
"""
import os
import re
import smtplib
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "pw.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import mailer  # noqa: E402
from core import site_settings as S  # noqa: E402
from core import throttle  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeSMTP:
    """记录 send_message 的假 SMTP。类属性 sent 跨实例累积,auth_fail 让 login 抛。"""
    sent = []
    auth_fail = False
    last_init = None

    def __init__(self, host, port, timeout=None):
        FakeSMTP.last_init = (host, port, timeout)

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, pw):
        if FakeSMTP.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def _db():
    return portal_state.USER_DB


class PasswordResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "pw-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@example.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls._seq = 0

    def setUp(self):
        FakeSMTP.sent = []
        FakeSMTP.auth_fail = False
        for k in S.SMTP_KEYS:
            _db().delete_setting(k)
        throttle.reset_all()
        self._patch = mock.patch.object(mailer.smtplib, "SMTP_SSL", FakeSMTP)
        self._patch.start()
        self._patch2 = mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP)
        self._patch2.start()
        # 管理员会话每个用例重新登:改密会让旧会话作废,类级别缓存的 token 会被
        # 前面的用例弄死 —— 这正是被测特性,所以不能把它缓存在类上。
        self.admin_h = {"Authorization": "Bearer " + self._login(
            "root@example.com", "secret123").json()["token"]}
        # 每个用例自己的用户,密码互不干扰
        type(self)._seq += 1
        self.email = f"u{self._seq}@example.com"
        r = self.client.post("/api/register", json={"email": self.email,
                                                    "password": "secret123"})
        self.assertEqual(r.status_code, 200, r.text)
        self.uid = r.json()["user_id"]

    def tearDown(self):
        self._patch.stop()
        self._patch2.stop()

    def _configure_smtp(self, security="ssl"):
        _db().set_setting("smtp_host", "smtp.example.com")
        _db().set_setting("smtp_from", "no-reply@example.com")
        _db().set_setting("smtp_user", "no-reply@example.com")
        _db().set_setting("smtp_pass", "pw")
        _db().set_setting("smtp_security", security)
        _db().set_setting("site_url", "https://ai.example.com")

    def _login(self, email, pw):
        return self.client.post("/api/login", json={"email": email, "password": pw})

    def _forgot(self, email, ip="1.1.1.1"):
        return self.client.post("/api/password/forgot", json={"email": email},
                                headers={"X-Forwarded-For": ip})

    @staticmethod
    def _token_from_mail(msg):
        body = msg.get_body(preferencelist=("plain",)).get_content()
        m = re.search(r"#/reset\?token=([A-Za-z0-9_\-]+)", body)
        return m.group(1) if m else None

    # ---- 配置状态 ----

    def test_unconfigured_mail_returns_503_not_fake_success(self):
        self.assertFalse(mailer.configured())
        r = self._forgot(self.email)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(FakeSMTP.sent, [])

    def test_settings_expose_mail_state_but_not_password(self):
        self._configure_smtp()
        r = self.client.get("/api/admin/settings", headers=self.admin_h).json()
        self.assertTrue(r["mail_configured"])
        self.assertEqual(r["smtp_host"], "smtp.example.com")
        self.assertTrue(r["smtp_pass_set"])
        self.assertNotIn("smtp_pass", r)
        self.assertTrue(r["from_db"]["smtp_host"])
        # 写入校验
        for bad in ({"smtp_port": 70000}, {"smtp_security": "tls1"},
                    {"smtp_from": "not-an-email"}):
            self.assertEqual(self.client.patch("/api/admin/settings", json=bad,
                                               headers=self.admin_h).status_code, 400, bad)
        ok = self.client.patch("/api/admin/settings", json={"smtp_port": 587,
                               "smtp_security": "starttls"}, headers=self.admin_h)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(S.smtp_port(), 587)
        self.assertEqual(S.smtp_security(), "starttls")

    # ---- 找回流程 ----

    def test_unknown_email_returns_ok_without_sending(self):
        self._configure_smtp()
        r = self._forgot("nobody@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(FakeSMTP.sent, [])

    def test_full_reset_flow_invalidates_old_session_and_token(self):
        self._configure_smtp()
        old_tok = self._login(self.email, "secret123").json()["token"]
        old_h = {"Authorization": "Bearer " + old_tok}
        self.assertEqual(self.client.get("/api/me", headers=old_h).status_code, 200)

        r = self._forgot(self.email)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(FakeSMTP.sent), 1)
        msg = FakeSMTP.sent[0]
        self.assertEqual(msg["To"], self.email)
        self.assertIn("重置密码", msg["Subject"])
        token = self._token_from_mail(msg)
        self.assertTrue(token)
        self.assertIn("https://ai.example.com/portal#/reset?token=", msg.get_body(
            preferencelist=("plain",)).get_content())
        # 库里存的是哈希,不是令牌本身
        with _db()._conn() as c:
            rows = c.execute("SELECT token_hash FROM password_resets").fetchall()
        self.assertTrue(rows)
        self.assertNotIn(token, [r_["token_hash"] for r_ in rows])

        # 令牌改密
        time.sleep(1.1)   # 让 password_changed_at 晚于旧 token 的 iat(秒级)
        r = self.client.post("/api/password/reset", json={"token": token,
                             "new_password": "brandnew1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email"], self.email)
        # 一次性:再用一次被拒
        r = self.client.post("/api/password/reset", json={"token": token,
                             "new_password": "another1"})
        self.assertEqual(r.status_code, 400)
        # 旧会话作废,新密码能登,旧密码不能
        self.assertEqual(self.client.get("/api/me", headers=old_h).status_code, 401)
        self.assertEqual(self._login(self.email, "secret123").status_code, 401)
        new = self._login(self.email, "brandnew1")
        self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(self.client.get("/api/me", headers={
            "Authorization": "Bearer " + new.json()["token"]}).status_code, 200)

    def test_second_request_invalidates_first_token(self):
        self._configure_smtp()
        self._forgot(self.email)
        self._forgot(self.email, ip="1.1.1.2")
        self.assertEqual(len(FakeSMTP.sent), 2)
        first = self._token_from_mail(FakeSMTP.sent[0])
        second = self._token_from_mail(FakeSMTP.sent[1])
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.post("/api/password/reset", json={
            "token": first, "new_password": "xxxxxx1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/password/reset", json={
            "token": second, "new_password": "xxxxxx1"}).status_code, 200)

    def test_expired_token_rejected(self):
        self._configure_smtp()
        self._forgot(self.email)
        token = self._token_from_mail(FakeSMTP.sent[0])
        with _db()._conn() as c:
            c.execute("UPDATE password_resets SET expires_at=?", (int(time.time()) - 1,))
        self.assertEqual(self.client.post("/api/password/reset", json={
            "token": token, "new_password": "xxxxxx1"}).status_code, 400)

    def test_garbage_token_rejected(self):
        self.assertEqual(self.client.post("/api/password/reset", json={
            "token": "x" * 40, "new_password": "xxxxxx1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/password/reset", json={
            "token": "short", "new_password": "xxxxxx1"}).status_code, 422)

    def test_forgot_is_throttled_per_email(self):
        self._configure_smtp()
        saved = throttle.RESET_EMAIL.limit
        throttle.RESET_EMAIL.limit = 2
        try:
            self.assertEqual(self._forgot(self.email, ip="2.2.2.1").status_code, 200)
            self.assertEqual(self._forgot(self.email, ip="2.2.2.2").status_code, 200)
            r = self._forgot(self.email, ip="2.2.2.3")
            self.assertEqual(r.status_code, 429, r.text)
            self.assertEqual(len(FakeSMTP.sent), 2)
        finally:
            throttle.RESET_EMAIL.limit = saved

    def test_smtp_failure_reported_not_swallowed(self):
        self._configure_smtp()
        FakeSMTP.auth_fail = True
        r = self._forgot(self.email)
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(FakeSMTP.sent, [])

    # ---- 改密让旧会话作废 ----

    def test_self_change_returns_new_token_and_kills_old(self):
        tok = self._login(self.email, "secret123").json()["token"]
        h = {"Authorization": "Bearer " + tok}
        time.sleep(1.1)
        r = self.client.post("/api/me/password", json={"old_password": "secret123",
                             "new_password": "secret456"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        new_h = {"Authorization": "Bearer " + r.json()["token"]}
        self.assertEqual(self.client.get("/api/me", headers=h).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=new_h).status_code, 200)

    def test_admin_reset_kills_target_session(self):
        tok = self._login(self.email, "secret123").json()["token"]
        h = {"Authorization": "Bearer " + tok}
        self.assertEqual(self.client.get("/api/me", headers=h).status_code, 200)
        time.sleep(1.1)
        r = self.client.post(f"/api/admin/users/{self.uid}/password",
                             json={"new_password": "adminset1"}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/me", headers=h).status_code, 401)
        self.assertEqual(self._login(self.email, "adminset1").status_code, 200)

    # ---- 邮箱验证与测试发信 ----

    def test_email_bind_tells_truth_about_sending(self):
        tok = self._login(self.email, "secret123").json()["token"]
        h = {"Authorization": "Bearer " + tok}
        r = self.client.post("/api/email/bind", json={"email": self.email}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["sent"])
        self._configure_smtp(security="starttls")
        r = self.client.post("/api/email/bind", json={"email": self.email}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["sent"])
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(FakeSMTP.last_init[:2], ("smtp.example.com", 465))
        code = re.search(r"验证码是:(\d{6})", FakeSMTP.sent[0].get_body(
            preferencelist=("plain",)).get_content()).group(1)
        r = self.client.post("/api/email/verify", json={"code": code}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_mail_test_endpoint(self):
        r = self.client.post("/api/admin/mail/test", json={"to": "me@example.com"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 502)          # 未配置
        self._configure_smtp()
        r = self.client.post("/api/admin/mail/test", json={"to": "me@example.com"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(FakeSMTP.sent[-1]["To"], "me@example.com")
        FakeSMTP.auth_fail = True
        r = self.client.post("/api/admin/mail/test", json={"to": "me@example.com"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 502)
        self.assertIn("认证失败", r.json()["detail"])
        # 非管理员不能拿站点的 SMTP 当发信机
        tok = self._login(self.email, "secret123").json()["token"]
        self.assertEqual(self.client.post("/api/admin/mail/test", json={"to": "x@example.com"},
                                          headers={"Authorization": "Bearer " + tok}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
