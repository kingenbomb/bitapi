"""Active streams may outlive the idle limit; heartbeats and silent sockets may not."""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from core import account_runtime, codex, gpt_transport, sse


def frame(content):
    return ("data: " + json.dumps(content) + "\n\n").encode()


def test_sse_progress_outlives_old_absolute_deadline():
    clock = [0.0]
    chunks = iter([frame({"choices": [{"delta": {"content": str(index)}}]}) for index in range(5)] + [b"data: [DONE]\n\n", b""])
    class Response:
        def read1(self, size):
            clock[0] += 0.6
            return next(chunks)
    with mock.patch("core.sse.time.monotonic", side_effect=lambda: clock[0]):
        lines = list(sse.iter_sse_lines(Response(), 1))
    assert b"data: [DONE]\n" in lines
    assert clock[0] > 4


@pytest.mark.parametrize("heartbeat", [b": keepalive\n\n", frame({"choices": [{"delta": {"role": "assistant"}}]}), frame({"type": "response.in_progress"})])
def test_sse_heartbeats_do_not_refresh_output_deadline(heartbeat):
    clock = [0.0]
    class Response:
        def read1(self, size):
            clock[0] += 0.4
            return heartbeat
    with mock.patch("core.sse.time.monotonic", side_effect=lambda: clock[0]):
        with pytest.raises(TimeoutError, match="idle timeout"):
            list(sse.iter_sse_lines(Response(), 1))
    assert clock[0] < 1.3


@pytest.mark.parametrize("output", [
    {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}}]},
    {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
    {"type": "response.function_call_arguments.delta", "delta": "{"},
    {"type": "response.image_generation_call.partial_image", "partial_image_b64": "fixture"},
])
def test_reasoning_tools_and_partial_images_are_progress(output):
    assert sse.is_output(output)


@pytest.fixture
def streaming_upstream(monkeypatch):
    # This fixture must exercise direct socket cancellation, not a system proxy
    # that can keep its own upstream connection alive after our client closes.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    closed = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/headers":
                self.connection.settimeout(2)
                if self.connection.recv(1) == b"":
                    closed.set()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if self.path == "/silence":
                    self.wfile.write(frame({"type": "response.output_text.delta", "delta": "hi"}))
                    self.wfile.flush()
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b"":
                        closed.set()
                    return
                for index in range(12):
                    self.wfile.write(b": keepalive\n\n" if self.path == "/heartbeat" else frame({"type": "response.output_text.delta", "delta": "hi"}))
                    self.wfile.flush()
                    time.sleep(0.03)
                self.wfile.write(frame({"type": "response.completed", "response": {"status": "completed", "output": []}}))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                closed.set()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", closed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def request(url):
    return urllib.request.Request(url, headers={"Accept": "text/event-stream"})


def test_real_gpt_transport_keeps_active_stream_and_closes_its_loop(streaming_upstream):
    url, _ = streaming_upstream
    started = time.monotonic()
    with gpt_transport.open_request(request(url + "/progress"), 0.15) as response:
        events = list(codex.iter_events(response, 0.15))
    assert events[-1]["type"] == "response.completed"
    assert time.monotonic() - started > 0.3
    assert response.closed and response.loop.is_closed()


@pytest.mark.parametrize("path", ["/heartbeat", "/silence"])
def test_real_gpt_transport_cancels_idle_transfer_not_just_the_reader(streaming_upstream, path):
    url, closed = streaming_upstream
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="idle timeout"):
        with gpt_transport.open_request(request(url + path), 2) as response:
            list(codex.iter_events(response, 0.12))
    assert time.monotonic() - started < 0.7
    assert closed.wait(0.4), "the canceled upstream must see its socket close"
    assert response.closed and response.loop.is_closed()


def test_admin_hi_budget_still_ends_even_if_model_keeps_outputting(streaming_upstream):
    url, _ = streaming_upstream
    with pytest.raises(account_runtime.AccountTestTimeout), account_runtime.test_budget(0.15):
        with gpt_transport.open_request(request(url + "/progress"), 2) as response:
            list(codex.iter_events(response, 1))
    assert response.closed and response.loop.is_closed()


def test_initial_header_timeout_cancels_socket_and_closes_unreturned_loop(streaming_upstream, monkeypatch):
    url, closed = streaming_upstream
    loops = []
    factory = gpt_transport.asyncio.SelectorEventLoop
    def create_loop():
        loop = factory()
        loops.append(loop)
        return loop
    monkeypatch.setattr(gpt_transport.asyncio, "SelectorEventLoop", create_loop)
    with pytest.raises(account_runtime.AccountTestTimeout), account_runtime.test_budget(0.12):
        gpt_transport.open_request(request(url + "/headers"), 2)
    assert closed.wait(0.4)
    assert len(loops) == 1 and loops[0].is_closed()

