# -*- coding: utf-8 -*-
"""流式首字超时:上游首帧迟迟不来时,按预算换号重试,别让客户端干等满 SEND_TIMEOUT。"""
import time

import config
import server
from core.adapter import Adapter


class _NeverYields(Adapter):
    name = "slow-probe"

    def stream_chat(self, acct, model, body, on_sse):
        time.sleep(30)          # 永不吐首帧(模拟上游卡住)


def _patch(monkeypatch, first_tt, total_tt):
    calls = []
    monkeypatch.setattr(config, "FIRST_TOKEN_TIMEOUT", first_tt)
    monkeypatch.setattr(config, "STREAM_TOTAL_TIMEOUT", total_tt)
    monkeypatch.setattr(server.POOL, "get_valid_account",
                        lambda ch, ad: {"id": 42})
    monkeypatch.setattr(server.POOL, "mark_failure",
                        lambda *a, **k: calls.append(a) or "rotate")
    return calls


def test_first_token_timeout_is_bounded_by_total_budget(monkeypatch):
    calls = _patch(monkeypatch, first_tt=1, total_tt=2)
    holder = {}
    t0 = time.time()
    out = list(server._stream_sse(_NeverYields(), "slow-probe", "m", {},
                                  max_switch=6, usage_holder=holder, err_holder={}))
    dt = time.time() - t0
    assert out == []                      # 一帧都没转发
    assert dt < 6, "受总预算约束,不能 6 次 x first_tt 累加: %.1fs" % dt
    assert calls, "每次首字超时都该标记失败(轮换)" 


def test_first_frame_arriving_stops_the_timer(monkeypatch):
    """第一帧到了之后不能再被首字超时打断(长回答本来就该慢慢流)。"""
    _patch(monkeypatch, first_tt=1, total_tt=2)

    class _SlowButAlive(Adapter):
        name = "slow-probe"

        def stream_chat(self, acct, model, body, on_sse):
            on_sse('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')   # 首帧立刻到
            time.sleep(2.5)              # 间隔超过 total_tt(2s),但首帧已出
            on_sse('data: {"choices":[{"delta":{"content":" there"}}]}\n\n')
            on_sse("data: [DONE]\n\n")

    out = list(server._stream_sse(_SlowButAlive(), "slow-probe", "m", {},
                                  max_switch=2, usage_holder={}, err_holder={}))
    assert any("hi" in line for line in out)
    assert any("there" in line for line in out), "首帧之后不该再被超时打断"


def test_output_idle_timeout_ignores_heartbeats_after_first_output(monkeypatch):
    _patch(monkeypatch, first_tt=1, total_tt=2)
    monkeypatch.setattr(config, "STREAM_IDLE_TIMEOUT", 0.08)
    class StallsAfterOutput(Adapter):
        name = "slow-probe"
        def stream_chat(self, acct, model, body, on_sse):
            on_sse('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            for _ in range(10):
                time.sleep(0.02)
                on_sse(": keepalive\n\n")
    holder = {}
    out = list(server._stream_sse(StallsAfterOutput(), "slow-probe", "m", {}, max_switch=1, usage_holder=holder))
    assert any("hi" in line for line in out)
    assert holder["failed_reason"] == "idle_timeout"
    assert not holder.get("terminal")
