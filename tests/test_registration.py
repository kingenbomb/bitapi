"""Email ownership before account/invite creation; SMTP is captured, never sent externally."""
import concurrent.futures
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

_IMPORT_TMP = tempfile.mkdtemp(prefix="bitapi-registration-import-")
os.environ.setdefault("BITAPI_DB", str(Path(_IMPORT_TMP) / "import.db"))

import config
import server
import core.portal_state as state
import routers.portal as portal
from core import auth, mailer, throttle
from core.billing import Billing
from core.user_db import UserDB


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bitapi-registration-")
        self.db = UserDB(str(Path(self.tmp.name) / "test.db"))
        self.old_db = state.USER_DB
        self.patches = []
        for mod in (state, server, portal):
            for name, value in (("USER_DB", self.db), ("BILLING", Billing(self.db))):
                patch = mock.patch.object(mod, name, value)
                patch.start()
                self.patches.append(patch)
        for name, value in (("REQUIRE_INVITE", False), ("SIGNUP_BONUS", 0)):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.patches.append(patch)
        state.rebind(self.db)
        state.ensure_default_group()
        self.db.set_setting("smtp_host", "smtp.example.com")
        self.db.set_setting("smtp_from", "sender@example.com")
        self.send_patch = mock.patch.object(mailer, "send")
        self.sent = self.send_patch.start()
        self.patches.append(self.send_patch)
        self.code_patch = mock.patch.object(auth, "generate_email_code", return_value="012345")
        self.code_patch.start()
        self.patches.append(self.code_patch)
        self.client = TestClient(server.app)
        throttle.reset_all()

    def tearDown(self):
        self.client.close()
        for patch in reversed(self.patches):
            patch.stop()
        state.rebind(self.old_db)
        with self.db._conns._lock:
            for conn in self.db._conns._live.values():
                conn.close()
            self.db._conns._live.clear()
        self.tmp.cleanup()
        throttle.reset_all()

    def send_code(self, email="2892672257@qq.com"):
        return self.client.post("/api/register/code", json={"email": email})

    def register(self, email="2892672257@qq.com", code="012345", invite=""):
        return self.client.post("/api/register", json={"email": email, "password": "secret123",
                                                       "verification_code": code, "invite_code": invite})

    def proof(self, email="2892672257@qq.com"):
        with self.db._conn() as conn:
            return conn.execute("SELECT * FROM registration_codes WHERE email=?", (email,)).fetchone()

    def test_qq_and_gmail_require_delivered_code_and_create_verified_accounts(self):
        for email in ("2892672257@qq.com", "fixturemember@gmail.com"):
            sent = self.send_code(email)
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertEqual(sent.json()["expires_in"], 900)
            self.assertNotIn("012345", sent.text)
            self.assertIsNone(self.db.get_user_by_email(email))
            self.assertNotIn("012345", self.proof(email)["code_hash"])
            self.assertEqual(self.sent.call_args.args[0], email)
            created = self.register(email)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertTrue(self.db.get_user(created.json()["user_id"])["email_verified_at"])
            self.assertIsNone(self.proof(email))
            self.assertEqual(self.register(email).status_code, 409)

    def test_registration_cannot_skip_code_or_use_another_mailbox(self):
        self.assertEqual(self.client.post("/api/register", json={"email": "fixturemember@gmail.com",
                                                               "password": "secret123"}).status_code, 422)
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(self.send_code().status_code, 200)
        self.assertEqual(self.register("fixturemember@gmail.com").status_code, 400)
        self.assertEqual(self.db.count_users(), 0)

    def test_rejects_other_domains_and_aliases_before_sending_or_creating(self):
        for email in ("alias@qq.com", "123456+tag@qq.com", "member+tag@gmail.com",
                      "mem.ber@gmail.com", "member@googlemail.com", "member@example.com"):
            with self.subTest(email=email):
                self.assertEqual(self.send_code(email).status_code, 400)
                self.assertEqual(self.register(email).status_code, 400)
        self.sent.assert_not_called()
        self.assertEqual(self.db.count_users(), 0)

    def test_mail_configuration_or_delivery_failure_cannot_issue_a_usable_code(self):
        self.db.delete_setting("smtp_host")
        with mock.patch.object(config, "SMTP_HOST", ""):
            self.assertEqual(self.send_code().status_code, 503)
        self.assertIsNone(self.proof())
        self.db.set_setting("smtp_host", "smtp.example.com")
        self.sent.side_effect = mailer.MailError("fixture delivery failed")
        self.assertEqual(self.send_code().status_code, 502)
        self.assertIsNone(self.proof())
        self.assertEqual(self.register().status_code, 400)

    def test_expiry_and_five_wrong_attempts_are_enforced(self):
        self.assertEqual(self.send_code().status_code, 200)
        for _ in range(5):
            self.assertEqual(self.register(code="999999").status_code, 400)
        self.assertEqual(self.proof()["attempts"], 5)
        self.assertEqual(self.register().status_code, 400)
        with self.db._conn() as conn:
            conn.execute("UPDATE registration_codes SET expires_at=0")
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(self.db.count_users(), 0)

    def test_resend_cooldown_and_new_code_invalidate_old_code(self):
        self.assertEqual(self.send_code().status_code, 200)
        response = self.send_code()
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertEqual(self.sent.call_count, 1)
        with self.db._conn() as conn:
            conn.execute("UPDATE registration_codes SET created_at=created_at-61")
        with mock.patch.object(auth, "generate_email_code", return_value="654321"):
            self.assertEqual(self.send_code().status_code, 200)
        self.assertEqual(self.register(code="012345").status_code, 400)
        self.assertEqual(self.register(code="654321").status_code, 200)

    def test_bad_invite_keeps_proof_and_wrong_code_keeps_invite(self):
        self.send_code()
        self.assertEqual(self.register().status_code, 200)
        self.db.set_setting("require_invite", True)
        email = "fixturemember@gmail.com"
        self.send_code(email)
        self.db.create_code("ONCE", 0, type="invitation")
        self.assertEqual(self.register(email, invite="WRONG").status_code, 403)
        self.assertIsNotNone(self.proof(email))
        self.assertEqual(self.register(email, code="999999", invite="ONCE").status_code, 400)
        self.assertEqual(self.db.get_code("ONCE")["status"], "unused")
        result = self.register(email, invite="ONCE")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.db.get_code("ONCE")["used_by"], result.json()["user_id"])

    def test_concurrent_code_redemption_creates_exactly_one_account(self):
        self.send_code()

        def submit(_):
            with TestClient(server.app) as client:
                return client.post("/api/register", json={"email": "2892672257@qq.com", "password": "secret123",
                                                          "verification_code": "012345"}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(submit, range(4)))
        self.assertEqual(results.count(200), 1, results)
        self.assertTrue(all(status in (200, 400, 409) for status in results), results)
        self.assertEqual(self.db.count_users(), 1)

    def test_send_limits_stop_cross_mailbox_spam(self):
        limiter = throttle.REGISTER_MAIL_IP
        previous = limiter.limit
        limiter.limit = 1
        try:
            self.assertEqual(self.send_code().status_code, 200)
            result = self.send_code("fixturemember@gmail.com")
            self.assertEqual(result.status_code, 429)
            self.assertIn("Retry-After", result.headers)
            self.assertEqual(self.sent.call_count, 1)
        finally:
            limiter.limit = previous

    def test_cumulative_tokens_include_all_users_old_logs_and_cache(self):
        self.send_code()
        registered = self.register().json()
        uid = registered["user_id"]
        other = self.db.create_user("legacy@example.com", "fixture", "OTHER")
        old = self.db.add_usage(user_id=other, input_tokens=100, output_tokens=50,
                                pricing_snapshot={"total_ctx": 300})
        self.db.add_usage(user_id=uid, input_tokens=10, output_tokens=20)
        with self.db._conn() as conn:
            conn.execute("UPDATE usage_logs SET created_at=? WHERE id=?", (int(time.time()) - 100 * 86400, old))
        headers = {"Authorization": "Bearer " + registered["token"]}
        for days in (7, 90):
            response = self.client.get("/api/admin/stats", params={"days": days}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total_usage"]["tokens"], 380)
            self.assertEqual(response.json()["windows"]["month"]["usage"]["tokens"], 30)

