#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64
import hashlib
import concurrent.futures
import time
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "bootstrap.db")
os.environ["BITAPI_JWT_SECRET"] = "community-test-secret"
os.environ["BITAPI_DEFAULT_GROUP"] = "free"
os.environ["BITAPI_COMMUNITY_BASE_URL"] = "https://baipiao.org/bbs"
os.environ["BITAPI_COMMUNITY_CLIENT_ID"] = "bitapi-test"
os.environ["BITAPI_COMMUNITY_CLIENT_SECRET"] = "never-send-to-browser"
os.environ["BITAPI_COMMUNITY_REDIRECT_URI"] = (
    "http://testserver/oauth/baipiao-community")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import auth, community_auth  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB, CommunityFlowError, CommunityLevelError  # noqa: E402
from tests.registration_fixture import TestClient  # noqa: E402


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
        community_only = mock.patch.object(config, "COMMUNITY_REGISTRATION_ONLY", True)
        community_only.start()
        self.addCleanup(community_only.stop)
        self.addCleanup(self.db._conns.close_current)
        config.REQUIRE_INVITE = True
        config.COMMUNITY_MIN_LEVEL = 3
        config.SIGNUP_BONUS = 0
        config.COMMUNITY_BASE_URL = "https://baipiao.org/bbs"
        config.COMMUNITY_CLIENT_ID = "bitapi-test"
        config.COMMUNITY_CLIENT_SECRET = "never-send-to-browser"
        config.COMMUNITY_REDIRECT_URI = (
            "http://testserver/oauth/baipiao-community")
        config.COMMUNITY_HTTP_TIMEOUT = 3
        self.client = TestClient(server.app)

    @staticmethod
    def profile(subject="42", email="community@gmail.com",
                username="alice", level=3):
        return {"id": subject, "username": username, "name": "Alice",
                "email": email, "email_verified": True, "active": True,
                "community_level": level}

    def _local_root(self, email="root@gmail.com"):
        uid = self.db.create_user(email, auth.hash_password("secret123"),
                                  role="admin", aff_code="ROOTCODE")
        return {"user_id": uid, "role": "admin", "aff_code": "ROOTCODE",
                "token": auth.issue_token(uid, role="admin")}

    def _start(self, purpose="login", headers=None, client=None):
        client = client or self.client
        response = client.post("/api/community/auth/start", json={
            "purpose": purpose}, headers=headers or {})
        self.assertEqual(response.status_code, 200, response.text)
        target = urllib.parse.urlsplit(response.json()["authorize_url"])
        query = urllib.parse.parse_qs(target.query)
        self.assertEqual(target.path, "/bbs/connect/authorize")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["openid profile email"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        nonce = client.cookies.get(community_auth.FLOW_COOKIE)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(nonce.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertNotIn(nonce, response.text)
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
                    "/oauth/baipiao-community",
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
        ex.assert_called_once_with("one-use-code", self.client.cookies.get(community_auth.FLOW_COOKIE))
        info.assert_called_once_with("short-token")

        replay, ex, _ = self._callback(state)
        self.assertEqual(replay.status_code, 400)
        ex.assert_not_called()

        pending = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertTrue(pending.json()["registered"])
        self.assertNotIn("needs_invite", pending.json())

    def test_first_lv3_user_registers_without_invite_as_regular_user(self):
        self.db.create_code("JOIN-ONCE", 0, type="invitation")
        state, _ = self._start()
        callback, _, _ = self._callback(state)
        self.assertEqual(callback.status_code, 302, callback.text)

        finished = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(finished.status_code, 200, finished.text)
        body = finished.json()
        self.assertTrue(body["registered"])
        self.assertEqual(body["role"], "user")

        user = self.db.get_user_by_email("community@gmail.com")
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["password_hash"], "oauth-only")
        identity = self.db.get_community_identity_by_subject("42")
        self.assertEqual(identity["user_id"], user["id"])
        used = self.db.get_code("JOIN-ONCE")
        self.assertEqual(used["status"], "unused")
        self.assertIsNone(used["used_by"])

        me = self.client.get("/api/me", headers={
            "Authorization": "Bearer " + body["token"]})
        self.assertEqual(me.status_code, 200, me.text)
        self.assertFalse(me.json()["has_password"])
        self.assertTrue(me.json()["email_verified"])
        self.assertEqual(me.json()["community"]["username"], "alice")
        self.assertEqual(self.client.post("/api/login", json={
            "email": "community@gmail.com", "password": "anything"
        }).status_code, 401)
        self.assertEqual(self.client.post(
            "/api/community/auth/finish", json={}).status_code, 400)

    def test_low_or_missing_level_cannot_be_bypassed_by_invites_or_client_claims(self):
        root = self._local_root()
        self.db.create_code("VALID-INVITE", 0, type="invitation")
        for level in (0, 1, 2, None):
            for code in ("", root["aff_code"], "VALID-INVITE"):
                with self.subTest(level=level, code=code):
                    state, _ = self._start()
                    callback, _, _ = self._callback(state, self.profile(level=level))
                    self.assertEqual(callback.status_code, 302)
                    rejected = self.client.post("/api/community/auth/finish", json={
                        "invite_code": code, "community_level": 6, "trust_level": 4})
                    self.assertEqual(rejected.status_code, 403, rejected.text)
                    self.assertIsNone(self.db.get_user_by_email("community@gmail.com"))
                    self.assertEqual(self.db.count_users(), 1)
        self.assertEqual(self.db.get_code("VALID-INVITE")["status"], "unused")

    def test_levels_above_threshold_also_register(self):
        for level in (4, 5, 6):
            with self.subTest(level=level):
                state, _ = self._start()
                self._callback(state, self.profile(subject=str(level),
                               email=f"lv{level}@gmail.com", level=level))
                response = self.client.post("/api/community/auth/finish", json={})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["registered"])
                self.assertEqual(response.json()["role"], "user")

    def test_admin_can_change_and_reset_live_registration_threshold(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        for level in range(7):
            with self.subTest(level=level):
                updated = self.client.patch("/api/admin/settings", headers=headers,
                                            json={"community_min_level": level})
                self.assertEqual(updated.status_code, 200, updated.text)
                public = self.client.get("/api/registration-policy").json()
                self.assertEqual(public, {"community_only": True, "community_min_level": level})
                settings = self.client.get("/api/admin/settings", headers=headers).json()
                self.assertEqual(settings["registration_policy"], public)
                self.assertTrue(settings["from_db"]["community_min_level"])
                blocked = self.client.post("/api/register/code", json={"email": "new@gmail.com"})
                self.assertEqual(blocked.status_code, 403)
                self.assertIn(f"Lv{level}", blocked.json()["detail"])
        reopened = UserDB(self.db.path)
        try:
            self.assertEqual(reopened.get_setting("community_min_level"), 6)
        finally:
            reopened._conns.close_current()
        reset = self.client.delete("/api/admin/settings/community_min_level", headers=headers)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(self.client.get("/api/registration-policy").json()["community_min_level"], 3)
        self.assertFalse(self.client.get("/api/admin/settings", headers=headers)
                         .json()["from_db"]["community_min_level"])

    def test_threshold_rejects_invalid_values_without_partial_changes(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        self.db.set_setting("community_min_level", 4)
        for value in (None, True, False, "2", 2.0, 2.5, -1, 7, [], {}):
            with self.subTest(value=value):
                response = self.client.patch("/api/admin/settings", headers=headers,
                    json={"community_min_level": value, "require_invite": False})
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.db.get_setting("community_min_level"), 4)
                self.assertIsNone(self.db.get_setting("require_invite"))

    def test_only_admin_can_change_or_reset_threshold(self):
        uid = self.db.create_user("ordinary@gmail.com", "oauth-only", aff_code="ORDINARY")
        for headers, status in (({}, 401),
                ({"Authorization": "Bearer " + auth.issue_token(uid, role="user")}, 403)):
            with self.subTest(status=status):
                self.assertEqual(self.client.patch("/api/admin/settings", headers=headers,
                    json={"community_min_level": 0}).status_code, status)
                self.assertEqual(self.client.delete("/api/admin/settings/community_min_level",
                    headers=headers).status_code, status)
        self.assertIsNone(self.db.get_setting("community_min_level"))

    def test_pending_registration_uses_latest_threshold_and_preserves_rejected_flow(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        state, _ = self._start()
        self._callback(state, self.profile(level=3))
        self.assertEqual(self.client.patch("/api/admin/settings", headers=headers,
            json={"community_min_level": 4}).status_code, 200)
        rejected = self.client.post("/api/community/auth/finish", json={"community_min_level": 0})
        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.assertIn("Lv4", rejected.json()["detail"])
        self.assertEqual(self.db.count_users(), 1)
        self.assertIsNone(self.db.get_community_identity_by_subject("42"))
        self.assertEqual(self.client.patch("/api/admin/settings", headers=headers,
            json={"community_min_level": 2}).status_code, 200)
        accepted = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["registered"])
        self.assertEqual(self.db.count_users(), 2)

    def test_threshold_extremes_check_real_level_not_unknown(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        cases = [(6, 5, 403), (6, 6, 200), (0, None, 403), (0, 0, 200)]
        for index, (threshold, level, status) in enumerate(cases):
            with self.subTest(threshold=threshold, level=level):
                self.assertEqual(self.client.patch("/api/admin/settings", headers=headers,
                    json={"community_min_level": threshold}).status_code, 200)
                state, _ = self._start()
                self._callback(state, self.profile(subject=str(100 + index),
                    email=f"extreme{index}@gmail.com", level=level))
                response = self.client.post("/api/community/auth/finish", json={})
                self.assertEqual(response.status_code, status, response.text)
                if status == 200:
                    self.assertEqual(response.json()["role"], "user")

    def test_email_registration_is_closed_even_with_code_and_invite(self):
        self.assertEqual(self.client.get("/api/registration-policy").json(),
                         {"community_only": True, "community_min_level": 3})
        self.db.create_code("VALID-INVITE", 0, type="invitation")
        with mock.patch("core.mailer.send") as send:
            response = self.client.post("/api/register/code", json={"email": "new@gmail.com"})
        self.assertEqual(response.status_code, 403)
        send.assert_not_called()
        response = self.client.post("/api/register", json={
            "email": "new@gmail.com", "password": "secret123", "invite_code": "VALID-INVITE"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.db.count_users(), 0)  # 也不能利用首用户免码建管理员
        self.assertEqual(self.db.get_code("VALID-INVITE")["status"], "unused")
        root = self._local_root()
        response = self.client.post("/api/login", json={"email": "root@gmail.com", "password": "secret123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.count_users(), 1)

    def test_concurrent_finish_creates_exactly_one_account(self):
        state, _ = self._start()
        self._callback(state)
        flow = self.db.get_ready_community_flow(community_auth.digest(
            self.client.cookies.get(community_auth.FLOW_COOKIE)))
        def register(aff):
            try:
                self.db.register_community_user(flow["id"], aff)
                return "registered"
            except CommunityFlowError:
                return "consumed"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, ["FIRST", "SECOND"]))
        self.assertCountEqual(results, ["registered", "consumed"])
        self.assertEqual(self.db.count_users(), 1)

    def test_migration_keeps_existing_flow_ineligible_until_reauthorization(self):
        state, _ = self._start()
        self._callback(state)
        nonce_hash = community_auth.digest(self.client.cookies.get(community_auth.FLOW_COOKIE))
        flow = self.db.get_ready_community_flow(nonce_hash)
        with self.db._conn() as c:
            c.execute("ALTER TABLE community_auth_flows DROP COLUMN community_level")
        migrated = UserDB(self.db.path)
        try:
            old_flow = migrated.get_ready_community_flow(nonce_hash)
            self.assertIsNone(old_flow["community_level"])
            with self.assertRaises(CommunityLevelError):
                migrated.register_community_user(flow["id"], "MIGRATED")
            self.assertEqual(migrated.count_users(), 0)
        finally:
            migrated._conns.close_current()

    def test_expired_flow_cannot_create_user(self):
        state, _ = self._start()
        self._callback(state)
        with self.db._conn() as c:
            c.execute("UPDATE community_auth_flows SET expires_at=?", (int(time.time()) - 1,))
        self.assertEqual(self.client.post("/api/community/auth/finish", json={}).status_code, 400)
        self.assertEqual(self.db.count_users(), 0)

    def test_bind_then_known_identity_logs_into_same_user(self):
        root = self._local_root()
        headers = {"Authorization": "Bearer " + root["token"]}
        self.assertEqual(self.client.patch("/api/admin/settings", headers=headers,
            json={"community_min_level": 6}).status_code, 200)
        state, _ = self._start("bind", headers=headers)
        callback, _, _ = self._callback(state, self.profile(level=0))
        self.assertEqual(callback.status_code, 302, callback.text)

        self.assertEqual(self.client.post(
            "/api/community/auth/finish", json={}).status_code, 401)
        bound = self.client.post(
            "/api/community/auth/finish", json={}, headers=headers)
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertTrue(bound.json()["bound"])

        login_client = TestClient(server.app)
        state, _ = self._start(client=login_client)
        callback, _, _ = self._callback(state, self.profile(level=None), client=login_client)
        self.assertEqual(callback.status_code, 302, callback.text)
        logged = login_client.post("/api/community/auth/finish", json={})
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertFalse(logged.json()["registered"])
        me = login_client.get("/api/me", headers={
            "Authorization": "Bearer " + logged.json()["token"]})
        self.assertEqual(me.json()["id"], root["user_id"])

    def test_email_conflict_does_not_merge_or_burn_invite(self):
        self._local_root(email="community@gmail.com")
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
            "https://ai.baipiao.co/oauth/baipiao-community")
        _, response = self._start()
        self.assertIn("secure", response.headers["set-cookie"].lower())

    def test_current_token_endpoint_sends_pkce_and_secret_only_server_side(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"access_token": "short-lived"}
        with mock.patch.object(community_auth.httpx, "post", return_value=response) as post:
            self.assertEqual(community_auth.exchange_code("code", "verifier"), "short-lived")
        self.assertEqual(post.call_args.args, ("https://baipiao.org/bbs/api/connect/token",))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["code_verifier"], "verifier")
        self.assertEqual(data["grant_type"], "authorization_code")
        self.assertEqual(data["client_secret"], config.COMMUNITY_CLIENT_SECRET)
        self.assertFalse(post.call_args.kwargs["follow_redirects"])

    def test_denied_consent_consumes_flow_and_returns_retry_page(self):
        state, _ = self._start()
        with mock.patch.object(community_auth, "exchange_code") as exchange:
            response = self.client.get("/oauth/baipiao-community", params={
                "state": state, "error": "access_denied", "error_description": "untrusted"},
                follow_redirects=False)
        exchange.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/portal#/community-callback?error=access_denied")
        self.assertNotIn("untrusted", response.text)
        self.assertIsNone(self.client.cookies.get(community_auth.FLOW_COOKIE))
        self.assertEqual(self._callback(state)[0].status_code, 400)

    def test_current_userinfo_subject_preserves_existing_binding(self):
        response = mock.Mock(status_code=200)
        profile = dict(self.profile(), sub="42", preferred_username="newname", silenced=False)
        response.json.return_value = profile
        with mock.patch.object(community_auth.httpx, "get", return_value=response) as get:
            result = community_auth.get_userinfo("access-token")
            self.assertEqual(result["id"], "42")
            self.assertEqual(result["username"], "newname")
            self.assertEqual(result["community_level"], 3)
            self.assertEqual(get.call_args.args, ("https://baipiao.org/bbs/api/connect/userinfo",))
            self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer access-token"})
            for invalid in ({"sub": ""}, {"id": 99}, {"active": False},
                            {"email_verified": False}, {"silenced": True}):
                with self.subTest(invalid=invalid):
                    response.json.return_value = dict(profile, **invalid)
                    with self.assertRaises(community_auth.CommunityAuthError):
                        community_auth.get_userinfo("access-token")

    def test_userinfo_never_substitutes_trust_level_or_coerces_invalid_level(self):
        response = mock.Mock(status_code=200)
        for level in (None, True, False, "3", 3.0, -1, 7, [], {}):
            response.json.return_value = dict(self.profile(), sub="42",
                                             community_level=level, trust_level=4)
            with self.subTest(level=level), mock.patch.object(
                    community_auth.httpx, "get", return_value=response):
                self.assertIsNone(community_auth.get_userinfo("access-token")["community_level"])
        profile = dict(self.profile(), sub="42", trust_level=4)
        del profile["community_level"]
        response.json.return_value = profile
        with mock.patch.object(community_auth.httpx, "get", return_value=response):
            self.assertIsNone(community_auth.get_userinfo("access-token")["community_level"])


if __name__ == "__main__":
    unittest.main()
