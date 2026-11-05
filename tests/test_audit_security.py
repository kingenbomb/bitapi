"""审计修复的行为回归：费用、认证、滥用、数据完整性和有界读取。"""
import asyncio
import csv
import io
import json
import socket
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import config
import server
from core import auth, credit, media_cache, pool_state, portal_state, preflight, scrub, throttle
from core.billing import Billing
from core.clock import day_start
from core.db import DB
from core.pricing import Pricing
from core.user_db import UserDB
import routers.portal as portal


@pytest.fixture
def env(tmp_path, monkeypatch):
    old = portal_state.USER_DB
    db = UserDB(str(tmp_path / "users.db"))
    pool = DB(str(tmp_path / "pool.db"))
    pricing = Pricing(db, cache_ttl=0)
    billing = Billing(db, pricing=pricing)
    for module in (portal_state, portal, server):
        monkeypatch.setattr(module, "USER_DB", db, raising=False)
        monkeypatch.setattr(module, "BILLING", billing)
        monkeypatch.setattr(module, "PRICING", pricing)
    monkeypatch.setattr(pool_state, "DB", pool)
    monkeypatch.setattr(server, "DB", pool)
    monkeypatch.setattr(config, "SITE_URL", "http://testserver")
    monkeypatch.setattr(config, "API_KEY", "security-master")
    monkeypatch.setattr(config, "ADMIN_KEY", "security-admin")
    monkeypatch.setattr(config, "JWT_SECRET", "security-test-secret-at-least-32-bytes")
    portal_state.rebind(db)
    gid = db.create_group("audit", supported_models=["*"], billing_policy="balance")
    uid = db.create_user("user@example.com", auth.hash_password("password"), "AU001", role="user", group_id=gid)
    aid = db.create_user("admin@example.com", auth.hash_password("password"), "AU002", role="admin", group_id=gid)
    db.create_api_key(uid, "sk-security")
    credit.credit(uid, 100, "admin", "seed")
    headers = {"Authorization": "Bearer " + auth.issue_token(uid)}
    admin = {"Authorization": "Bearer " + auth.issue_token(aid, role="admin")}
    client = TestClient(server.app)
    yield SimpleNamespace(db=db, pool=pool, pricing=pricing, billing=billing, uid=uid, aid=aid,
                          headers=headers, admin=admin, client=client, gid=gid)
    client.close()
    db._conns.close_all()
    pool._conns.close_all()
    # monkeypatch 尚未恢复时，显式恢复外部绑定，防止测试污染后续模块。
    credit.bind(old)
    from core import site_settings
    site_settings.bind(old)


def test_canonical_model_variants_charge_identically(env):
    env.db.upsert_pricing("audit-model", billing_mode="token", input_price=1)
    user, group = env.db.get_user(env.uid), env.db.get_group(env.gid)
    for model in ("audit-model", "AUDIT-MODEL", "Audit-Model[1M]"):
        assert env.pricing.resolve(model)["input_price"] == 1
        env.billing.record_usage(user, group, "audit", model,
            upstream_usage={"prompt_tokens": 1000000, "completion_tokens": 0})
    assert env.db.get_user(env.uid)["balance"] == 97
    with pytest.raises(ValueError, match="pricing not configured"):
        env.pricing.resolve("audit-unpriced")


