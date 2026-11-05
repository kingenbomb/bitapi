"""有效注册 → 邀请关系 → 奖励流水；资金上限、回滚和既有登录边界。"""
import concurrent.futures
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

# mkdtemp 而非 TemporaryDirectory:后者 GC 时删目录会撞上未释放的 sqlite 句柄
_BOOTSTRAP = tempfile.mkdtemp(prefix="bitapi-invite-")
os.environ.setdefault("BITAPI_DB", os.path.join(_BOOTSTRAP, "bootstrap.db"))

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as state  # noqa: E402
import routers.portal as routes  # noqa: E402
from core import auth, community_auth, hooks, site_settings  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient, verified_registration_body  # noqa: E402


class InviteRewardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bitapi-invite-")
        # LIFO:先关连接,再尽力删目录
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = UserDB(os.path.join(self.tmp, "invite.db"))
        self.addCleanup(self.db._conns.close_current)
        original_db = state.USER_DB
        self.addCleanup(lambda: state.rebind(original_db))
        handlers = {key: list(value) for key, value in hooks._HANDLERS.items()}
        def restore_handlers():
            hooks._HANDLERS.clear()
            hooks._HANDLERS.update(handlers)
        self.addCleanup(restore_handlers)
        for module in (state, routes, server):
            for key, value in (("USER_DB", self.db), ("BILLING", Billing(self.db))):
                # server 只在函数内部 import USER_DB,模块级无此属性
                patch = mock.patch.object(module, key, value, create=True)
                patch.start(); self.addCleanup(patch.stop)
        state.rebind(self.db)
        for key, value in {
            "COMMUNITY_REGISTRATION_ONLY": False, "SIGNUP_BONUS": 0, "PLUGINS": [],
            "COMMUNITY_MIN_LEVEL": 3, "COMMUNITY_BASE_URL": "https://baipiao.org/bbs",
            "COMMUNITY_CLIENT_ID": "test-client", "COMMUNITY_CLIENT_SECRET": "test-secret",
            "COMMUNITY_REDIRECT_URI": "http://testserver/oauth/baipiao-community",
        }.items():
            patch = mock.patch.object(config, key, value)
            patch.start(); self.addCleanup(patch.stop)
        self.db.set_setting("require_invite", False)
        self.admin = self.db.create_user("owner@gmail.com", "unused", "OWNER", role="admin")
        self.headers = {"Authorization": "Bearer " + auth.issue_token(self.admin, role="admin")}
        self.client = TestClient(server.app)

    def policy(self, enabled=True, amount=0.25, limit=0):
        response = self.client.patch("/api/admin/settings", headers=self.headers,
            json={"invite_rewards": {"enabled": enabled, "amount": amount, "max_invitees": limit}})
        self.assertEqual(response.status_code, 200, response.text)

    def register(self, name, ref="OWNER"):
        return self.client.post("/api/register", json={
            "email": name + "@gmail.com", "password": "secret123", "invite_code": ref})

    def community_ready(self, subject="42", ref="OWNER", level=3, client=None):
        client = client or self.client
        started = client.post("/api/community/auth/start", json={
            "purpose": "login", "referral_code": ref})
        self.assertEqual(started.status_code, 200, started.text)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(started.json()["authorize_url"]).query)
        profile = {"id": subject, "username": "community" + subject, "name": "Community User",
                   "email": "community" + subject + "@gmail.com", "email_verified": True,
                   "active": True, "community_level": level}
        with mock.patch.object(community_auth, "exchange_code", return_value="test-token"), \
                mock.patch.object(community_auth, "get_userinfo", return_value=profile):
            callback = client.get("/oauth/baipiao-community", params={
                "state": query["state"][0], "code": "test-code"}, follow_redirects=False)
        self.assertEqual(callback.status_code, 302, callback.text)
        return self.db.get_ready_community_flow(community_auth.digest(client.cookies.get(community_auth.FLOW_COOKIE)))

    def test_admin_policy_validation_persistence_and_permissions(self):
        initial = self.client.get("/api/admin/settings", headers=self.headers).json()
        self.assertFalse(initial["invite_rewards"]["enabled"])
        self.policy(amount=0.1234, limit=5)
        reopened = UserDB(self.db.path)
        self.addCleanup(reopened._conns.close_current)
        self.assertEqual(reopened.get_setting("invite_rewards"),
                         {"enabled": True, "amount": 0.1234, "max_invitees": 5})
        for policy in (
            {"enabled": True, "amount": 0, "max_invitees": 1},
            {"enabled": True, "amount": -1, "max_invitees": 1},
            {"enabled": True, "amount": 0.12345, "max_invitees": 1},
            {"enabled": True, "amount": 1, "max_invitees": -1},
            {"enabled": True, "amount": 1, "max_invitees": 1.5},
            {"enabled": True, "amount": 1, "max_invitees": True},
            {"enabled": True, "amount": None, "max_invitees": 1},
        ):
            with self.subTest(policy=policy):
                response = self.client.patch("/api/admin/settings", headers=self.headers,
                                             json={"invite_rewards": policy, "require_invite": True})
                self.assertIn(response.status_code, (400, 422))
                self.assertFalse(self.db.get_setting("require_invite"))
                self.assertEqual(self.db.get_setting("invite_rewards")["amount"], 0.1234)
        user = self.db.create_user("ordinary@gmail.com", "unused", "ORDINARY")
        payload = {"invite_rewards": {"enabled": True, "amount": 1, "max_invitees": 0}}
        self.assertEqual(self.client.patch("/api/admin/settings", json=payload).status_code, 401)
        self.assertEqual(self.client.patch("/api/admin/settings", json=payload,
            headers={"Authorization": "Bearer " + auth.issue_token(user)}).status_code, 403)

    def test_only_new_verified_registrations_receive_current_reward(self):
        old = self.register("beforepolicy")
        self.assertEqual(old.status_code, 200, old.text)
        self.assertEqual(self.db.get_user(self.admin)["balance"], 0)
        self.policy(amount=0.1234)
        new = self.register("afterpolicy")
        self.assertEqual(new.status_code, 200, new.text)
        uid = new.json()["user_id"]
        self.assertEqual(self.db.get_user(uid)["inviter_id"], self.admin)
        self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], 0.1234)
        ledger = self.db.get_ledger_by_idem("aff:invite:" + str(uid))
        self.assertEqual(ledger["meta"], {"kind": "fixed_invite", "source_user_id": uid})
        self.assertEqual(self.register("afterpolicy").status_code, 409)
        self.policy(enabled=False, amount=0.5)
        self.assertEqual(self.register("afterdisabled").status_code, 200)
        self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], 0.1234)
        summary = self.client.get("/api/invite", headers=self.headers).json()
        self.assertEqual(summary["invitees"], 3)
        self.assertEqual(summary["rewarded_invitees"], 1)
        self.assertFalse(summary["reward_policy"]["enabled"])

    def test_no_referral_or_unverified_registration_has_no_reward(self):
        self.policy()
        self.assertEqual(self.register("withoutref", ref="").status_code, 200)
        response = self.client.post("/api/register", json={
            "email": "unverified@gmail.com", "password": "secret123", "invite_code": "OWNER",
            "verification_code": "654321"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.db.get_user_by_email("unverified@gmail.com"))
        self.assertEqual(self.db.get_user(self.admin)["balance"], 0)

    def test_concurrent_registrations_cannot_exceed_rewarded_people_limit(self):
        self.assertEqual(self.register("historicinvite").status_code, 200)
        self.policy(amount=0.5, limit=1)
        for name in ("raceone", "racetwo"):
            verified_registration_body(self.db, {"email": name + "@gmail.com"})
        def finish(name):
            try:
                return self.db.register_verified_user(name + "@gmail.com", "123456", "unused",
                    "AFF" + name, None, "OWNER", False)[0]
            finally:
                self.db._conns.close_current()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(finish, ("raceone", "racetwo")))
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(self.db.count_invitees(self.admin), 3)
        self.assertEqual(self.db.invite_reward_count(self.admin), 1)
        self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], 0.5)

    def test_community_referral_survives_oauth_and_login_cannot_rebind(self):
        self.policy()
        self.community_ready()
        finished = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(finished.status_code, 200, finished.text)
        user = self.db.get_user_by_email("community42@gmail.com")
        self.assertEqual(user["inviter_id"], self.admin)
        self.assertEqual(self.client.post("/api/community/auth/finish", json={}).status_code, 400)
        other = self.db.create_user("other@gmail.com", "unused", "OTHER")
        self.community_ready(ref="OTHER")
        login = self.client.post("/api/community/auth/finish", json={})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertFalse(login.json()["registered"])
        self.assertEqual(self.db.get_user(user["id"])["inviter_id"], self.admin)
        self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], 0.25)
        self.assertEqual(self.db.get_user(other)["balance"], 0)

    def test_community_level_invalid_referral_and_disabled_inviter_do_not_pay(self):
        self.policy()
        for subject, ref, level, status in (("51", "OWNER", 2, 403), ("52", "NO-SUCH-CODE", 3, 400)):
            self.community_ready(subject, ref, level)
            response = self.client.post("/api/community/auth/finish", json={})
            self.assertEqual(response.status_code, status, response.text)
            self.assertIsNone(self.db.get_user_by_email("community" + subject + "@gmail.com"))
        self.community_ready("53")
        self.db.update_user(self.admin, status="disabled")
        self.assertEqual(self.client.post("/api/community/auth/finish", json={}).status_code, 400)
        self.assertEqual(self.db.get_user(self.admin)["balance"], 0)

    def test_failed_registration_rolls_back_reward_and_account_together(self):
        self.policy()
        flow = self.community_ready()
        with mock.patch.object(self.db, "_consume_community_flow", side_effect=RuntimeError("test failure")):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                self.db.register_community_user(flow["id"], "NEW-AFF")
        self.assertIsNone(self.db.get_user_by_email("community42@gmail.com"))
        self.assertEqual(self.db.get_user(self.admin)["balance"], 0)
        self.assertEqual(self.db.invite_reward_count(self.admin), 0)

    def test_legacy_fixed_plugin_does_not_add_a_second_managed_reward(self):
        self.policy(amount=0.75)
        from plugins import affiliate_fixed
        response = self.register("plugintest")
        user = self.db.get_user(response.json()["user_id"])
        affiliate_fixed.on_registered(user, self.db.get_user(self.admin))
        self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], 0.75)
        self.assertEqual(self.db.invite_reward_count(self.admin), 1)

    def test_old_flow_schema_migrates_without_changing_pending_authorization(self):
        flow_id = self.db.create_community_flow("state", "nonce", "login", None, int(time.time()) + 300)
        with self.db._conn() as conn:
            conn.execute("ALTER TABLE community_auth_flows DROP COLUMN referral_code")
        migrated = UserDB(self.db.path)
        self.addCleanup(migrated._conns.close_current)
        with migrated._conn() as conn:
            row = conn.execute("SELECT * FROM community_auth_flows WHERE id=?", (flow_id,)).fetchone()
            self.assertIsNone(row["referral_code"])
            self.assertEqual(row["status"], "pending")

    def test_legacy_configuration_is_preserved_until_admin_saves_policy(self):
        with mock.patch.object(config, "PLUGINS", ["affiliate_fixed"]):
            from plugins import affiliate_fixed
            defaults = site_settings.invite_rewards()
            self.assertEqual(defaults["amount"], affiliate_fixed.CONFIG["inviter_bonus"])
            first = self.register("legacyregistration")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], defaults["amount"])
            self.policy(enabled=False)
            second = self.register("legacydisabled")
            affiliate_fixed.on_registered(self.db.get_user(second.json()["user_id"]),
                                          self.db.get_user(self.admin))
            self.assertAlmostEqual(self.db.get_user(self.admin)["balance"], defaults["amount"])

    def test_balance_notification_observes_committed_registration_and_reward(self):
        self.policy()
        seen = []
        @hooks.on("balance.changed")
        def observe(ledger, **payload):
            if ledger["meta"].get("kind") == "fixed_invite":
                seen.append((self.db.get_user(ledger["meta"]["source_user_id"]) is not None,
                             self.db.get_user(self.admin)["balance"]))
        self.assertEqual(self.register("notification").status_code, 200)
        self.assertEqual(seen, [(True, 0.25)])
