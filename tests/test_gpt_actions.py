import tempfile
import time
import unittest
import os
from unittest import mock

from core.db import DB


class GPTActionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = DB(temp.name + "/actions.db")
        self.addCleanup(self.db.close)
        self.enterContext(mock.patch.dict(os.environ, {"BITAPI_DB": self.db.path}))
        global A, G, pool_state
        from core import gpt_actions as A, gpt_runtime as G, pool_state
        self.enterContext(mock.patch.object(pool_state, "DB", self.db))
        self.aid = self.db.upsert_account("gpt", "fixture@example.test", secret={"access_token": "secret-fixture"},
            meta={"admin_paused": True}, status="active")
        self.adapter = mock.Mock()
        self.enterContext(mock.patch.object(A.M, "adapter_of", return_value=self.adapter))
        self.adapter.refresh_token.return_value = {"token": "secret-fixture", "token_exp": int(time.time()) + 1000}
        self.adapter.account_reset_credits.return_value = {"available_count": 1, "expirations": []}
        self.adapter.reset_candidates.return_value = [{"id": "private-card", "expires_at": int(time.time()) + 1000}]
        self.adapter.reset_quota.return_value = {"code": "ok", "windows_reset": 2}
        self.adapter.account_usage.return_value = {"fetched_at": int(time.time()), "five_hour": {"utilization": 10, "reset_at": int(time.time()) + 1000}}
        self.adapter.referral_eligibility.return_value = {"should_show": True, "available_invites": 1,
            "requires_confirmation": True, "program_id": "codex_referral_consumer"}
        self.adapter.send_referral.return_value = {"sent": True, "email": "friend@example.com"}

    def test_reset_preview_does_not_consume_and_confirmation_is_idempotent(self):
        preview = A.prepare(self.aid, 7, "reset")
        self.assertNotIn("credit_id", preview)
        self.adapter.reset_quota.assert_not_called()
        with self.assertRaises(ValueError):
            A.execute(preview["request_id"], self.aid, 7, False)
        with self.assertRaises(ValueError):
            A.execute(preview["request_id"], self.aid, 8, True)
        result = A.execute(preview["request_id"], self.aid, 7, True)
        self.assertTrue(result["ok"])
        self.assertEqual(A.execute(preview["request_id"], self.aid, 7, True), result)
        self.adapter.reset_quota.assert_called_once()
        self.assertTrue(self.db.get_account(self.aid)["meta"]["admin_paused"])
        self.assertNotIn("private-card", str(A.history(self.aid)))

    def test_unknown_result_blocks_a_second_consume_until_manually_acknowledged(self):
        preview = A.prepare(self.aid, 7, "reset")
        self.adapter.reset_quota.side_effect = TimeoutError("unknown")
        result = A.execute(preview["request_id"], self.aid, 7, True)
        self.assertEqual(result["state"], "unknown")
        A.execute(preview["request_id"], self.aid, 7, True)
        self.adapter.reset_quota.assert_called_once()
        with self.assertRaises(ValueError):
            A.prepare(self.aid, 7, "reset")
        A.acknowledge(self.aid, preview["request_id"], 7)
        self.assertEqual(A.history(self.aid)[0]["state"], "acknowledged")

    def test_referral_eligibility_is_rechecked_and_sending_never_retries(self):
        preview = A.prepare(self.aid, 7, "referral", "friend@example.com")
        self.adapter.send_referral.assert_not_called()
        self.adapter.referral_eligibility.return_value["available_invites"] = 0
        result = A.execute(preview["request_id"], self.aid, 7, True)
        self.assertFalse(result["ok"])
        self.adapter.send_referral.assert_not_called()
        self.adapter.referral_eligibility.return_value["available_invites"] = 1
        preview = A.prepare(self.aid, 7, "referral", "friend@example.com")
        A.execute(preview["request_id"], self.aid, 7, True)
        A.execute(preview["request_id"], self.aid, 7, True)
        self.adapter.send_referral.assert_called_once()

    def test_auto_reset_requires_opt_in_and_rechecks_live_quota(self):
        A.run_maintenance()
        self.adapter.reset_quota.assert_not_called()
        now = int(time.time())
        G.edit_meta(self.db, self.aid, lambda meta: meta.update(admin_paused=False,
            gpt_settings={"auto_reset": True}, pool_upstream_usage={"five_hour": {"utilization": 100, "reset_at": now + 500}}))
        # Cached quota reached 100%, but another client already restored it.
        A.run_maintenance()
        self.adapter.reset_quota.assert_not_called()

