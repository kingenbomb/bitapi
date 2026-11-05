"""Reviewable GPT reset/referral actions and opt-in automatic reset execution."""
import json
import time
import uuid

from core import account_management as M, account_runtime, gpt_runtime as G, pool_state
from core.pool import apply_refresh


def _parent(aid):
    row = M.account_or_404(aid)
    if row["channel"] != "gpt" or (row.get("meta") or {}).get("parent_account_id"):
        raise ValueError("请在 GPT 主账号上执行此操作")
    return row, M.adapter_of(row)


def query_referrals(aid):
    row, adapter = _parent(aid)
    with account_runtime.network(pool_state.DB, row):
        apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
        result = adapter.referral_eligibility(row)
    G.edit_meta(pool_state.DB, aid, lambda meta: meta.update(pool_gpt_referrals=result))
    return result


def prepare(aid, admin_id, action, email=None, automatic=False):
    row, adapter = _parent(aid)
    with pool_state.DB._conn() as conn:
        unknown = conn.execute("SELECT request_id FROM gpt_actions WHERE account_id=? AND action=? AND state IN ('running','unknown') LIMIT 1",
                               (aid, action)).fetchone()
    if unknown:
        raise ValueError("上一笔操作尚未确认结果，请先在 Codex 核对并处理该记录")
    with account_runtime.network(pool_state.DB, row):
        apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
        if action == "reset":
            credits = adapter.account_reset_credits(row)
            if credits["available_count"] <= 0:
                raise ValueError("没有可用额度重置券")
            candidates = adapter.reset_candidates(row)
            if automatic and not candidates:
                raise ValueError("自动重置需要上游返回未过期、可定位的重置券")
            intent = {"available_count": credits["available_count"], "units": 1,
                "expires_at": candidates[0]["expires_at"] if candidates else None,
                "credit_id": candidates[0]["id"] if candidates else None, "automatic": automatic}
        elif action == "referral":
            from email_validator import validate_email, EmailNotValidError
            try:
                valid = validate_email(email or "", check_deliverability=False).normalized
            except EmailNotValidError:
                raise ValueError("请输入一个有效的收件邮箱") from None
            if any(value in (email or "") for value in ("\r", "\n", "<", ">")):
                raise ValueError("请输入一个有效的收件邮箱")
            eligibility = adapter.referral_eligibility(row)
            if not eligibility["should_show"] or not eligibility["available_invites"]:
                raise ValueError("此账号当前没有可用邀请名额")
            intent = {"email": valid, "program_id": eligibility["program_id"],
                "requires_confirmation": eligibility["requires_confirmation"],
                "available_invites": eligibility["available_invites"], "automatic": False}
        else:
            raise ValueError("未知 GPT 操作")
    request_id, now = str(uuid.uuid4()), int(time.time())
    with pool_state.DB._conn() as conn:
        conn.execute("INSERT INTO gpt_actions(request_id,account_id,admin_id,action,state,created_at,intent) VALUES(?,?,?,?,?,?,?)",
                     (request_id, aid, admin_id, action, "prepared", now, json.dumps(intent)))
    return {"request_id": request_id, "account_id": aid, "action": action, "expires_at": now + 600,
            **{key: value for key, value in intent.items() if key != "credit_id"}}


def execute(request_id, aid, admin_id, confirmed):
    if not confirmed:
        raise ValueError("请先确认操作内容及收件人同意")
    row, adapter = _parent(aid)
    with pool_state.DB._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        record = conn.execute("SELECT * FROM gpt_actions WHERE request_id=? AND account_id=? AND admin_id=?",
                              (request_id, aid, admin_id)).fetchone()
        if record is None:
            raise ValueError("操作预览不存在，请重新预览")
        record = dict(record)
        if record["state"] != "prepared":
            return json.loads(record["result"]) if record["result"] else {"ok": False, "state": record["state"], "request_id": request_id}
        if record["created_at"] < time.time() - 600:
            raise ValueError("预览已过期，请重新预览")
        if conn.execute("SELECT 1 FROM gpt_actions WHERE account_id=? AND action=? AND request_id<>? AND state IN ('running','unknown') LIMIT 1",
                        (aid, record["action"], request_id)).fetchone():
            raise ValueError("此账号已有正在执行或待核对的同类操作")
        conn.execute("UPDATE gpt_actions SET state='running' WHERE request_id=?", (request_id,))
    intent = json.loads(record["intent"])
    posted = False
    try:
        with account_runtime.network(pool_state.DB, row):
            apply_refresh(pool_state.DB, row, adapter.refresh_token(row))
            if record["action"] == "reset":
                if intent.get("automatic"):
                    if not G.settings(row)["auto_reset"] or (row.get("meta") or {}).get("admin_paused"):
                        raise ValueError("自动重置已关闭或账号已被管理员停用")
                    latest_usage = adapter.account_usage(row)
                    G.save_usage(pool_state.DB, row, latest_usage)
                    policy = G.settings(row)
                    if not any((latest_usage.get(key) or {}).get("utilization", -1) is not None and
                               (latest_usage.get(key) or {}).get("utilization", -1) >= policy[setting] * 100 and
                               ((latest_usage.get(key) or {}).get("reset_at") or 0) > time.time()
                               for key, setting in (("five_hour", "reset_5h"), ("seven_day", "reset_7d"))):
                        raise ValueError("上游额度已恢复，未消耗重置券")
                if adapter.account_reset_credits(row)["available_count"] <= 0:
                    raise ValueError("重置券已不可用，请重新查询")
                if intent.get("expires_at") and intent["expires_at"] <= time.time():
                    raise ValueError("预览中的重置券已到期")
                if intent.get("credit_id") and not any(candidate["id"] == intent["credit_id"] for candidate in adapter.reset_candidates(row)):
                    raise ValueError("预览中的重置券已不可用，未改用其他券")
                posted = True
                output = adapter.reset_quota(row, request_id, intent.get("credit_id"))
                result = {"ok": True, "state": "done", "output": output}
                # A successful consume stays successful even if its subsequent read fails.
                try:
                    usage, credits = adapter.account_usage(row), adapter.account_reset_credits(row)
                    G.save_usage(pool_state.DB, row, usage)
                    G.edit_meta(pool_state.DB, aid, lambda meta: meta.update(
                        pool_gpt_reset_credits=credits, gpt_rate_limits={}), status="active", last_error="")
                except Exception as exc:
                    result["warning"] = "重置已成功，额度刷新未完成：" + M.safe_error(exc, row)
            else:
                eligibility = adapter.referral_eligibility(row)
                if eligibility["program_id"] != intent["program_id"] or not eligibility["available_invites"] or not eligibility["should_show"]:
                    raise ValueError("邀请资格已变化，请重新查询和预览")
                posted = True
                output = adapter.send_referral(row, intent["email"], intent["program_id"])
                result = {"ok": True, "state": "done", "output": output}
    except Exception as exc:
        rejected = getattr(exc, "code", None) in (400, 401, 403, 404, 409, 422, 429)
        result = {"ok": False, "state": "unknown" if posted and not rejected else "failed", "error": M.safe_error(exc, row)}
    result.update(request_id=request_id, action=record["action"])
    with pool_state.DB._conn() as conn:
        conn.execute("UPDATE gpt_actions SET state=?,result=? WHERE request_id=?",
                     (result["state"], json.dumps(result, ensure_ascii=False), request_id))
    return result


