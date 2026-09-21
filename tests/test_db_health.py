import os
import tempfile
import unittest
from unittest import mock

from core.db import DB, ST_ACTIVE, ST_UNCHECKED


class DBHealthTest(unittest.TestCase):
    def test_pool_sync_does_not_forge_health_check_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "bitapi.db"))
            account_id = db.upsert_account("grok", "sync@example.com",
                                           status=ST_ACTIVE)
            db.update_account(account_id, last_check=111)
            db.upsert_accounts_bulk("grok", [(
                "sync@example.com", {"access_token": "new"}, {}, ST_ACTIVE, 999,
            )])
            new_id = db.upsert_accounts_bulk("grok", [(
                "new-sync@example.com", {"access_token": "new"}, {}, ST_UNCHECKED, 999,
            )])

            self.assertEqual(db.get_account(account_id)["last_check"], 111)
            self.assertEqual(db.get_by_identity(
                "grok", "new-sync@example.com")["last_check"], 0)
            self.assertEqual(new_id, 1)
            db.close()

    def test_record_check_updates_timestamp_when_status_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "bitapi.db"))
            account_id = db.upsert_account("grok", "same@example.com",
                                           status=ST_ACTIVE)
            with mock.patch("core.db.time.time", return_value=123456):
                db.record_account_checks("grok", [("same@example.com", ST_ACTIVE)])

            account = db.get_account(account_id)
            self.assertEqual(account["status"], ST_ACTIVE)
            self.assertEqual(account["last_check"], 123456)
            db.close()

    def test_record_check_changes_status_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "bitapi.db"))
            account_id = db.upsert_account("grok", "new@example.com",
                                           status=ST_UNCHECKED)
            with mock.patch("core.db.time.time", return_value=654321):
                db.record_account_checks("grok", [("new@example.com", ST_ACTIVE)])

            account = db.get_account(account_id)
            self.assertEqual(account["status"], ST_ACTIVE)
            self.assertEqual(account["last_check"], 654321)
            db.close()


if __name__ == "__main__":
    unittest.main()
