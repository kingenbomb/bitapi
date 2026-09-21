"""生图模型走 chat 接口 —— 桥接出图,失败不计费。

用户在普通客户端里选一个生图模型,发出来的是 /v1/chat/completions。这条路以前
会一直走到基类 Adapter.chat 抛空消息的 NotImplementedError,而 SSE 头和第一帧
已经发出去了 —— 客户端看到「200 然后流断掉」,per_request 却照扣一次钱。
"""
import os
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "gen.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "nx")
os.environ["BITAPI_JWT_SECRET"] = "gen-secret"
os.environ["BITAPI_API_KEY"] = "sk-gen-master"
os.environ["BITAPI_PRICING_URL"] = ""
os.environ["BITAPI_PRICING_PATH"] = os.path.join(_TMP, "nopricing.json")

import config  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
import server  # noqa: E402
from core import auth  # noqa: E402
from core import credit as C  # noqa: E402
from core.adapter import BILL_PER_REQUEST, Adapter, register_adapter  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.pricing import Pricing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PRICE = 0.05
_UPSTREAM = {"fail": False, "slow": 0.0}


class StubGen(Adapter):
    """假生图渠道:kind=generation,和真实的生图渠道一样没有 chat()。"""
    name = "stubgen"
    capabilities = []
    kind = "generation"
    billing_mode = BILL_PER_REQUEST
    models = ["stubgen-image"]

    def model_kind(self, model_id):
        return "image" if model_id == "stubgen-image" else None

    def generate(self, _acct, _model, prompt, size="1024x1024", n=1):
        if _UPSTREAM["slow"]:
            time.sleep(_UPSTREAM["slow"])
        if _UPSTREAM["fail"]:
            raise RuntimeError("upstream refused")
        return ["https://cdn.example/%s.png" % (prompt.strip() or "x")]


class _Pool:
    """取号永远给得出;失败标记不落库(这里只关心网关行为)。"""

    def get_valid_account(self, _channel, _adapter):
        return {"id": 1}

    def mark_failure(self, *_a, **_kw):
        pass

    def mark_cooldown(self, *_a, **_kw):
        pass


_DB = None
_KEY = None
_UID = None


def setUpModule():
    global _DB, _KEY, _UID
    register_adapter(StubGen())
    config.API_KEY = "sk-gen-master"
    config.JWT_SECRET = "gen-secret"
    db_path = os.path.join(_TMP, "gen.db")
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
    _DB.upsert_pricing("stubgen-image", None, billing_mode="per_request",
                       per_request_price=PRICE)
    pricing.invalidate()
    gid = _DB.create_group(name="gen-paid", supported_models=["*"],
                           billing_policy="balance", rpm_limit=0)
    _UID = _DB.create_user("gen@example.com", auth.hash_password("secret123"),
                           "GENAFF01", group_id=gid)
    _KEY = auth.generate_api_key()
    _DB.create_api_key(_UID, _KEY, name="gen")
    C.credit(_UID, 10.0, "recharge", "gen-topup")


def tearDownModule():
    from core import adapter as adapter_mod
    adapter_mod._REGISTRY.pop("stubgen", None)


class PromptTest(unittest.TestCase):
    def test_takes_last_user_message(self):
        p = server._gen_prompt([{"role": "user", "content": "第一句"},
                                {"role": "assistant", "content": "好"},
                                {"role": "user", "content": " 画只猫 "}])
        self.assertEqual(p, "画只猫")

    def test_multimodal_keeps_text_parts_only(self):
        p = server._gen_prompt([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x/a.png"}},
            {"type": "text", "text": "画只狗"}]}])
        self.assertEqual(p, "画只狗")

    def test_no_user_text_is_empty(self):
        self.assertEqual(server._gen_prompt([{"role": "system", "content": "hi"}]),
                         "")


