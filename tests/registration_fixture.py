"""Existing flow fixtures model a delivered email code in their isolated SQLite DB.

Registration delivery/validation tests use the unmodified FastAPI TestClient.
No production flag or email policy override is used.
"""
from fastapi.testclient import TestClient as FastAPITestClient

from core import auth


def verified_registration_body(db, body):
    body = dict(body)
    if "verification_code" in body:
        return body
    email = str(body.get("email", "")).strip().lower()
    body["verification_code"] = "123456"
    if email and not db.get_user_by_email(email):
        with db._conn() as conn:
            proof = conn.execute("SELECT 1 FROM registration_codes WHERE email=?", (email,)).fetchone()
        if not proof:
            digest = auth.hash_password(body["verification_code"])
            db.reserve_registration_code(email, digest)
            db.finish_registration_mail(email, digest, True)
    return body


class TestClient(FastAPITestClient):
    __test__ = False

    def post(self, url, **kwargs):
        if str(url).endswith("/api/register") and isinstance(kwargs.get("json"), dict):
            from routers import portal
            kwargs["json"] = verified_registration_body(portal.USER_DB, kwargs["json"])
        return super().post(url, **kwargs)
