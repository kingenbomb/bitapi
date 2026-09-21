#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TOTP 二次验证的门禁。

  1. 算法与 RFC 6238 测试向量一致(SHA-1、30 秒、6 位),允许前后各一步漂移,
     同一时间步的码不能用第二次
  2. 开启要「先 setup 再拿码 enable」;关闭要密码 + 当前码;已开启不能被 setup 顶掉
  3. 开了之后密码登录只换到一张受限票:票不能当会话用;票 + 码才换会话;
     错码计入登录限速
  4. /me 只回「开没开」,用户列表不漏 secret;管理员能替别人关、不能替自己关
"""
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "totp.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import auth, throttle, totp  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class AlgorithmTest(unittest.TestCase):
    # RFC 6238 附录 B 的 SHA-1 向量(secret = "12345678901234567890" 的 base32)
    RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc_vectors_truncated_to_six_digits(self):
        for t, eight in ((59, "94287082"), (1111111109, "07081804"),
                         (1234567890, "89005924"), (2000000000, "69279037")):
            with self.subTest(t=t):
                self.assertEqual(totp.code(self.RFC_SECRET, now=t), eight[-6:])

    def test_verify_window_and_replay(self):
        s = totp.generate_secret()
        now = 1_700_000_000
        step = totp.step_of(now)
        good = totp.code(s, now=now)
        self.assertEqual(totp.verify(s, good, now=now), step)
        # 前后一步都认(时钟漂移),两步不认
        self.assertEqual(totp.verify(s, totp.code_at(s, step - 1), now=now), step - 1)
        self.assertEqual(totp.verify(s, totp.code_at(s, step + 1), now=now), step + 1)
        self.assertIsNone(totp.verify(s, totp.code_at(s, step + 2), now=now))
        # 用过的步不能再用
        self.assertIsNone(totp.verify(s, good, now=now, last_step=step))
        self.assertIsNone(totp.verify(s, "12345", now=now))
        self.assertIsNone(totp.verify(s, "abcdef", now=now))

    def test_secret_and_uri_shape(self):
        s = totp.generate_secret()
        self.assertEqual(len(s), 32)
        self.assertNotIn("=", s)
        uri = totp.otpauth_uri(s, "a@b.c", "bit-api")
        self.assertTrue(uri.startswith("otpauth://totp/bit-api%3Aa%40b.c?"))
        self.assertIn("secret=" + s, uri)
        self.assertIn("issuer=bit-api", uri)


class TotpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "totp-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@example.com", "password": "secret123"})
        cls.admin_h = {"Authorization": "Bearer " + r.json()["token"]}
        cls._seq = 0

    def setUp(self):
        throttle.reset_all()
        type(self)._seq += 1
        self.email = f"t{self._seq}@example.com"
        r = self.client.post("/api/register", json={"email": self.email, "password": "secret123"})
        self.uid = r.json()["user_id"]
        self.h = {"Authorization": "Bearer " + r.json()["token"]}

    def _db(self):
        return portal_state.USER_DB

    # 服务端按真实时间算步;测试里把 core.totp 的时钟钉住,每个动作往后拨一步
    # (30 秒)—— 同一步的码只能用一次,这正是被测的防重放。
    T0 = 1_800_000_000

    def _at(self, step_offset):
        # 只替换 core.totp 手里那个 time 引用,不动全局 time 模块 —— 否则 JWT 的 iat
        # 也会被钉到未来,PyJWT 会以「签发时间在未来」拒掉会话
        class _Clock:
            @staticmethod
            def time():
                return self.T0 + 30 * step_offset
        return mock.patch.object(totp, "time", _Clock)

    def _enable(self):
        r = self.client.post("/api/2fa/setup", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        secret = r.json()["secret"]
        with self._at(0):
            r = self.client.post("/api/2fa/enable", json={"code": totp.code(secret)}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return secret

    def test_setup_then_enable_requires_a_valid_code(self):
        r = self.client.post("/api/2fa/setup", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        secret = r.json()["secret"]
        self.assertIn("otpauth://totp/", r.json()["otpauth_uri"])
        # 没确认前不算开启
        self.assertFalse(self.client.get("/api/me", headers=self.h).json()["totp_enabled"])
        self.assertEqual(self.client.post("/api/2fa/enable", json={"code": "000000"},
                                          headers=self.h).status_code, 400)
        with self._at(0):
            r = self.client.post("/api/2fa/enable", json={"code": totp.code(secret)}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        me = self.client.get("/api/me", headers=self.h).json()
        self.assertTrue(me["totp_enabled"])
        self.assertNotIn("totp_secret", me)
        # 已开启不能被 setup 顶掉
        self.assertEqual(self.client.post("/api/2fa/setup", headers=self.h).status_code, 409)

    def test_login_becomes_two_step_and_ticket_is_not_a_session(self):
        secret = self._enable()
        r = self.client.post("/api/login", json={"email": self.email, "password": "secret123"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["needs_totp"])
        self.assertNotIn("token", body)
        ticket = body["ticket"]
        # 票不能当会话
        self.assertEqual(self.client.get("/api/me", headers={"Authorization": "Bearer " + ticket}).status_code, 401)
        self.assertEqual(self.client.get("/admin/stats", headers={"Authorization": "Bearer " + ticket}).status_code, 401)
        with self._at(1):
            # 错码 401;enable 时用过的那一步的码也不能用(重放);下一步的码换会话
            self.assertEqual(self.client.post("/api/login/totp", json={"ticket": ticket, "code": "000000"}).status_code, 401)
            self.assertEqual(self.client.post("/api/login/totp", json={
                "ticket": ticket, "code": totp.code_at(secret, totp.step_of(self.T0))}).status_code, 401)
            r = self.client.post("/api/login/totp", json={"ticket": ticket, "code": totp.code(secret)})
            self.assertEqual(r.status_code, 200, r.text)
            tok = r.json()["token"]
            self.assertEqual(self.client.get("/api/me", headers={"Authorization": "Bearer " + tok}).status_code, 200)
            # 同一个码不能再用一次(重放)
            r = self.client.post("/api/login/totp", json={"ticket": ticket, "code": totp.code(secret)})
            self.assertEqual(r.status_code, 401)

    def test_wrong_codes_count_toward_login_throttle(self):
        self._enable()
        ticket = self.client.post("/api/login", json={"email": self.email, "password": "secret123"}).json()["ticket"]
        saved = throttle.LOGIN_EMAIL.limit
        throttle.LOGIN_EMAIL.limit = 2
        try:
            for _ in range(2):
                self.assertEqual(self.client.post("/api/login/totp", json={"ticket": ticket, "code": "000000"}).status_code, 401)
            r = self.client.post("/api/login/totp", json={"ticket": ticket, "code": "000000"})
            self.assertEqual(r.status_code, 429, r.text)
        finally:
            throttle.LOGIN_EMAIL.limit = saved

    def test_expired_or_forged_ticket_rejected(self):
        self._enable()
        stale = auth.issue_token(self.uid, ttl=-1, scope="totp")
        self.assertEqual(self.client.post("/api/login/totp", json={"ticket": stale, "code": "000000"}).status_code, 401)
        # 普通会话 token 不能当票用:票必须带 scope=totp
        session = auth.issue_token(self.uid)
        self.assertEqual(self.client.post("/api/login/totp", json={"ticket": session, "code": "000000"}).status_code, 401)

    def test_disable_needs_password_and_code(self):
        secret = self._enable()
        with self._at(2):
            code = totp.code(secret)
            self.assertEqual(self.client.post("/api/2fa/disable", json={"password": "wrong", "code": code},
                                              headers=self.h).status_code, 403)
            self.assertEqual(self.client.post("/api/2fa/disable", json={"password": "secret123", "code": "000000"},
                                              headers=self.h).status_code, 400)
            r = self.client.post("/api/2fa/disable", json={"password": "secret123", "code": code}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.client.get("/api/me", headers=self.h).json()["totp_enabled"])
        # 关了之后登录又是一步
        r = self.client.post("/api/login", json={"email": self.email, "password": "secret123"})
        self.assertIn("token", r.json())

    def test_admin_can_disable_for_others_but_not_self(self):
        self._enable()
        listed = self.client.get("/api/admin/users", params={"q": self.email}, headers=self.admin_h).json()["users"][0]
        self.assertTrue(listed["totp_enabled"])
        self.assertNotIn("totp_secret", listed)
        r = self.client.post(f"/api/admin/users/{self.uid}/2fa/disable", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.client.get("/api/me", headers=self.h).json()["totp_enabled"])
        admin_id = self._db().get_user_by_email("root@example.com")["id"]
        self.assertEqual(self.client.post(f"/api/admin/users/{admin_id}/2fa/disable",
                                          headers=self.admin_h).status_code, 400)
        self.assertEqual(self.client.post(f"/api/admin/users/{self.uid}/2fa/disable",
                                          headers=self.h).status_code, 403)


if __name__ == "__main__":
    unittest.main()
