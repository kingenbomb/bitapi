"""GPT OAuth slow output, request-scoped overloads and opt-in account pauses."""
import io
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
import config
from adapters.openai_compat import UpstreamError
from core import account_runtime as R, gpt_request_policy as policy, gpt_transport, sse
from core.db import DB
from core.pool import Pool
from tests.test_gpt_import import wire
from tests.test_stream_idle_timeout import streaming_upstream, request


def adapter(responses):
    return SimpleNamespace(_open_codex=mock.Mock(side_effect=responses), _database=lambda: None)


def frame(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


def test_capacity_retry_discards_abandoned_metadata_and_closes_every_attempt(monkeypatch):
    monkeypatch.setattr(config, "GPT_CAPACITY_RETRIES", 3)
    rejected = io.BytesIO(frame({"type": "response.created", "response": {"id": "abandoned"}}) +
        frame({"type": "response.failed", "response": {"error": {"code": "server_is_overloaded"}}}))
    completed = io.BytesIO(wire())
    ad = adapter([rejected, completed])
    with mock.patch.object(policy, "time", SimpleNamespace(sleep=mock.Mock(), time=time.time)) as clock:
        events = list(policy.iter_events(ad, {"id": 1}, {}))
    assert ad._open_codex.call_count == 2
    assert all(event.get("response", {}).get("id") != "abandoned" for event in events)
    assert events[-1]["type"] == "response.completed"
    assert rejected.closed and completed.closed
    clock.sleep.assert_called_once_with(.5)
    assert R._FIRST_OUTPUT_DEADLINE.get() is None


def test_http_400_capacity_exhaustion_has_one_bounded_budget(monkeypatch):
    monkeypatch.setattr(config, "GPT_CAPACITY_RETRIES", 3)
    error = UpstreamError(400, "Our servers are currently overloaded")
    ad = adapter(error)
    with mock.patch.object(policy.time, "sleep") as sleep, pytest.raises(UpstreamError):
        list(policy.iter_events(ad, {"id": 1}, {}))
    assert ad._open_codex.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [.5, 1, 2]
    assert error._gpt_capacity_retries_exhausted


def test_large_pre_output_metadata_spools_and_preserves_all_events(monkeypatch):
    opened = []
    original = policy.tempfile.SpooledTemporaryFile
    def spool(*args, **kwargs):
        value = original(*args, **kwargs)
        opened.append(value)
        return value
    monkeypatch.setattr(policy.tempfile, "SpooledTemporaryFile", spool)
    metadata = [{"type": "response.created", "response": {"id": "large", "instructions": "x" * 270000}},
                {"type": "response.in_progress", "response": {"id": "large", "instructions": "y" * 270000}}]
    source = io.BytesIO(b"".join(frame(event) for event in metadata) + wire())
    result = list(policy.iter_events(adapter([source]), {"id": 1}, {}))
    assert result[:2] == metadata
    assert result[-1]["type"] == "response.completed"
    assert opened[0]._rolled and opened[0].closed and source.closed


def test_staging_limit_is_a_request_failure_and_closes_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "PRE_OUTPUT_LIMIT", 128)
    source = io.BytesIO(frame({"type": "response.created", "response": {"instructions": "x" * 256}}))
    with pytest.raises(policy.PreOutputLimitError) as raised:
        list(policy.iter_events(adapter([source]), {"id": 1}, {}))
    assert source.closed
    db = DB(str(tmp_path / "accounts.db"))
    aid = db.upsert_account("gpt", "staging-fixture", status="active")
    Pool(db).mark_failure(aid, raised.value, adapter=SimpleNamespace(name="gpt"))
    assert db.get_account(aid)["status"] == "active"
    for conn in db._conns._live.values():
        conn.close()
    db._conns._live.clear()


def test_capacity_after_output_is_never_replayed():
    source = io.BytesIO(frame({"type": "response.output_text.delta", "delta": "paid output"}) +
        frame({"type": "response.failed", "response": {"error": {"code": "server_is_overloaded"}}}))
    ad = adapter([source])
    events = policy.iter_events(ad, {"id": 1}, {})
    assert next(events)["delta"] == "paid output"
    with pytest.raises(UpstreamError):
        next(events)
    assert ad._open_codex.call_count == 1
    assert source.closed


def test_retry_after_and_high_effort_configuration(monkeypatch):
    error = UpstreamError(503, "Our servers are currently overloaded")
    error.headers = {"Retry-After": "60"}
    assert policy.retry_delay(error, 0) == 8
    error.headers = {}
    error.details = {"error": {"headers": {"retry-after-ms": "7000"}}}
    assert policy.retry_delay(error, 0) == 7
    monkeypatch.setattr(config, "GPT_FIRST_OUTPUT_TIMEOUT", 0)
    monkeypatch.setattr(config, "GPT_HIGH_EFFORT_FIRST_OUTPUT_TIMEOUT", 240)
    assert policy.first_output_timeout({"reasoning": {"effort": "high"}}) == 0
    monkeypatch.setattr(config, "GPT_FIRST_OUTPUT_TIMEOUT", 90)
    assert policy.first_output_timeout({}) == 90
    assert policy.first_output_timeout({"reasoning": {"effort": "xhigh"}}) == 240


