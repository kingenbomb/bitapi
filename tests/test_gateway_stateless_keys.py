"""透传型 key 池在网关热路径上的接线 —— 声明了 stateless_keys 就必须真的不罚号。

tests/test_pool_failure.py 用假库验 Pool.mark_failure 的口径,那一层过了不代表
线上生效:网关是否把 adapter 传下去,是另一件事。把 server.py 那四处
`adapter=adapter` 删掉,纯单元测试全绿,而好 key 照旧被 park 半小时 —— 所以这里
走真 server._sync / server._stream,只让上游抛错。
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "stateless.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "nx")
os.environ["BITAPI_PRICING_URL"] = ""
os.environ["BITAPI_PRICING_PATH"] = os.path.join(_TMP, "nopricing.json")

import config  # noqa: E402
import server  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.adapter import CAP_CHAT, Adapter, register_adapter  # noqa: E402


class _Upstream(Exception):
    """带 HTTP 状态码的上游错误,和 adapters 抛的形状一致(classify_failure 按 code 判)。"""

    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg)


class _FlakyAdapter(Adapter):
    """上游每次都抛错的假 key 池渠道。raise_with 由用例改。"""
    name = "flaky_stateless"
    capabilities = [CAP_CHAT]
    models = ["flaky-model"]
    stateless_keys = True
    raise_with = None

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        if self.raise_with is not None:
            raise self.raise_with
        return {"content": "", "raw": None}   # 空回复:走 mark_exhausted 那条路


class _PenalizedAdapter(_FlakyAdapter):
    """同形状但没声明豁免 —— 用来确认豁免是渠道级的,不是被全局放宽了。"""
    name = "flaky_penalized"
    stateless_keys = False


ADAPTERS = {}


def setUpModule():
    for cls in (_FlakyAdapter, _PenalizedAdapter):
        ad = cls()
        ADAPTERS[ad.name] = ad
        register_adapter(ad)


class StatelessKeysGatewayTest(unittest.TestCase):
    def _fresh_account(self, channel):
        aid = server.DB.upsert_account(channel, "id-" + channel,
                                       secret={"api_key": "k"},
                                       status=dbmod.ST_ACTIVE)
        server.DB.update_account(aid, status=dbmod.ST_ACTIVE)
        return aid

    def _run_sync(self, adapter, exc):
        adapter.raise_with = exc
        aid = self._fresh_account(adapter.name)
        with self.assertRaises(Exception):
            server._sync(adapter, adapter.name, [{"role": "user", "content": "x"}],
                         "flaky-model")
        return aid, server.DB.get_account(aid)["status"]

    def test_upstream_5xx_leaves_key_active(self):
        """上游 502 是「上游此刻不好」,不是「这把 key 坏了」。"""
        _, status = self._run_sync(ADAPTERS["flaky_stateless"],
                                   _Upstream(502, "Model gateway is unavailable"))
        self.assertEqual(status, dbmod.ST_ACTIVE)

    def test_upstream_400_leaves_key_active(self):
        """实测 AMD 网关会对好 key 回 400「Unsupported model」,一分钟后自愈 ——
        400 落 transient,不豁免的话一次抖动就 park 整个渠道。"""
        _, status = self._run_sync(ADAPTERS["flaky_stateless"],
                                   _Upstream(400, "Unsupported model"))
        self.assertEqual(status, dbmod.ST_ACTIVE)

    def test_empty_reply_leaves_key_active(self):
        _, status = self._run_sync(ADAPTERS["flaky_stateless"], None)
        self.assertEqual(status, dbmod.ST_ACTIVE)

    def test_upstream_401_still_retires_key(self):
        """豁免不包括凭据失效:401 说的就是这把 key 本身不行。"""
        _, status = self._run_sync(ADAPTERS["flaky_stateless"],
                                   _Upstream(401, "unauthorized"))
        self.assertEqual(status, dbmod.ST_EXHAUSTED)

    def test_channel_without_declaration_still_cools_down(self):
        _, status = self._run_sync(ADAPTERS["flaky_penalized"],
                                   _Upstream(502, "Bad Gateway"))
        self.assertEqual(status, dbmod.ST_COOLDOWN)

    def test_stream_path_also_leaves_key_active(self):
        """非流式和流式是两条独立的取号/落状态路径,得各验一次。"""
        ad = ADAPTERS["flaky_stateless"]
        ad.raise_with = _Upstream(502, "Model gateway is unavailable")
        aid = self._fresh_account(ad.name)
        list(server._stream(ad, ad.name, [{"role": "user", "content": "x"}],
                            "flaky-model"))
        self.assertEqual(server.DB.get_account(aid)["status"], dbmod.ST_ACTIVE)


if __name__ == "__main__":
    unittest.main()
