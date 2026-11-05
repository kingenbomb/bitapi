"""GPT OAuth request deadlines, bounded capacity retries and opt-in timeout penalties."""
from collections import deque
from email.utils import parsedate_to_datetime
import math
import json
import threading
import time
import tempfile

import config
from core import account_runtime as R, codex, sse

_TIMEOUTS = {}
_RATE_RETRIES = {}
_LOCK = threading.Lock()
PRE_OUTPUT_LIMIT = 8 * 1024 * 1024
OAUTH_429_RETRY_WINDOW = 120


class PreOutputLimitError(ValueError):
    pass


def status_code(error):
    for value in (getattr(error, "code", None), getattr(error, "status_code", None), getattr(error, "status", None)):
        if isinstance(value, int) and 400 <= value <= 599:
            return value
    return None


def failure_scope(error, phase="request"):
    if isinstance(error, (R.AccountBusyError, R.AccountTestTimeout)):
        return "local"
    code = status_code(error)
    if code in (401, 403) or (phase == "refresh" or getattr(error, "_gpt_refresh_error", False)) and "invalid_grant" in str(error).lower():
        return "auth"
    if code == 402:
        return "quota"
    if code == 429:
        return "rate_limit"
    if codex.is_capacity_error(error):
        return "temporary"
    if isinstance(error, PreOutputLimitError) or code in (400, 404, 405, 409, 413, 415, 422):
        return "request"
    return "temporary"


def can_switch(error, attempts=1, first_timeouts=0):
    if getattr(error, "_gpt_output_started", False) or failure_scope(error) in ("request", "local"):
        return False
    if isinstance(error, sse.FirstOutputTimeout) and first_timeouts >= 2:
        return False
    # sub2's OAuth 429 window is followed by at most three account attempts.
    return status_code(error) != 429 or attempts < 3


def mark_failure(db, account_id, error, model=None, *, phase="request"):
    """All GPT entry points share one request/model/account error decision."""
    key = (db.path, account_id)
    handled = getattr(error, "_gpt_failure_handled", {})
    if key in handled:
        return handled[key]
    row = db.get_account(account_id)
    if not row:
        return "request_error"
    if getattr(error, "_gpt_refresh_error", False):
        phase = "refresh"
    scope = failure_scope(error, phase)
    from core import gpt_runtime as G
    from core.account_management import safe_error
    fields = {"last_check": int(time.time())}
    result = "ratelimit"
    if scope not in ("request", "local"):
        fields["last_error"] = safe_error(error, row)
    if scope == "auth":
        fields["status"] = "dead" if phase == "refresh" and "invalid_grant" in str(error).lower() else "cooldown"
        result = fields["status"]
    elif scope == "quota":
        fields["status"] = "exhausted"
        result = "exhausted"
    elif scope == "rate_limit":
        G.record_limit(db, row, error, model, final=True)
    elif isinstance(error, sse.StreamIdleTimeout) and phase != "refresh":
        record_timeout(db, account_id, error)
    elif scope in ("request", "local"):
        result = "request_error"
    db.update_account(account_id, **fields)
    handled[key] = result
    error._gpt_failure_handled = handled
    return result


def first_output_timeout(payload):
    base = config.GPT_FIRST_OUTPUT_TIMEOUT
    effort = str((payload.get("reasoning") or {}).get("effort") or "").lower()
    if base > 0 and effort in ("high", "xhigh", "max"):
        return config.GPT_HIGH_EFFORT_FIRST_OUTPUT_TIMEOUT or base
    return base


def semantic_output(event):
    kind = event.get("type", "")
    if kind == "response.output_item.added":
        item = event.get("item") or {}
        return item.get("type") != "message" or bool(item.get("content"))
    return sse.is_output(event)


def retry_delay(error, attempt):
    headers = {str(k).lower(): v for k, v in (getattr(error, "headers", None) or {}).items()}
    details = getattr(error, "details", {}) or {}
    extra = (details.get("error") or {}).get("headers") or {}
    headers = {**{str(k).lower(): v for k, v in extra.items()}, **headers}
    for key, divisor in (("retry-after", 1), ("retry-after-ms", 1000)):
        value = headers.get(key)
        if value is None:
            continue
        try:
            seconds = float(value) / divisor
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds > 0:
            return min(8, seconds)
    return min(8, 0.5 * 2 ** attempt)


