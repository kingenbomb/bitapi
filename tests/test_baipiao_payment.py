"""社区公开支付契约回放：隔离 SQLite，模拟社区通知/查单，不操作真实毛。"""
import hashlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

_BOOT = tempfile.mkdtemp(prefix="bitapi-mao-tests-")
os.environ.setdefault("BITAPI_DB", os.path.join(_BOOT, "bootstrap.db"))

import server  # noqa: E402
import core.portal_state as state  # noqa: E402
import routers.portal as routes  # noqa: E402
from core import credit, orders, site_settings as S  # noqa: E402
from core.auth import issue_token  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
import payments.baipiao  # noqa: E402,F401 (注册渠道)
from tests.registration_fixture import TestClient  # noqa: E402

KEY = "isolated-community-payment-test-key"


def signed_notify(order, **changes):
    data = {"pid": "27", "trade_no": "community-trade-1", "type": "mao",
            "out_trade_no": order["out_trade_no"], "money": f'{order["pay_amount_mao"]}.00',
            "name": "公益站额度", "param": "0", "trade_status": "TRADE_SUCCESS"}
    data.update(changes)
    # 按公开文档独立构造签名，不使用被测 provider 的签名函数。
    raw = "&".join(f"{k}={v}" for k, v in sorted(data.items()) if v != "") + KEY
    data["sign"] = hashlib.md5(raw.encode()).hexdigest()
    data["sign_type"] = "MD5"
    return data


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = UserDB(str(tmp_path / "orders.db"))
    monkeypatch.setattr(S, "_DB", db)
    monkeypatch.setattr(credit, "_DB", db)
    for mod in (state, routes):
        monkeypatch.setattr(mod, "USER_DB", db)
        monkeypatch.setattr(mod, "BILLING", Billing(db))
    for k, v in {"payment_providers": ["baipiao", "epay"], "min_topup": 0.5,
                 "site_url": "https://ai.baipiao.co", "baipiao_pid": "27",
                 "baipiao_key": KEY, "mao_per_usd": 1}.items():
        db.set_setting(k, v)
    gid = db.create_group("test", billing_policy="balance", supported_models=["*"])
    uid = db.create_user("test@gmail.com", "unused", "MAOTEST", group_id=gid, role="admin")
    # 与现有路由测试一致，不启动无关的渠道巡检/备份等 lifespan 后台任务。
    client = TestClient(server.app)
    try:
        yield db, db.get_user(uid), client, {"Authorization": "Bearer " + issue_token(uid, "admin")}
    finally:
        client.close()


def new_order(env, amount=10):
    db, user, _, _ = env
    return orders.create_order(db, user, amount, "baipiao")


def query_response(order, **changes):
    data = {"code": 1, "pid": "27", "type": "mao", "trade_no": "community-trade-1",
            "out_trade_no": order["out_trade_no"], "money": f'{order["pay_amount_mao"]}.00',
            "wool": order["pay_amount_mao"], "status": 1, "order_status": "paid",
            "trade_status": "TRADE_SUCCESS", "refunded": False}
    data.update(changes)
    return data


def mock_query(monkeypatch, payload):
    import httpx
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=payload)
    monkeypatch.setattr("payments.baipiao.httpx.post", post)
    return calls


def test_quote_signed_with_frozen_mao_and_merchant(env):
    db, _, client, auth = env
    response = client.post("/api/orders", json={"amount": 10, "provider": "baipiao"}, headers=auth)
    assert response.status_code == 200
    order, payment = response.json()["order"], response.json()["payment"]
    assert order["pay_amount_mao"] == 10 and order["pay_merchant_id"] == "27"
    assert order["pay_amount_cny"] is None
    url = urlsplit(payment["pay_url"])
    assert url.scheme == "https" and url.netloc == "baipiao.org" and url.path == "/bbs/payment/submit.php"
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert params["money"] == "10.00" and params["type"] == "mao"
    assert params["notify_url"] == "https://ai.baipiao.co/pay/notify/baipiao"
    assert params["return_url"] == "https://ai.baipiao.co/pay/return/baipiao"
    raw = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("sign", "sign_type")) + KEY
    assert params["sign"] == hashlib.md5(raw.encode()).hexdigest()
    assert KEY not in response.text
    db.set_setting("mao_per_usd", 2.5)
    next_order, _ = new_order(env, 1.01)
    assert next_order["pay_amount_mao"] == 3
    assert db.get_order(order["id"])["pay_amount_mao"] == 10


def test_notify_replay_and_browser_return_never_credit_twice(env):
    db, user, client, _ = env
    order, _ = new_order(env)
    params = signed_notify(order)
    assert client.get("/pay/return/baipiao", params=params).status_code == 200
    assert not db.get_user(user["id"])["balance"]
    db.set_setting("mao_per_usd", 9)
    for _ in range(3):
        result = client.get("/pay/notify/baipiao", params=params)
        assert result.status_code == 200 and result.text == "success"
    assert db.get_user(user["id"])["balance"] == 10
    assert len(db.list_ledger(user["id"])) == 1
    assert db.orders_completed()["mao"] == 10
    assert db.orders_completed()["cny"] == 0


@pytest.mark.parametrize("changes", [
    {"money": "9.99"}, {"money": "11.00"}, {"money": "NaN"}, {"money": "1e1"},
    {"pid": "28"}, {"type": "alipay"}, {"extra": "unlisted"}, {"money": "10.001"},
])
def test_signed_wrong_fields_do_not_credit(env, changes):
    db, user, client, _ = env
    order, _ = new_order(env)
    assert client.get("/pay/notify/baipiao", params=signed_notify(order, **changes)).text != "success"
    assert not db.get_user(user["id"])["balance"]


