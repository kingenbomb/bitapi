"""Request-scoped account attribution and real in-flight capacity.

The mutable request context is shared with copied worker contexts; no thread-local
"last account" can leak into another request or a background probe.
"""
from contextlib import contextmanager
from collections import deque
from contextvars import ContextVar, copy_context
from functools import partial
import threading
import time
import hashlib
import urllib.request

_REQUEST = ContextVar("pool_request", default=None)
_NETWORK = ContextVar("pool_network", default=None)
_DEADLINE = ContextVar("pool_deadline", default=None)
_FIRST_OUTPUT_DEADLINE = ContextVar("gpt_first_output_deadline", default=None)
_CREDENTIAL_DEADLINE = ContextVar("gpt_credential_deadline", default=None)
_LOCK = threading.Lock()
_CAPACITY_CHANGED = threading.Condition(_LOCK)
_WAITING = {}
_INFLIGHT = {}
_FAMILY_ACTIVE = {}
_RECENT = {}


class AccountBusyError(RuntimeError):
    status = 429


class AccountTestTimeout(TimeoutError):
    status = 408


class CredentialRefreshTimeout(TimeoutError):
    code = 504


def bounded_timeout(timeout=None, *, expired=False):
    deadline = _DEADLINE.get()
    first = _FIRST_OUTPUT_DEADLINE.get()
    credential = _CREDENTIAL_DEADLINE.get()
    now = time.monotonic()
    if expired:
        # I/O timers may fire a clock tick early; retain the deadline that
        # selected that timer instead of misclassifying it as stream idleness.
        active = [value for value in (deadline, first, credential) if value is not None]
        if active:
            now = max(now, min(active))
    if deadline is not None:
        remaining = deadline - now
        if remaining <= 0:
            raise AccountTestTimeout("账号测试超时（最多 30 秒），上游未及时返回，请稍后重试")
        timeout = min(timeout, remaining) if timeout is not None else remaining
    if first is not None:
        remaining = first - now
        if remaining <= 0:
            from core.sse import FirstOutputTimeout
            raise FirstOutputTimeout("GPT first output timeout: no semantic output")
        timeout = min(timeout, remaining) if timeout is not None else remaining
    if credential is not None:
        remaining = credential - now
        if remaining <= 0:
            raise CredentialRefreshTimeout("GPT credential refresh timed out after 30 seconds")
        timeout = min(timeout, remaining) if timeout is not None else remaining
    return timeout


@contextmanager
def credential_budget(seconds=30):
    previous = _CREDENTIAL_DEADLINE.get()
    end = time.monotonic() + seconds
    token = _CREDENTIAL_DEADLINE.set(min(previous, end) if previous is not None else end)
    try:
        try:
            yield
        except Exception as error:
            if isinstance(error, TimeoutError) or getattr(error, "code", None) == 28:
                bounded_timeout(expired=True)
            raise
        bounded_timeout()
    finally:
        _CREDENTIAL_DEADLINE.reset(token)


@contextmanager
def first_output_budget(seconds):
    token = _FIRST_OUTPUT_DEADLINE.set(time.monotonic() + seconds if seconds > 0 else None)
    try:
        yield
    finally:
        _FIRST_OUTPUT_DEADLINE.reset(token)


def first_output_received():
    _FIRST_OUTPUT_DEADLINE.set(None)


@contextmanager
def test_budget(seconds=30):
    """Share one GPT test deadline across credentials, slot wait and upstream I/O."""
    previous = _DEADLINE.get()
    end = time.monotonic() + seconds
    token = _DEADLINE.set(min(previous, end) if previous is not None else end)
    try:
        try:
            yield
        except Exception as exc:
            # libcurl may finish its millisecond timer just before our deadline.
            if isinstance(exc, TimeoutError) or getattr(exc, "code", None) == 28:
                raise AccountTestTimeout("账号测试超时（最多 30 秒），上游未及时返回，请稍后重试") from None
            bounded_timeout()
            raise
        bounded_timeout()
    finally:
        _DEADLINE.reset(token)


@contextmanager
def refresh_lock(lock):
    remaining = bounded_timeout()
    acquired = lock.acquire() if remaining is None else lock.acquire(timeout=min(5, remaining))
    if not acquired:
        bounded_timeout()
        raise AccountBusyError("账号正在刷新凭据，排队超过 5 秒，请稍后测试")
    try:
        yield
    finally:
        lock.release()


class AccountContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        ua = (headers.get("user-agent", "") + " " + headers.get("originator", "")).lower()
        token = _REQUEST.set({"group_id": None, "account": None, "client": {
            "owner": hashlib.sha256(headers.get("authorization", headers.get("x-api-key", "")).removeprefix("Bearer ").strip().encode()).hexdigest(),
            "codex": "codex" in ua, "headers": headers,
            "session": headers.get("session_id") or headers.get("x-session-id") or headers.get("x-codex-session-id")}})
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST.reset(token)


def worker_target(fn):
    """Raw threading.Thread does not inherit ContextVars (unlike to_thread)."""
    return partial(copy_context().run, fn)


def bind_group(group):
    ctx = _REQUEST.get()
    if ctx is not None:
        ctx["group_id"] = (group or {}).get("id")


def group_id():
    return (_REQUEST.get() or {}).get("group_id")


