"""用量榜契约：同口径统计、完整账号集合、分页本人排名和最小信息披露。"""
import os
import tempfile
import time
import unittest
from unittest import mock

_BOOTSTRAP = tempfile.TemporaryDirectory()
os.environ.setdefault("BITAPI_DB", os.path.join(_BOOTSTRAP.name, "bootstrap.db"))
os.environ.setdefault("BITAPI_GROK_AUTH_DIR", os.path.join(_BOOTSTRAP.name, "no-auths"))

import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core import auth  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient, close_test_db  # noqa: E402


class LeaderboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = UserDB(os.path.join(self.tmp.name, "leaderboard.db"))
        self.addCleanup(close_test_db, self.db)
        original_db = portal_state.USER_DB
        self.addCleanup(lambda: portal_state.rebind(original_db))
        billing = Billing(self.db)
        for module in (portal_state, portal_routes, server):
            for name, value in (("USER_DB", self.db), ("BILLING", billing)):
                if module is server and name == "USER_DB":
                    continue
                patch = mock.patch.object(module, name, value)
                patch.start()
                self.addCleanup(patch.stop)
        portal_state.rebind(self.db)
        self.client = TestClient(server.app)
        self.me = self.user("current")
        self.headers = {"Authorization": "Bearer " + auth.issue_token(self.me)}

    def user(self, name, **values):
        uid = self.db.create_user(name + "@example.com", "unused-hash", "AFF-" + name)
        if values:
            self.db.update_user(uid, **values)
        return uid

    def usage(self, user_id, input_tokens, output_tokens=0, cache_tokens=None, age=0):
        snapshot = None if cache_tokens is None else {"total_ctx": input_tokens + cache_tokens}
        log_id = self.db.add_usage(user_id, input_tokens=input_tokens,
                                   output_tokens=output_tokens, pricing_snapshot=snapshot)
        if age:
            with self.db._conn() as conn:
                conn.execute("UPDATE usage_logs SET created_at=? WHERE id=?",
                             (int(time.time()) - age, log_id))

    def get(self, **query):
        response = self.client.get("/api/leaderboard", params=query, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_requires_login_and_valid_query(self):
        self.assertEqual(self.client.get("/api/leaderboard").status_code, 401)
        for query in ({"range": "bad"}, {"page": 0}, {"page_size": 101}, {"page_size": 0}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/leaderboard", params=query,
                                                headers=self.headers).status_code, 422)

    def test_all_accounts_include_no_usage_and_disabled_are_excluded(self):
        self.user("unused")
        disabled = self.user("disabled", status="disabled")
        self.usage(disabled, 99999)
        result = self.get()
        self.assertEqual(result["total_users"], 2)
        self.assertEqual(result["total_tokens"], 0)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["me"]["total_tokens"], 0)
        self.assertIsNone(result["me"]["rank"])

    def test_ranks_input_and_output_with_ties_without_counting_cache(self):
        leader = self.user("leader")
        tied = self.user("tied")
        self.usage(leader, 10, 20, cache_tokens=70)
        self.usage(tied, 20, 10)
        self.usage(self.me, 5, 15)
        result = self.get()
        self.assertEqual([row["rank"] for row in result["rows"]], [1, 1, 3])
        self.assertEqual(result["total_tokens"], 80)
        row = next(row for row in result["rows"] if row["cache_tokens"])
        overview = self.db.usage_overview(leader)
        self.assertEqual(row["total_tokens"], sum(overview[key] for key in
                         ("input_tokens", "output_tokens")))
        self.assertEqual(row["cache_tokens"], 70)
        self.assertEqual(row["cache_rows"], 1)
        self.assertEqual(result["me"]["cache_rows"], 0)

    def test_range_filters_before_ranking_and_pagination_keeps_my_position(self):
        other = self.user("other")
        self.usage(other, 500, age=8 * 86400)
        self.usage(self.me, 20)
        cumulative = self.get(page_size=1)
        self.assertEqual(cumulative["rows"][0]["total_tokens"], 500)
        self.assertEqual(cumulative["me"]["rank"], 2)
        self.assertFalse(cumulative["rows"][0]["is_me"])
        page_two = self.get(page=2, page_size=1)
        self.assertTrue(page_two["rows"][0]["is_me"])
        recent = self.get(range="7d", page_size=1)
        self.assertEqual(recent["me"]["rank"], 1)
        self.assertEqual(recent["total_users"], 2)
        self.assertEqual(recent["total_tokens"], 20)
        self.assertEqual(recent["since"], recent["as_of"] - 7 * 86400)
        month = self.get(range="30d")
        self.assertEqual(month["total_tokens"], 520)
        beyond = self.get(page=100)
        self.assertEqual(beyond["rows"], [])
        self.assertEqual(beyond["total_users"], 2)
        self.assertEqual(beyond["me"]["rank"], 2)

    def test_other_users_have_masked_names_and_no_private_fields(self):
        self.user("private-address", display_name="测试昵称")
        result = self.get()
        row = next(row for row in result["rows"] if not row["is_me"])
        self.assertEqual(row["name"], "测***称")
        for item in result["rows"] + [result["me"]]:
            self.assertFalse({"email", "display_name", "user_id", "avatar", "balance"}.intersection(item))
        self.assertNotIn("private-address", str(result))
