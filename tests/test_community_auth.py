#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "bootstrap.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "no-grok")
os.environ["BITAPI_JWT_SECRET"] = "community-test-secret"
os.environ["BITAPI_DEFAULT_GROUP"] = "free"
os.environ["BITAPI_COMMUNITY_BASE_URL"] = "https://community.example.com"
os.environ["BITAPI_COMMUNITY_CLIENT_ID"] = "bitapi-test"
os.environ["BITAPI_COMMUNITY_CLIENT_SECRET"] = "never-send-to-browser"
os.environ["BITAPI_COMMUNITY_REDIRECT_URI"] = (
    "http://testserver/oauth/community")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import community_auth  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class CommunityAuthTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(_TMP, self._testMethodName + ".db")
        self.db = UserDB(path)
        billing = Billing(self.db)
        portal_state.USER_DB = self.db
        portal_state.BILLING = billing
        portal_routes.USER_DB = self.db
        portal_routes.BILLING = billing
        server.BILLING = billing
        portal_state.rebind(self.db)
        portal_state.ensure_default_group()
        config.REQUIRE_INVITE = True
        config.SIGNUP_BONUS = 0
        config.COMMUNITY_BASE_URL = "https://community.example.com"
        config.COMMUNITY_CLIENT_ID = "bitapi-test"
        config.COMMUNITY_CLIENT_SECRET = "never-send-to-browser"
        config.COMMUNITY_REDIRECT_URI = (
            "http://testserver/oauth/community")
        config.COMMUNITY_HTTP_TIMEOUT = 3
        self.client = TestClient(server.app)

    @staticmethod
    def profile(subject="42", email="community@example.com",
                username="alice"):
        return {"id": subject, "username": username, "name": "Alice",
                "email": email, "email_verified": True, "active": True}

    def _local_root(self, email="root@example.com"):
        created = self.client.post("/api/register", json={
            "email": email, "password": "secret123"})
        self.assertEqual(created.status_code, 200, created.text)
        return created.json()

    def _start(self, purpose="login", headers=None, client=None):
        client = client or self.client
        response = client.post("/api/community/auth/start", json={
            "purpose": purpose}, headers=headers or {})
        self.assertEqual(response.status_code, 200, response.text)
        target = urllib.parse.urlsplit(response.json()["authorize_url"])
        query = urllib.parse.parse_qs(target.query)
        # 授权地址 = 配置的社区根 + 固定路径,不能写死成某个论坛的具体路径
        base_path = urllib.parse.urlsplit(config.COMMUNITY_BASE_URL).path.rstrip("/")
        self.assertEqual(target.path, base_path + "/community-connect")
        self.assertEqual(query["client_id"], ["bitapi-test"])
        self.assertEqual(query["redirect_uri"],
                         [config.COMMUNITY_REDIRECT_URI])
        return query["state"][0], response

    def _callback(self, state, profile=None, client=None):
        client = client or self.client
        with mock.patch.object(
                community_auth, "exchange_code", return_value="short-token") as ex:
            with mock.patch.object(
                    community_auth, "get_userinfo",
                    return_value=profile or self.profile()) as info:
                response = client.get(
                    "/oauth/community",
                    params={"code": "one-use-code", "state": state},
                    follow_redirects=False)
        return response, ex, info

    def test_state_cookie_and_callback_replay(self):
        self.assertEqual(self.client.post(
            "/api/community/auth/start", json={"purpose": "bind"}
        ).status_code, 401)

        state, started = self._start()
        cookie = started.headers["set-cookie"].lower()
        self.assertIn("bitapi_community_flow=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)

        no_cookie = TestClient(server.app)
        missing, ex, _ = self._callback(state, client=no_cookie)
        self.assertEqual(missing.status_code, 400)
        ex.assert_not_called()

        wrong, ex, _ = self._callback(state + "x")
        self.assertEqual(wrong.status_code, 400)
        ex.assert_not_called()

        accepted, ex, info = self._callback(state)
        self.assertEqual(accepted.status_code, 302, accepted.text)
        self.assertEqual(accepted.headers["location"],
                         "/portal#/community-callback")
        ex.assert_called_once_with("one-use-code")
        info.assert_called_once_with("short-token")

        replay, ex, _ = self._callback(state)
        self.assertEqual(replay.status_code, 400)
        ex.assert_not_called()

        pending = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertTrue(pending.json()["needs_invite"])
        self.assertEqual(pending.json()["community"]["username"], "alice")

    def test_first_community_user_needs_and_consumes_invite(self):
        self.db.create_code("JOIN-ONCE", 0, type="invitation")
        state, _ = self._start()
        callback, _, _ = self._callback(state)
        self.assertEqual(callback.status_code, 302, callback.text)

        no_code = self.client.post("/api/community/auth/finish", json={})
        self.assertTrue(no_code.json()["needs_invite"])
        finished = self.client.post("/api/community/auth/finish", json={
            "invite_code": "join-once"})
        self.assertEqual(finished.status_code, 200, finished.text)
        body = finished.json()
        self.assertTrue(body["registered"])
        self.assertEqual(body["role"], "user")

        user = self.db.get_user_by_email("community@example.com")
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["password_hash"], "oauth-only")
        identity = self.db.get_community_identity_by_subject("42")
        self.assertEqual(identity["user_id"], user["id"])
        used = self.db.get_code("JOIN-ONCE")
        self.assertEqual(used["status"], "used")
        self.assertEqual(used["used_by"], user["id"])

        me = self.client.get("/api/me", headers={
            "Authorization": "Bearer " + body["token"]})
        self.assertEqual(me.status_code, 200, me.text)
        self.assertFalse(me.json()["has_password"])
        self.assertTrue(me.json()["email_verified"])
        self.assertEqual(me.json()["community"]["username"], "alice")
        self.assertEqual(self.client.post("/api/login", json={
            "email": "community@example.com", "password": "anything"
        }).status_code, 401)
        self.assertEqual(self.client.post(
            "/api/community/auth/finish", json={}).status_code, 400)

    def test_affiliate_code_cannot_unlock_community_registration(self):
        root = self._local_root()
        state, _ = self._start()
        callback, _, _ = self._callback(state)
        self.assertEqual(callback.status_code, 302, callback.text)

        rejected = self.client.post("/api/community/auth/finish", json={
            "invite_code": root["aff_code"]})
        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.assertIsNone(self.db.get_user_by_email("community@example.com"))

    def test_bind_then_known_identity_logs_into_same_user(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        state, _ = self._start("bind", headers=headers)
        callback, _, _ = self._callback(state)
        self.assertEqual(callback.status_code, 302, callback.text)

        self.assertEqual(self.client.post(
            "/api/community/auth/finish", json={}).status_code, 401)
        bound = self.client.post(
            "/api/community/auth/finish", json={}, headers=headers)
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertTrue(bound.json()["bound"])

        login_client = TestClient(server.app)
        state, _ = self._start(client=login_client)
        callback, _, _ = self._callback(state, client=login_client)
        self.assertEqual(callback.status_code, 302, callback.text)
        logged = login_client.post("/api/community/auth/finish", json={})
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertFalse(logged.json()["registered"])
        me = login_client.get("/api/me", headers={
            "Authorization": "Bearer " + logged.json()["token"]})
        self.assertEqual(me.json()["id"], root["user_id"])

    def test_email_conflict_does_not_merge_or_burn_invite(self):
        self._local_root(email="community@example.com")
        self.db.create_code("KEEP-ME", 0, type="invitation")
        state, _ = self._start()
        callback, _, _ = self._callback(state, profile=self.profile(subject="77"))
        self.assertEqual(callback.status_code, 302, callback.text)
        conflict = self.client.post("/api/community/auth/finish", json={
            "invite_code": "KEEP-ME"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("先使用邮箱登录", conflict.json()["detail"])
        self.assertEqual(self.db.get_code("KEEP-ME")["status"], "unused")
        self.assertIsNone(self.db.get_community_identity_by_subject("77"))
        self.assertEqual(self.db.count_users(), 1)

    def test_secure_cookie_follows_https_redirect_uri(self):
        config.COMMUNITY_REDIRECT_URI = (
            "https://your-domain.com/oauth/community")
        _, response = self._start()
        self.assertIn("secure", response.headers["set-cookie"].lower())


if __name__ == "__main__":
    unittest.main()