def iter_events(adapter, account, payload, line_limit=PRE_OUTPUT_LIMIT):
    def attempt_events():
        response = adapter._open_codex(account, payload)
        try:
            yield from codex.iter_events(response, config.GPT_STREAM_IDLE_TIMEOUT, line_limit=line_limit)
        finally:
            response.close()
    yield from retry_events(attempt_events, payload, line_limit, db=adapter._database(), account_id=account["id"])


def retry_events(factory, payload, line_limit=PRE_OUTPUT_LIMIT, *, db, account_id):
    """Only retry rejected pre-output requests. Stage metadata until output commits."""
    from core import gpt_runtime as G
    rate_key = (getattr(db, "path", None), account_id)
    attempt = 0
    while True:
        committed = False
        terminal = False
        size = 0
        try:
            with tempfile.SpooledTemporaryFile(max_size=64 * 1024) as staged, R.first_output_budget(first_output_timeout(payload)):
                upstream = factory()
                try:
                    for event in upstream:
                        terminal = terminal or event.get("type") in ("response.completed", "response.done", "response.incomplete")
                        if not committed and semantic_output(event):
                            R.first_output_received()
                            committed = True
                            with _LOCK:
                                _RATE_RETRIES.pop(rate_key, None)
                            staged.seek(0)
                            for line in staged:
                                yield json.loads(line)
                            staged.close()
                        if committed:
                            yield event
                        else:
                            # Match sub2's 8 MB staging budget and 64 KB memory
                            # threshold. TemporaryFile is anonymous on Linux.
                            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
                            size += len(encoded)
                            if size > PRE_OUTPUT_LIMIT:
                                raise PreOutputLimitError("GPT pre-output events exceed 8 MB staging limit")
                            staged.write(encoded)
                    if not terminal:
                        from adapters.openai_compat import UpstreamError
                        raise UpstreamError(502, "GPT stream ended before response.completed")
                finally:
                    upstream.close()
                with _LOCK:
                    _RATE_RETRIES.pop(rate_key, None)
                return
        except Exception as error:
            error._gpt_output_started = committed
            if isinstance(error, sse.StreamIdleTimeout) and not getattr(error, "_gpt_refresh_error", False):
                record_timeout(db, account_id, error)
            transient_429 = status_code(error) == 429 and not G.rate_limit_reset(error)[0]
            if committed or (not transient_429 and not codex.is_capacity_error(error)):
                raise
            remaining_window = None
            if transient_429:
                with _LOCK:
                    deadline = _RATE_RETRIES.setdefault(rate_key, time.monotonic() + OAUTH_429_RETRY_WINDOW)
                remaining_window = deadline - time.monotonic()
                if remaining_window <= 0:
                    with _LOCK:
                        _RATE_RETRIES.pop(rate_key, None)
                    G.record_limit(db, db.get_account(account_id), error, payload.get("model"), final=True)
                    raise
            else:
                if attempt >= config.GPT_CAPACITY_RETRIES:
                    error._gpt_capacity_retries_exhausted = True
                    raise
            # The administrator test budget also covers retry backoff.
            delay = retry_delay(error, 0 if transient_429 else attempt)
            if remaining_window is not None:
                delay = min(delay, remaining_window)
            remaining = R.bounded_timeout()
            time.sleep(min(delay, remaining) if remaining is not None else delay)
            R.bounded_timeout()
            if remaining_window is not None and time.monotonic() >= deadline:
                with _LOCK:
                    _RATE_RETRIES.pop(rate_key, None)
                G.record_limit(db, db.get_account(account_id), error, payload.get("model"), final=True)
                raise
            if not transient_429:
                attempt += 1


def record_timeout(db, account_id, error):
    """Default: preserve account health. Opt-in: 3 in 10 minutes pauses for 5 minutes."""
    if getattr(error, "_gpt_timeout_recorded", False):
        return
    error._gpt_timeout_recorded = True
    if not config.GPT_TIMEOUT_PENALTY_ENABLED or isinstance(error, R.AccountTestTimeout):
        return
    now = time.time()
    with _LOCK:
        recent = _TIMEOUTS.setdefault(account_id, deque())
        while recent and recent[0] <= now - config.GPT_TIMEOUT_WINDOW:
            recent.popleft()
        recent.append(now)
        if len(recent) < config.GPT_TIMEOUT_THRESHOLD:
            return
        recent.clear()
    row = db.get_account(account_id)
    until = int(now + config.GPT_TIMEOUT_PAUSE)
    if row and (row.get("temp_until") or 0) < until:
        db.update_account(account_id, temp_until=until, temp_reason="GPT 多次输出超时，暂缓调度")
