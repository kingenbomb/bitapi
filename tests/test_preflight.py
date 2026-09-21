#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动前检查与号池端点鉴权的门禁。

  1. 默认密钥 + 非回环监听 → 拒绝启动;回环地址 / 已改密钥 / 明确放行 → 照常
  2. /admin/* 号池端点认管理密钥,也认管理员 JWT;普通用户 JWT 与坏 token 401;
     改密后的旧管理员会话同样 401
"""
import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("BITAPI_DB", os.path.join(_TMP, "pf.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_TMP, "nx"))
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import auth, preflight  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self._saved = (config.JWT_SECRET, config.API_KEY, config.ADMIN_KEY)

    def tearDown(self):
        config.JWT_SECRET, config.API_KEY, config.ADMIN_KEY = self._saved

    def _all_default(self):
        config.JWT_SECRET = preflight.DEFAULTS["BITAPI_JWT_SECRET"]
        config.API_KEY = preflight.DEFAULTS["BITAPI_API_KEY"]
        config.ADMIN_KEY = preflight.DEFAULTS["BITAPI_ADMIN_KEY"]

    def test_defaults_on_public_bind_refused(self):
        self._all_default()
        for host in ("0.0.0.0", "192.168.1.5", "::", "example.com"):
            with self.subTest(host=host):
                ok, problems = preflight.check(host=host, allow_default=False)
                self.assertFalse(ok)
                self.assertTrue(any("BITAPI_JWT_SECRET" in p for p in problems))
                self.assertTrue(any("ALLOW_DEFAULT_SECRETS" in p for p in problems))

    def test_defaults_on_loopback_allowed(self):
        self._all_default()
        for host in ("127.0.0.1", "localhost", "::1", ""):
            with self.subTest(host=host):
                self.assertEqual(preflight.check(host=host, allow_default=False), (True, []))

    def test_one_default_left_is_enough_to_refuse(self):
        config.JWT_SECRET, config.API_KEY = "x" * 32, "sk-real"
        config.ADMIN_KEY = preflight.DEFAULTS["BITAPI_ADMIN_KEY"]
        ok, problems = preflight.check(host="0.0.0.0", allow_default=False)
        self.assertFalse(ok)
        self.assertEqual(preflight.default_secrets_in_use(), ["BITAPI_ADMIN_KEY"])

    def test_changed_secrets_pass_and_explicit_allow_passes(self):
        config.JWT_SECRET, config.API_KEY, config.ADMIN_KEY = "x" * 32, "sk-real", "adm-real"
        self.assertEqual(preflight.check(host="0.0.0.0", allow_default=False), (True, []))
        self._all_default()
        self.assertEqual(preflight.check(host="0.0.0.0", allow_default=True), (True, []))


class AdminEndpointAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        config.ADMIN_KEY = "adm-test"
        db_path = os.path.join(_TMP, "pf-ep.db")
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
        cls.admin_tok = r.json()["token"]
        cls.admin_id = r.json()["user_id"]
        r = cls.client.post("/api/register", json={"email": "u@example.com", "password": "secret123"})
        cls.user_tok = r.json()["token"]

    def _stats(self, tok):
        return self.client.get("/admin/stats", headers={"Authorization": "Bearer " + tok}).status_code

    def test_admin_key_and_admin_jwt_both_pass(self):
        self.assertEqual(self._stats("adm-test"), 200)
        self.assertEqual(self._stats(self.admin_tok), 200)

    def test_user_jwt_and_garbage_fail(self):
        self.assertEqual(self._stats(self.user_tok), 401)
        self.assertEqual(self._stats("nope"), 401)
        self.assertEqual(self.client.get("/admin/stats").status_code, 401)

    def test_forged_role_claim_is_not_enough(self):
        """token 里写着 admin 但库里那个人不是 admin → 401。角色以库为准,不以 token 为准。"""
        uid = portal_state.USER_DB.get_user_by_email("u@example.com")["id"]
        forged = auth.issue_token(uid, role="admin")
        self.assertEqual(self._stats(forged), 401)

    def test_admin_session_dies_after_password_change(self):
        tok = auth.issue_token(self.admin_id, role="admin")
        self.assertEqual(self._stats(tok), 200)
        time.sleep(1.1)
        portal_state.USER_DB.set_password(self.admin_id, auth.hash_password("secret456"))
        self.assertEqual(self._stats(tok), 401)
        fresh = self.client.post("/api/login", json={"email": "root@example.com",
                                                    "password": "secret456"}).json()["token"]
        self.assertEqual(self._stats(fresh), 200)


if __name__ == "__main__":
    unittest.main()