def test_missing_price_rejected_before_upstream(env):
    with mock.patch.object(server, "get_adapter") as upstream, mock.patch.object(
            server, "model_to_channel", return_value={"audit-unpriced": "stub"}):
        response = env.client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-security"},
            json={"model": "audit-unpriced", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert "pricing not configured" in response.text
    upstream.assert_not_called()


@pytest.mark.parametrize("value", ["", " ", "换成 32 位以上随机串"])
def test_empty_and_example_secrets_fail_preflight(monkeypatch, value):
    monkeypatch.setattr(config, "JWT_SECRET", value)
    assert not preflight.check(host="0.0.0.0", allow_default=False)[0]


def test_empty_master_keys_never_authorize(env, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "ADMIN_KEY", "")
    assert env.client.get("/admin/stats").status_code == 401
    assert env.client.get("/v1/models").status_code == 401


def test_no_auth_audio_never_parses_multipart(env):
    with mock.patch.object(Request, "form", side_effect=AssertionError("body parsed")):
        assert env.client.post("/v1/audio/transcriptions", content=b"bad multipart").status_code == 401


def test_authenticated_malformed_multipart_is_400(env):
    response = env.client.post("/v1/audio/transcriptions", content=b"not multipart",
        headers={"Authorization": "Bearer security-master", "Content-Type": "multipart/form-data; boundary=zzz"})
    assert response.status_code == 400


def test_body_limit_includes_chunked_requests(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 16)
    assert env.client.post("/api/login", content=b"x" * 17).status_code == 413
    response = env.client.post("/api/login", content=iter([b"x" * 10, b"x" * 10]))
    assert response.status_code == 413


def test_private_operational_endpoints(env):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(path).status_code == 404
    assert env.client.get("/mcp/status").status_code == 401
    assert env.client.get("/mcp/status", headers={"Authorization": "Bearer security-admin"}).status_code == 200
    assert env.client.get("/health").json() == {"status": "ok"}
    assert env.client.get("/admin/produce?channel=../bad", headers=env.admin).status_code == 400


def test_email_change_consumes_latest_code_and_limits_guesses(env):
    env.db.add_email_verification(env.uid, "first@example.com", "123456")
    env.db.add_email_verification(env.uid, "next@example.com", "654321")
    for _ in range(5):
        assert env.client.post("/api/email/verify", headers=env.headers, json={"code": "123456"}).status_code == 400
    assert env.client.post("/api/email/verify", headers=env.headers, json={"code": "654321"}).status_code == 400
    assert env.db.get_user(env.uid)["email"] == "user@example.com"
    env.db.add_email_verification(env.uid, "next@example.com", "654321")
    assert env.client.post("/api/email/verify", headers=env.headers, json={"code": "654321"}).status_code == 200
    assert env.db.get_user(env.uid)["email"] == "next@example.com"
    assert env.db.get_user(env.uid)["email_verified_at"]
    assert env.client.post("/api/email/verify", headers=env.headers, json={"code": "654321"}).status_code == 400


def test_email_ownership_conflict_is_atomic(env):
    env.db.add_email_verification(env.uid, "admin@example.com", "123456")
    with pytest.raises(ValueError, match="其他账号"):
        env.db.verify_and_bind_email(env.uid, "123456")
    assert env.db.get_user(env.uid)["email"] == "user@example.com"
    assert not env.db.get_latest_verification_by_user(env.uid)["verified_at"]


def test_bind_and_redeem_return_429(env):
    with mock.patch.object(portal.mailer, "configured", return_value=True), mock.patch.object(portal.mailer, "send"):
        for n in range(5):
            assert env.client.post("/api/email/bind", headers=env.headers, json={"email": f"bind{n}@example.com"}).status_code == 200
        assert env.client.post("/api/email/bind", headers=env.headers, json={"email": "other@example.com"}).status_code == 429
    for _ in range(10):
        assert env.client.post("/api/redeem", headers=env.headers, json={"code": "INVALID"}).status_code == 400
    assert env.client.post("/api/redeem", headers=env.headers, json={"code": "INVALID"}).status_code == 429


@pytest.mark.parametrize("path,body,admin", [
    ("/api/keys", '{"quota":1e999}', False),
    ("/api/admin/codes", '{"count":1,"value":1e999}', True),
    ("/api/admin/groups", '{"name":"unsafe","price":1e999}', True),
    ("/api/admin/pricing", '{"model_pattern":"unsafe","input_price":1e999}', True),
    ("/api/admin/users/{uid}/balance", '{"amount":1e999}', True)])
def test_nonfinite_numbers_rejected_without_writes(env, path, body, admin):
    response = env.client.post(path.format(uid=env.uid), content=body,
        headers={**(env.admin if admin else env.headers), "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert env.db.get_user(env.uid)["balance"] == 100
    if admin:
        assert env.client.get("/api/admin/audit", headers=env.admin).status_code == 200
    with pytest.raises(ValueError, match="finite"):
        env.db.apply_credit(env.uid, float("inf"), "admin", "invalid")


def test_csv_formula_and_key_name_bounds(env):
    response = portal._usage_csv([{"email": "=bad", "key_name": "\t=cmd", "model": "  +evil"}], with_user=True)
    cells = list(csv.reader(io.StringIO(response.body.decode())))[1]
    assert "'=bad" in cells and "'\t=cmd" in cells and "'  +evil" in cells
    assert env.client.post("/api/keys", headers=env.headers, json={"name": "x" * 65}).status_code == 422


def test_admin_mutation_audited_without_credentials(env):
    response = env.client.post(f"/api/admin/users/{env.uid}/balance", headers=env.admin, json={"amount": 2})
    assert response.status_code == 200
    rows = env.client.get("/api/admin/audit", headers=env.admin).json()["rows"]
    assert rows[0]["actor"] == "user:" + str(env.aid)
    assert rows[0]["before_state"]["balance"] == 100
    assert rows[0]["after_state"]["balance"] == 102
    assert rows[0]["status"] == 200
    assert "password_hash" not in json.dumps(rows)
    assert env.client.get("/api/admin/audit", headers=env.headers).status_code == 403


def test_cookie_session_csrf_and_logout(env):
    response = env.client.post("/api/login", headers={"X-Bitapi-Session": "cookie"},
        json={"email": "admin@example.com", "password": "password"})
    assert response.status_code == 200
    assert "token" not in response.json() and response.json()["authenticated"]
    cookie_headers = response.headers.get_list("set-cookie")
    assert any("bitapi_session=" in h and "HttpOnly" in h for h in cookie_headers)
    csrf = env.client.cookies["bitapi_csrf"]
    assert env.client.get("/api/me").status_code == 200
    assert env.client.get("/admin/stats").status_code == 200
    path = f"/api/admin/users/{env.uid}/balance"
    assert env.client.post(path, json={"amount": 1}).status_code == 403
    assert env.client.post(path, headers={"X-CSRF-Token": csrf}, json={"amount": 1}).status_code == 200
    assert env.client.post(path, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.test"}, json={"amount": 1}).status_code == 403
    assert env.client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert env.client.get("/api/me").status_code == 401


def test_cookie_session_can_relogin_after_jwt_secret_rotation(env, monkeypatch):
    body = {"email": "admin@example.com", "password": "password"}
    headers = {"X-Bitapi-Session": "cookie"}
    assert env.client.post("/api/login", headers=headers, json=body).status_code == 200
    old_csrf = env.client.cookies["bitapi_csrf"]
    monkeypatch.setattr(config, "JWT_SECRET", "rotated-test-secret-at-least-32-bytes")
    response = env.client.post("/api/login", headers={**headers, "X-CSRF-Token": old_csrf}, json=body)
    assert response.status_code == 200 and response.json()["authenticated"]
    assert env.client.get("/api/me").status_code == 200


def test_preview_and_export_page_all_accounts_without_usage(env):
    with env.pool._conn() as c:
        c.executemany("INSERT INTO accounts(channel,identity,status,secret,meta) VALUES('nv',?,'active','{}','{}')",
                      [(f"audit-{n}",) for n in range(501)])
    first = env.client.post("/api/admin/pool/preview", headers=env.admin, json={"filters": {"channel": "nv"}}).json()
    assert first["count"] == 501 and len(first["targets"]) == 500 and first["truncated"]
    second = env.client.post("/api/admin/pool/preview", headers=env.admin,
        json={"filters": {"channel": "nv"}, "offset": first["next_offset"]}).json()
    assert len(second["targets"]) == 1 and second["next_offset"] is None
    exported = env.client.post("/api/admin/pool/export", headers=env.admin, json={"filters": {"channel": "nv"}})
    assert exported.status_code == 200 and len(exported.json()["accounts"]) == 501


def test_explicit_preview_counts_existing_ids_and_export_rejects_late_kind_conflict(env):
    with env.pool._conn() as c:
        c.executemany("INSERT INTO accounts(channel,identity,status,secret,meta,account_type) VALUES('nv',?,'active','{}','{}','apikey')",
                      [(f"audit-{n}",) for n in range(500)])
    ids = [row["id"] for row in env.pool.list_accounts(channel="nv", limit=500)]
    other = env.pool.upsert_account("gpt", "oauth-last", status="active")
    missing = max(ids + [other]) + 1
    response = env.client.post("/api/admin/pool/preview", headers=env.admin, json={"ids": [ids[0], missing]})
    assert response.json()["count"] == 1 and len(response.json()["targets"]) == 1
    response = env.client.post("/api/admin/pool/export", headers=env.admin,
        json={"ids": [*ids, other], "filters": {"pool_kind": "key"}})
    assert response.status_code == 409
    assert "accounts" not in response.json()


def test_credit_overflow_rolls_back(env):
    env.db.apply_credit(env.uid, 1e308, "admin", "large-once")
    with pytest.raises(ValueError, match="finite"):
        env.db.apply_credit(env.uid, 1e308, "admin", "overflow")
    assert env.db.get_user(env.uid)["balance"] == 1e308
    assert env.db.get_ledger_by_idem("overflow") is None


def test_media_rejects_nonpublic_dns_and_non_http_schemes():
    for value in ("file:///etc/passwd", "ftp://example.com/x", "https://user:pass@example.com/x"):
        with pytest.raises(ValueError):
            media_cache.fetch_and_store(value)
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]
        with mock.patch.object(socket, "getaddrinfo", return_value=info), mock.patch.object(socket, "create_connection") as connect:
            with pytest.raises(ValueError):
                media_cache._public_socket("upstream.test", 80, 1)
            connect.assert_not_called()
    with pytest.raises(ValueError, match="redirect"):
        media_cache._NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/")


def test_media_dns_pins_checked_address():
    info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    with mock.patch.object(socket, "getaddrinfo", return_value=info), mock.patch.object(socket, "create_connection") as connect:
        media_cache._public_socket("upstream.test", 443, 1)
    connect.assert_called_once_with(("8.8.8.8", 443), 1)


def test_expired_media_unavailable_without_sweep(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MEDIA_TTL", 10)
    path = tmp_path / "expired.png"
    path.write_bytes(b"png")
    os.utime(path, (time.time() - 20, time.time() - 20))
    assert media_cache.read(path.name) is None
    assert path.exists()


def test_site_day_is_independent_of_server_timezone():
    assert day_start(1735689600) == 1735660800


def test_scrubber_raw_and_partial_edges(monkeypatch):
    monkeypatch.setattr(scrub, "_ON", True)
    raw = {"choices": [{"message": {"content": "OpenRouter hello", "reasoning_content": "KOGO thought"}},
                       {"delta": {}}, {"message": {"content": None}}]}
    assert "OpenRouter" not in scrub.scrub_raw(raw)["choices"][0]["message"]["content"]
    assert scrub._partial_tail("") == 0
    assert scrub._partial_tail("normal x") == 0
    stream = scrub.Scrubber()
    assert stream.feed("OpenRou") == ""
    assert stream.feed("ter hello") + stream.flush() == " hello"
    assert scrub.scrub_raw(None) is None


def test_affiliate_config_rejects_unbounded_rewards(monkeypatch):
    from plugins import affiliate_percent, affiliate_fixed
    monkeypatch.setitem(affiliate_percent.CONFIG, "rate", 5)
    with pytest.raises(ValueError, match="rate"):
        affiliate_percent._payout({"id": 1}, {}, 1, "bad", {})
    monkeypatch.setitem(affiliate_fixed.CONFIG, "inviter_bonus", float("inf"))
    with mock.patch("core.site_settings.is_from_db", return_value=False), pytest.raises(ValueError):
        affiliate_fixed.on_registered({}, {})


# ---- N2: 扣费与 usage_log 必须同生共死 -------------------------------------

def _billing_case(tmp_path):
    """一个 balance 策略 + 真价目的结算环境。"""
    db = UserDB(str(tmp_path / "n2-users.db"))
    credit.bind(db)
    db.upsert_pricing("gpt-4o", None, billing_mode="per_token",
                      input_price=10.0, output_price=30.0)
    uid = db.create_user("n2@example.com", "hash", "N2AFF")
    db.apply_credit(uid, 100.0, "topup", "n2-topup")
    person = dict(db.get_user(uid))
    group = {"id": None, "billing_policy": "balance", "rate_multiplier": 1.0}
    return db, uid, person, group, Billing(db, pricing=Pricing(db, cache_ttl=0))


def _settle(billing, person, group, request_id):
    return billing.record_usage(
        person, group, "test", "gpt-4o",
        request_messages=[{"role": "user", "content": "x" * 400}],
        output_text="y" * 400, request_id=request_id)


def test_usage_log_failure_rolls_back_the_charge(tmp_path, monkeypatch):
    """写日志失败时扣费必须一起回滚,不能留下"钱扣了、用量查不到"的窟窿。

    失败点必须落在**真正的 INSERT 语句**上:替换掉 add_usage 整个方法会绕过事务
    传递,那样测试永远通过,抓不到任何回归。这里用一条必然违反的 CHECK 约束
    让数据库层自己拒写,走的就是真实的事务路径。
    """
    db, uid, person, group, billing = _billing_case(tmp_path)
    conn = db._conn()
    conn.execute(
        "CREATE TRIGGER n2_block_usage BEFORE INSERT ON usage_logs "
        "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")
    conn.commit()

    with pytest.raises(Exception, match="disk I/O error"):
        _settle(billing, person, group, "req-rollback")

    # 扣费必须跟着回滚:余额回到原值,账本里只剩充值那一条
    assert db.get_user(uid)["balance"] == pytest.approx(person["balance"])
    assert [r["reason"] for r in db.list_ledger(uid)] == ["topup"]
    assert db.recent_usage(uid, limit=20) == []
    assert not conn.in_transaction, "事务必须已经回滚干净"


def test_successful_settlement_writes_charge_and_log_together(tmp_path):
    """正常路径:扣费与日志都在,重复结算不再扣第二次。"""
    db, uid, person, group, billing = _billing_case(tmp_path)

    log_id = _settle(billing, person, group, "req-ok")
    assert log_id is not None
    charged = person["balance"] - db.get_user(uid)["balance"]
    assert charged > 0
    rows = db.recent_usage(uid, limit=20)
    assert len(rows) == 1
    assert rows[0]["pricing_snapshot"]["request_id"] == "req-ok"

    # 同一 request_id 再来一次:幂等放行,不重复扣、不重复记
    again = _settle(billing, person, group, "req-ok")
    assert again == log_id
    assert db.get_user(uid)["balance"] == pytest.approx(person["balance"] - charged)
    assert len(db.recent_usage(uid, limit=20)) == 1
