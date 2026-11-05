"""GPT HTTP transport with a stable TLS fingerprint and account proxy."""
import io
import urllib.error
import asyncio

from curl_cffi import requests
from curl_cffi.const import CurlOpt


class Response:
    def __init__(self, session, response):
        self.session, self.response = session, response
        self.headers = response.headers
        self.chunks = iter(response.iter_content())
        self.pending = bytearray()

    def read1(self, size=16384):
        if self.pending:
            part = bytes(self.pending[:size])
            del self.pending[:size]
            return part
        part = next(self.chunks, b"")
        self.pending.extend(part[size:])
        return part[:size]

    def read(self, size=-1):
        if size < 0:
            return bytes(self.pending) + b"".join(self.chunks)
        out = bytearray()
        while len(out) < size:
            part = self.read1(size - len(out))
            if not part:
                break
            out.extend(part)
        return bytes(out)

    def close(self):
        self.response.close()
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class StreamResponse:
    """Synchronous adapter over cancelable curl I/O on this worker's event loop.

    A timed queue read must cancel the actual transfer, including silent sockets;
    the threaded curl response's close() otherwise waits for its network worker.
    """
    def __init__(self, request, timeout, proxy, *, impersonate="chrome136"):
        from core.account_runtime import bounded_timeout
        budget = bounded_timeout(timeout)
        self.loop = asyncio.SelectorEventLoop()
        self.session = requests.AsyncSession(loop=self.loop, impersonate=impersonate, max_clients=1,
            curl_options={CurlOpt.LOW_SPEED_TIME: 0, CurlOpt.LOW_SPEED_LIMIT: 0,
                          CurlOpt.CONNECTTIMEOUT_MS: 30000})
        self.response = None
        self.closed = False
        self.pending = bytearray()
        import config
        self.read_timeout = timeout if timeout is not None else config.GPT_STREAM_IDLE_TIMEOUT
        try:
            self.response = self.loop.run_until_complete(asyncio.wait_for(self.session.request(
                request.get_method(), request.full_url, data=request.data,
                headers=dict(request.header_items()), timeout=timeout,
                proxy=proxy, stream=True, allow_redirects=False), timeout=budget))
            self.headers = self.response.headers
            self.chunks = self.response.aiter_content()
        except TimeoutError:
            from core.sse import StreamIdleTimeout
            self.close()
            bounded_timeout(expired=timeout is None or budget < timeout)
            raise StreamIdleTimeout("upstream stream idle timeout: no model output") from None
        except BaseException:
            self.close()
            raise

    def set_read_timeout(self, timeout):
        self.read_timeout = timeout

    def read1(self, size=16384):
        from core.account_runtime import bounded_timeout
        if self.pending:
            part = bytes(self.pending[:size])
            del self.pending[:size]
            return part
        budget = bounded_timeout(self.read_timeout)
        try:
            chunk = self.loop.run_until_complete(asyncio.wait_for(
                self.chunks.__anext__(), timeout=budget))
        except StopAsyncIteration:
            return b""
        except TimeoutError:
            from core.sse import StreamIdleTimeout
            bounded_timeout(expired=budget < self.read_timeout)
            raise StreamIdleTimeout("upstream stream idle timeout: no model output") from None
        self.pending.extend(chunk[size:])
        return chunk[:size]

    def read(self, size=-1):
        out = bytearray()
        while size < 0 or len(out) < size:
            chunk = self.read1(16384 if size < 0 else size - len(out))
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.loop.run_until_complete(self.session.close())
            if self.response is not None:
                self.loop.run_until_complete(self.response.aclose())
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        finally:
            self.loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_request(request, timeout, proxy=None):
    from core.account_runtime import bounded_timeout
    if "text/event-stream" in str(request.get_header("Accept", "")).lower():
        wrapper = StreamResponse(request, timeout, proxy)
        if wrapper.response.status_code >= 300:
            try:
                body = wrapper.read(65536)
            finally:
                wrapper.close()
            raise urllib.error.HTTPError(request.full_url, wrapper.response.status_code,
                "GPT upstream rejected request", wrapper.headers, io.BytesIO(body))
        return wrapper
    remaining = bounded_timeout()
    options = {"curl_options": {CurlOpt.TIMEOUT_MS: max(1, int(remaining * 1000))}} if remaining is not None else {}
    # curl_cffi's stream timeout is only a low-speed timeout. The test budget
    # must also stop streams that keep sending heartbeats without an answer.
    session = requests.Session(impersonate="chrome136", **options)
    try:
        response = session.request(request.get_method(), request.full_url,
            data=request.data, headers=dict(request.header_items()), timeout=timeout,
            proxy=proxy, stream=True, allow_redirects=False)
        wrapper = Response(session, response)
        if response.status_code >= 300:
            body = wrapper.read(65536)
            wrapper.close()
            raise urllib.error.HTTPError(request.full_url, response.status_code, "GPT upstream rejected request", response.headers, io.BytesIO(body))
        return wrapper
    except Exception:
        session.close()
        raise