@pytest.mark.parametrize("path", ["/headers", "/heartbeat"])
def test_first_output_deadline_cancels_actual_http_io(streaming_upstream, monkeypatch, path):
    url, closed = streaming_upstream
    monkeypatch.setattr(config, "GPT_FIRST_OUTPUT_TIMEOUT", .08)
    monkeypatch.setattr(config, "GPT_TIMEOUT_PENALTY_ENABLED", False)
    opened = []
    def open_response(*args):
        response = gpt_transport.open_request(request(url + path), None)
        opened.append(response)
        return response
    ad = SimpleNamespace(_open_codex=open_response, _database=lambda: None)
    with pytest.raises(sse.FirstOutputTimeout):
        list(policy.iter_events(ad, {"id": 1}, {}))
    if path == "/headers":
        assert closed.wait(1), "the timeout must close the silent socket"
    assert all(response.closed and response.loop.is_closed() for response in opened)
    assert R._FIRST_OUTPUT_DEADLINE.get() is None


def test_first_output_deadline_stops_after_output_and_long_stream_finishes(streaming_upstream, monkeypatch):
    url, _ = streaming_upstream
    monkeypatch.setattr(config, "GPT_FIRST_OUTPUT_TIMEOUT", .15)
    monkeypatch.setattr(config, "GPT_STREAM_IDLE_TIMEOUT", .15)
    opened = []
    def open_response(*args):
        response = gpt_transport.open_request(request(url + "/progress"), None)
        opened.append(response)
        return response
    ad = SimpleNamespace(_open_codex=open_response, _database=lambda: None)
    started = time.monotonic()
    assert list(policy.iter_events(ad, {"id": 1}, {}))[-1]["type"] == "response.completed"
    assert time.monotonic() - started > .3
    assert opened[0].closed and opened[0].loop.is_closed()


def test_credential_refresh_budget_cancels_real_silent_http_connection(streaming_upstream):
    url, closed = streaming_upstream
    req = request(url + '/headers')
    req.add_header('Accept', 'application/json')
    with pytest.raises(R.CredentialRefreshTimeout), R.credential_budget(.08):
        gpt_transport.open_request(req, R.bounded_timeout(120))
    assert closed.wait(1)
    assert R._CREDENTIAL_DEADLINE.get() is None


def test_timeout_penalty_is_opt_in_windowed_and_counted_once(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "accounts.db"))
    aid = db.upsert_account("gpt", "policy-fixture", status="active")
    pool = Pool(db)
    ad = SimpleNamespace(name="gpt")
    monkeypatch.setattr(config, "GPT_TIMEOUT_PENALTY_ENABLED", False)
    pool.mark_failure(aid, sse.FirstOutputTimeout("slow"), adapter=ad)
    assert db.get_account(aid)["status"] == "active"
    assert db.get_account(aid)["temp_until"] == 0
    monkeypatch.setattr(config, "GPT_TIMEOUT_PENALTY_ENABLED", True)
    monkeypatch.setattr(config, "GPT_TIMEOUT_THRESHOLD", 3)
    monkeypatch.setattr(config, "GPT_TIMEOUT_WINDOW", 600)
    monkeypatch.setattr(config, "GPT_TIMEOUT_PAUSE", 300)
    monkeypatch.setattr(policy, "_TIMEOUTS", {})
    now = time.time()
    with mock.patch.object(policy.time, "time", return_value=now):
        for _ in range(2):
            error = sse.StreamIdleTimeout("slow")
            policy.record_timeout(db, aid, error)
            pool.mark_failure(aid, error, adapter=ad)
        assert db.get_account(aid)["temp_until"] == 0
        pool.mark_failure(aid, sse.StreamIdleTimeout("slow"), adapter=ad)
        assert db.get_account(aid)["status"] == "active"
        assert db.get_account(aid)["temp_until"] == int(now + 300)
        assert db.pick_active("gpt") is None
    with mock.patch.object(policy.time, "time", return_value=now + 301):
        assert db.pick_active("gpt")["id"] == aid
    for conn in db._conns._live.values():
        conn.close()
    db._conns._live.clear()


def test_chat_stream_does_not_apply_generic_60_90_second_deadlines(monkeypatch):
    import server
    monkeypatch.setattr(config, "FIRST_TOKEN_TIMEOUT", .01)
    monkeypatch.setattr(config, "STREAM_TOTAL_TIMEOUT", .02)
    class GPT:
        def stream_chat(self, account, model, body, on_sse):
            time.sleep(.06)
            on_sse('data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            on_sse('data: [DONE]\n\n')
    monkeypatch.setattr(server.POOL, "get_valid_account", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(R, "call", lambda db, ad, method, account, *a, **kw: getattr(ad, method)(account, *a, **kw))
    with mock.patch.object(server.POOL, "mark_failure") as failure:
        result = list(server._stream_sse(GPT(), "gpt", "model", {}))
    assert any("hello" in line for line in result)
    failure.assert_not_called()