class ChatBridgeTest(unittest.TestCase):
    def setUp(self):
        _UPSTREAM.update(fail=False, slow=0.0)
        self.client = TestClient(server.app)
        self.pool = mock.patch.object(server, "POOL", _Pool())
        self.pool.start()
        self.addCleanup(self.pool.stop)

    def _post(self, stream, prompt="cat"):
        return self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + _KEY},
            json={"model": "stubgen-image", "stream": stream,
                  "messages": [{"role": "user", "content": prompt}]})

    def _balance(self):
        return _DB.get_user(_UID)["balance"]

    def test_nonstream_returns_markdown_image_and_charges_once(self):
        before = self._balance()
        r = self._post(stream=False)
        self.assertEqual(r.status_code, 200, r.text[:300])
        content = r.json()["choices"][0]["message"]["content"]
        self.assertEqual(content, "![](https://cdn.example/cat.png)")
        self.assertAlmostEqual(before - self._balance(), PRICE, places=6)

    def test_stream_emits_image_and_charges_once(self):
        before = self._balance()
        r = self._post(stream=True)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("![](https://cdn.example/cat.png)", r.text)
        self.assertIn("data: [DONE]", r.text)
        self.assertAlmostEqual(before - self._balance(), PRICE, places=6)

    def test_stream_failure_says_why_and_does_not_charge(self):
        _UPSTREAM["fail"] = True
        before = self._balance()
        logs_before = _DB.usage_count(_UID, 0)
        r = self._post(stream=True)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn("[生成失败]", r.text)
        self.assertIn("data: [DONE]", r.text)       # 流是收尾的,不是被掐断的
        self.assertEqual(self._balance(), before)
        self.assertEqual(_DB.usage_count(_UID, 0), logs_before)

    def test_nonstream_failure_is_502_and_does_not_charge(self):
        _UPSTREAM["fail"] = True
        before = self._balance()
        r = self._post(stream=False)
        self.assertEqual(r.status_code, 502, r.text[:300])
        self.assertEqual(self._balance(), before)

    def test_slow_generation_keeps_the_connection_fed(self):
        """CF 在原点 100 秒不出字节时掐断,所以等图期间必须有心跳。"""
        _UPSTREAM["slow"] = 0.25
        with mock.patch.object(server, "GEN_KEEPALIVE_EVERY", 0.05):
            r = self._post(stream=True)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn(": keepalive", r.text)
        self.assertIn("![](https://cdn.example/cat.png)", r.text)

    def test_empty_prompt_is_400(self):
        r = self._post(stream=False, prompt="   ")
        self.assertEqual(r.status_code, 400, r.text[:300])


class RehostTest(unittest.TestCase):
    """/v1/images/generations:上游 URL 一律抓到自家域名;response_format=b64_json
    改内联字节;抓取失败回退原始 URL,不丢空 data。"""

    def setUp(self):
        _UPSTREAM.update(fail=False, slow=0.0)
        self.client = TestClient(server.app)
        self.pool = mock.patch.object(server, "POOL", _Pool())
        self.pool.start()
        self.addCleanup(self.pool.stop)

    def _gen(self, **extra):
        return self.client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer " + _KEY},
            json={"model": "stubgen-image", "prompt": "cat", **extra})

    def _fake_store(self):
        # 假装抓取成功:返回 token 文件名 + 一个存在的临时文件(b64 模式要读它)。
        from core import media_cache
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=_TMP)
        f.write(b"\x89PNG-bytes")
        f.close()
        return mock.patch.object(
            media_cache, "fetch_and_store",
            return_value=("tok123.png", f.name, "image/png"))

    def test_url_mode_returns_self_hosted_url(self):
        with self._fake_store():
            r = self._gen()
        self.assertEqual(r.status_code, 200, r.text[:300])
        url = r.json()["data"][0]["url"]
        self.assertIn("/media/gen/tok123.png", url)
        self.assertNotIn("cdn.example", url)

    def test_b64_mode_returns_inline_bytes(self):
        import base64
        with self._fake_store():
            r = self._gen(response_format="b64_json")
        self.assertEqual(r.status_code, 200, r.text[:300])
        d = r.json()["data"][0]
        self.assertNotIn("url", d)
        self.assertEqual(base64.b64decode(d["b64_json"]), b"\x89PNG-bytes")

    def test_fetch_failure_falls_back_to_original_url(self):
        from core import media_cache
        with mock.patch.object(media_cache, "fetch_and_store",
                               side_effect=RuntimeError("blocked")):
            r = self._gen()
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["data"][0]["url"], "https://cdn.example/cat.png")


class GenRateLimitTest(unittest.TestCase):
    """生成渠道的按渠道限速(护上游账号,不是护本站成本)。

    生图渠道高频调用会被上游不可逆封号,所以 adapter 用 max_rpm 声明全站合计
    上限,超了 429。这里断言的是「超限请求不碰账号、不扣钱」。
    """

    def setUp(self):
        _UPSTREAM.update(fail=False, slow=0.0)
        self.client = TestClient(server.app)
        self.pool = mock.patch.object(server, "POOL", _Pool())
        self.pool.start()
        self.addCleanup(self.pool.stop)
        server._gen_rpm.clear()
        self.addCleanup(server._gen_rpm.clear)
        # StubGen 本身没声明 max_rpm(不限速),这里临时给它挂一个。
        p = mock.patch.object(StubGen, "max_rpm", 1, create=True)
        p.start()
        self.addCleanup(p.stop)

    def _gen(self):
        return self.client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer " + _KEY},
            json={"model": "stubgen-image", "prompt": "cat"})

    def _balance(self):
        return _DB.get_user(_UID)["balance"]

    def test_over_limit_is_429_and_does_not_charge(self):
        self.assertEqual(self._gen().status_code, 200)
        before = self._balance()
        r = self._gen()
        self.assertEqual(r.status_code, 429, r.text[:300])
        self.assertIn("rate limit", r.text)
        self.assertEqual(self._balance(), before, "被限速的请求不该扣钱")

    def test_no_max_rpm_means_unlimited(self):
        """没声明 max_rpm 的渠道行为不变,一律放行。"""
        with mock.patch.object(StubGen, "max_rpm", 0):
            server._gen_rpm.clear()
            for _ in range(5):
                self.assertEqual(self._gen().status_code, 200)


if __name__ == "__main__":
    unittest.main()