def test_ambiguous_or_tampered_callback_rejected(env):
    db, user, client, _ = env
    order, _ = new_order(env)
    params = signed_notify(order)
    for raw in (urlencode(params) + "&money=10.00", urlencode(params) + "&money[]=10.00"):
        assert client.get("/pay/notify/baipiao?" + raw).text != "success"
        assert client.post("/pay/notify/baipiao", content=raw).text != "success"
    assert client.post("/pay/notify/baipiao", params=params, content=urlencode(params)).text != "success"
    params["money"] = "1.00"
    assert client.get("/pay/notify/baipiao", params=params).text != "success"
    assert not db.get_user(user["id"])["balance"]


def test_post_callback_and_concurrent_replay(env):
    db, user, client, _ = env
    order, _ = new_order(env)
    params = signed_notify(order)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: orders.handle_notify(db, "baipiao", {}, b"", params), range(8)))
    assert all(result[0] for result in results)
    assert client.post("/pay/notify/baipiao", content=urlencode(params)).text == "success"
    assert db.get_user(user["id"])["balance"] == 10
    assert len(db.list_ledger(user["id"])) == 1


def test_query_posts_secret_and_recovers_missing_notify(env, monkeypatch):
    db, user, client, auth = env
    order, _ = new_order(env)
    calls = mock_query(monkeypatch, query_response(order))
    assert orders.poll_pending(db) == 1
    assert db.get_user(user["id"])["balance"] == 10
    url, kwargs = calls[0]
    assert url == "https://baipiao.org/bbs/payment/api.php" and KEY not in url
    assert kwargs["data"]["key"] == KEY and kwargs["follow_redirects"] is False
    result = client.post(f'/api/admin/orders/{order["out_trade_no"]}/requery', headers=auth)
    assert result.status_code == 200 and not result.json()["credited"]
    assert len(db.list_ledger(user["id"])) == 1


@pytest.mark.parametrize("changes", [
    {"money": "9.00"}, {"pid": "28"}, {"out_trade_no": "another-order"},
    {"type": "alipay"}, {"order_status": "refunded", "refunded": True},
    {"order_status": "pending", "trade_status": "WAIT_BUYER_PAY"},
])
def test_query_guards_cover_automatic_and_admin_paths(env, monkeypatch, changes):
    db, user, client, auth = env
    order, _ = new_order(env)
    mock_query(monkeypatch, query_response(order, **changes))
    assert orders.poll_pending(db) == 0
    result = client.post(f'/api/admin/orders/{order["out_trade_no"]}/requery', headers=auth)
    assert result.status_code == 200 and not result.json()["credited"]
    assert not db.get_user(user["id"])["balance"]


def test_disabled_channel_and_replaced_merchant_cannot_settle(env, monkeypatch):
    db, user, client, auth = env
    order, _ = new_order(env)
    mock_query(monkeypatch, query_response(order))
    db.set_setting("payment_providers", [])
    assert orders.poll_pending(db) == 0
    assert not client.post(f'/api/admin/orders/{order["out_trade_no"]}/requery', headers=auth).json()["credited"]
    assert client.get("/pay/notify/baipiao", params=signed_notify(order)).text != "success"
    db.set_setting("payment_providers", ["baipiao"])
    db.set_setting("baipiao_pid", "28")
    assert client.get("/pay/notify/baipiao", params=signed_notify(order, pid="28")).text != "success"
    assert not db.get_user(user["id"])["balance"]


def test_settings_admin_only_key_write_only_and_rate_validation(env):
    db, _, client, auth = env
    body = {"baipiao_pid": "27", "baipiao_key": KEY, "mao_per_usd": 2}
    uid = db.create_user("ordinary@gmail.com", "unused", "ORDINARY")
    ordinary = {"Authorization": "Bearer " + issue_token(uid)}
    assert client.patch("/api/admin/settings", json=body, headers=ordinary).status_code == 403
    assert client.patch("/api/admin/settings", json=body, headers=auth).status_code == 200
    result = client.get("/api/admin/settings", headers=auth)
    assert KEY not in result.text and result.json()["baipiao_key_set"] is True
    assert result.json()["mao_per_usd"] == 2
    assert client.get("/api/payment/providers", headers=ordinary).json()["mao_per_usd"] == 2
    for invalid in (0, -1, 10001, 0.12345, None):
        result = client.patch("/api/admin/settings", json={"mao_per_usd": invalid}, headers=auth)
        assert result.status_code == 400
    db.set_setting("baipiao_key", "")
    result = client.post("/api/orders", json={"amount": 10, "provider": "baipiao"}, headers=auth)
    assert result.status_code == 400


def test_old_database_adds_nullable_quote_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    db = UserDB(path)
    uid = db.create_user("old@gmail.com", "unused", "OLD")
    oid = db.create_order(uid, "old-epay", 5, "epay", expires_at=9999999999, recharge_code="OLD-PAY")
    with db._conn() as conn:
        conn.execute("ALTER TABLE orders DROP COLUMN pay_amount_mao")
        conn.execute("ALTER TABLE orders DROP COLUMN pay_merchant_id")
    migrated = UserDB(path)
    order = migrated.get_order(oid)
    assert order["provider"] == "epay" and order["amount"] == 5
    assert order["pay_amount_mao"] is None and order["pay_merchant_id"] is None