def bind_request(body):
    ctx = _REQUEST.get()
    if ctx is not None:
        ctx["model"] = body.get("model")
        client = ctx.setdefault("client", {})
        if body.get("prompt_cache_key"):
            client["session"] = str(body["prompt_cache_key"])


def request_context():
    return _REQUEST.get() or {}


def attribution(channel):
    row = (_REQUEST.get() or {}).get("account")
    return row if row and row["channel"] == channel else None


def capacity(account_id):
    with _LOCK:
        return max(_INFLIGHT.get(account_id, 0), _FAMILY_ACTIVE.get(account_id, 0))


def busy_ids():
    with _LOCK:
        return dict(_INFLIGHT)


def rpm(account_id):
    with _LOCK:
        recent = _RECENT.get(account_id)
        if not recent:
            return 0
        since = time.time() - 60
        while recent and recent[0] <= since:
            recent.popleft()
        return len(recent)


def capacity_limit(db, account):
    parent = account
    if account.get("channel") == "gpt":
        from core.gpt_runtime import credential_account
        parent = credential_account(db, account)
    return parent["id"], parent.get("concurrency") or 0


def saturated(account, db=None):
    family, limit = capacity_limit(db, account) if db is not None else (account["id"], account.get("concurrency") or 0)
    return bool((limit and capacity(family) >= limit) or
                (account.get("base_rpm") and rpm(account["id"]) >= account["base_rpm"]))


@contextmanager
def network(db, account):
    token = _NETWORK.set((db, account))
    try:
        yield
    finally:
        _NETWORK.reset(token)


def urlopen(request, timeout):
    timeout = bounded_timeout(timeout)
    selected = _NETWORK.get()
    is_gpt = bool(selected and selected[1].get("channel") == "gpt")
    if selected and selected[1].get("channel") == "gpt" and (selected[1].get("meta") or {}).get("parent_account_id"):
        from core.gpt_runtime import credential_account
        selected = (selected[0], credential_account(*selected))
    proxy_url = None
    if selected and selected[1].get("proxy_id"):
        db, row = selected
        proxy = db.get_pool_proxy(row["proxy_id"])
        if not proxy or not proxy["enabled"]:
            raise RuntimeError("account proxy is unavailable")
        proxy_url = proxy["url"]
    if is_gpt:
        from core.gpt_transport import open_request
        return open_request(request, timeout, proxy_url)
    if proxy_url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


@contextmanager
def dispatch(db, account, *, record=True, clear_error=True):
    aid = account["id"]
    family, limit = capacity_limit(db, account)
    with _CAPACITY_CHANGED:
        queued = False
        if limit and _FAMILY_ACTIVE.get(family, 0) >= limit:
            import config
            if account.get("channel") != "gpt" or _WAITING.get(family, 0) >= config.GPT_ACCOUNT_MAX_WAITING:
                raise AccountBusyError("account concurrency queue is full")
            end = time.monotonic() + config.GPT_ACCOUNT_WAIT_TIMEOUT
            _WAITING[family] = _WAITING.get(family, 0) + 1
            queued = True
            try:
                while _FAMILY_ACTIVE.get(family, 0) >= limit:
                    left = bounded_timeout(end - time.monotonic())
                    if left <= 0:
                        raise AccountBusyError("account concurrency queue wait timed out")
                    _CAPACITY_CHANGED.wait(left)
                bounded_timeout()
            finally:
                _WAITING[family] -= 1
                if not _WAITING[family]:
                    del _WAITING[family]
        if queued:
            current = db.get_account(aid)
            from core.gpt_runtime import eligible
            if not current or current["status"] != "active" or (current.get("meta") or {}).get("admin_paused") or current.get("temp_until", 0) > time.time() or not eligible(db, current, request_context().get("model"), request_context().get("client")):
                raise AccountBusyError("queued account is no longer available")
        rate = account.get("base_rpm") or 0
        recent = _RECENT.setdefault(aid, deque())
        now = time.time()
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if rate and len(recent) >= rate:
            raise AccountBusyError("account rate limit reached")
        recent.append(now)
        _INFLIGHT[aid] = _INFLIGHT.get(aid, 0) + 1
        _FAMILY_ACTIVE[family] = _FAMILY_ACTIVE.get(family, 0) + 1
    try:
        if record:
            now = int(time.time())
            db.update_account(aid, last_used_at=now, **({"last_error": ""} if clear_error else {}))
            ctx = _REQUEST.get()
            if ctx is not None:
                ctx["account"] = {"id": aid, "channel": account["channel"],
                                  "rate_multiplier": account.get("rate_multiplier", 1)}
        with network(db, account):
            yield
    finally:
        with _CAPACITY_CHANGED:
            remaining = _INFLIGHT[aid] - 1
            if remaining:
                _INFLIGHT[aid] = remaining
            else:
                del _INFLIGHT[aid]
            _FAMILY_ACTIVE[family] -= 1
            if not _FAMILY_ACTIVE[family]:
                del _FAMILY_ACTIVE[family]
            _CAPACITY_CHANGED.notify_all()


def call(db, adapter, method, account, *args, **kwargs):
    with dispatch(db, account):
        try:
            return getattr(adapter, method)(account, *args, **kwargs)
        except Exception as exc:
            if account.get("channel") == "gpt":
                from core.gpt_request_policy import mark_failure
                mark_failure(db, account["id"], exc, request_context().get("model"))
            else:
                from core.account_management import safe_error
                db.update_account(account["id"], last_error=safe_error(exc, account))
            raise
