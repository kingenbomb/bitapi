"""网关 HTTP 层真实扣费 —— 用 stub adapter 走完整条 /v1/chat/completions。

已有的 test_stream_billing 直接调 billing.record_usage(单元级),
这里补 FastAPI 层:请求进来 → 鉴权/预检 → adapter 出 usage → 扣费 → 流水。
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "gw.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "nx")
os.environ["BITAPI_JWT_SECRET"] = "gw-secret"
os.environ["BITAPI_API_KEY"] = "sk-gw-master"
os.environ["BITAPI_PRICING_URL"] = ""
os.environ["BITAPI_PRICING_PATH"] = os.path.join(_TMP, "nopricing.json")

import config  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
import server  # noqa: E402
from core import auth  # noqa: E402
from core import credit as C  # noqa: E402
from core.adapter import (BILL_UPSTREAM, CAP_CHAT, Adapter,  # noqa: E402
                          register_adapter)
from core.billing import Billing  # noqa: E402
from core.pricing import Pricing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class StubAdapter(Adapter):
    """假上游:非流式返回固定 usage;流式吐两个 chunk + 带 usage 的终止帧。"""
    name = "stub"
    capabilities = [CAP_CHAT]
    models = ["stub-model"]
    billing_mode = BILL_UPSTREAM
    proxy = True

    def proxy_chat(self, body, stream=False, on_sse=None):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                 "total_tokens": 1_500_000}
        if not stream:
            return {"id": "x", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": "ok"},
                        "finish_reason": "stop"}],
                    "usage": usage}
        import json
        for line in (
            'data: {"choices":[{"index":0,"delta":{"content":"ok"},'
            '"finish_reason":null}]}',
            'data: ' + json.dumps({"choices": [{"index": 0, "delta": {},
                                                "finish_reason": "stop"}],
                                   "usage": usage}),
            "data: [DONE]",
        ):
            on_sse(line)
        return None


_DB = None
_KEY = None
_UID = None


def setUpModule():
    global _DB, _KEY, _UID
    register_adapter(StubAdapter())
    config.API_KEY = "sk-gw-master"
    config.JWT_SECRET = "gw-secret"
    db_path = os.path.join(_TMP, "gw.db")
    config.DB_PATH = db_path
    _DB = UserDB(db_path)
    C.bind(_DB)
    pricing = Pricing(_DB, catalog=None, cache_ttl=0)
    billing = Billing(_DB, pricing=pricing)
    for mod in (portal_state, server, portal_routes):
        mod.USER_DB = _DB
        mod.BILLING = billing
    portal_state.PRICING = pricing
    portal_routes.PRICING = pricing
    # $1/1M in, $2/1M out → 1M in + 0.5M out = $2
    _DB.upsert_pricing("stub-model", None, billing_mode="token",
                       input_price=1, output_price=2)
    pricing.invalidate()
    gid = _DB.create_group(name="gw-paid", supported_models=["*"],
                           billing_policy="balance", rpm_limit=0)
    _UID = _DB.create_user("gw@example.com", auth.hash_password("secret123"),
                           "GWAFF001", group_id=gid)
    key = auth.generate_api_key()
    _DB.create_api_key(_UID, key, name="gw")
    _KEY = key
    C.credit(_UID, 10.0, "recharge", "gw-topup")


def tearDownModule():
    """别把 stub 留在进程级注册表里污染其它测试。"""
    from core import adapter as adapter_mod
    adapter_mod._REGISTRY.pop("stub", None)


class GatewayChargeTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.h = {"Authorization": "Bearer " + _KEY}

    def _balance(self):
        return _DB.get_user(_UID)["balance"]

    def _usage_entries(self):
        return [e for e in _DB.list_ledger(_UID) if e["reason"] == "usage"]

    def test_nonstream_request_deducts_from_balance(self):
        before = self._balance()
        r = self.client.post("/v1/chat/completions", headers=self.h, json={
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["usage"]["prompt_tokens"], 1_000_000)
        self.assertAlmostEqual(self._balance(), before - 2.0, places=6)
        log = _DB.recent_usage(_UID, limit=1)[0]
        self.assertAlmostEqual(log["actual_cost"], 2.0)
        self.assertEqual(log["billing_mode"], "token")
        self.assertEqual(log["pricing_snapshot"]["tier"], "official")
        self.assertEqual(log["pricing_snapshot"]["frt_ms"], log["duration_ms"])

    def test_anthropic_nonstream_records_frt(self):
        r = self.client.post("/v1/messages", headers=self.h, json={
            "model": "stub-model", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200, r.text)
        log = _DB.recent_usage(_UID, limit=1)[0]
        self.assertEqual(log["stream"], 0)
        self.assertEqual(log["pricing_snapshot"]["frt_ms"], log["duration_ms"])

    def test_stream_request_deducts_and_records_frt(self):
        before = self._balance()
        with self.client.stream("POST", "/v1/chat/completions", headers=self.h,
                                json={"model": "stub-model", "stream": True,
                                      "messages": [{"role": "user",
                                                    "content": "hi"}]}) as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())
        self.assertIn("[DONE]", body)
        self.assertAlmostEqual(self._balance(), before - 2.0, places=6)
        log = _DB.recent_usage(_UID, limit=1)[0]
        self.assertEqual(log["stream"], 1)
        self.assertEqual(log["pricing_snapshot"]["end_reason"], "done")
        self.assertIn("frt_ms", log["pricing_snapshot"])

    def test_ledger_entry_per_request(self):
        n = len(self._usage_entries())
        self.client.post("/v1/chat/completions", headers=self.h, json={
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(len(self._usage_entries()), n + 1)
        self.assertLess(self._usage_entries()[0]["amount"], 0)

    def test_master_key_is_not_billed(self):
        before = self._balance()
        n = len(self._usage_entries())
        r = self.client.post("/v1/chat/completions", json={
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-gw-master"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertAlmostEqual(self._balance(), before)
        self.assertEqual(len(self._usage_entries()), n)

    def test_drained_balance_returns_402(self):
        bal = self._balance()
        C.credit(_UID, -bal, "admin", "gw-drain")
        server.BILLING.invalidate()
        r = self.client.post("/v1/chat/completions", headers=self.h, json={
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(r.json()["detail"]["code"], "INSUFFICIENT_BALANCE")
        C.credit(_UID, bal, "admin", "gw-restore")
        server.BILLING.invalidate()


class PlaygroundChatTest(unittest.TestCase):
    """在线体验:JWT 鉴权,其余与网关同路,消费落在同一张 usage_logs 上。"""

    def setUp(self):
        self.client = TestClient(server.app)
        tok = self.client.post("/api/login", json={
            "email": "gw@example.com", "password": "secret123"}).json()["token"]
        self.h = {"Authorization": "Bearer " + tok}
        # 和网关那组用例共用同一个账号,余额是被前面几条花过的。这里补到够花,
        # 否则本组能不能跑要看上面花了多少 —— 那种依赖出问题时最难看出来。
        # idem 键带上用例名,credit 才不会把多次补款当成同一笔。
        bal = _DB.get_user(_UID)["balance"]
        if bal < 5.0:
            C.credit(_UID, 10.0 - bal, "admin", "pg-topup-" + self.id())
            server.BILLING.invalidate()

    def _post(self, headers=None, **over):
        body = {"model": "stub-model",
                "messages": [{"role": "user", "content": "hi"}]}
        body.update(over)
        return self.client.post("/api/playground/chat", json=body,
                                headers=self.h if headers is None else headers)

    def test_requires_login(self):
        self.assertEqual(self._post(headers={}).status_code, 401)
        self.assertEqual(
            self._post(headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_api_key_is_not_accepted(self):
        """体验走 JWT。sk- key 不该在这里通:两套凭据各守各的入口,
        混用会让「密钥禁用了但体验里还能用」这类洞长出来。"""
        r = self._post(headers={"Authorization": "Bearer " + _KEY})
        self.assertEqual(r.status_code, 401, r.text)

    def test_streams_sse_and_bills_like_the_gateway(self):
        before = _DB.get_user(_UID)["balance"]
        n = len(_DB.recent_usage(_UID, limit=99))
        with self.client.stream("POST", "/api/playground/chat", headers=self.h,
                                json={"model": "stub-model",
                                      "messages": [{"role": "user",
                                                    "content": "hi"}]}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith(
                "text/event-stream"))
            self.assertTrue(r.headers.get("x-request-id", "").startswith("req_"))
            body = "".join(r.iter_text())
        self.assertIn("data: ", body)
        self.assertIn("[DONE]", body)
        # 同一套定价:1M in + 0.5M out = $2
        self.assertAlmostEqual(_DB.get_user(_UID)["balance"], before - 2.0,
                               places=6)
        logs = _DB.recent_usage(_UID, limit=99)
        self.assertEqual(len(logs), n + 1)
        log = logs[0]
        self.assertEqual(log["stream"], 1)
        self.assertAlmostEqual(log["actual_cost"], 2.0)
        # 体验没有密钥,这条账不该挂在任何 key 上
        self.assertIsNone(log["api_key_id"])

    def test_stream_flag_false_is_ignored(self):
        """页面要逐字出,非流式那条路没有消费者,所以恒定流式。"""
        r = self._post(stream=False)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))

    def test_generation_model_is_rejected_at_the_door(self):
        """图/视频模型塞进对话框,要在门口说清,而不是让 adapter 抛个看不懂的错。"""
        from core import adapter as adapter_mod
        from core.adapter import Adapter, register_adapter

        class _Gen(Adapter):
            name = "stubgen"
            capabilities = []          # 没有 CAP_CHAT
            kind = "generation"
            models = ["stubgen-image"]

        register_adapter(_Gen())
        try:
            r = self._post(model="stubgen-image")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("不是对话模型", r.json()["detail"])
        finally:
            adapter_mod._REGISTRY.pop("stubgen", None)

    def test_unknown_model_is_404(self):
        self.assertEqual(self._post(model="no-such-model").status_code, 404)

    def test_empty_messages_is_400(self):
        self.assertEqual(self._post(messages=[]).status_code, 400)

    def test_group_model_whitelist_applies(self):
        """广场按白名单裁过,体验也必须按同一份裁 —— 否则点得到、发得出,
        真接进去却 403。"""
        gid = _DB.create_group(name="pg-nostub", supported_models=["other-*"],
                               billing_policy="balance", rpm_limit=0)
        old = _DB.get_user(_UID)["group_id"]
        _DB.update_user(_UID, group_id=gid)
        server.BILLING.invalidate()
        try:
            r = self._post()
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("not allowed", r.json()["detail"])
        finally:
            _DB.update_user(_UID, group_id=old)
            server.BILLING.invalidate()

    def test_drained_balance_returns_402(self):
        bal = _DB.get_user(_UID)["balance"]
        C.credit(_UID, -bal, "admin", "pg-drain")
        server.BILLING.invalidate()
        try:
            r = self._post()
            self.assertEqual(r.status_code, 402, r.text)
            self.assertEqual(r.json()["detail"]["code"], "INSUFFICIENT_BALANCE")
        finally:
            C.credit(_UID, bal, "admin", "pg-restore")
            server.BILLING.invalidate()


if __name__ == "__main__":
    unittest.main()
