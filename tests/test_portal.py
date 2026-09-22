import os
import itertools
import tempfile
import threading
import time
import unittest
from unittest import mock

# 隔离环境:临时 DB、避免读 CPA grok 目录、可预测配置。
# 必须在 import config / server 之前设好,但即便其他测试已先导入过 config,
# 下方 setUpModule 也会强制重建单例,保证 import 顺序无关。
_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "bitapi.db")
os.environ["BITAPI_GROK_AUTH_DIR"] = os.path.join(_TMP, "noexist_auths")
os.environ["BITAPI_JWT_SECRET"] = "test-secret"
os.environ["BITAPI_API_KEY"] = "sk-master-test"
os.environ["BITAPI_DEFAULT_GROUP"] = "free"

import config  # noqa: E402
import server  # noqa: E402
import core.portal_state as portal_state  # noqa: E402
import routers.portal as portal_routes  # noqa: E402
from core.billing import Billing  # noqa: E402
from core.user_db import UserDB  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MASTER_KEY = "sk-master-test"


def setUpModule():
    """强制把配置与单例重建到隔离的临时 DB,消除测试 import 顺序依赖。"""
    config.API_KEY = MASTER_KEY
    config.JWT_SECRET = "test-secret"
    config.DEFAULT_GROUP = "free"
    config.REQUIRE_INVITE = True
    db_path = os.path.join(_TMP, "bitapi.db")
    config.DB_PATH = db_path
    fresh_db = UserDB(db_path)
    fresh_billing = Billing(fresh_db)
    # 重指所有引用了旧单例的模块
    for mod in (portal_state, server, portal_routes):
        mod.USER_DB = fresh_db
        mod.BILLING = fresh_billing
    # credit 与 site_settings 各自存了 db 引用,漏掉的表现是「写进去了但读不到」
    portal_state.rebind(fresh_db)


# setUpModule 尚未运行时的占位;测试内统一用 _db()/_billing() 取当前单例
def _db():
    return portal_state.USER_DB


_INVITE_SEQ = itertools.count(1)


def _registration_invite():
    code = "TESTINV%06d" % next(_INVITE_SEQ)
    _db().create_code(code, 0, type="invitation")
    return code


USER_DB = None  # 兼容旧引用,setUpClass 后由 _db() 提供


class PortalFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global USER_DB
        USER_DB = portal_state.USER_DB
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        if not USER_DB.get_user_by_email("root@example.com"):
            cls.client.post("/api/register", json={
                "email": "root@example.com", "password": "secret123"})

    def _register(self, email, password="secret123", invite=""):
        return self.client.post("/api/register", json={
            "email": email, "password": password, "invite_code": invite})

    def test_first_user_is_admin(self):
        admin = USER_DB.get_user_by_email("root@example.com")
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["aff_code"])

    def test_second_user_requires_invite_code(self):
        # 已有用户后,无码注册被拒
        r = self._register("noinvite@example.com")
        self.assertEqual(r.status_code, 403, r.text)
        # 错误邀请码被拒
        r = self._register("badcode@example.com", invite="ZZZZZZZZ")
        self.assertEqual(r.status_code, 403, r.text)

    def test_affiliate_code_does_not_unlock_invite_only_registration(self):
        admin = USER_DB.get_user_by_email("root@example.com")
        code = admin["aff_code"]
        USER_DB.set_setting("require_invite", True)
        r = self._register("blockedref@example.com", invite=code)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNone(USER_DB.get_user_by_email("blockedref@example.com"))

    def test_affiliate_code_binds_inviter_when_registration_is_open(self):
        admin = USER_DB.get_user_by_email("root@example.com")
        USER_DB.set_setting("require_invite", False)
        try:
            r = self._register("invitee@example.com", invite=admin["aff_code"])
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            USER_DB.set_setting("require_invite", True)
        u = USER_DB.get_user_by_email("invitee@example.com")
        self.assertEqual(u["inviter_id"], admin["id"])
        self.assertEqual(u["role"], "user")

    # ---- 管理台批量发的一次性邀请码 ----

    def _gen_invites(self, count=1, **over):
        body = {"count": count, "type": "invitation"}
        body.update(over)
        r = self.client.post("/api/admin/codes", json=body,
                             headers=self._admin_headers())
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["codes"]

    def test_admin_batch_generates_invite_codes(self):
        codes = self._gen_invites(5, notes="内测第一批")
        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5)
        rec = USER_DB.get_code(codes[0])
        self.assertEqual(rec["type"], "invitation")
        self.assertEqual(rec["value"], 0)      # 邀请码不入账,不带面额
        self.assertEqual(rec["status"], "unused")
        self.assertEqual(rec["notes"], "内测第一批")

    def test_register_with_admin_invite_code(self):
        code = self._gen_invites()[0]
        r = self._register("bycode@example.com", invite=code)
        self.assertEqual(r.status_code, 200, r.text)
        u = USER_DB.get_user_by_email("bycode@example.com")
        # 一次性码不代表任何邀请人,所以不绑 inviter —— 返佣那条路走 aff_code
        self.assertIsNone(u["inviter_id"])
        rec = USER_DB.get_code(code)
        self.assertEqual(rec["status"], "used")
        self.assertEqual(rec["used_by"], u["id"])

    def test_invite_code_is_single_use(self):
        code = self._gen_invites()[0]
        self.assertEqual(
            self._register("once1@example.com", invite=code).status_code, 200)
        r = self._register("once2@example.com", invite=code)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNone(USER_DB.get_user_by_email("once2@example.com"))

    def test_invite_code_survives_a_concurrent_rush(self):
        """一张码八个人同时抢,只能有一个人注册成功。

        「先占用、再建号」就是为这个:反过来的话八个请求都能先通过校验,
        一张一次性码进八个账号,而事后从任何一行日志里都看不出来。
        """
        code = self._gen_invites()[0]
        ok, failed = [], []

        def worker(i):
            # 每线程一个 client:TestClient 自带 anio portal,不适合跨线程共用
            r = TestClient(server.app).post("/api/register", json={
                "email": f"rush{i}@example.com", "password": "secret123",
                "invite_code": code})
            (ok if r.status_code == 200 else failed).append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), 1, f"ok={ok} failed={len(failed)}")
        rec = USER_DB.get_code(code)
        self.assertEqual(rec["status"], "used")
        # 使用者记的是抢到的那个人,不是随便哪个参与者
        self.assertEqual(USER_DB.get_user(rec["used_by"])["email"],
                         f"rush{ok[0]}@example.com")
        # 落败的七个一个都没建号
        for i in failed:
            self.assertIsNone(
                USER_DB.get_user_by_email(f"rush{i}@example.com"))

    def test_expired_invite_code_rejected(self):
        code = self._gen_invites(expires_at=1)[0]
        r = self._register("expiredinvite@example.com", invite=code)
        self.assertEqual(r.status_code, 403, r.text)
        # 拒得干净:过期不等于被用掉,码还留在库里等站长作废
        self.assertEqual(USER_DB.get_code(code)["status"], "unused")

    def test_disabled_invite_code_rejected(self):
        code = self._gen_invites()[0]
        self.client.delete("/api/admin/codes/" + code,
                           headers=self._admin_headers())
        r = self._register("killedinvite@example.com", invite=code)
        self.assertEqual(r.status_code, 403, r.text)

    def test_invite_code_cannot_be_redeemed_for_balance(self):
        """否则任何登录用户都能把待发的邀请码逐张兑成 0 元,把码烧光。"""
        code = self._gen_invites()[0]
        ah = self._admin_headers()
        r = self.client.post("/api/redeem", json={"code": code}, headers=ah)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"]["code"], "CODE_NOT_REDEEMABLE")
        self.assertEqual(USER_DB.get_code(code)["status"], "unused")

    def test_code_list_separates_the_two_kinds(self):
        ah = self._admin_headers()
        inv = self._gen_invites()[0]
        bal = self.client.post("/api/admin/codes", json={"count": 1, "value": 5},
                               headers=ah).json()["codes"][0]

        def listed(kind):
            return [c["code"] for c in self.client.get(
                "/api/admin/codes?limit=500&type=" + kind,
                headers=ah).json()["codes"]]

        self.assertIn(inv, listed("invitation"))
        self.assertNotIn(bal, listed("invitation"))
        self.assertIn(bal, listed("balance"))
        self.assertNotIn(inv, listed("balance"))

    def test_code_gen_rejects_unknown_type(self):
        r = self.client.post("/api/admin/codes",
                             json={"count": 1, "type": "nope"},
                             headers=self._admin_headers())
        self.assertEqual(r.status_code, 400, r.text)

    def test_login_and_me(self):
        admin = USER_DB.get_user_by_email("root@example.com")
        r = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        r = self.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email"], "root@example.com")
        self.assertEqual(r.json()["role"], "admin")

    def test_wrong_password_rejected(self):
        r = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_api_key_lifecycle(self):
        token = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        r = self.client.post("/api/keys", json={"name": "test"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        key = r.json()["key"]
        self.assertTrue(key.startswith("sk-"))
        # 列表可见(脱敏)
        r = self.client.get("/api/keys", headers=h)
        self.assertTrue(any(k["key_masked"].endswith(key[-4:]) for k in r.json()["keys"]))

    def test_gateway_rejects_unknown_key(self):
        r = self.client.post("/v1/chat/completions",
                             json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": "Bearer sk-nonexistent"})
        self.assertEqual(r.status_code, 401)

    def test_key_reveal_and_rename_and_toggle(self):
        h = self._admin_headers()
        created = self.client.post("/api/keys", json={"name": "reveal-me"},
                                   headers=h).json()
        kid, full = created["id"], created["key"]
        # 列表只给脱敏串,不带完整明文(截图/投屏时不整表泄露)
        listed = self.client.get("/api/keys", headers=h).json()["keys"]
        row = [k for k in listed if k["id"] == kid][0]
        self.assertNotIn("key", row)
        self.assertTrue(row["key_masked"].endswith(full[-4:]))
        self.assertIn("usage", row)
        # 按需取回完整密钥
        r = self.client.get(f"/api/keys/{kid}/reveal", headers=h)
        self.assertEqual(r.json()["key"], full)
        # 改名
        self.client.patch(f"/api/keys/{kid}", json={"name": "renamed"}, headers=h)
        listed = self.client.get("/api/keys", headers=h).json()["keys"]
        self.assertEqual([k for k in listed if k["id"] == kid][0]["name"], "renamed")
        # 停用后网关应拒绝
        self.client.patch(f"/api/keys/{kid}", json={"status": "disabled"}, headers=h)
        r = self.client.post("/v1/chat/completions",
                             json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {full}"})
        self.assertEqual(r.status_code, 401)

    def test_key_batch_create_suffixes_names(self):
        h = self._admin_headers()
        r = self.client.post("/api/keys", json={"name": "batch", "count": 3},
                             headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["count"], 3)
        names = [k["name"] for k in body["keys"]]
        self.assertEqual(names[0], "batch")          # 第一个用原名
        self.assertTrue(all(n.startswith("batch-") for n in names[1:]))
        self.assertEqual(len(set(k["key"] for k in body["keys"])), 3)

    def test_key_expiry_blocks_gateway(self):
        h = self._admin_headers()
        import time as _t
        created = self.client.post(
            "/api/keys", json={"name": "expired", "expires_at": int(_t.time()) + 1},
            headers=h).json()
        listed = self.client.get("/api/keys", headers=h).json()["keys"]
        row = [k for k in listed if k["id"] == created["id"]][0]
        self.assertGreater(row["expires_at"], 0)
        self.assertFalse(row["expired"])
        # 手动把到期时间挪到过去,应被网关拒绝且列表标记 expired
        self.client.patch(f"/api/keys/{created['id']}",
                          json={"expires_at": int(_t.time()) - 10}, headers=h)
        listed = self.client.get("/api/keys", headers=h).json()["keys"]
        self.assertTrue([k for k in listed if k["id"] == created["id"]][0]["expired"])
        r = self.client.post("/v1/chat/completions",
                             json={"model": "kg", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {created['key']}"})
        self.assertEqual(r.status_code, 401)

    def test_key_create_rejects_past_expiry(self):
        h = self._admin_headers()
        r = self.client.post("/api/keys", json={"expires_at": 1}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_reveal_other_users_key_is_404(self):
        h = self._admin_headers()
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("otherkey@example.com"):
            self._register("otherkey@example.com", invite=_registration_invite())
        other = USER_DB.get_user_by_email("otherkey@example.com")
        otok = self.client.post("/api/login", json={
            "email": "otherkey@example.com", "password": "secret123"}).json()["token"]
        mine = self.client.post("/api/keys", json={}, headers=h).json()
        r = self.client.get(f"/api/keys/{mine['id']}/reveal",
                            headers={"Authorization": f"Bearer {otok}"})
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(other)

    def test_gateway_master_key_bypasses_billing(self):
        # master key 命中未知模型 → 404(说明通过了鉴权,进入路由解析)
        r = self.client.post("/v1/chat/completions",
                             json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": "Bearer sk-master-test"})
        self.assertEqual(r.status_code, 404)

    def test_gateway_model_whitelist(self):
        # 建个只允许 demo-* 的组,把用户挪进去,请求 grok 应 403
        gid = USER_DB.create_group(name="demoonly", supported_models=["demo-*"],
                                   rpm_limit=0)
        admin = USER_DB.get_user_by_email("root@example.com")
        USER_DB.update_user(admin["id"], group_id=gid)
        server.BILLING.invalidate()
        token = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        # 建 key
        key = self.client.post("/api/keys", json={}, headers={
            "Authorization": f"Bearer {token}"}).json()["key"]
        # grok 不在 demo-* 白名单 → 403
        r = self.client.post("/v1/chat/completions",
                             json={"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 403, r.text)
        # 恢复默认组
        USER_DB.update_user(admin["id"], group_id=portal_state.ensure_default_group()["id"])
        server.BILLING.invalidate()

    def test_rpm_limit_triggers_429(self):
        # 确保 invitee 存在(不依赖测试执行顺序)
        if not USER_DB.get_user_by_email("invitee@example.com"):
            self._register("invitee@example.com", invite=_registration_invite())
        # 建 rpm=2 的组,第三次请求应 429
        gid = USER_DB.create_group(name="rpm2", supported_models=["*"], rpm_limit=2)
        u = USER_DB.get_user_by_email("invitee@example.com")
        USER_DB.update_user(u["id"], group_id=gid)
        server.BILLING.invalidate()
        token = self.client.post("/api/login", json={
            "email": "invitee@example.com", "password": "secret123"}).json()["token"]
        key = self.client.post("/api/keys", json={}, headers={
            "Authorization": f"Bearer {token}"}).json()["key"]
        h = {"Authorization": f"Bearer {key}"}
        body = {"model": "kg", "messages": [{"role": "user", "content": "hi"}]}
        # 前两次通过限流(会因无号 503 或上游错,但不是 429);第三次 429
        codes = [self.client.post("/v1/chat/completions", json=body, headers=h).status_code
                 for _ in range(3)]
        self.assertEqual(codes[2], 429, codes)

    def test_admin_endpoints_require_admin(self):
        # 普通用户拿 admin 接口应 403
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("plainuser@example.com"):
            self._register("plainuser@example.com", invite=_registration_invite())
        tok = self.client.post("/api/login", json={
            "email": "plainuser@example.com", "password": "secret123"}).json()["token"]
        r = self.client.get("/api/admin/users", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 403)

    def test_admin_creates_group_and_moves_user(self):
        atok = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        ah = {"Authorization": f"Bearer {atok}"}
        r = self.client.post("/api/admin/groups", json={
            "name": "vip", "supported_models": ["demo-*"], "rpm_limit": 60}, headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        gid = r.json()["id"]
        # 移动一个用户到 vip
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("moveme@example.com"):
            self._register("moveme@example.com", invite=_registration_invite())
        target = USER_DB.get_user_by_email("moveme@example.com")
        r = self.client.patch(f"/api/admin/users/{target['id']}",
                              json={"group_id": gid}, headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(USER_DB.get_user(target["id"])["group_id"], gid)

    def test_record_usage_estimate_and_upstream(self):
        # 直接验证计量层落库:估算(逆向)与上游(正版)两条口径都写对 token_source
        from core.adapter import Adapter, BILL_ESTIMATE, BILL_UPSTREAM
        u = USER_DB.get_user_by_email("root@example.com")
        user = dict(u, _api_key_id=None)
        grp = USER_DB.get_group(u["group_id"])

        class _Rev(Adapter):
            billing_mode = BILL_ESTIMATE

        class _Off(Adapter):
            billing_mode = BILL_UPSTREAM

        server.BILLING.record_usage(
            user, grp, "grok", "grok-4.6", adapter=_Rev(),
            upstream_usage=None,
            request_messages=[{"role": "user", "content": "hello there friend"}],
            output_text="general kenobi you are a bold one")
        server.BILLING.record_usage(
            user, grp, "metered", "m-a", adapter=_Off(),
            upstream_usage={"prompt_tokens": 11, "completion_tokens": 22},
            request_messages=[{"role": "user", "content": "ignored"}],
            output_text="ignored")
        recent = USER_DB.recent_usage(u["id"], limit=5)
        srcs = {r["token_source"] for r in recent}
        self.assertIn("estimate", srcs)
        self.assertIn("upstream", srcs)
        up = [r for r in recent if r["token_source"] == "upstream"][0]
        self.assertEqual((up["input_tokens"], up["output_tokens"]), (11, 22))
        est = [r for r in recent if r["token_source"] == "estimate"][0]
        self.assertGreater(est["input_tokens"] + est["output_tokens"], 0)

    def _admin_headers(self):
        tok = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def test_admin_create_user_endpoint_removed(self):
        # 建号功能已按需求移除,该端点不应存在
        ah = self._admin_headers()
        r = self.client.post("/api/admin/users", json={
            "email": "byadmin@example.com", "password": "secret123"}, headers=ah)
        self.assertIn(r.status_code, (404, 405), r.text)

    def test_admin_resets_other_password(self):
        ah = self._admin_headers()
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("pwtarget@example.com"):
            self._register("pwtarget@example.com", password="oldpass123",
                           invite=_registration_invite())
        target = USER_DB.get_user_by_email("pwtarget@example.com")
        r = self.client.post(f"/api/admin/users/{target['id']}/password",
                             json={"new_password": "brandnew123"}, headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.post("/api/login", json={
            "email": "pwtarget@example.com", "password": "brandnew123"}).status_code, 200)
        self.assertEqual(self.client.post("/api/login", json={
            "email": "pwtarget@example.com", "password": "oldpass123"}).status_code, 401)

    def test_cannot_demote_last_admin(self):
        ah = self._admin_headers()
        admin = USER_DB.get_user_by_email("root@example.com")
        admins = [u for u in USER_DB.list_users(limit=10000)
                  if u["role"] == "admin" and u["status"] == "active"]
        if len(admins) > 1:
            self.skipTest("more than one admin in this run")
        r = self.client.patch(f"/api/admin/users/{admin['id']}",
                              json={"role": "user"}, headers=ah)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(USER_DB.get_user(admin["id"])["role"], "admin")

    def test_settings_require_invite_toggle(self):
        ah = self._admin_headers()
        # 关闭强制邀请码 → 无码注册成功
        self.client.patch("/api/admin/settings",
                          json={"require_invite": False}, headers=ah)
        r = self._register("freereg@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        # 恢复 → 无码注册 403
        self.client.patch("/api/admin/settings",
                          json={"require_invite": True}, headers=ah)
        r = self._register("blockedreg@example.com")
        self.assertEqual(r.status_code, 403, r.text)

    def test_settings_rejects_unknown_default_group(self):
        ah = self._admin_headers()
        r = self.client.patch("/api/admin/settings",
                              json={"default_group": "does-not-exist"}, headers=ah)
        self.assertEqual(r.status_code, 400)

    # ---- 公告 ----

    def _user_headers(self):
        """普通用户的会话,用来验证公告读接口对非管理员也开放、写接口不开放。"""
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("annuser@example.com"):
            self._register("annuser@example.com", invite=_registration_invite())
        tok = self.client.post("/api/login", json={
            "email": "annuser@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def _mk_ann(self, **over):
        body = {"title": "维护通知", "body": "今晚 0 点重启网关。", "level": "warning"}
        body.update(over)
        return self.client.post("/api/admin/announcements", json=body,
                                headers=self._admin_headers())

    def test_announcement_crud_and_user_visibility(self):
        r = self._mk_ann(title="公告一")
        self.assertEqual(r.status_code, 200, r.text)
        aid = r.json()["announcement"]["id"]
        # 已发布 → 普通用户可见
        got = self.client.get("/api/announcements", headers=self._user_headers())
        self.assertEqual(got.status_code, 200, got.text)
        titles = [a["title"] for a in got.json()["announcements"]]
        self.assertIn("公告一", titles)
        # 下架 → 用户端消失,管理端仍在
        self.client.patch(f"/api/admin/announcements/{aid}",
                          json={"active": False}, headers=self._admin_headers())
        self.assertNotIn("公告一", [a["title"] for a in self.client.get(
            "/api/announcements", headers=self._user_headers())
            .json()["announcements"]])
        self.assertIn("公告一", [a["title"] for a in self.client.get(
            "/api/admin/announcements", headers=self._admin_headers())
            .json()["announcements"]])
        # 删除
        d = self.client.delete(f"/api/admin/announcements/{aid}",
                               headers=self._admin_headers())
        self.assertEqual(d.status_code, 200, d.text)
        self.assertNotIn(aid, [a["id"] for a in self.client.get(
            "/api/admin/announcements", headers=self._admin_headers())
            .json()["announcements"]])

    def test_announcement_edit_bumps_updated_at(self):
        """改正文要抬 updated_at —— 前端靠它把已读的公告重新标成未读。"""
        created = self._mk_ann(title="改前").json()["announcement"]
        r = self.client.patch(f"/api/admin/announcements/{created['id']}",
                              json={"body": "内容换了"},
                              headers=self._admin_headers())
        self.assertEqual(r.status_code, 200, r.text)
        after = r.json()["announcement"]
        self.assertEqual(after["body"], "内容换了")
        self.assertGreaterEqual(after["updated_at"], created["created_at"])
        self.client.delete(f"/api/admin/announcements/{created['id']}",
                           headers=self._admin_headers())

    def test_announcement_pinned_sorts_first(self):
        a = self._mk_ann(title="普通条目").json()["announcement"]
        b = self._mk_ann(title="置顶条目", pinned=True).json()["announcement"]
        listed = self.client.get("/api/announcements",
                                 headers=self._user_headers()).json()["announcements"]
        self.assertEqual(listed[0]["title"], "置顶条目")
        for aid in (a["id"], b["id"]):
            self.client.delete(f"/api/admin/announcements/{aid}",
                               headers=self._admin_headers())

    def test_announcement_rejects_bad_level(self):
        r = self._mk_ann(level="catastrophic")
        self.assertEqual(r.status_code, 400, r.text)

    def test_announcement_write_is_admin_only(self):
        uh = self._user_headers()
        self.assertEqual(self.client.get("/api/admin/announcements",
                                         headers=uh).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/announcements",
                                          json={"title": "x", "body": "y"},
                                          headers=uh).status_code, 403)

    def test_announcement_patch_unknown_id_404(self):
        r = self.client.patch("/api/admin/announcements/deadbeef",
                              json={"pinned": True}, headers=self._admin_headers())
        self.assertEqual(r.status_code, 404)

    def test_announcement_read_state_is_per_user_and_persisted(self):
        """已读落库而不是留在前端:同一用户换会话仍是已读,别人不受影响。"""
        a = self._mk_ann(title="已读测试").json()["announcement"]
        uh = self._user_headers()
        first = self.client.get("/api/announcements", headers=uh).json()
        mine = [x for x in first["announcements"] if x["id"] == a["id"]][0]
        self.assertTrue(mine["unread"])
        self.assertEqual(mine["read_at"], 0)

        r = self.client.post("/api/announcements/read", json={"ids": [a["id"]]},
                             headers=uh)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["marked"], 1)

        # 重新登录(新 token)仍然是已读 —— 状态不在客户端
        again = self.client.get("/api/announcements",
                                headers=self._user_headers()).json()
        mine = [x for x in again["announcements"] if x["id"] == a["id"]][0]
        self.assertFalse(mine["unread"])
        self.assertGreater(mine["read_at"], 0)

        # 管理员是另一个人,自己那份仍未读
        admin_view = self.client.get("/api/announcements",
                                     headers=self._admin_headers()).json()
        theirs = [x for x in admin_view["announcements"] if x["id"] == a["id"]][0]
        self.assertTrue(theirs["unread"])

        self.client.delete(f"/api/admin/announcements/{a['id']}",
                           headers=self._admin_headers())

    def test_announcement_edit_makes_read_one_unread_again(self):
        a = self._mk_ann(title="改后重亮").json()["announcement"]
        uh = self._user_headers()
        user = USER_DB.get_user_by_email("annuser@example.com")
        # 直接把 read_at 写在过去,而不是去 patch time.time():routers.portal 里
        # `time` 就是 time 模块本身,patch 它会把 JWT 签发的时钟也一起改掉。
        USER_DB.mark_announcements_read(user["id"], [a["id"]],
                                        when=a["created_at"] - 60)
        self.assertFalse([x for x in self.client.get(
            "/api/announcements", headers=uh).json()["announcements"]
            if x["id"] == a["id"]][0]["unread"] is None)
        # 改正文会把 updated_at 抬到现在,落在 read_at 之后 → 重新未读
        r = self.client.patch(f"/api/admin/announcements/{a['id']}",
                              json={"body": "换了内容"},
                              headers=self._admin_headers())
        self.assertEqual(r.status_code, 200, r.text)
        row = [x for x in self.client.get(
            "/api/announcements", headers=uh).json()["announcements"]
            if x["id"] == a["id"]][0]
        self.assertGreater(row["updated_at"], row["read_at"])
        self.assertTrue(row["unread"])
        self.client.delete(f"/api/admin/announcements/{a['id']}",
                           headers=self._admin_headers())

    def test_announcement_read_all_when_ids_omitted(self):
        made = [self._mk_ann(title=f"批量{i}").json()["announcement"]
                for i in range(3)]
        uh = self._user_headers()
        r = self.client.post("/api/announcements/read", json={}, headers=uh)
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/announcements", headers=uh).json()
        self.assertEqual(got["unread"], 0)
        for a in made:
            self.client.delete(f"/api/admin/announcements/{a['id']}",
                               headers=self._admin_headers())

    def test_announcement_window_hides_before_start_and_after_end(self):
        now = int(time.time())
        future = self._mk_ann(title="未来的", starts_at=now + 3600).json()["announcement"]
        past = self._mk_ann(title="过期的", starts_at=now - 7200,
                            ends_at=now - 3600).json()["announcement"]
        live = self._mk_ann(title="窗口内", starts_at=now - 60,
                            ends_at=now + 3600).json()["announcement"]
        titles = [a["title"] for a in self.client.get(
            "/api/announcements", headers=self._user_headers())
            .json()["announcements"]]
        self.assertNotIn("未来的", titles)
        self.assertNotIn("过期的", titles)
        self.assertIn("窗口内", titles)
        # 管理端仍然全都看得到
        admin_titles = [a["title"] for a in self.client.get(
            "/api/admin/announcements", headers=self._admin_headers())
            .json()["announcements"]]
        for t in ("未来的", "过期的", "窗口内"):
            self.assertIn(t, admin_titles)
        for a in (future, past, live):
            self.client.delete(f"/api/admin/announcements/{a['id']}",
                               headers=self._admin_headers())

    def test_announcement_rejects_inverted_window(self):
        now = int(time.time())
        r = self._mk_ann(starts_at=now + 3600, ends_at=now + 60)
        self.assertEqual(r.status_code, 400, r.text)

    def test_announcement_rejects_bad_notify_mode(self):
        r = self._mk_ann(notify_mode="carrier-pigeon")
        self.assertEqual(r.status_code, 400, r.text)

    def test_announcement_read_count_visible_to_admin(self):
        a = self._mk_ann(title="计数").json()["announcement"]
        self.client.post("/api/announcements/read", json={"ids": [a["id"]]},
                         headers=self._user_headers())
        listed = self.client.get("/api/admin/announcements",
                                 headers=self._admin_headers()).json()["announcements"]
        row = [x for x in listed if x["id"] == a["id"]][0]
        self.assertEqual(row["read_count"], 1)
        self.assertGreaterEqual(row["user_count"], 1)
        self.client.delete(f"/api/admin/announcements/{a['id']}",
                           headers=self._admin_headers())

    def test_deleting_announcement_drops_read_rows(self):
        a = self._mk_ann(title="删了也别留孤儿").json()["announcement"]
        uh = self._user_headers()
        self.client.post("/api/announcements/read", json={"ids": [a["id"]]},
                         headers=uh)
        user = USER_DB.get_user_by_email("annuser@example.com")
        self.assertIn(a["id"], USER_DB.announcement_reads(user["id"]))
        self.client.delete(f"/api/admin/announcements/{a['id']}",
                           headers=self._admin_headers())
        self.assertNotIn(a["id"], USER_DB.announcement_reads(user["id"]))

    def test_announcement_read_ignores_unknown_ids(self):
        """伪造 id 不该写进已读表 —— 否则表会被任意字符串灌满。"""
        uh = self._user_headers()
        r = self.client.post("/api/announcements/read",
                             json={"ids": ["not-a-real-announcement"]}, headers=uh)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["marked"], 0)
        user = USER_DB.get_user_by_email("annuser@example.com")
        self.assertNotIn("not-a-real-announcement",
                         USER_DB.announcement_reads(user["id"]))


class CheckinApiTest(unittest.TestCase):
    """每日签到：额度配置、UTC+8 日界线、重复与并发幂等。"""

    _seq = itertools.count(1)

    @classmethod
    def setUpClass(cls):
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        if not _db().get_user_by_email("root@example.com"):
            cls.client.post("/api/register", json={
                "email": "root@example.com", "password": "secret123"})

    def setUp(self):
        n = next(self._seq)
        self.email = f"checkin{n}@example.com"
        r = self.client.post("/api/register", json={
            "email": self.email, "password": "secret123",
            "invite_code": _registration_invite()})
        self.assertEqual(r.status_code, 200, r.text)
        token = self.client.post("/api/login", json={
            "email": self.email, "password": "secret123"}).json()["token"]
        self.headers = {"Authorization": "Bearer " + token}
        self.user = _db().get_user_by_email(self.email)
        for key in ("checkin_min", "checkin_max"):
            _db().delete_setting(key)

    def tearDown(self):
        for key in ("checkin_min", "checkin_max"):
            _db().delete_setting(key)

    def _admin_headers(self):
        token = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": "Bearer " + token}

    def _set_range(self, low, high):
        r = self.client.patch("/api/admin/settings", headers=self._admin_headers(),
                              json={"checkin_min": low, "checkin_max": high})
        self.assertEqual(r.status_code, 200, r.text)

    def test_first_claim_credits_once_and_overview_reports_status(self):
        self._set_range(0.0234, 0.0234)
        now = 2_000_000_000
        before = _db().get_user(self.user["id"])["balance"]
        with mock.patch("routers.portal.time.time", return_value=now):
            state = self.client.get("/api/overview", headers=self.headers).json()
            self.assertFalse(state["checkin"]["checked_in"])
            self.assertEqual(state["checkin"]["range"],
                             {"min": 0.0234, "max": 0.0234})

            first = self.client.post("/api/checkin", headers=self.headers)
            second = self.client.post("/api/checkin", headers=self.headers)
            state = self.client.get("/api/overview", headers=self.headers).json()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["claimed"])
        self.assertFalse(second.json()["claimed"])
        self.assertAlmostEqual(first.json()["reward"], 0.0234)
        self.assertAlmostEqual(second.json()["reward"], 0.0234)
        self.assertTrue(state["checkin"]["checked_in"])
        self.assertAlmostEqual(state["checkin"]["reward"], 0.0234)
        after = _db().get_user(self.user["id"])["balance"]
        self.assertAlmostEqual(after - before, 0.0234)
        rows = _db().list_ledger(self.user["id"], reason="checkin")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["timezone"], "UTC+8")

    def test_utc8_midnight_starts_a_new_claim_day(self):
        from datetime import datetime, timezone
        self._set_range(0.01, 0.01)
        # UTC 15:59:59/16:00:00 = UTC+8 的 23:59:59/次日 00:00:00。
        before_midnight = datetime(
            2030, 1, 1, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        with mock.patch("routers.portal.time.time", return_value=before_midnight):
            first = self.client.post("/api/checkin", headers=self.headers).json()
        with mock.patch("routers.portal.time.time", return_value=before_midnight + 1):
            second = self.client.post("/api/checkin", headers=self.headers).json()
        self.assertTrue(first["claimed"])
        self.assertTrue(second["claimed"])
        self.assertNotEqual(first["date"], second["date"])
        self.assertEqual(len(_db().list_ledger(
            self.user["id"], reason="checkin")), 2)

    def test_concurrent_claims_all_return_200_but_only_one_credits(self):
        self._set_range(0.01, 0.01)
        barrier = threading.Barrier(8)
        responses = []
        errors = []

        def claim():
            try:
                barrier.wait()
                responses.append(
                    self.client.post("/api/checkin", headers=self.headers))
            except Exception as exc:
                errors.append(exc)

        before = _db().get_user(self.user["id"])["balance"]
        with mock.patch("routers.portal.time.time", return_value=2_100_000_000):
            threads = [threading.Thread(target=claim) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual([r.status_code for r in responses], [200] * 8)
        self.assertEqual(sum(1 for r in responses if r.json()["claimed"]), 1)
        after = _db().get_user(self.user["id"])["balance"]
        self.assertAlmostEqual(after - before, 0.01)
        self.assertEqual(len(_db().list_ledger(
            self.user["id"], reason="checkin")), 1)

    def test_admin_range_is_atomic_and_inverted_values_are_rejected(self):
        self._set_range(0.01, 0.02)
        ah = self._admin_headers()
        bad = self.client.patch("/api/admin/settings", headers=ah,
                                json={"checkin_min": 0.03, "checkin_max": 0.02})
        self.assertEqual(bad.status_code, 400, bad.text)
        bad_single = self.client.patch("/api/admin/settings", headers=ah,
                                       json={"checkin_min": 0.03})
        self.assertEqual(bad_single.status_code, 400, bad_single.text)
        cfg = self.client.get("/api/admin/settings", headers=ah).json()
        self.assertEqual((cfg["checkin_min"], cfg["checkin_max"]), (0.01, 0.02))

    def test_checkin_requires_login_and_frontend_is_wired(self):
        self.assertEqual(self.client.post("/api/checkin").status_code, 401)
        root = os.path.dirname(os.path.abspath(server.__file__))
        pages = open(os.path.join(root, "static", "portal-pages.js"),
                     encoding="utf-8").read()
        admin = open(os.path.join(root, "static", "portal-admin.js"),
                     encoding="utf-8").read()
        shared = open(os.path.join(root, "static", "portal-shared.js"),
                      encoding="utf-8").read()
        app = open(os.path.join(root, "portal-app.js"), encoding="utf-8").read()
        self.assertIn('api("POST", "/checkin")', pages)
        self.assertIn("立即签到", pages)
        self.assertIn("checkin_min", admin)
        self.assertIn("checkin_max", admin)
        self.assertIn('checkin: ["success", "每日签到"]', shared)
        self.assertIn('"bitapi:balance-changed"', app)


class PortalOrderApiTest(unittest.TestCase):
    """订单查询与人工补单的 HTTP 面。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        import payments.mock  # noqa: F401  (import 即注册)
        config.PAYMENT_PROVIDERS = ["mock"]
        admin = USER_DB.get_user_by_email("root@example.com")
        for email in ("buyer@example.com", "nosy@example.com"):
            if not USER_DB.get_user_by_email(email):
                cls.client.post("/api/register", json={
                    "email": email, "password": "secret123",
                    "invite_code": _registration_invite()})

    def _h(self, email):
        tok = self.client.post("/api/login", json={
            "email": email, "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def _order(self):
        r = self.client.post("/api/orders", json={"amount": 5.0, "provider": "mock"},
                             headers=self._h("buyer@example.com"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["order"]

    def test_owner_can_poll_single_order(self):
        otn = self._order()["out_trade_no"]
        r = self.client.get(f"/api/orders/{otn}", headers=self._h("buyer@example.com"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["order"]["status"], "pending")

    def test_other_user_cannot_read_order(self):
        """订单号是可猜的(PG+时间戳+8 位 hex),不校验归属就是越权读。"""
        otn = self._order()["out_trade_no"]
        r = self.client.get(f"/api/orders/{otn}", headers=self._h("nosy@example.com"))
        self.assertEqual(r.status_code, 404)

    def test_min_topup_exposed_to_frontend(self):
        r = self.client.get("/api/payment/providers",
                            headers=self._h("buyer@example.com"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("min_topup", r.json())

    def test_below_min_topup_rejected_by_api(self):
        saved = config.MIN_TOPUP
        config.MIN_TOPUP = 1.0
        try:
            r = self.client.post("/api/orders",
                                 json={"amount": 0.01, "provider": "mock"},
                                 headers=self._h("buyer@example.com"))
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["code"], "BELOW_MIN_TOPUP")
        finally:
            config.MIN_TOPUP = saved

    def test_admin_requery_order(self):
        otn = self._order()["out_trade_no"]
        ah = self._h("root@example.com")
        r = self.client.post(f"/api/admin/orders/{otn}/requery", headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        # mock 的 query_order 返回 None → 上游状态未知,不该到账
        self.assertFalse(r.json()["credited"])
        self.assertEqual(r.json()["order"]["status"], "pending")

    def test_admin_requery_unknown_order_404(self):
        r = self.client.post("/api/admin/orders/PG-nope/requery",
                             headers=self._h("root@example.com"))
        self.assertEqual(r.status_code, 404)

    def test_requery_requires_admin(self):
        otn = self._order()["out_trade_no"]
        r = self.client.post(f"/api/admin/orders/{otn}/requery",
                             headers=self._h("buyer@example.com"))
        self.assertEqual(r.status_code, 403)

    def test_pay_return_shows_order_status(self):
        otn = self._order()["out_trade_no"]
        r = self.client.get(f"/pay/return/mock?out_trade_no={otn}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("还没收到付款", r.text)
        # 打一次回调让它到账,同一个页面文案必须跟着变
        self.client.get(f"/pay/notify/mock?out_trade_no={otn}"
                        f"&trade_no=T1&amount=5.0")
        r2 = self.client.get(f"/pay/return/mock?out_trade_no={otn}")
        self.assertIn("已到账", r2.text)

    def test_pay_return_does_not_credit(self):
        """入账唯一入口是 notify 与后台对账。return 页若也能触发到账,
        用户手动构造一次 return 就成了第二条入账路径。"""
        otn = self._order()["out_trade_no"]
        before = USER_DB.get_user_by_email("buyer@example.com")["balance"] or 0
        self.client.get(f"/pay/return/mock?out_trade_no={otn}")
        after = USER_DB.get_user_by_email("buyer@example.com")["balance"] or 0
        self.assertAlmostEqual(before, after)
        self.assertEqual(USER_DB.get_order_by_trade_no(otn)["status"], "pending")


class PortalPaymentSettingsTest(unittest.TestCase):
    """管理台支付设置的 HTTP 面。重点是坏值进不去库、密钥不外泄。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        import payments.epay  # noqa: F401  (import 即注册)

    def _ah(self):
        tok = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def tearDown(self):
        for k in ("payment_providers", "min_topup", "site_url",
                  "epay_api_url", "epay_pid", "epay_key", "epay_usd_rate"):
            USER_DB.delete_setting(k)

    def test_get_settings_never_returns_key_plaintext(self):
        """这个端点的凭据是浏览器里的 JWT,前端一个 XSS 就能读走响应体。"""
        USER_DB.set_setting("epay_key", "super-secret-merchant-key")
        r = self.client.get("/api/admin/settings", headers=self._ah())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("super-secret-merchant-key", r.text)
        body = r.json()
        self.assertTrue(body["epay_key_set"])
        self.assertEqual(body["epay_key_tail"], "-key")
        self.assertNotIn("epay_key", body["defaults"])

    def test_patch_persists_and_takes_effect(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"min_topup": 3.5, "epay_usd_rate": 7.25})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/admin/settings", headers=self._ah()).json()
        self.assertAlmostEqual(got["min_topup"], 3.5)
        self.assertTrue(got["from_db"]["min_topup"])
        # 用户端接口读到的是同一个生效值
        self.assertAlmostEqual(
            self.client.get("/api/payment/providers",
                            headers=self._ah()).json()["min_topup"], 3.5)

    def test_delete_restores_env_default(self):
        self.client.patch("/api/admin/settings", headers=self._ah(),
                          json={"min_topup": 9.0})
        r = self.client.delete("/api/admin/settings/min_topup", headers=self._ah())
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/admin/settings", headers=self._ah()).json()
        self.assertFalse(got["from_db"]["min_topup"])
        self.assertAlmostEqual(got["min_topup"], config.MIN_TOPUP)

    def test_explicit_null_rejected_and_writes_nothing(self):
        """清空数值输入框发出的是 null。若当成「本次不改」静默跳过,管理台会弹
        「已保存」而库里没动 —— 刷新后值又回来了,而管理员以为已经改了。
        同一请求里合法的那项也不能落库,否则一次点击写进去一半。"""
        self.client.patch("/api/admin/settings", headers=self._ah(),
                          json={"min_topup": 4.0})
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"min_topup": 9.0, "epay_usd_rate": None})
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertIn("epay_usd_rate", detail)
        self.assertIn("DELETE /admin/settings/epay_usd_rate", detail)
        got = self.client.get("/api/admin/settings", headers=self._ah()).json()
        self.assertAlmostEqual(got["min_topup"], 4.0)
        self.assertIsNone(USER_DB.get_setting("epay_usd_rate"))

    def test_omitted_key_still_means_no_change(self):
        """拒 null 不能顺手把「没传就不改」也带走:两张卡各自只发自己的字段,
        保存支付设置不该把注册设置一起覆盖掉。"""
        self.client.patch("/api/admin/settings", headers=self._ah(),
                          json={"min_topup": 4.0, "epay_pid": "1001"})
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"epay_pid": "2002"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["updated"], ["epay_pid"])
        got = self.client.get("/api/admin/settings", headers=self._ah()).json()
        self.assertEqual(got["epay_pid"], "2002")
        self.assertAlmostEqual(got["min_topup"], 4.0)

    def test_rejects_zero_rate(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"epay_usd_rate": 0})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(USER_DB.get_setting("epay_usd_rate"))

    def test_rejects_low_rate(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"epay_usd_rate": 0.72})
        self.assertEqual(r.status_code, 400, r.text)

    def test_rejects_mock_provider(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"payment_providers": ["mock"]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("mock", r.json()["detail"])

    def test_rejects_http_epay_url(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"epay_api_url": "http://pay.example.com"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_rejects_unknown_provider(self):
        r = self.client.patch("/api/admin/settings", headers=self._ah(),
                              json={"payment_providers": ["epya"]})
        self.assertEqual(r.status_code, 400, r.text)

    def test_settings_require_admin(self):
        admin = USER_DB.get_user_by_email("root@example.com")
        if not USER_DB.get_user_by_email("payplain@example.com"):
            self.client.post("/api/register", json={
                "email": "payplain@example.com", "password": "secret123",
                "invite_code": _registration_invite()})
        tok = self.client.post("/api/login", json={
            "email": "payplain@example.com", "password": "secret123"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/api/admin/settings", headers=h).status_code, 403)
        self.assertEqual(self.client.patch("/api/admin/settings", headers=h,
                                           json={"min_topup": 1}).status_code, 403)
        self.assertEqual(self.client.delete("/api/admin/settings/min_topup",
                                            headers=h).status_code, 403)

    def test_reset_rejects_unknown_key(self):
        r = self.client.delete("/api/admin/settings/admin_key", headers=self._ah())
        self.assertEqual(r.status_code, 400)


class PortalAssetCacheTest(unittest.TestCase):
    """守 docs/incidents/2026-08-31-portal-前端资源被缓存白屏.md。

    portal 的前端文件都没有版本号或内容哈希,浏览器按启发式缓住其中一个、
    另一个是新的,新代码就会调到旧文件里还不存在的函数,Vue setup 抛异常,
    整页白屏 —— 不是某个区块坏掉,是什么都不显示。唯一挡住它的是 no-cache。

    glob 而不是写死文件名:事故本身就是「跨文件版本错配」,明天新增的
    static/portal-*.js 也必须自动被这条盖住。
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_front_end_assets_send_no_cache(self):
        import glob
        root = os.path.dirname(os.path.abspath(server.__file__))
        names = sorted(os.path.basename(p)
                       for p in glob.glob(os.path.join(root, "static", "portal-*.js")))
        # 空 glob 说明资源被改名了,不能静默通过 —— 那样这条门禁就白装了
        self.assertTrue(names, "static/portal-*.js 一个都没匹配到")
        landing_assets = ["/static/landing.css", "/static/landing.js",
                          "/static/landing-orbit.png"]
        for path in ["/", "/portal", "/portal-app.js"] + landing_assets + [f"/static/{n}" for n in names]:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, path)
                self.assertEqual(r.headers.get("cache-control"), "no-cache", path)

    def test_landing_preserves_public_entries_and_self_hosted_assets(self):
        from html.parser import HTMLParser

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.refs = set()
                self.ids = set()
                self.header_links = set()
                self.in_header = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "header":
                    self.in_header = True
                if self.in_header and tag == "a" and attrs.get("href"):
                    self.header_links.add(attrs["href"])
                self.refs.update(v for k, v in attrs.items()
                                 if k in ("src", "href") and v)
                if attrs.get("id"):
                    self.ids.add(attrs["id"])

            def handle_endtag(self, tag):
                if tag == "header":
                    self.in_header = False

        page = Page()
        html = self.client.get("/").text
        page.feed(html)
        self.assertIn("/portal#/plaza", page.header_links)
        self.assertIn("#models", page.header_links)
        self.assertNotIn("ai.baipiao.co", html)
        script = self.client.get("/static/landing.js").text
        self.assertNotIn("ai.baipiao.co", script)
        for link in ("/portal", "/portal#/register", "/portal#/plaza", "/portal#/keys"):
            self.assertIn(link, page.refs)
        for target in ("n-models", "n-ch", "n-price", "statnote", "vendrow",
                       "code", "copybtn", "endpoint-copy", "api-endpoint", "themebtn", "copy-status"):
            self.assertIn(target, page.ids)
        for path, mime in (("/static/landing.css", "text/css"),
                           ("/static/landing.js", "application/javascript"),
                           ("/static/landing-orbit.png", "image/png"),
                           ("/static/bit-api-icon-32.png", "image/png"),
                           ("/static/bit-api-icon-180.png", "image/png")):
            with self.subTest(path=path):
                self.assertIn(path, page.refs)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith(mime))


class PortalPlazaTest(unittest.TestCase):
    """模型广场:/api/models 的可见范围、折叠与图标资源。"""

    @classmethod
    def setUpClass(cls):
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        if not _db().get_user_by_email("root@example.com"):
            cls.client.post("/api/register", json={
                "email": "root@example.com", "password": "secret123"})

    def _h(self):
        tok = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": "Bearer " + tok}

    def test_guest_sees_default_group_view(self):
        """未登录也能看广场,按默认分组的价与可见范围出。"""
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["guest"])
        self.assertEqual(d["group"],
                         portal_state.ensure_default_group()["name"])
        self.assertTrue(d["data"])

    def test_bad_token_is_rejected_not_downgraded(self):
        """带了坏 token 就是 401,不能悄悄按游客放行。

        静默降级的话,一个已掉线的用户会看到默认分组的价而页面毫无提示,
        他会以为那就是自己的价。
        """
        r = self.client.get("/api/models",
                            headers={"Authorization": "Bearer not-a-jwt"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_logged_in_view_is_not_marked_guest(self):
        d = self.client.get("/api/models", headers=self._h()).json()
        self.assertFalse(d["guest"])

    def test_lists_models_with_resolved_price(self):
        self.client.post("/api/admin/pricing/import", headers=self._h(), json={
            "pricing": [{"model_pattern": "grok-4.5", "billing_mode": "token",
                         "input_price": 2, "output_price": 6}]})
        d = self.client.get("/api/models", headers=self._h()).json()
        by = {r["model"]: r for r in d["data"]}
        self.assertIn("grok-4.5", by, "网关暴露的模型必须出现在广场里")
        self.assertEqual(by["grok-4.5"]["billing_mode"], "token")
        self.assertEqual(by["grok-4.5"]["input_price"], 2.0)
        self.assertEqual(by["grok-4.5"]["price_source"], "global")
        self.assertEqual(by["grok-4.5"]["channel"], "grok")

    def test_group_whitelist_narrows_plaza(self):
        """广场的可见范围必须和 /v1/models 一致,否则点得到、调用却 403。"""
        gid = _db().create_group(name="plazagrok", supported_models=["grok-*"],
                                   rpm_limit=0)
        admin = _db().get_user_by_email("root@example.com")
        old = admin["group_id"]
        _db().update_user(admin["id"], group_id=gid)
        server.BILLING.invalidate()
        try:
            names = [r["model"]
                     for r in self.client.get("/api/models",
                                              headers=self._h()).json()["data"]]
            self.assertTrue(names)
            self.assertTrue(all(n.startswith("grok") for n in names), names[:5])
        finally:
            _db().update_user(admin["id"], group_id=old)
            server.BILLING.invalidate()

    def test_spec_variants_fold_into_one_card(self):
        """图/视频模型按「模型 × 分辨率 × 时长」展开,广场折叠成一张卡。

        规格之间计费模式可能不一致(只给部分规格配了价),卡面要如实标 mixed
        并给出区间,不能挑第一个规格的模式冒充整卡。
        """
        self.assertEqual(portal_routes._collapse_key("demo-vid/kling-3-0/1080p/10s"),
                         ("demo-vid/kling-3-0", "1080p/10s"))
        self.assertEqual(portal_routes._collapse_key("deepseek-ai/deepseek-v4-pro"),
                         ("deepseek-ai/deepseek-v4-pro", ""))
        row = {"model": "demo-vid/x", "billing_mode": "free",
               "price_source": "fallback", "per_request_price": 0.0,
               "variants": [
                   {"spec": "1080p/5s", "billing_mode": "free",
                    "price_source": "fallback", "per_request_price": 0.0},
                   {"spec": "720p/5s", "billing_mode": "per_request",
                    "price_source": "global", "per_request_price": 0.35}]}
        portal_routes._fold_variants(row)
        self.assertEqual(row["billing_mode"], "mixed")
        self.assertIsNone(row["price_source"])
        self.assertEqual(row["per_request_price"], 0.0)
        self.assertEqual(row["per_request_price_max"], 0.35)

    def test_vendor_icons_referenced_by_prov_exist(self):
        """PROV 里写的图标名必须有对应文件,否则卡面是一堆碎图。

        PROV 每行是 [正则, 主色, 供应商名, 字母, 图标名, 单色?];该块里
        纯小写的字符串只有图标名(供应商名带大写或中文,字母是单个大写)。
        """
        import glob
        import re
        root = os.path.dirname(os.path.abspath(server.__file__))
        src = open(os.path.join(root, "static", "portal-shared.js"),
                   encoding="utf-8").read()
        block = re.search(r"const PROV = \[(.*?)\n  \];", src, re.S)
        self.assertIsNotNone(block, "PROV 块没找到,这条门禁失效了")
        # 注释里也会出现引号包着的小写词(比如解释 minimaxai 里含 "xai"),先剥掉
        rows = re.sub(r"/\*.*?\*/", "", block.group(1), flags=re.S)
        slugs = set(re.findall(r'"([a-z0-9]+(?:-[a-z0-9]+)*)"', rows))
        self.assertTrue(slugs, "PROV 里没解析出图标名")
        files = {os.path.basename(p)[:-4]
                 for p in glob.glob(os.path.join(root, "static", "model-icons",
                                                 "*.svg"))}
        self.assertEqual(slugs - files, set(), "PROV 引用了不存在的图标文件")
        self.assertEqual(files - slugs, set(), "有图标文件没人引用,应删掉")

    def test_usage_latency_display_contract(self):
        """非流首字有历史回退；总耗时严格按 10/25 秒边界着色。"""
        import re
        root = os.path.dirname(os.path.abspath(server.__file__))
        shared = open(os.path.join(root, "static", "portal-shared.js"),
                      encoding="utf-8").read()
        pages = open(os.path.join(root, "static", "portal-pages.js"),
                     encoding="utf-8").read()
        self.assertRegex(
            shared,
            r'const elapsedLv = \(s\) => \(s <= 10 \? "success"\s*'
            r': s <= 25 \? "warning" : "error"\);')
        self.assertIn(
            "snap.frt_ms == null ? (!row.stream ? ms : null)", shared)
        self.assertIn("r.frt == null ? \"—\"", pages)
        self.assertIn("elapsedLv(r.ms / 1000)", pages)

    def test_api_key_toolbar_exposes_copyable_endpoint(self):
        root = os.path.dirname(os.path.abspath(server.__file__))
        pages = open(os.path.join(root, "static", "portal-pages.js"),
                     encoding="utf-8").read()
        # 端点必须从 location 算出来,不能写死域名 —— 写死了换域名就复制出打不通的地址
        self.assertIn('const API_ENDPOINT = (location.origin + BASE)', pages)
        self.assertNotIn('API_ENDPOINT = "http', pages)   # 也不能是写死的绝对地址
        self.assertIn('copy(API_ENDPOINT, "已复制 API 端点")', pages)
        self.assertIn('aria-label="复制 API 端点"', pages)
        self.assertNotIn("分组可用模型 {{ grp.models.length }} 项", pages)

    def test_vendor_icons_are_visible_on_light_cards(self):
        """白色填充的图标在浅色卡片上会整块消失,只剩一个角标。

        lobehub 里 *-color 有两种:品牌图,和「彩底白字」的应用图标(kimi-color
        就是后者)。后者主体是 #fff,放到白卡片上肉眼看不见,而且不报错。
        单色图(currentColor)不算 —— 那种在 <img> 里解析成黑,深色主题走反色。
        """
        import glob
        root = os.path.dirname(os.path.abspath(server.__file__))
        bad = []
        for p in glob.glob(os.path.join(root, "static", "model-icons", "*.svg")):
            svg = open(p, encoding="utf-8").read()
            if 'fill="#fff"' in svg.lower() or 'fill="white"' in svg.lower():
                bad.append(os.path.basename(p))
        self.assertEqual(bad, [], "这些图标主体是白的,浅色卡片上看不见")

    def test_model_icon_is_served_from_static(self):
        """图标在 static 的子目录里,靠 {name:path} 才取得到。"""
        r = self.client.get("/static/model-icons/deepseek-color.svg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("content-type"), "image/svg+xml")
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        # 子目录放开后越权仍要挡住
        self.assertEqual(
            self.client.get("/static/model-icons/../../config.py").status_code,
            404)


class PortalPlanApiTest(unittest.TestCase):
    """订阅时长卡的 HTTP 面:上架、买、续费、换卡、到期回落。

    到期语义本身在 test_site_settings.py 的 PlanSubscriptionTest 里(库层),
    这里只管接口:钱扣对没有、组换没换、越权的卡能不能买。
    """

    @classmethod
    def setUpClass(cls):
        global USER_DB
        USER_DB = portal_state.USER_DB
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        # 单独跑这个类时(-k Plan)前面的类不会运行,root 得自己建 —— 库空时
        # 第一个注册的人就是管理员,后面几个才需要邀请码。
        if not USER_DB.get_user_by_email("root@example.com"):
            cls.client.post("/api/register", json={
                "email": "root@example.com", "password": "secret123"})
        for email in ("planbuyer@example.com", "planpoor@example.com",
                      "planpayer@example.com", "plandouble@example.com"):
            if not USER_DB.get_user_by_email(email):
                cls.client.post("/api/register", json={
                    "email": email, "password": "secret123",
                    "invite_code": _registration_invite()})
        ah = cls._headers("root@example.com")
        cls.hour = cls._group(ah, {
            "name": "plan-hour", "price": 1.0, "duration_hours": 1,
            "listed": 1, "billing_policy": "quota", "daily_limit": 100,
            "supported_models": ["*"], "notes": "一小时不限量"})
        cls.day = cls._group(ah, {
            "name": "plan-day", "price": 2.0, "duration_hours": 24,
            "listed": 1, "billing_policy": "balance",
            "supported_models": ["*"]})
        # 内部档位:同一张表,只是没上架 —— 用户端不该看见,更不该买得到
        cls.hidden = cls._group(ah, {
            "name": "plan-internal", "price": 3.0, "duration_hours": 24,
            "listed": 0, "supported_models": ["*"]})

    @classmethod
    def _headers(cls, email):
        tok = cls.client.post("/api/login", json={
            "email": email, "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    @classmethod
    def _group(cls, ah, body):
        """建组;同名已存在(同一个库里重复跑)就复用。"""
        r = cls.client.post("/api/admin/groups", json=body, headers=ah)
        if r.status_code == 409:
            return USER_DB.get_group_by_name(body["name"])["id"]
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def _topup(self, email, amount):
        uid = USER_DB.get_user_by_email(email)["id"]
        r = self.client.post(f"/api/admin/users/{uid}/balance",
                             json={"amount": amount},
                             headers=self._headers("root@example.com"))
        self.assertEqual(r.status_code, 200, r.text)

    def _reset(self, email):
        """把买过的卡清掉,让每个用例从「按量计费」这一档起跑。"""
        uid = USER_DB.get_user_by_email(email)["id"]
        USER_DB.update_user(uid, plan_group_id=None, plan_expires_at=0)
        return uid

    def _buy(self, email, gid):
        return self.client.post("/api/plans/purchase", json={"group_id": gid},
                                headers=self._headers(email))

    def _buy_at(self, headers, gid, at):
        """在指定的那一秒买。幂等键按秒算,「同一秒」与「隔几秒」是两种语义,
        不冻时间的话两次请求偶尔跨秒,用例会时好时坏。

        登录必须在冻结之外:JWT 的过期判定走 PyJWT 自己的时钟,冻了签发时间
        会让 token 当场就算「过期」。所以这里收 headers 而不是 email。
        """
        with mock.patch("routers.portal.time.time", return_value=float(at)):
            return self.client.post("/api/plans/purchase",
                                    json={"group_id": gid}, headers=headers)

    def test_only_listed_plans_are_offered(self):
        h = self._headers("planbuyer@example.com")
        r = self.client.get("/api/plans", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        names = [p["name"] for p in body["plans"]]
        self.assertIn("plan-hour", names)
        self.assertIn("plan-day", names)
        self.assertNotIn("plan-internal", names)
        # 卡面要能自己算出「小时卡 / 天卡」与价,少一项前端就没东西显示
        hour = [p for p in body["plans"] if p["name"] == "plan-hour"][0]
        self.assertEqual((hour["price"], hour["duration_hours"]), (1.0, 1))
        self.assertEqual(hour["notes"], "一小时不限量")
        self.assertEqual(hour["billing_policy"], "quota")
        self.assertEqual(hour["daily_limit"], 100)

    def test_no_plan_means_pay_as_you_go(self):
        """没买卡时 current 为 null、base_group 指向按量计费那一档 —— 这是常态,
        不是缺省错误,前端靠它决定把「当前」标在哪张卡上。"""
        self._reset("planbuyer@example.com")
        body = self.client.get(
            "/api/plans", headers=self._headers("planbuyer@example.com")).json()
        self.assertIsNone(body["current"])
        self.assertEqual(body["base_group"], "free")

    def test_purchase_charges_balance_and_switches_group(self):
        # 专用一个买家:购买的幂等键按「用户 + 套餐 + 秒」算,跟别的用例共用
        # 账号会让同一秒里的第二笔被当成双击,钱就不动了(见下一个用例)。
        email = "planpayer@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        before = USER_DB.get_user(uid)["balance"]
        r = self._buy(email, self.day)
        self.assertEqual(r.status_code, 200, r.text)
        now = int(time.time())
        after = USER_DB.get_user(uid)
        self.assertAlmostEqual(after["balance"], before - 2.0, places=6)
        self.assertEqual(after["plan_group_id"], self.day)
        self.assertAlmostEqual(after["plan_expires_at"], now + 24 * 3600, delta=5)
        # 钱少了必须有对应流水,否则「余额变了但查不出为什么」
        reasons = [e["reason"] for e in USER_DB.list_ledger(uid, limit=5)]
        self.assertIn("plan", reasons)
        # 生效分组立刻换成套餐组,不等下一次登录
        bill = self.client.get("/api/billing", headers=self._headers(email)).json()
        self.assertEqual(bill["group"], "plan-day")

    def test_double_submit_buys_one_card_not_two(self):
        """双击只算一次。只挡扣款不挡时长的话,一次点击的钱能买到两段有效期。"""
        email = "plandouble@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        before = USER_DB.get_user(uid)["balance"]
        h = self._headers(email)
        at = int(time.time()) + 10
        first = self._buy_at(h, self.day, at)
        second = self._buy_at(h, self.day, at)
        self.assertEqual((first.status_code, second.status_code), (200, 200),
                         second.text)
        self.assertEqual(first.json()["expires_at"], second.json()["expires_at"])
        after = USER_DB.get_user(uid)
        self.assertAlmostEqual(after["balance"], before - 2.0, places=6)
        self.assertEqual(after["plan_expires_at"], at + 24 * 3600)

    def test_renew_same_plan_stacks_expiry(self):
        """同一张卡没到期时再买,从原到期时间往后加 —— 重置会吞掉用户已付的那段。"""
        email = "planbuyer@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        h = self._headers(email)
        at = int(time.time()) + 10
        self.assertEqual(self._buy_at(h, self.day, at).status_code, 200)
        first = USER_DB.get_user(uid)["plan_expires_at"]
        before = USER_DB.get_user(uid)["balance"]
        # 隔几秒的第二次是真续费,不是双击:时长叠加,钱也要再扣一次
        r = self._buy_at(h, self.day, at + 5)
        self.assertEqual(r.status_code, 200, r.text)
        after = USER_DB.get_user(uid)
        self.assertEqual(after["plan_expires_at"], first + 24 * 3600)
        self.assertAlmostEqual(after["balance"], before - 2.0, places=6)

    def test_switching_plan_starts_from_now(self):
        """换成另一张卡从现在起算,原卡剩余时长不折算(这条写在用户端卡片文案里)。"""
        email = "planbuyer@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        self.assertEqual(self._buy(email, self.day).status_code, 200)
        self.assertEqual(self._buy(email, self.hour).status_code, 200)
        after = USER_DB.get_user(uid)
        self.assertEqual(after["plan_group_id"], self.hour)
        self.assertAlmostEqual(after["plan_expires_at"], int(time.time()) + 3600,
                               delta=5)

    def test_expired_plan_falls_back_to_pay_as_you_go(self):
        email = "planbuyer@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        self.assertEqual(self._buy(email, self.day).status_code, 200)
        USER_DB.update_user(uid, plan_expires_at=int(time.time()) - 1)
        h = self._headers(email)
        self.assertEqual(
            self.client.get("/api/billing", headers=h).json()["group"], "free")
        cur = self.client.get("/api/plans", headers=h).json()["current"]
        # 过期后 current 还在(要能告诉用户「已回落」),但 active 必须是 False
        self.assertEqual(cur["name"], "plan-day")
        self.assertFalse(cur["active"])

    def test_insufficient_balance_is_402(self):
        email = "planpoor@example.com"
        uid = self._reset(email)
        bal = USER_DB.get_user(uid)["balance"] or 0
        if bal > 0:
            self._topup(email, -bal)
        r = self._buy(email, self.day)
        self.assertEqual(r.status_code, 402, r.text)
        self.assertIsNone(USER_DB.get_user(uid)["plan_group_id"])

    def test_unlisted_group_cannot_be_purchased(self):
        """没上架的分组只是管理员分配用的内部档位。能买等于让用户自助升级套餐。"""
        email = "planbuyer@example.com"
        self._reset(email)
        self._topup(email, 10.0)
        self.assertEqual(self._buy(email, self.hidden).status_code, 404)

    def test_disabled_plan_leaves_the_shelf(self):
        ah = self._headers("root@example.com")
        self.client.patch(f"/api/admin/groups/{self.hour}",
                          json={"status": "disabled"}, headers=ah)
        try:
            names = [p["name"] for p in self.client.get(
                "/api/plans",
                headers=self._headers("planbuyer@example.com")).json()["plans"]]
            self.assertNotIn("plan-hour", names)
            self.assertEqual(self._buy("planbuyer@example.com",
                                       self.hour).status_code, 404)
        finally:
            self.client.patch(f"/api/admin/groups/{self.hour}",
                              json={"status": "active"}, headers=ah)

    def test_zero_price_plan_cannot_be_listed(self):
        """0 元「套餐」上架是个白送的漏洞:用户点一次不扣钱,却换掉了自己的计费档。"""
        ah = self._headers("root@example.com")
        r = self.client.post("/api/admin/groups", json={
            "name": "plan-free-lunch", "listed": 1, "price": 0}, headers=ah)
        self.assertEqual(r.status_code, 400, r.text)
        # 先建成不上架的档,再单独打开上架开关也要拦住(那一下不带 price)
        gid = self._group(ah, {"name": "plan-nofee", "listed": 0, "price": 0})
        r = self.client.patch(f"/api/admin/groups/{gid}",
                              json={"listed": 1}, headers=ah)
        self.assertEqual(r.status_code, 400, r.text)

    def test_admin_edits_price_and_duration(self):
        ah = self._headers("root@example.com")
        r = self.client.patch(f"/api/admin/groups/{self.day}", json={
            "price": 2.5, "duration_hours": 48, "notes": "两天"}, headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        try:
            card = [p for p in self.client.get(
                "/api/plans",
                headers=self._headers("planbuyer@example.com")).json()["plans"]
                if p["name"] == "plan-day"][0]
            self.assertEqual((card["price"], card["duration_hours"]), (2.5, 48))
            self.assertEqual(card["notes"], "两天")
        finally:
            self.client.patch(f"/api/admin/groups/{self.day}", json={
                "price": 2.0, "duration_hours": 24}, headers=ah)

    def test_delete_group_blocked_while_someone_is_on_it(self):
        """删掉还有人挂着的组,那些用户的分组会指空,之后每次调用都被模型白名单
        挡成 403 —— 而报错里看不出是删组导致的。"""
        ah = self._headers("root@example.com")
        email = "planbuyer@example.com"
        uid = self._reset(email)
        self._topup(email, 10.0)
        self.assertEqual(self._buy(email, self.day).status_code, 200)
        r = self.client.delete(f"/api/admin/groups/{self.day}", headers=ah)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNotNone(USER_DB.get_group(self.day))
        # 过期也算引用:字段还指着它,清掉组等于把那段历史指空
        USER_DB.update_user(uid, plan_expires_at=int(time.time()) - 1)
        self.assertEqual(self.client.delete(
            f"/api/admin/groups/{self.day}", headers=ah).status_code, 409)
        self._reset(email)

    def test_delete_unreferenced_group_also_drops_its_pricing(self):
        ah = self._headers("root@example.com")
        gid = self._group(ah, {"name": "plan-throwaway", "supported_models": ["*"]})
        self.client.post("/api/admin/pricing", json={
            "model_pattern": "demo-*", "group_id": gid, "billing_mode": "token",
            "input_price": 1.0, "output_price": 2.0}, headers=ah)
        self.assertTrue(USER_DB.list_pricing(gid))
        r = self.client.delete(f"/api/admin/groups/{gid}", headers=ah)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(USER_DB.get_group(gid))
        # 组没了还留着分组专属价,就是一堆永远解析不到、却还在定价列表里的死行
        self.assertEqual(USER_DB.list_pricing(gid), [])

    def test_default_group_cannot_be_deleted(self):
        ah = self._headers("root@example.com")
        base = USER_DB.get_group_by_name("free")
        r = self.client.delete(f"/api/admin/groups/{base['id']}", headers=ah)
        self.assertEqual(r.status_code, 400, r.text)

    def test_purchase_requires_login(self):
        r = self.client.post("/api/plans/purchase", json={"group_id": self.day})
        self.assertEqual(r.status_code, 401)


class ChannelSwitchApiTest(unittest.TestCase):
    """渠道上下线的 HTTP 面。

    断言的是「三处一起消失」:模型清单、模型广场、/v1/* 路由。少一处就成了
    「广场里看不到但照样调得通」或者反过来,那种不一致最难查。
    """

    CH = "grok"

    @classmethod
    def setUpClass(cls):
        global USER_DB
        USER_DB = portal_state.USER_DB
        portal_state.ensure_default_group()
        cls.client = TestClient(server.app)
        if not USER_DB.get_user_by_email("root@example.com"):
            cls.client.post("/api/register", json={
                "email": "root@example.com", "password": "secret123"})

    def setUp(self):
        self.ah = self._admin_headers()
        r = self.client.post("/api/keys", json={"name": "chsw"}, headers=self.ah)
        self.key = {"Authorization": "Bearer " + r.json()["key"]}
        # 模块级注册表 + settings 行都要复位,否则污染同进程后面的用例
        self.addCleanup(self._reset)

    def _reset(self):
        USER_DB.delete_setting("disabled_channels")
        portal_state.apply_channel_switches()

    def _admin_headers(self):
        tok = self.client.post("/api/login", json={
            "email": "root@example.com", "password": "secret123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def _off(self, names):
        return self.client.patch("/api/admin/settings",
                                 json={"disabled_channels": names}, headers=self.ah)

    def _listed(self, headers):
        return [m["id"] for m in
                self.client.get("/v1/models", headers=headers).json()["data"]]

    def _plaza(self):
        """广场走的是 JWT(浏览器里的登录态),不是 sk- key。"""
        return [c["model"] for c in
                self.client.get("/api/models", headers=self.ah).json()["data"]]

    def _mine(self, names):
        """筛出本渠道的模型。

        不能按 `渠道名/` 前缀过滤:模型名不一定带渠道前缀(grok 的就是裸的
        grok-4.6),而 xai-grok-4.6 又只是碰巧含 grok。以渠道目录里的清单为准。
        """
        owned = set(self._channels()[self.CH]["models"])
        return [m for m in names if m in owned]

    def _channels(self):
        r = self.client.get("/api/admin/channels", headers=self.ah)
        self.assertEqual(r.status_code, 200, r.text)
        return {c["name"]: c for c in r.json()["channels"]}

    def test_channels_endpoint_lists_models_and_switch_state(self):
        """分组编辑里的「可用模型」下拉靠这份目录,所以要给真实模型名,
        不能只给个数 —— 让管理员手打模型名就是等着打错。"""
        row = self._channels()[self.CH]
        self.assertFalse(row["disabled"])
        self.assertEqual(row["kind"], "chat")
        self.assertIn("grok-4.6", row["models"])
        self.assertEqual(row["wildcard"], "grok-4.*")
        # 设置端点只回「开关的值」,渠道目录不在那儿 —— 同一件事一处口径
        cfg = self.client.get("/api/admin/settings", headers=self.ah).json()
        self.assertEqual(cfg["disabled_channels"], [])
        self.assertNotIn("channels", cfg)

    def test_channel_wildcard_is_computed_not_guessed(self):
        """通配是算出来的,不是按渠道名猜的:grok 的模型是 grok-4.6 / grok-4.5,
        公共前缀给的是 grok-4.*。

        算不出公共前缀的形状(vendor/model)不给通配 —— 给一个错的通配比不给更糟。
        这条打在纯函数上,不需要真有那样一个渠道在册。
        """
        chans = self._channels()
        self.assertEqual(chans["grok"]["wildcard"], "grok-4.*")
        self.assertIsNone(portal_routes._channel_wildcard(
            ["minimaxai/minimax-m3", "google/gemma-4-31b-it"]))
        self.assertIsNone(portal_routes._channel_wildcard(["lonely-model"]))

    def test_disabling_removes_models_from_list_plaza_and_routing(self):
        before = self._mine(self._listed(self.key))
        self.assertTrue(before, "grok 的模型本该在清单里")
        model = before[0]
        self.assertEqual(self._off([self.CH]).status_code, 200)

        self.assertEqual(self._mine(self._listed(self.key)), [])
        self.assertEqual(self._mine(self._plaza()), [])
        r = self.client.post("/v1/chat/completions",
                             json={"model": model,
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers=self.key)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("unknown model", r.text)

    def test_master_key_is_not_a_back_door(self):
        """下线是站点级动作。留个「看不见但调得通」的后门,以后没人查得出
        为什么那个渠道还在扣额度。"""
        master = {"Authorization": "Bearer " + MASTER_KEY}
        self.assertTrue(self._mine(self._listed(master)))
        self._off([self.CH])
        self.assertEqual(self._mine(self._listed(master)), [])

    def test_pool_and_panel_still_see_a_disabled_channel(self):
        """下线只对用户下线 —— 号池维护面照旧,否则一下线就再也开不回来。"""
        self._off([self.CH])
        r = self.client.get("/admin/stats",
                            headers={"Authorization": "Bearer " + config.ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.CH, r.json()["channels"])
        # 渠道目录也要照旧列出它(开关列表就是从这份目录渲染的)
        row = self._channels()[self.CH]
        self.assertTrue(row["disabled"])
        self.assertTrue(row["models"])

    def test_reset_puts_it_back_online(self):
        self._off([self.CH])
        r = self.client.delete("/api/admin/settings/disabled_channels",
                               headers=self.ah)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self._mine(self._listed(self.key)))

    def test_typo_in_channel_name_is_rejected(self):
        """静默不生效意味着界面写着「已下线」而那个渠道照旧在卖。"""
        r = self._off(["grokk"])
        self.assertEqual(r.status_code, 400, r.text)
        self.assertTrue(self._mine(self._listed(self.key)))

    def test_switch_state_survives_a_restart(self):
        """存库即生效,重启后也仍然生效 —— 注册表是内存的,重启走的是
        apply_channel_switches() 那条路。"""
        self._off([self.CH])
        portal_state.apply_channel_switches()      # 模拟启动时那一次推送
        self.assertEqual(self._mine(self._listed(self.key)), [])


class AppSchemaTest(unittest.TestCase):
    """应用级 schema 不变量:OpenAPI 合法 + 页面引用的图标真的存在。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_operation_ids_are_unique(self):
        """OpenAPI 要求 operationId 唯一,重了客户端生成器和 schema 校验会报。

        最容易踩的是 api_route(methods=["GET","POST"]):一个函数两种方法,
        自动生成的 id 只带第一个方法名,两条路由拿到同一个 id。
        """
        import collections
        spec = self.client.get("/openapi.json").json()
        ids = [(op["operationId"], m.upper() + " " + p)
               for p, methods in spec["paths"].items()
               for m, op in methods.items()
               if isinstance(op, dict) and "operationId" in op]
        dup = {i: [w for x, w in ids if x == i]
               for i, n in collections.Counter(i for i, _ in ids).items() if n > 1}
        self.assertEqual(dup, {}, "operationId 重复")

    def test_pay_notify_takes_both_methods(self):
        """拆成两个装饰器是为了 operationId,不能顺手把某个方法弄丢 ——
        易支付走 GET 带 query,别的渠道 POST,少一个就收不到回调。"""
        spec = self.client.get("/openapi.json").json()
        self.assertEqual(set(spec["paths"]["/pay/notify/{provider}"]),
                         {"get", "post"})

    def test_pool_panel_can_carry_its_own_admin_key(self):
        """号池面板必须能自己带 admin key,不能只靠反代注入。

        原来 hdr() 硬返回 {},鉴权全指望反代注入 Authorization。那份配置不在
        仓库里,于是面板一旦不是从那条反代进来的(直连源站、换域名、或代理只弹
        Basic Auth 而没注入头),整页就只剩「无权限」四个字,连个填 key 的地方
        都没有 —— 而 /admin/* 数据端点本身是好的,纯粹是前端没带凭据。
        """
        import re
        root = os.path.dirname(os.path.abspath(server.__file__))
        html = open(os.path.join(root, "dashboard.html"), encoding="utf-8").read()
        m = re.search(r"function hdr\(\)\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", html)
        self.assertIsNotNone(m, "dashboard.html 里找不到 hdr()")
        self.assertIn("Authorization", m.group(1),
                      "hdr() 不带 Authorization,面板只能靠反代注入")
        # 得有个入口能填/改 key,否则 401 之后还是死胡同
        self.assertRegex(html, r"function setKey\(\)")
        self.assertIn("setKey()", html.split("<script>")[0],
                      "工具条里没有设置密钥的按钮")
        # key 只存本机浏览器,不该被写进任何随仓库分发的文件
        self.assertIn("localStorage", m.group(1) + html[m.end():m.end() + 900])

    def test_html_pages_use_the_new_brand_icon(self):
        """页面必须引降采样产物,不能引 1254×1254 的母版。

        母版 946 KB,而 favicon 只画 16/32 px、顶栏 brandmark 只有 30 px CSS ——
        挂母版等于每个访客每次打开都下将近 1 MB(/static/ 是 no-cache,
        每次都要回源)。所以这里断言的是「小」:上一版这条断言写的是
        assertGreater(len, 100_000),等于把浪费本身锁进了门禁。
        """
        import re
        import struct

        def png(path, want_side, max_kb):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("content-type"), "image/png", path)
            self.assertEqual(r.content[:8], b"\x89PNG\r\n\x1a\n", path)
            self.assertEqual(r.content[-12:], b"\x00\x00\x00\x00IEND\xaeB`\x82",
                             path)
            # IHDR 紧跟 8 字节签名:长度(4)+类型(4)+宽(4)+高(4)
            w, h = struct.unpack(">II", r.content[16:24])
            self.assertEqual((w, h), (want_side, want_side), path)
            self.assertLess(len(r.content), max_kb * 1024,
                            "%s 比 %d KB 还大" % (path, max_kb))

        root = os.path.dirname(os.path.abspath(server.__file__))
        for page in ("portal.html", "dashboard.html"):
            with self.subTest(page=page):
                html = open(os.path.join(root, page), encoding="utf-8").read()
                m = re.search(r'<link rel="icon"[^>]*href="([^"]+)"', html)
                self.assertIsNotNone(m, page + " 没声明 favicon")
                self.assertEqual(m.group(1), "/static/bit-api-icon-32.png")
                png(m.group(1), 32, 8)
                t = re.search(r'<link rel="apple-touch-icon"[^>]*href="([^"]+)"',
                              html)
                self.assertIsNotNone(t, page + " 没声明 apple-touch-icon")
                self.assertEqual(t.group(1), "/static/bit-api-icon-180.png")
                png(t.group(1), 180, 40)

        portal_app = open(os.path.join(root, "portal-app.js"),
                          encoding="utf-8").read()
        # 四处:登录/注册卡、找回密码卡、社区回调卡、侧栏品牌位
        self.assertEqual(portal_app.count(
            'class="brandmark" src="/static/bit-api-icon-180.png"'), 4)
        self.assertNotIn('<span class="brandmark">b</span>', portal_app)

    def test_master_icon_is_never_shipped_to_browsers(self):
        """母版只当源文件留着。任何页面或前端脚本引到它,那 946 KB 就又回到
        每次访问的成本里,而界面上看不出任何区别 —— 只有网络面板看得见。"""
        root = os.path.dirname(os.path.abspath(server.__file__))
        master = "bit-api-icon-v3.png"
        for name in ("portal.html", "dashboard.html", "portal-app.js",
                     "static/portal-shared.js", "static/portal-pages.js",
                     "static/portal-admin.js"):
                path = os.path.join(root, name)
                with self.subTest(file=name):
                    self.assertNotIn(master, open(path, encoding="utf-8").read())

    def test_declared_icons_exist(self):
        """页面声明了哪个图标,仓里就得真有那个文件、且是完整 PNG。

        半截文件照样能过 /health,只有访客的浏览器解不出来 —— 所以按页面声明的
        清单逐个查,而不是挑仓里最大的那个文件查。
        """
        import re
        root = os.path.dirname(os.path.abspath(server.__file__))
        declared = set()
        for page in ("portal.html", "dashboard.html"):
            html = open(os.path.join(root, page), encoding="utf-8").read()
            for href in re.findall(r'<link rel="(?:icon|apple-touch-icon)"'
                                   r'[^>]*href="/static/([^"]+)"', html):
                declared.add(href)
        self.assertTrue(declared, "页面里没解析出图标声明")
        for name in sorted(declared):
            path = os.path.join(root, "static", name)
            self.assertTrue(os.path.exists(path), f"页面引用了不存在的图标 {name}")
            with open(path, "rb") as f:
                head = f.read(8)
            self.assertEqual(head, b"\x89PNG\r\n\x1a\n", f"{name} 不是完整 PNG")


if __name__ == "__main__":
    unittest.main()
