"""GPT OAuth 导入：兼容 sub2api 的 token / auth.json / session / 导出格式。

身份、期限与去重语义参考 sub2api account_codex_import.go；凭据只进现有 accounts 表。
JWT 解码用于提取字段，不是签名验证或验活。
"""
import base64
import datetime
import hashlib
import json
import math
import re
import threading
import time

from core import db as dbmod

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
MAX_IMPORT_ENTRIES = 1000
MAX_IMPORT_BYTES = 10 * 1024 * 1024
CLOCK_SKEW = 120
IMPORT_LOCK = threading.RLock()


def fingerprint(value):
    return hashlib.sha256(str(value or "").strip().encode()).hexdigest()


def first_string(obj, *paths):
    for path in paths:
        value = obj
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def epoch(value):
    if value is None or isinstance(value, bool) or value == "":
        return 0
    try:
        number = float(value)
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError, OSError, OverflowError):
            raise ValueError("过期时间格式无效") from None
    if not math.isfinite(number):
        raise ValueError("过期时间格式无效")
    result = int(number / 1000) if number > 1e12 else int(number)
    try:
        datetime.datetime.fromtimestamp(result, datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        raise ValueError("过期时间超出有效范围") from None
    return result


def decode_jwt_claims(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        value = json.loads(payload)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def _flatten(values):
    out = []
    for value in values:
        if isinstance(value, list):
            out.extend(_flatten(value))
        elif isinstance(value, dict) and isinstance(value.get("accounts"), list):
            out.extend(_flatten(value["accounts"]))
        else:
            out.append(value)
    return out


def _json_stream(text):
    decoder, values, offset = json.JSONDecoder(), [], 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        value, offset = decoder.raw_decode(text, offset)
        values.append(value)
    return _flatten(values)


def parse_import_text(text):
    if not isinstance(text, str):
        raise ValueError("导入内容必须是文本")
    text = text.strip().lstrip("\ufeff").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            return _json_stream(text)
        except ValueError:
            if "\n" not in text:
                raise ValueError("JSON 解析失败") from None
    values = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip().rstrip(",")
        if not line:
            continue
        if line[0] in "[{":
            try:
                values.extend(_json_stream(line))
            except ValueError:
                raise ValueError(f"第 {number} 行 JSON 解析失败") from None
        else:
            parts = line.split("----", 3)
            if len(parts) == 4 and re.fullmatch(r"[^\s@]+@[^\s@]+", parts[0].strip()):
                # 四段文本仅提取邮箱和 token，不保留密码或二步验证密钥。
                values.append({"email": parts[0].strip(), "access_token": parts[3].strip()})
            else:
                values.append(line)
    return values


def parse_import_body(body):
    if isinstance(body, list):
        return _flatten(body)
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象或数组")
    contents = body.get("contents") or []
    if not isinstance(contents, list) or any(not isinstance(v, str) for v in contents):
        raise ValueError("contents 必须是文本数组")
    texts = ([body["content"]] if body.get("content") is not None else []) + contents
    if texts:
        return [v for text in texts for v in parse_import_text(text)]
    if isinstance(body.get("accounts"), list):
        return _flatten(body["accounts"])
    return [body] if any(k in body for k in ("tokens", "access_token", "accessToken", "credentials", "agent_identity", "agent_private_key")) else []


def _enrich(item, token):
    claims = decode_jwt_claims(token)
    if not claims:
        return
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    fields = {
        "email": first_string(claims, ("email",)) or first_string(profile, ("email",)),
        "account_id": first_string(auth, ("chatgpt_account_id",)),
        "user_id": first_string(auth, ("chatgpt_user_id",), ("user_id",)) or first_string(claims, ("sub",)),
        "plan_type": first_string(auth, ("chatgpt_plan_type",)),
        "organization": first_string(auth, ("poid",)),
    }
    orgs = auth.get("organizations") if isinstance(auth, dict) else None
    if not fields["organization"] and isinstance(orgs, list):
        candidates = [o for o in orgs if isinstance(o, dict) and o.get("id")]
        default = next((o for o in candidates if o.get("is_default")), candidates[0] if candidates else {})
        fields["organization"] = first_string(default, ("id",))
    for key, value in fields.items():
        if not item[key]:
            item[key] = value


def normalize_entry(value, validate_expiry=True):
    if not isinstance(value, (str, dict)):
        raise ValueError("账号必须是 token 文本或 JSON 对象")
    raw = {"access_token": value} if isinstance(value, str) else value
    if isinstance(raw.get("credentials"), dict):
        if raw.get("platform", "openai") != "openai" or raw.get("type", "oauth") != "oauth":
            raise ValueError("只支持 OpenAI OAuth 账号")
        raw = {**raw, **raw["credentials"]}
    if raw.get("agent_identity") or raw.get("agent_private_key") or raw.get("auth_mode") == "agentIdentity":
        from core.gpt_auth import agent_credentials
        credentials = agent_credentials(raw)
        return {**{key: "" for key in ("access_token", "refresh_token", "id_token", "email", "account_id", "user_id", "plan_type", "client_id", "organization", "name")},
                **credentials, "expires_at": 0, "warnings": []}
    paths = {
        "access_token": (("tokens", "access_token"), ("tokens", "accessToken"), ("access_token",), ("accessToken",), ("token",)),
        "refresh_token": (("tokens", "refresh_token"), ("tokens", "refreshToken"), ("refresh_token",), ("refreshToken",)),
        "id_token": (("tokens", "id_token"), ("tokens", "idToken"), ("id_token",), ("idToken",)),
        "client_id": (("tokens", "client_id"), ("client_id",)),
        "email": (("email",), ("user", "email")),
        "name": (("name",), ("user", "name")),
        "account_id": (("tokens", "account_id"), ("chatgpt_account_id",), ("chatgptAccountId",), ("account_id",), ("accountId",), ("account", "id"), ("account", "account_id")),
        "user_id": (("chatgpt_user_id",), ("chatgptUserId",), ("user_id",), ("userId",), ("user", "id")),
        "plan_type": (("plan_type",), ("planType",), ("account", "plan_type"), ("account", "planType")),
        "organization": (("organization_id",), ("organizationId",), ("org_id",), ("orgId",)),
    }
    item = {key: first_string(raw, *choices) for key, choices in paths.items()}
    item["warnings"] = []
    if not item["access_token"]:
        raise ValueError("缺少 accessToken / access_token")
    if re.search(r"\s", item["access_token"]):
        raise ValueError("accessToken 不能包含空白字符，请一行粘贴一个 token")
    if item["access_token"].startswith("sk-"):
        raise ValueError("这是 API key，请使用 OpenAI 兼容渠道导入 key")
    item["auth_mode"] = "personal_access_token" if item["access_token"].startswith("at-") else "oauth"
    if item["auth_mode"] == "personal_access_token":
        for key in ("refresh_token", "id_token", "client_id"):
            item[key] = ""
    _enrich(item, item["id_token"])
    _enrich(item, item["access_token"])
    access = decode_jwt_claims(item["access_token"])
    if access is None:
        item["warnings"].append("accessToken 不是可解析的 JWT，身份与过期时间只能取导出文件字段")
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    explicit = tokens.get("expires_at") or tokens.get("expiresAt") or raw.get("expires_at") or raw.get("expiresAt") or raw.get("expired")
    item["expires_at"] = epoch((access or {}).get("exp")) or epoch(explicit)
    if (validate_expiry and not item["refresh_token"] and not item["expires_at"]
            and not item["access_token"].startswith("at-")):
        raise ValueError("未包含 refresh_token，且无法解析 accessToken 到期时间；请导入完整 Codex auth.json 或有效的 accessToken")
    if first_string(raw, ("session_token",), ("sessionToken",)):
        item["warnings"].append("sessionToken 已忽略，不会作为 refresh_token 存储")
    if not item["expires_at"]:
        item["warnings"].append("无法解析 accessToken 过期时间，请通过渠道测试确认令牌有效性")
    elif validate_expiry and item["expires_at"] <= time.time() - CLOCK_SKEW:
        if not item["refresh_token"]:
            raise ValueError("access_token 已过期且没有 refresh_token")
        item["access_token_expired"] = True
        item["warnings"].append("accessToken 已过期，将由 bit-api 使用 refresh_token 自动续期")
    if not item["refresh_token"]:
        item["warnings"].append("未包含 refresh_token，accessToken 过期后需重新导入")
    return item


def identity_keys(item):
    account, user = item.get("account_id"), item.get("user_id")
    email = (item.get("email") or "").lower()
    keys = []
    if item.get("agent_runtime_id"):
        keys.append(("agent_runtime", item["agent_runtime_id"]))
    if account and user:
        keys.append(("account_user", account, user))
    if account and email:
        keys.append(("account_email", account, email))
    if not account and user:
        keys.append(("user", user))
    if not account and not user and email:
        keys.append(("email", email))
    if item.get("access_token"):
        keys.append(("access", fingerprint(item["access_token"])))
    if account:
        keys.append(("account", account))
    return keys


def conflicts(left, right):
    if any(left.get(k) and right.get(k) and left[k] != right[k] for k in ("account_id", "user_id")):
        return True
    # 缺成员 ID 时，Team account_id 不能把两个不同邮箱误合并。
    return (not left.get("user_id") or not right.get("user_id")) and bool(
        left.get("email") and right.get("email") and left["email"].lower() != right["email"].lower())


def build_identity(item):
    seed = item["account_id"] + ":" + item["user_id"] if item["account_id"] or item["user_id"] else item.get("agent_runtime_id") or item["access_token"]
    return item["email"] or "gpt-" + fingerprint(seed)[:24]


def import_accounts(db, values, update_existing=False, dry_run=False):
    if not values:
        raise ValueError("请输入 accessToken / Codex auth.json / JSON 数组")
    if len(values) > MAX_IMPORT_ENTRIES:
        raise ValueError(f"每批最多导入 {MAX_IMPORT_ENTRIES} 个账号")
    result = {"total": len(values), "imported": 0, "updated": 0, "skipped": 0,
              "failed": 0, "items": [], "warnings": [], "errors": []}
    with IMPORT_LOCK:
        records, indexed, seen = {}, {}, {}

        def index_row(row):
            item = normalize_entry(row["secret"], validate_expiry=False)
            row["_item"] = item
            records[row["identity"]] = row
            for key in identity_keys(item):
                indexed.setdefault(key, {})[row["identity"]] = row

        for row in db.list_accounts(channel="gpt", limit=100000):
            try:
                index_row(row)
            except ValueError:
                records[row["identity"]] = row
        for number, value in enumerate(values, 1):
            try:
                item = normalize_entry(value)
                keys = identity_keys(item)
                duplicate = any(not conflicts(item, previous) for key in keys for previous in seen.get(key, []))
                old = None
                for key in keys:
                    matches = [r for r in indexed.get(key, {}).values() if not conflicts(item, r["_item"])]
                    if len(matches) == 1:
                        old = matches[0]
                        break
                identity = old["identity"] if old else build_identity(item)
                if not old and identity in records:
                    identity += "#" + fingerprint(json.dumps([item["account_id"], item["user_id"], item["access_token"]]))[:24]
                result["warnings"].extend({"index": number, "name": identity, "message": w} for w in item["warnings"])
                if duplicate or (old and not update_existing):
                    action, message = "skipped", "本批次内重复" if duplicate else "已存在，跳过"
                    aid = old["id"] if old else None
                else:
                    secret = dict((old or {}).get("secret") or {})
                    if item.get("auth_mode") in ("agentIdentity", "personal_access_token"):
                        for key in ("access_token", "refresh_token", "id_token", "client_id"):
                            secret.pop(key, None)
                    if item.get("auth_mode") != "agentIdentity":
                        for key in ("agent_runtime_id", "agent_private_key", "task_id"):
                            secret.pop(key, None)
                    for key in ("access_token", "refresh_token", "id_token", "email", "account_id", "user_id", "plan_type", "client_id", "auth_mode", "agent_runtime_id", "agent_private_key", "task_id"):
                        if item.get(key):
                            secret[key] = item[key]
                    if not item["id_token"]:
                        secret.pop("id_token", None)
                    if item.get("auth_mode") == "oauth":
                        secret["client_id"] = item["client_id"] or secret.get("client_id") or CLIENT_ID
                    if item["organization"]:
                        secret["organization_id"] = item["organization"]
                    meta = {**((old or {}).get("meta") or {}), "source": "gpt_import"}
                    meta.update({key: item[key] for key in ("email", "account_id", "user_id", "plan_type")})
                    if dry_run:
                        aid = old["id"] if old else -number
                        index_row({"id": aid, "identity": identity, "secret": secret, "meta": meta})
                    else:
                        aid = db.upsert_account("gpt", identity, secret=secret, meta=meta, status=dbmod.ST_ACTIVE)
                        db.update_account(aid, token=item["access_token"], token_exp=item["expires_at"], status=dbmod.ST_ACTIVE)
                        index_row(db.get_account(aid))
                    action, message = ("updated" if old else "imported"), item["plan_type"]
                result[action] += 1
                result["items"].append({"index": number, "name": identity, "action": action, "account_id": aid, "message": message})
                for key in keys:
                    seen.setdefault(key, []).append(item)
            except ValueError as e:
                result["failed"] += 1
                result["items"].append({"index": number, "action": "failed", "message": str(e)})
                result["errors"].append({"index": number, "message": str(e)})
    return result