def history(aid):
    with pool_state.DB._conn() as conn:
        records = conn.execute("SELECT * FROM gpt_actions WHERE account_id=? ORDER BY created_at DESC,rowid DESC LIMIT 30", (aid,)).fetchall()
    return [{"request_id": item["request_id"], "action": item["action"], "state": item["state"],
             "created_at": item["created_at"], "result": json.loads(item["result"]) if item["result"] else None,
             "email": json.loads(item["intent"]).get("email"), "automatic": json.loads(item["intent"]).get("automatic", False)} for item in records]


def acknowledge(aid, request_id, admin_id):
    import config
    with pool_state.DB._conn() as conn:
        changed = conn.execute("UPDATE gpt_actions SET state='acknowledged' WHERE account_id=? AND request_id=? "
            "AND (admin_id=? OR admin_id=0) AND (state='unknown' OR (state='running' AND created_at<?))", (aid, request_id, admin_id, int(time.time()) - config.SEND_TIMEOUT - 60)).rowcount
    if not changed:
        raise ValueError("操作仍在执行或没有待核对记录，请等待执行超时后再核对")
    return {"ok": True}


def run_maintenance():
    with pool_state.DB._conn() as conn:
        rows = [pool_state.DB._row(raw) for raw in conn.execute(
            "SELECT * FROM accounts WHERE channel='gpt' AND status IN ('active','cooldown') "
            "AND COALESCE(json_extract(meta,'$.admin_paused'),0)=0 "
            "AND json_extract(meta,'$.gpt_settings.auto_reset')=1 "
            "AND (auto_pause_on_expired=0 OR account_expires_at=0 OR account_expires_at>strftime('%s','now')) "
            "AND json_extract(meta,'$.parent_account_id') IS NULL "
            "AND COALESCE(json_extract(meta,'$.gpt_auto_reset_state.checked_at'),0)<=strftime('%s','now')-60 "
            "ORDER BY COALESCE(json_extract(meta,'$.gpt_auto_reset_state.checked_at'),0),id LIMIT 20")]
    for row in rows:
        policy = G.settings(row)
        usage = (row.get("meta") or {}).get("pool_upstream_usage") or {}
        state = (row.get("meta") or {}).get("gpt_auto_reset_state") or {}
        if state.get("checked_at", 0) > time.time() - 60:
            continue
        trigger = None
        for key, threshold in (("five_hour", policy["reset_5h"]), ("seven_day", policy["reset_7d"])):
            window = usage.get(key) or {}
            if window.get("utilization", -1) is not None and window.get("utilization", -1) >= threshold * 100 and (window.get("reset_at") or 0) > time.time():
                trigger = key
                break
        if not trigger:
            G.edit_meta(pool_state.DB, row["id"], lambda meta: meta.update(gpt_auto_reset_state={"checked_at": int(time.time()), "state": "watching"}))
            continue
        try:
            pending = prepare(row["id"], 0, "reset", automatic=True)
            result = execute(pending["request_id"], row["id"], 0, True)
            state = {"checked_at": int(time.time()), "trigger": trigger, "state": result["state"],
                     "error": result.get("error"), "request_id": pending["request_id"]}
        except Exception as exc:
            state = {"checked_at": int(time.time()), "trigger": trigger, "state": "failed", "error": M.safe_error(exc, row)}
        G.edit_meta(pool_state.DB, row["id"], lambda meta, value=state: meta.update(gpt_auto_reset_state=value))
