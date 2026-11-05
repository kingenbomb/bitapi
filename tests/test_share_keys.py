#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 Key(用户上传上游 key 换余额奖励)的门禁。

  1. 开关:默认关;开了才收
  2. 验活:上游 401 的 key 不入库、不发奖 —— 垃圾 key 进不了号池
  3. 奖励:验活通过入库 + 按把发钱,且**一把 key 全站只发一次**(幂等靠流水)
  4. 边界:渠道不收 / 超出每人上限 / key 格式不对,都是 4xx 且无副作用
  5. 管理端:共享清单;非管理员 403

上游用 class 级 mock 顶掉 OpenAICompatAdapter._post,不出网。
"""
import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "share.db")
os.environ.setdefault("BITAPI_JWT_SECRET", "test-secret")

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter, UpstreamError  # noqa: E402
from core import pool_state  # noqa: E402
from core.adapter import register_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from tests.registration_fixture import TestClient  # noqa: E402

# 共享 key 只收「key 池型 + 支持对话」的渠道(core/share.py 的 _shareable):
# 要 proxy=False、stateless_keys=True 且 key_identity 可调用。gpt 是账号池型
# (凭据存库、不支持共享 key),所以这里现注册一个 OpenAICompatAdapter 承载用例。
SHARE_CHANNEL = "sharepool"
register_adapter(OpenAICompatAdapter(name=SHARE_CHANNEL,
                                     base_url="https://share.example.com/v1",
                                     models=["share-model"]))

OK_REPLY = {"id": "x", "model": "claude-opus-4-6",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "pong"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


class _Json:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def close(self):
        pass


PROBED = []


def fake_post(self, body, api_key, timeout):
    PROBED.append({"url": self._url(), "key": api_key, "model": body.get("model")})
    return _Json(dict(OK_REPLY))


# 每把 key 全站只发一次奖励(幂等靠流水,跨用例不清理),所以用例之间必须用不同的
# key —— 否则第二个用例拿到的是「已经被收录过了」。key 在 setUp 里按用例现生成。


class ShareKeysTest(unittest.TestCase):
    _seq = 0

    @classmethod
    def setUpClass(cls):
        config.JWT_SECRET = "test-secret"
        config.REQUIRE_INVITE = False
        db_path = os.path.join(_TMP, "share-ep.db")
        config.DB_PATH = db_path
        fresh = UserDB(db_path)
        billing = Billing(fresh, pricing=portal_state.PRICING)
        for mod in (portal_state, server, portal_routes):
            mod.USER_DB = fresh
            mod.BILLING = billing
        portal_state.rebind(fresh)
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/register", json={"email": "root@gmail.com",
                                                   "password": "secret123"})
        assert r.status_code == 200, r.text
        cls.admin_h = {"Authorization": "Bearer " + r.json()["token"]}
        cls._patch = mock.patch.object(OpenAICompatAdapter, "_post", fake_post)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()

    def _new_user(self, tag):
        """每个用例一对全新用户:余额与流水从零起算,不被上个用例的奖励污染。
        邮箱必须是 Gmail 主地址(local 部分只允许 [a-z0-9]+),见 core/registration.py。"""
        ShareKeysTest._seq += 1
        r = self.client.post("/api/register", json={
            "email": f"share{ShareKeysTest._seq}{tag}@gmail.com",
            "password": "secret123"})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["user_id"]

    def setUp(self):
        PROBED.clear()
        pool_state.DB.delete_channel(SHARE_CHANNEL)
        # 每个用例从「功能关闭」这个干净状态起步(删掉设置 = 回落 env 默认)
        self.client.delete("/api/admin/settings/share_rewards", headers=self.admin_h)
        self.user_h, self.uid = self._new_user("a")
        self.user2_h, self.uid2 = self._new_user("b")
        self.key_a = "sk-" + uuid.uuid4().hex
        self.key_b = "sk-" + uuid.uuid4().hex

    # ---- 辅助 ----

    def _enable(self, **over):
        body = {"enabled": True, "amount": 2.0, "channels": [SHARE_CHANNEL], "max_per_user": 0}
        body.update(over)
        return self.client.patch("/api/admin/settings", json={"share_rewards": body},
                                 headers=self.admin_h)

    def _submit(self, key=None, channel=SHARE_CHANNEL, headers=None):
        return self.client.post("/api/share/keys",
                                json={"channel": channel, "key": key or self.key_a},
                                headers=headers or self.user_h)

    def _balance(self, uid=None):
        return portal_routes.USER_DB.get_user(uid or self.uid)["balance"]

    # ---- 开关 ----

    def test_disabled_by_default_rejects(self):
        r = self._submit()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("未开启", r.json()["detail"])
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 0)
        self.assertEqual(self._balance(), 0)

    def test_enable_then_accepts(self):
        self.assertEqual(self._enable().status_code, 200)
        r = self._submit()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reward"], 2.0)
        self.assertEqual(r.json()["channel"], SHARE_CHANNEL)

    # ---- 验活 ----

    def test_probe_failure_leaves_no_trace(self):
        self._enable()
        with mock.patch.object(OpenAICompatAdapter, "_post",
                               side_effect=UpstreamError(401, '{"error":"invalid api key"}')):
            r = self._submit()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("验活失败", r.json()["detail"])
        self.assertIn("invalid api key", r.json()["detail"])
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 0)
        self.assertEqual(self._balance(), 0)

    def test_probe_uses_channel_first_model_and_real_key(self):
        self._enable()
        self._submit()
        self.assertEqual(PROBED[-1]["key"], self.key_a)
        # 验活打的是渠道清单里的第一个模型
        self.assertEqual(PROBED[-1]["model"], "share-model")
        self.assertTrue(PROBED[-1]["url"].startswith("https://share.example.com/v1"))

    # ---- 奖励与幂等 ----

    def test_accepted_key_lands_in_pool_and_pays_once(self):
        self._enable()
        r = self._submit()
        self.assertEqual(r.status_code, 200, r.text)
        identity = r.json()["identity"]
        self.assertTrue(identity.startswith(SHARE_CHANNEL + "-"))
        acct = pool_state.DB.get_by_identity(SHARE_CHANNEL, identity)
        self.assertIsNotNone(acct)
        self.assertEqual(acct["status"], "active")
        self.assertEqual(acct["secret"]["api_key"], self.key_a)
        self.assertEqual(acct["meta"]["shared_by"], self.uid)
        # 奖励到账,且流水 reason 是 share_key(不是直接改 balance)
        self.assertEqual(self._balance(), 2.0)
        ledger = portal_routes.USER_DB.list_ledger(self.uid, reason="share_key")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["idem_key"], f"sharekey:{SHARE_CHANNEL}:{identity}")

        # 同一把 key 再交一次:400,余额不动
        r2 = self._submit()
        self.assertEqual(r2.status_code, 400, r2.text)
        self.assertEqual(self._balance(), 2.0)
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 1)

    def test_other_user_cannot_double_claim_same_key(self):
        self._enable()
        self.assertEqual(self._submit().status_code, 200)
        r = self._submit(headers=self.user2_h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._balance(self.uid2), 0)
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 1)

    def test_max_per_user_caps_rewards(self):
        self._enable(max_per_user=1)
        self.assertEqual(self._submit(key=self.key_a).status_code, 200)
        r = self._submit(key=self.key_b)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("上限", r.json()["detail"])
        self.assertEqual(self._balance(), 2.0)
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 1)

    # ---- 边界 ----

    def test_channel_not_allowed(self):
        self._enable(channels=["gpt"])
        r = self._submit()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不在允许共享", r.json()["detail"])

    def test_empty_channel_list_means_all_shareable(self):
        self._enable(channels=[])
        self.assertEqual(self._submit().status_code, 200)

    def test_non_chat_channel_rejected(self):
        # gpt 是账号池型(凭据存库、非 key 池),存在但不可共享
        self._enable(channels=[])
        r = self._submit(channel="gpt")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不支持共享", r.json()["detail"])

    def test_unknown_channel_rejected(self):
        self._enable()
        r = self._submit(channel="ghost")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("未知渠道", r.json()["detail"])

    def test_bad_key_shape_rejected(self):
        self._enable()
        r = self._submit(key="short")
        self.assertEqual(r.status_code, 422)   # pydantic min_length 先挡
        self.assertEqual(pool_state.DB.count_accounts(channel=SHARE_CHANNEL), 0)

    def test_confirmation_gate_requires_login(self):
        self._enable()
        r = self.client.post("/api/share/keys",
                             json={"channel": SHARE_CHANNEL, "key": "sk-" + uuid.uuid4().hex})
        self.assertEqual(r.status_code, 401)

    # ---- 设置校验 ----

    def test_settings_validation_rejects_bad_values(self):
        # 越过 0 / 负数由 pydantic 挡(422),其余由 site_settings.validate 挡(400)。
        # 两条路都不能让坏值进库。
        cases = [
            ({"amount": -1}, {422}, ""),
            ({"max_per_user": -1}, {422}, ""),
            ({"amount": 5000}, {400}, "不能超过"),
            ({"enabled": True, "amount": 0, "channels": [], "max_per_user": 0}, {400}, "大于 0"),
            ({"channels": ["ghost"]}, {400}, "未知渠道"),
        ]
        for over, codes, needle in cases:
            with self.subTest(over=over):
                r = self._enable(**over)
                self.assertIn(r.status_code, codes, r.text)
                if needle:
                    self.assertIn(needle, r.json()["detail"])
        self.assertIsNone(portal_routes.USER_DB.get_setting("share_rewards"))

    # ---- 视图 ----

    def test_summary_reports_rule_and_earnings(self):
        self._enable()
        self._submit()
        r = self.client.get("/api/share/keys", headers=self.user_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["amount"], 2.0)
        self.assertEqual(body["rewarded_count"], 1)
        self.assertEqual(body["earned"], 2.0)
        self.assertEqual(len(body["items"]), 1)
        self.assertNotIn(self.key_a, json.dumps(body["items"]))   # 不回明文 key

    def test_admin_contributions_listed_and_gated(self):
        self._enable()
        self._submit()
        r = self.client.get("/api/admin/share/contributions", headers=self.user_h)
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/api/admin/share/contributions", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["contributions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], SHARE_CHANNEL)
        self.assertEqual(rows[0]["shared_by"], self.uid)
        self.assertNotIn(self.key_a, json.dumps(rows))       # 不回明文 key

    def test_admin_settings_expose_share_rewards(self):
        self._enable(amount=2.5)
        body = self.client.get("/api/admin/settings", headers=self.admin_h).json()
        self.assertEqual(body["share_rewards"]["amount"], 2.5)
        self.assertTrue(body["share_rewards"]["enabled"])
        self.assertTrue(body["from_db"]["share_rewards"])
        self.assertFalse(body["defaults"]["share_rewards"]["enabled"])


if __name__ == "__main__":
    unittest.main()
