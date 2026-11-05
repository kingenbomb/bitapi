"""Admin account management. Normal CRUD never returns credential values."""
import asyncio
import json
import threading
import time
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core import account_management as M
from core import adapter as A, pool_state, portal_state
from routers.portal import require_admin

router = APIRouter(prefix="/api/admin/pool", dependencies=[Depends(require_admin)])


def _filters(channel: str = None, status: str = None, q: str = None, account_type: str = None,
             group_id: str = None, proxy_id: int = None, schedulable: str = None, privacy_mode: str = None,
             pool_kind: str = None):
    return dict(channel=channel, status=status, q=q, account_type=account_type,
                group_id=group_id, proxy_id=proxy_id, schedulable=schedulable, privacy_mode=privacy_mode, pool_kind=pool_kind)


def _checked(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class GPTSettingsReq(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    pause_5h: float | None = Field(default=1, ge=0.001, le=1)
    pause_7d: float | None = Field(default=1, ge=0.001, le=1)
    auto_reset: bool = False
    reset_5h: float = Field(default=1, ge=0.001, le=1)
    reset_7d: float = Field(default=1, ge=0.001, le=1)
    model_whitelist: list[str] = Field(default_factory=list, max_length=1000)
    model_mapping: dict[str, str] = Field(default_factory=dict)
    cli_only: bool = False
    ws_mode: Literal["off", "ctx_pool", "passthrough"] = "off"
    passthrough: bool = True
    flatten_namespaces: bool = False
    compact_mode: Literal["auto", "enabled", "disabled"] = "auto"
    compact_model_mapping: dict[str, str] = Field(default_factory=dict)
    image_tool: Literal["auto", "enabled", "disabled"] = "auto"
    fingerprint_mode: Literal["canonical", "session", "passthrough"] = "canonical"
    header_overrides: dict[str, str] = Field(default_factory=dict)
    plan_override: str | None = Field(default=None, max_length=100)


class PoolPatchReq(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    account_type: Literal["oauth", "apikey", "password", "token"] | None = None
    paused: bool | None = None
    status: str | None = Field(default=None, max_length=30)
    note: str | None = Field(default=None, max_length=2000)
    credentials: dict | None = None
    priority: int | None = Field(default=None, ge=0, le=100000)
    concurrency: int | None = Field(default=None, ge=0, le=10000)
    rate_multiplier: float | None = Field(default=None, ge=0, le=10000)
    group_ids: list[int] | None = Field(default=None, max_length=200)
    account_expires_at: int | None = Field(default=None, ge=0)
    auto_pause_on_expired: bool | None = None
    temp_until: int | None = Field(default=None, ge=0)
    temp_reason: str | None = Field(default=None, max_length=500)
    proxy_id: int | None = Field(default=None, ge=1)
    quota_limit: float | None = Field(default=None, ge=0)
    quota_daily_limit: float | None = Field(default=None, ge=0)
    quota_weekly_limit: float | None = Field(default=None, ge=0)
    base_rpm: int | None = Field(default=None, ge=0, le=100000)
    window_cost_limit: float | None = Field(default=None, ge=0)
    gpt_settings: GPTSettingsReq | None = None

    @model_validator(mode="after")
    def validate_nulls(self):
        for key in self.model_fields_set - {"proxy_id", "note", "credentials"}:
            if getattr(self, key) is None:
                raise ValueError(key + " cannot be null")
        return self


class CreateReq(PoolPatchReq):
    channel: str = Field(min_length=1, max_length=100)
    identity: str = Field(min_length=1, max_length=300)
    credentials: dict = Field(min_length=1)
    pool_kind: Literal["account", "key"] | None = None


class DuplicateReq(PoolPatchReq):
    identity: str = Field(min_length=1, max_length=300)
    pool_kind: Literal["account", "key"] | None = None


class TargetReq(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=100000)
    filters: dict = Field(default_factory=dict)
    offset: int = Field(default=0, ge=0)


class PoolBulkReq(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["pause", "resume", "reset", "recover", "delete", "test", "refresh", "edit", "reset_quota", "balance", "usage", "privacy", "sync"]
    pool_kind: Literal["account", "key"] | None = None
    changes: PoolPatchReq | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    model: str | None = None


class TestReq(BaseModel):
    model: str | None = None


class ImageTestReq(TestReq):
    prompt: str = Field(min_length=1, max_length=2000)


class PlanReq(BaseModel):
    model: str
    interval_seconds: int = Field(ge=300, le=2592000)
    enabled: bool = True


class ProxyReq(BaseModel):
    name: str = Field(default="", max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    format: Literal["auto", "url", "host_port_user_pass", "user_pass_at_host_port", "user_pass_host_port", "host_port_at_user_pass"] = "auto"
    scheme: Literal["http", "https"] = "http"


class ImportReq(BaseModel):
    content: str = Field(default="", max_length=10 * 1024 * 1024)
    contents: list[str] = Field(default_factory=list, max_length=1000)
    channel: str | None = None
    update_existing: bool = False
    pool_kind: Literal["account", "key"] | None = None


class SyncReq(BaseModel):
    force: bool = False


class GPTActionPrepareReq(BaseModel):
    action: Literal["reset", "referral"]
    email: str | None = Field(default=None, max_length=254)


class GPTActionConfirmReq(BaseModel):
    request_id: str = Field(min_length=10, max_length=100)
    confirmed: bool


class SparkReq(BaseModel):
    models: list[str] = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=100)


class PATReq(BaseModel):
    token: str = Field(min_length=4, max_length=10000)
    proxy_id: int | None = None
    confirmed: bool = False


@router.post("/pat/gpt")
def gpt_pat(req: PATReq):
    adapter = M.adapter_of({"channel": "gpt"})
    try:
        credentials = adapter.validate_pat(req.token.strip(), req.proxy_id)
        if not req.confirmed:
            return {"identity": {key: value for key, value in credentials.items() if key != "access_token"}}
        result = adapter.import_accounts(pool_state.DB, [credentials])
        if result.get("failed"):
            raise ValueError("PAT 导入失败，请核对身份字段")
        if result.get("items") and result["items"][0].get("account_id") and req.proxy_id:
            M.patch_account(result["items"][0]["account_id"], {"proxy_id": req.proxy_id})
        return result
    except Exception as exc:
        raise HTTPException(400, M.safe_error(exc, {"token": req.token})) from exc


@router.post("/accounts/{account_id}/spark")
def gpt_spark(account_id: int, req: SparkReq):
    from core.gpt_models import create_shadow, rebuild
    parent = M.account_or_404(account_id)
    _checked(M.validate_changes, parent, {"gpt_settings": {"model_whitelist": req.models}})
    row = _checked(create_shadow, pool_state.DB, parent, req.models, req.name)
    rebuild(M.adapter_of(parent))
    return {"ok": True, "account": M.view(row)}


class OAuthStartReq(BaseModel):
    account_id: int | None = None
    proxy_id: int | None = None


class OAuthFinishReq(BaseModel):
    session_id: str = Field(max_length=200)
    callback_url: str = Field(max_length=10000)


@router.post("/oauth/gpt/start")
def start_oauth(req: OAuthStartReq, user=Depends(require_admin)):
    from core import gpt_oauth
    return _checked(gpt_oauth.start, user["id"], req.account_id, req.proxy_id)


@router.post("/oauth/gpt/finish")
def finish_oauth(req: OAuthFinishReq, user=Depends(require_admin)):
    from core import gpt_oauth
    return _checked(gpt_oauth.finish, user["id"], req.session_id, req.callback_url)


@router.get("")
def admin_pool(filters: dict = Depends(_filters), sort: str = "id", order: str = "desc", limit: int = 50, offset: int = 0):
    if filters.get("channel") and filters["channel"] not in A.all_adapters():
        with pool_state.DB._conn() as c:
            found = c.execute("SELECT 1 FROM accounts WHERE channel=? LIMIT 1", (filters["channel"],)).fetchone()
        if not found:
            raise HTTPException(404, "unknown channel")
    rows = _checked(M.matching_rows, filters, limit=limit, offset=offset, sort=sort, order=order)
    where, args = _checked(M.filters_sql, filters)
    with pool_state.DB._conn() as c:
        total = c.execute("SELECT COUNT(*) FROM accounts WHERE " + where, args).fetchone()[0]
    ids = [r["id"] for r in rows]
    today, week = M.usage_map(ids, M.today_start()), M.usage_map(ids, int(time.time()) - 7 * 86400)
    total_usage = M.usage_map(ids, 0)
    return {"accounts": [M.view(r, today, week, total_usage) for r in rows], "total": total,
            "limit": max(1, min(limit, 500)), "offset": max(0, offset), "generated_at": int(time.time())}


@router.get("/options")
def options():
    loaded = A.all_adapters()
    with pool_state.DB._conn() as c:
        names = {r[0] for r in c.execute("SELECT DISTINCT channel FROM accounts")} | set(loaded)
        kinds = {}
        for r in c.execute("SELECT channel," + M.POOL_KIND_SQL + " kind FROM accounts GROUP BY channel,kind"):
            kinds.setdefault(r["channel"], set()).add(r["kind"])
    from adapters.openai_compat import OpenAICompatAdapter
    channels = [{"name": n, "loaded": n in loaded, "models": list(getattr(loaded.get(n), "models", [])),
                 "capabilities": list(getattr(loaded.get(n), "capabilities", [])),
                 "can_proxy": M.supports_proxy(loaded.get(n)),
                 "pool_kinds": sorted(kinds.get(n) or {"key" if isinstance(loaded.get(n), OpenAICompatAdapter) and n != "gpt" else "account"})}
                for n in sorted(names)]
    return {"channels": channels, "groups": [{"id": g["id"], "name": g["name"]} for g in portal_state.USER_DB.list_groups()],
            "proxies": list_proxies()["proxies"]}


@router.get("/summary")
def admin_pool_summary(filters: dict = Depends(_filters)):
    where, args = _checked(M.filters_sql, filters)
    with pool_state.DB._conn() as c:
        rows = c.execute("SELECT channel,status,COUNT(*) n,SUM(" + M.PAUSED_SQL + ") paused, "
            "SUM(CASE WHEN (" + M.AVAILABLE_SQL + ") AND channel IN (SELECT value FROM json_each(?)) THEN 1 ELSE 0 END) available, "
            "SUM(CASE WHEN token_exp>? AND token_exp<=? THEN 1 ELSE 0 END) expiring "
            "FROM accounts WHERE " + where + " GROUP BY channel,status",
            [json.dumps(list(A.all_adapters())), int(time.time()), int(time.time()) + 3600, *args]).fetchall()
    channels, totals = {}, dict(total=0, active=0, dead=0, cooldown=0, exhausted=0, unchecked=0, paused=0, expiring=0, schedulable=0)
    for r in rows:
        channel = channels.setdefault(r["channel"], {"channel": r["channel"], "total": 0,
            "by_status": {s: 0 for s in M.STATUS_LABELS}, "paused": 0, "expiring": 0, "schedulable": 0})
        for key, value in (("total", r["n"]), ("paused", r["paused"] or 0), ("expiring", r["expiring"] or 0), ("schedulable", r["available"] or 0)):
            channel[key] += value
            totals[key] += value
        channel["by_status"][r["status"]] = r["n"]
        totals[r["status"]] = totals.get(r["status"], 0) + r["n"]
    with portal_state.USER_DB._conn() as c:
        unassigned = c.execute("SELECT COUNT(*) FROM usage_logs WHERE created_at>=? AND upstream_account_id IS NULL", (M.today_start(),)).fetchone()[0]
    return {"channels": list(channels.values()), "totals": totals, "categories": M.category_counts(), "unassigned_today": unassigned,
            "tracking_since": portal_state.USER_DB.get_setting("pool_usage_tracking_since"), "generated_at": int(time.time())}


def _targets(req, offset=0, before_id=None):
    if req.ids is None:
        return _checked(M.matching_rows, req.filters, limit=500,
                        offset=offset if before_id is None else 0, before_id=before_id)
    ids, rows = list(dict.fromkeys(req.ids))[offset:offset + 500], []
    with pool_state.DB._conn() as c:
        for start in range(0, len(ids), 400):
            part = ids[start:start + 400]
            marks = ",".join("?" for _ in part)
            rows.extend(pool_state.DB._row(r) for r in c.execute(f"SELECT * FROM accounts WHERE id IN ({marks})", part))
    try:
        for row in rows:
            M.check_pool_kind(row, req.filters.get("pool_kind"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return rows


def _target_count(req):
    if req.ids is None:
        where, args = _checked(M.filters_sql, req.filters)
        with pool_state.DB._conn() as c:
            return c.execute("SELECT COUNT(*) FROM accounts WHERE " + where, args).fetchone()[0]
    ids, count = list(dict.fromkeys(req.ids)), 0
    kind = req.filters.get("pool_kind")
    with pool_state.DB._conn() as c:
        for start in range(0, len(ids), 400):
            part = ids[start:start + 400]
            marks = ",".join("?" for _ in part)
            count += c.execute(f"SELECT COUNT(*) FROM accounts WHERE id IN ({marks})", part).fetchone()[0]
            if kind and c.execute(f"SELECT 1 FROM accounts WHERE id IN ({marks}) AND (" +
                                 M.POOL_KIND_SQL + ")<>? LIMIT 1", [*part, kind]).fetchone():
                raise HTTPException(409, "选择包含其他资源类型，请重新预览")
    return count


@router.post("/preview")
def preview(req: TargetReq):
    rows = _targets(req, req.offset)
    count = _target_count(req)
    extent = len(set(req.ids)) if req.ids is not None else count
    next_offset = req.offset + 500 if req.offset + 500 < extent else None
    return {"count": count, "truncated": next_offset is not None, "next_offset": next_offset,
            "targets": [{"id": r["id"], "name": r.get("name") or r["identity"],
            "channel": r["channel"], "pool_kind": M.pool_kind(r), "version": M.fingerprint(r)} for r in rows]}


@router.post("/bulk")
def admin_pool_bulk(req: PoolBulkReq):
    results = []
    changes = req.changes.model_dump(exclude_unset=True) if req.changes else {}
    for aid in dict.fromkeys(req.ids):
        row = None
        try:
            row = M.account_or_404(aid)
            M.check_pool_kind(row, req.pool_kind)
            if str(aid) in req.versions and req.versions[str(aid)] != M.fingerprint(row):
                raise ValueError("账号在预览后发生变化，请重新预览")
            M.operate(row, req.action, changes, req.model)
            results.append({"id": aid, "ok": True, "message": "完成"})
        except Exception as exc:
            message = exc.detail if isinstance(exc, HTTPException) else M.safe_error(exc, row or {})
            results.append({"id": aid, "ok": False, "message": message})
    return {"ok": True, "action": req.action, "results": results,
            "success": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)}


@router.post("/accounts")
def create_account(req: CreateReq):
    row = {"channel": req.channel, "identity": req.identity, "secret": {}, "meta": {}}
    _checked(M.adapter_of, row)
    if pool_state.DB.get_by_identity(req.channel, req.identity):
        raise HTTPException(409, "账号已存在，请编辑已有账号")
    values = _checked(M.validate_changes, row, req.model_dump(exclude_unset=True))
    _checked(M.check_pool_kind, {**row, **values}, req.pool_kind)
    secret, meta = values.pop("secret", req.credentials), values.pop("meta", {})
    try:
        aid = pool_state.DB.insert_account(req.channel, req.identity, secret=secret, meta=meta, status=req.status or "unchecked")
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "账号已存在，请编辑已有账号") from exc
    pool_state.DB.update_account(aid, **values)
    return {"ok": True, "account": M.view(M.account_or_404(aid))}


@router.get("/accounts/{account_id}")
def account_detail(account_id: int):
    row = M.account_or_404(account_id)
    return {"account": M.view(row, M.usage_map([account_id], M.today_start()), M.usage_map([account_id], int(time.time()) - 7 * 86400))}


@router.post("/accounts/{account_id}/sync")
def sync_account(account_id: int, req: SyncReq = SyncReq()):
    return _checked(M.sync_gpt_account, M.account_or_404(account_id), req.force)


@router.get("/accounts/{account_id}/referrals")
def gpt_referrals(account_id: int):
    from core.gpt_actions import query_referrals
    return _checked(query_referrals, account_id)


@router.get("/accounts/{account_id}/gpt-actions")
def gpt_action_history(account_id: int):
    from core.gpt_actions import history
    M.account_or_404(account_id)
    return {"history": history(account_id)}


@router.post("/accounts/{account_id}/gpt-actions/preview")
def gpt_action_preview(account_id: int, req: GPTActionPrepareReq, user=Depends(require_admin)):
    from core.gpt_actions import prepare
    return _checked(prepare, account_id, user["id"], req.action, req.email)


@router.post("/accounts/{account_id}/gpt-actions/execute")
def gpt_action_execute(account_id: int, req: GPTActionConfirmReq, user=Depends(require_admin)):
    from core.gpt_actions import execute
    return _checked(execute, req.request_id, account_id, user["id"], req.confirmed)


@router.post("/accounts/{account_id}/gpt-actions/acknowledge")
def gpt_action_acknowledge(account_id: int, req: GPTActionConfirmReq, user=Depends(require_admin)):
    from core.gpt_actions import acknowledge
    if not req.confirmed:
        raise HTTPException(400, "请先到 Codex 核对操作结果")
    return _checked(acknowledge, account_id, req.request_id, user["id"])


@router.post("/accounts/{account_id}/duplicate")
def duplicate_account(account_id: int, req: DuplicateReq):
    row = M.account_or_404(account_id)
    _checked(M.check_pool_kind, row, req.pool_kind)
    if M.account_type(row) != "apikey":
        raise HTTPException(400, "只复制静态 API Key 账号；OAuth 账号请使用授权或凭据导入")
    payload = {key: row.get(key) for key in M.EDITABLE}
    payload["name"] = ((row.get("name") or row["identity"])[:94] + "（副本）")
    payload.update(credentials=dict(row.get("secret") or {}), note=(row.get("meta") or {}).get("note") or "",
                   paused=bool((row.get("meta") or {}).get("admin_paused")), status=row["status"])
    payload.update(req.model_dump(exclude_unset=True))
    payload.update(channel=row["channel"], identity=req.identity)
    validated = _checked(CreateReq, **payload)
    return create_account(validated)


@router.patch("/accounts/{account_id}")
def admin_pool_patch(account_id: int, req: PoolPatchReq):
    return {"ok": True, "account": _checked(M.patch_account, account_id, req.model_dump(exclude_unset=True))}


@router.post("/accounts/{account_id}/test")
def admin_pool_test(account_id: int, req: TestReq = TestReq()):
    return _checked(M.run_test, M.account_or_404(account_id), req.model)


@router.post("/accounts/{account_id}/test-image")
async def admin_pool_image_test(account_id: int, req: ImageTestReq):
    from core import account_image_test as image_test
    from core import account_runtime
    row = await asyncio.to_thread(M.account_or_404, account_id)
    await asyncio.to_thread(_checked, image_test.prepare, row, req.model, req.prompt)
    model = await asyncio.to_thread(M.test_model, row, req.model)

    async def stream():
        loop = asyncio.get_running_loop()
        pending = asyncio.Queue()
        cancelled = threading.Event()

        def notify(kind, data):
            if not cancelled.is_set():
                loop.call_soon_threadsafe(pending.put_nowait, (kind, data))

        def produce():
            try:
                image_test.run(row, model, req.prompt, notify, cancelled)
            except Exception as exc:
                notify("result", {"ok": False, "kind": "image", "error": M.safe_error(exc, row)})
            finally:
                notify("end", None)

        worker = asyncio.create_task(asyncio.to_thread(account_runtime.worker_target(produce)))
        try:
            while True:
                try:
                    kind, data = await asyncio.wait_for(pending.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "end":
                    break
                yield "event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            await worker
        finally:
            cancelled.set()
            worker.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/accounts/{account_id}/models")
def account_models(account_id: int):
    return _checked(M.account_test_models, M.account_or_404(account_id))


@router.post("/accounts/{account_id}/models/detect")
def detect_models(account_id: int, background_tasks: BackgroundTasks):
    row = M.account_or_404(account_id)
    if _checked(M.begin_model_scan, row):
        background_tasks.add_task(M.scan_account_models, account_id)
    return M.account_test_models(row)


@router.post("/accounts/{account_id}/action/{action}")
def account_action(account_id: int, action: Literal["refresh", "recover", "reset_quota", "balance", "usage", "privacy"]):
    row = M.account_or_404(account_id)
    try:
        result = M.operate(row, action)
    except Exception as exc:
        raise HTTPException(400, M.safe_error(exc, row)) from exc
    return {**result, "account": M.view(M.account_or_404(account_id))}


@router.get("/accounts/{account_id}/stats")
def account_stats(account_id: int, days: int = 30):
    M.account_or_404(account_id)
    days = max(1, min(days, 90))
    since = int(time.time()) - days * 86400
    with portal_state.USER_DB._conn() as c:
        summary = dict(c.execute("SELECT " + M.USAGE_SQL + " FROM usage_logs WHERE upstream_account_id=? AND created_at>=?", (account_id, since)).fetchone())
        series = [dict(r) for r in c.execute("SELECT date(created_at,'unixepoch','localtime') day," + M.USAGE_SQL +
            " FROM usage_logs WHERE upstream_account_id=? AND created_at>=? GROUP BY day ORDER BY day", (account_id, since))]
        models = [dict(r) for r in c.execute("SELECT model," + M.USAGE_SQL +
            " FROM usage_logs WHERE upstream_account_id=? AND created_at>=? GROUP BY model ORDER BY requests DESC", (account_id, since))]
        recent = [dict(r) for r in c.execute("SELECT id,model,input_tokens,output_tokens,cost,actual_cost,account_cost,duration_ms,created_at "
            "FROM usage_logs WHERE upstream_account_id=? ORDER BY id DESC LIMIT 30", (account_id,))]
    return {"summary": summary, "series": series, "models": models, "recent": recent, "days": days,
            "tracking_since": portal_state.USER_DB.get_setting("pool_usage_tracking_since"),
            "windows": {k: M.usage_map([account_id], int(time.time()) - seconds).get(account_id, dict(M.ZERO)) for k, seconds in (("5h", 18000), ("7d", 604800))}}


@router.get("/accounts/{account_id}/tests")
def test_history(account_id: int):
    M.account_or_404(account_id)
    with pool_state.DB._conn() as c:
        plan = c.execute("SELECT * FROM pool_test_plans WHERE account_id=?", (account_id,)).fetchone()
        history = [{"id": r["id"], "created_at": r["created_at"], **json.loads(r["result"])} for r in c.execute(
            "SELECT * FROM pool_test_results WHERE account_id=? ORDER BY id DESC LIMIT 30", (account_id,))]
    return {"plan": dict(plan) if plan else None, "history": history}


@router.put("/accounts/{account_id}/plan")
def save_plan(account_id: int, req: PlanReq):
    _checked(M.test_model, M.account_or_404(account_id), req.model)
    with pool_state.DB._conn() as c:
        c.execute("INSERT INTO pool_test_plans(account_id,model,interval_seconds,enabled,next_run) VALUES(?,?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET model=excluded.model,interval_seconds=excluded.interval_seconds,enabled=excluded.enabled,next_run=excluded.next_run",
            (account_id, req.model, req.interval_seconds, int(req.enabled), int(time.time()) + req.interval_seconds))
    return {"ok": True}


@router.get("/proxies")
def list_proxies():
    with pool_state.DB._conn() as c:
        proxies = []
        for row in c.execute("SELECT p.*,COUNT(a.id) accounts FROM pool_proxies p LEFT JOIN accounts a ON a.proxy_id=p.id GROUP BY p.id"):
            parsed = urlsplit(row["url"])
            proxies.append({"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]),
                "address": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}", "authenticated": bool(parsed.username), "accounts": row["accounts"]})
    return {"proxies": proxies}


@router.post("/proxies")
def save_proxy(req: ProxyReq):
    url = _checked(M.normalize_proxy_url, req.url, req.format, req.scheme)
    with pool_state.DB._conn() as c:
        c.execute("INSERT INTO pool_proxies(name,url,enabled) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "name=CASE WHEN excluded.name='' THEN pool_proxies.name ELSE excluded.name END,enabled=excluded.enabled",
            (req.name.strip(), url, int(req.enabled)))
        c.execute("UPDATE pool_proxies SET name='代理 '||id WHERE url=? AND name=''", (url,))
    return list_proxies()


@router.post("/proxies/{proxy_id}/test")
def test_proxy(proxy_id: int):
    proxy = pool_state.DB.get_pool_proxy(proxy_id)
    if proxy is None:
        raise HTTPException(404, "代理不存在")
    return M.test_proxy(proxy)


@router.delete("/proxies/{proxy_id}")
def delete_proxy(proxy_id: int):
    with pool_state.DB._conn() as c:
        if c.execute("SELECT 1 FROM accounts WHERE proxy_id=? LIMIT 1", (proxy_id,)).fetchone():
            raise HTTPException(409, "该代理仍有账号使用，请先更换账号代理")
        c.execute("DELETE FROM pool_proxies WHERE id=?", (proxy_id,))
    return {"ok": True}


@router.post("/import/preview")
def import_preview(req: ImportReq):
    from core.account_transfer import import_data
    return _checked(import_data, req.content, req.channel, req.update_existing, preview=True, contents=req.contents, pool_kind=req.pool_kind)


@router.post("/import")
def import_accounts(req: ImportReq):
    from core.account_transfer import import_data
    return _checked(import_data, req.content, req.channel, req.update_existing, contents=req.contents, pool_kind=req.pool_kind)


@router.post("/export")
def export_accounts(req: TargetReq):
    from core.account_transfer import export_data
    _target_count(req)  # 发出任何凭据之前校验全部显式选择，避免跨页类型冲突造成半份导出。
    first = _targets(req)

    def chunks():
        metadata = export_data([])
        metadata.pop("accounts")
        metadata.pop("proxies")
        yield json.dumps(metadata)[:-1] + ',"accounts":['
        offset, rows, separator, proxies = 0, first, "", {}
        selected_count = len(set(req.ids)) if req.ids is not None else 0
        while rows or req.ids is not None and offset < selected_count:
            page = export_data(rows)
            for account in page["accounts"]:
                yield separator + json.dumps(account, ensure_ascii=False)
                separator = ","
            for proxy in page["proxies"]:
                proxies[proxy["proxy_key"]] = proxy
            offset += 500
            boundary = rows[-1]["id"] if rows and req.ids is None else None
            rows = _targets(req, offset, before_id=boundary)
        yield '],"proxies":' + json.dumps(list(proxies.values()), ensure_ascii=False) + "}"
    return StreamingResponse(chunks(), media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="accounts.json"', "Cache-Control": "no-store"})
