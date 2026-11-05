"""GPT 原生账号池：数据库 OAuth 凭据 → 直连 ChatGPT Codex Responses。

导入契约、OAuth client_id/scope、请求头与 Codex 强制 stream/store 语义参考 sub2api。
Chat/Anthropic/Responses 入站仍复用 bit-api 网关；本 adapter 完成原生协议转换和续期。
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config
from adapters.openai_compat import OpenAICompatAdapter, UpstreamError
from core import codex, gpt_accounts, pool_state, account_runtime
from core.adapter import BILL_UPSTREAM, CAP_CHAT, CAP_REFRESH, register_adapter

# 保留已有解析工具的公共入口，脚本与测试均使用同一实现。
normalize_entry = gpt_accounts.normalize_entry
decode_jwt_claims = gpt_accounts.decode_jwt_claims
parse_import_text = gpt_accounts.parse_import_text
parse_import_body = gpt_accounts.parse_import_body


class GPTAdapter(OpenAICompatAdapter):
    name = "gpt"
    proxy = False
    streaming = True
    stateless_keys = False
    key_identity = None           # OAuth 账号不参与用户 API key 共享入池
    capabilities = [CAP_CHAT, CAP_REFRESH]
    billing_mode = BILL_UPSTREAM
    columns = [
        {"key": "email", "label": "账号", "type": "text"},
        {"key": "plan_type", "label": "套餐", "type": "text"},
        {"key": "chatgpt_account_id", "label": "Account", "type": "text"},
        {"key": "token_exp", "label": "Token 到期", "type": "time"},
        {"key": "status", "label": "状态", "type": "text"},
    ]

    def __init__(self, db=None):
        super().__init__(base_url=config.GPT_BASE_URL, chat_path="/responses",
                         models=config.GPT_MODELS.split(",") if config.GPT_MODELS else [],
                         key_concurrency=0)
        self.models = [m.strip() for m in self.models if m.strip()]
        self.configured_models = list(self.models)
        self._db = db
        self._lock_guard = threading.Lock()
        self._refresh_locks = {}
        from core.gpt_models import rebuild
        rebuild(self)

    def _database(self):
        return self._db if self._db is not None else pool_state.DB

    def supports_chat(self, model):
        from core.gpt_web.runtime import MODEL
        return model != MODEL

    def billing_mode_for(self, model):
        from core.adapter import BILL_ESTIMATE
        from core.gpt_web.runtime import MODEL
        return BILL_ESTIMATE if model == MODEL else self.billing_mode

    def _acquire_slot(self, acct):
        # HTTP, WS, admin tests and images reserve the same account capacity in
        # account_runtime.dispatch. A second hidden semaphore would double queue.
        return None

    def _refresh_lock(self, acct):
        key = (self._database().path, acct.get("id") or acct["identity"])
        with self._lock_guard:
            return self._refresh_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _client_headers():
        version = config.GPT_CLIENT_VERSION
        return {"User-Agent": f"codex_cli_rs/{version} (Ubuntu 22.4.0; x86_64) xterm-256color",
                "originator": "codex_cli_rs"}

    def _codex_headers(self, acct, payload):
        from core import account_runtime, gpt_runtime as G
        credential = G.credential_account(self._database(), acct)
        secret = credential.get("secret") or {}
        policy = G.settings(acct)
        if secret.get("auth_mode") == "agentIdentity":
            secret = self._ensure_access(credential)["secret"]
            from core.gpt_auth import assertion
            authorization = assertion(secret)
        else:
            authorization = "Bearer " + (credential.get("token") or secret.get("access_token") or "")
        if authorization == "Bearer ":
            raise UpstreamError(401, "GPT account missing access_token")
        headers = {**self._client_headers(), "Content-Type": "application/json",
                   "Accept": "text/event-stream", "Authorization": authorization,
                   "version": config.GPT_CLIENT_VERSION}
        client = account_runtime.request_context().get("client") or {}
        if policy["fingerprint_mode"] == "passthrough":
            allowed = {"user-agent", "originator", "version", "x-codex-installation-id", "x-codex-window-id", "x-codex-turn-state", "x-codex-turn-metadata"}
            for key, value in (client.get("headers") or {}).items():
                if key in allowed:
                    for old in list(headers):
                        if old.lower() == key:
                            del headers[old]
                    headers[key] = value
        elif policy["fingerprint_mode"] == "session":
            headers["x-codex-installation-id"] = str(uuid.UUID(gpt_accounts.fingerprint(credential["identity"])[:32]))
        account_id = secret.get("account_id") or secret.get("chatgpt_account_id")
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if secret.get("user_id"):
            headers["ChatGPT-User-ID"] = secret["user_id"]
        if payload.get("prompt_cache_key") or client.get("session"):
            # 不同账号的 session ID 隔离；同账号同 cache key 稳定。
            headers["session_id"] = gpt_accounts.fingerprint(acct["identity"] + ":" + str(payload.get("prompt_cache_key") or client["session"]))
        for key, value in policy["header_overrides"].items():
            for old in list(headers):
                if old.lower() == key.lower():
                    del headers[old]
            headers[key] = value
        return headers

    def _request(self, url, data, headers):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            from core.account_runtime import urlopen
            timeout = (config.GPT_RESPONSE_HEADER_TIMEOUT or config.GPT_STREAM_IDLE_TIMEOUT) if "text/event-stream" in headers.get("Accept", "") else config.SEND_TIMEOUT
            return urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            error_headers = dict(e.headers or {})
            try:
                message = e.read(600).decode("utf-8", "replace")
            finally:
                e.close()
            error = UpstreamError(e.code, message or e.reason or "", url)
            error.headers = error_headers
            try:
                error.details = json.loads(message)
            except ValueError:
                error.details = {}
            raise error from e

    def refresh_token(self, acct):
        return self._ensure_access(acct)

    def account_usage(self, acct):
        """ChatGPT's own usage windows, matching sub2api's /wham/usage source."""
        from core.gpt_profile import quota_snapshot
        data = self._account_json(acct, "https://chatgpt.com/backend-api/wham/usage")
        from core.gpt_runtime import credential_account
        expected = (credential_account(self._database(), acct).get("secret") or {}).get("account_id")
        if expected and data.get("account_id") and data["account_id"] != expected:
            raise ValueError("上游用量返回的工作区与此账号不匹配")
        if (acct.get("meta") or {}).get("parent_account_id"):
            feature = next((item for item in data.get("additional_rate_limits") or [] if item.get("metered_feature") == "codex_bengalfox"), {})
            return quota_snapshot({"rate_limit": feature.get("rate_limit"), "plan_type": data.get("plan_type")})
        return quota_snapshot(data)

    def account_reset_credits(self, acct):
        from core.gpt_profile import reset_credit_snapshot
        return reset_credit_snapshot(self._account_json(acct, "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"))

    def account_models(self, acct):
        data = self._account_json(acct, self.api_base.rstrip("/") + "/models?" +
                                  urllib.parse.urlencode({"client_version": config.GPT_CLIENT_VERSION}))
        models, seen = [], set()
        for item in data.get("models") or []:
            slug = item.get("slug") if isinstance(item, dict) else None
            if not isinstance(slug, str) or not slug or slug in seen:
                continue
            if item.get("visibility") == "hide" or item.get("supported_in_api") is False:
                continue
            seen.add(slug)
            models.append({"id": slug, "name": item.get("display_name") or slug,
                           "description": item.get("description") or ""})
        if not models:
            raise ValueError("上游未返回可见的对话模型")
        from core.gpt_models import cache_manifest
        cache_manifest(self, acct, data, config.GPT_CLIENT_VERSION)
        return models

    def public_models(self, group_id=None):
        from core.gpt_models import public_models
        return public_models(self, group_id)

    def upstream_account_model(self, acct, model, compact=False):
        from core.gpt_runtime import settings
        policy = settings(acct)
        mapping = policy["compact_model_mapping"] if compact else policy["model_mapping"]
        return mapping.get(model, self.upstream_model(model))

    def validate_pat(self, token, proxy_id=None):
        if not token.startswith("at-") or any(c.isspace() for c in token):
            raise ValueError("Codex PAT 必须是 at- 开头的完整令牌")
        row = {"identity": "pat-validation", "channel": "gpt", "token": token, "secret": {}, "meta": {}, "proxy_id": proxy_id}
        with account_runtime.network(self._database(), row):
            data = self._account_json(row, "https://auth.openai.com/api/accounts/v1/user-auth-credential/whoami")
        fields = {"email": data.get("email"), "account_id": data.get("chatgpt_account_id"),
                  "user_id": data.get("chatgpt_user_id"), "plan_type": data.get("chatgpt_plan_type")}
        if not all(isinstance(value, str) and value.strip() for value in fields.values()):
            raise ValueError("PAT 校验响应缺少账号身份字段")
        return {**fields, "auth_mode": "personal_access_token", "access_token": token}

    def delete_account(self, db, identity):
        row = db.get_by_identity("gpt", identity)
        if not row:
            return
        with db._conn() as conn:
            if conn.execute("SELECT 1 FROM accounts WHERE channel='gpt' AND json_extract(meta,'$.parent_account_id')=?", (row["id"],)).fetchone():
                raise ValueError("请先明确选择并删除关联的 Spark 影子账号，再删除主账号")
            conn.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
        from core.gpt_models import rebuild
        rebuild(self)

    def _account_post(self, acct, url, body):
        headers = self._codex_headers(acct, {})
        headers.update(Accept="application/json", Origin="https://chatgpt.com", Referer="https://chatgpt.com/")
        response = self._request(url, json.dumps(body).encode(), headers)
        try:
            return json.loads(response.read())
        finally:
            response.close()

    def reset_candidates(self, acct):
        data = self._account_json(acct, "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
        candidates = []
        from core.gpt_accounts import epoch
        while isinstance(data, dict):
            data = next((data[key] for key in ("credits", "rate_limit_reset_credits", "items", "data") if isinstance(data.get(key), (dict, list))), [])
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or str(item.get("status") or "").lower() not in ("", "available") or (item.get("reset_type") or item.get("resetType")) not in (None, "", "codex_rate_limits") or item.get("is_supported_by_plan") is False:
                continue
            try:
                expiry = epoch(item.get("expires_at") or item.get("expiresAt"))
            except ValueError:
                continue
            credit_id = item.get("id") or item.get("credit_id") or item.get("creditId")
            if credit_id and expiry > time.time():
                candidates.append({"id": credit_id, "expires_at": expiry})
        return sorted(candidates, key=lambda item: item["expires_at"])

    def reset_quota(self, acct, request_id, credit_id=None):
        body = {"redeem_request_id": request_id}
        if credit_id:
            body["credit_id"] = credit_id
        data = self._account_post(acct, "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume", body)
        return {"code": data.get("code"), "windows_reset": data.get("windows_reset"),
                "expires_at": (data.get("credit") or {}).get("expires_at")}

    def referral_program(self, acct):
        profile = (acct.get("meta") or {}).get("pool_gpt_profile") or {}
        plan = profile.get("plan_type") or (acct.get("secret") or {}).get("plan_type")
        return "codex_referral_workspace" if plan in ("team", "business", "self_serve_business_prolite", "self_serve_business_usage_based", "free_workspace", "enterprise", "enterprise_cbp_usage_based", "enterprise_cbp_automation", "edu", "education") else "codex_referral_consumer"

    def referral_eligibility(self, acct):
        program = self.referral_program(acct)
        data = self._account_json(acct, "https://chatgpt.com/backend-api/referrals/invite/eligibility?" +
                                  urllib.parse.urlencode({"program_id": program, "entrypoint": "persistent"}))
        send, reward = data.get("remaining_send_capacity"), data.get("remaining_reward_capacity")
        count = max(0, send) if isinstance(send, int) and not isinstance(send, bool) else None
        grants = [{key: grant.get(key) for key in ("grant_type", "recipient", "amount")}
                  for grant in data.get("grants") or [] if isinstance(grant, dict)]
        if grants or data.get("offer_id") not in (None, "", "none"):
            count = min(count, max(0, reward)) if count is not None and isinstance(reward, int) else None
        if not data.get("should_show"):
            count = 0
        return {"program_id": program, "available_invites": count, "should_show": bool(data.get("should_show")),
            "requires_confirmation": data.get("requires_explicit_confirmation") is not False,
            "title": data.get("title"), "description": data.get("description"), "rules": data.get("rules") or [],
            "grants": grants, "fetched_at": int(time.time())}

    def send_referral(self, acct, email, program):
        data = self._account_post(acct, "https://chatgpt.com/backend-api/referrals/invite",
            {"program_id": program, "entrypoint": "persistent", "emails": [email]})
        invites = data.get("invites")
        if not isinstance(invites, list) or len(invites) != 1 or invites[0] is None:
            raise RuntimeError("邀请结果未知，请先到 Codex 核对，避免重复发送")
        return {"sent": True, "email": email}

    def _account_json(self, acct, url):
        from core.account_runtime import urlopen
        headers = self._codex_headers(acct, {})
        headers.update({"Accept": "application/json", "Origin": "https://chatgpt.com", "Referer": "https://chatgpt.com/"})
        request = urllib.request.Request(url, headers=headers)
        try:
            with urlopen(request, timeout=12) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(600).decode("utf-8", "replace")
            finally:
                exc.close()
            raise UpstreamError(exc.code, body or exc.reason, url) from exc

    def account_profile(self, acct):
        from core.gpt_profile import select_profile
        from core.gpt_runtime import credential_account
        identity = normalize_entry(credential_account(self._database(), acct).get("secret") or {}, validate_expiry=False)
        expected = identity.get("account_id")
        data = self._account_json(acct, "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27")
        profile = select_profile(data, expected, identity)
        if not profile.get("subscription_expires_at") and profile.get("plan_type") not in (None, "free"):
            try:
                subscription = self._account_json(acct, "https://chatgpt.com/backend-api/subscriptions?" +
                    urllib.parse.urlencode({"account_id": profile["account_id"]}))
                from core.gpt_accounts import epoch
                profile["subscription_expires_at"] = epoch(subscription.get("active_until")) or None
            except Exception as exc:
                # Keep usable profile data but expose the failed expiration read.
                profile["subscription_error"] = "订阅到期时间读取失败（" + type(exc).__name__ + "）"
        return profile

    def disable_training(self, acct):
        from core.account_runtime import urlopen
        headers = self._codex_headers(acct, {})
        headers.update({"Accept": "application/json", "Origin": "https://chatgpt.com", "Referer": "https://chatgpt.com/"})
        request = urllib.request.Request(
            "https://chatgpt.com/backend-api/settings/account_user_setting?feature=training_allowed&value=false",
            headers=headers, method="PATCH")
        with urlopen(request, timeout=20) as response:
            response.read(1024)
        return "training_off"

    def _ensure_access(self, acct, force=False, expected_task=None):
        try:
            with account_runtime.credential_budget():
                return self._refresh_access(acct, force, expected_task)
        except Exception as error:
            error._gpt_refresh_error = True
            raise

    def _refresh_access(self, acct, force=False, expected_task=None):
        from core import gpt_auth, gpt_runtime as G
        if (acct.get("meta") or {}).get("parent_account_id"):
            parent = G.credential_account(self._database(), acct)
            with account_runtime.network(self._database(), parent):
                updated = self._ensure_access(parent, force)
            return {"token": "", "token_exp": updated.get("token_exp", 0)}
        # 同号并发刷新读最新库值，避免轮换 RT 被多个请求重复消费。
        with account_runtime.refresh_lock(self._refresh_lock(acct)):
            db = self._database()
            current = db.get_account(acct["id"]) if acct.get("id") else None
            current = current or acct
            secret = dict(current.get("secret") or {})
            if secret.get("auth_mode") == "agentIdentity":
                if (force and (expected_task is None or secret.get("task_id") == expected_task)) or not secret.get("task_id"):
                    secret["task_id"] = gpt_auth.register_task(secret)
                    if acct.get("id"):
                        db.update_account(acct["id"], secret=secret, token="", token_exp=0)
                acct.update(secret=secret, token="", token_exp=0)
                return {"token": "", "token_exp": 0, "secret": secret, "_persisted": bool(acct.get("id"))}
            token = current.get("token") or secret.get("access_token")
            expiry = int(current.get("token_exp") or 0)
            refresh = secret.get("refresh_token")
            if force and token != (acct.get("token") or (acct.get("secret") or {}).get("access_token")):
                force = False
            if token and not force and (expiry > time.time() + config.REFRESH_MARGIN or
                                        not refresh and (not expiry or expiry > time.time())):
                return {"token": token, "token_exp": expiry, "secret": secret,
                        "meta": current.get("meta") or {}, "_persisted": bool(acct.get("id"))}
            if not refresh:
                raise UpstreamError(401, "access_token_expired: no refresh_token; re-import this account")
            form = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh,
                                           "client_id": secret.get("client_id") or gpt_accounts.CLIENT_ID,
                                           "scope": "openid profile email"}).encode()
            response = self._request(config.GPT_TOKEN_URL, form,
                                     {**self._client_headers(), "Content-Type": "application/x-www-form-urlencoded"})
            try:
                result = json.loads(response.read())
            finally:
                response.close()
            if not isinstance(result, dict) or not result.get("access_token"):
                raise UpstreamError(502, "OAuth refresh response missing access_token")
            new_secret = {**secret, "access_token": result["access_token"]}
            for key in ("refresh_token", "id_token"):
                if result.get(key):
                    new_secret[key] = result[key]
            info = normalize_entry(new_secret, validate_expiry=False)
            lifetime = result.get("expires_in")
            expiry = int(time.time()) + int(lifetime) if lifetime is not None else info["expires_at"]
            if info["expires_at"]:
                expiry = min(expiry, info["expires_at"]) if expiry else info["expires_at"]
            if expiry <= time.time():
                raise UpstreamError(502, "OAuth refresh returned an expired token")
            meta = dict(current.get("meta") or {})
            for key in ("email", "account_id", "user_id", "plan_type"):
                if info[key]:
                    new_secret[key] = info[key]
                    meta[key] = info[key]
            upd = {"token": result["access_token"], "token_exp": expiry,
                   "secret": new_secret, "meta": meta}
            # 在解锁前持久化轮换后的 RT；Pool/巡检仍经 apply_refresh 写回同一库。
            if acct.get("id"):
                with gpt_accounts.IMPORT_LOCK:
                    G.edit_meta(db, acct["id"], lambda latest: latest.update({k: info[k] for k in ("email", "account_id", "user_id", "plan_type") if info[k]}),
                                token=upd["token"], token_exp=expiry, secret=json.dumps(new_secret, ensure_ascii=False))
                upd["_persisted"] = True
            acct.update(upd)
            return upd

    def _open_codex(self, acct, payload):
        from core import account_runtime, gpt_runtime
        data = json.dumps(payload, ensure_ascii=False).encode()
        credential_before = gpt_runtime.credential_account(self._database(), acct)
        task_before = (credential_before.get("secret") or {}).get("task_id")
        try:
            response = self._request(self._url(), data, self._codex_headers(acct, payload))
        except UpstreamError as e:
            gpt_runtime.record_limit(self._database(), acct, e, account_runtime.request_context().get("model") or payload.get("model"))
            from core import gpt_auth
            credential = gpt_runtime.credential_account(self._database(), acct)
            if (credential.get("secret") or {}).get("auth_mode") == "agentIdentity" and gpt_auth.invalid_task(e):
                self._ensure_access(credential, force=True, expected_task=task_before)
            elif e.code == 401 and (credential.get("secret") or {}).get("refresh_token"):
                acct.update(self._ensure_access(acct, force=True))
            else:
                raise
            # AT 被提前撤销时用 RT 续期后重试一次，不把可续期账号直接踢出池。
            response = self._request(self._url(), data, self._codex_headers(acct, payload))
        gpt_runtime.capture_headers(self._database(), acct, getattr(response, "headers", {}))
        return response

    def response_events(self, acct, payload, line_limit=8 * 1024 * 1024):
        from core.gpt_request_policy import iter_events
        return iter_events(self, acct, payload, line_limit)

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        model = model or self.models[0]
        if stream:
            def on_sse(line):
                data = line[5:].strip() if line.startswith("data:") else ""
                if not data or data == "[DONE]":
                    return
                event = json.loads(data)
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for key, kind in (("content", "content"), ("reasoning_content", "reasoning")):
                        if delta.get(key) and on_token:
                            on_token(delta[key], kind)
            return self.stream_chat(acct, model, {**(body or {}), "messages": messages}, on_sse)
        slot = self._acquire_slot(acct)
        try:
            payload = codex.chat_to_responses({**(body or {}), "messages": messages}, self.upstream_account_model(acct, model))
            response = self.response_events(acct, payload)
            try:
                result = codex.collect_response(response)
            finally:
                response.close()
            return {"raw": codex.response_to_chat(result, model)}
        except Exception as exc:
            from core.gpt_runtime import record_limit
            record_limit(self._database(), acct, exc, model)
            raise
        finally:
            self._release_slot(slot)

    def stream_chat(self, acct, model, body, on_sse):
        slot = self._acquire_slot(acct)
        try:
            payload = codex.chat_to_responses(body, self.upstream_account_model(acct, model))
            response = self.response_events(acct, payload)
            try:
                for line in codex.stream_to_chat(response, model):
                    on_sse(line)
            finally:
                response.close()
        except Exception as exc:
            from core.gpt_runtime import record_limit
            record_limit(self._database(), acct, exc, model)
            raise
        finally:
            self._release_slot(slot)

    def import_accounts(self, db, values, force=False):
        result = gpt_accounts.import_accounts(db, values, update_existing=force)
        from core.gpt_models import rebuild
        rebuild(self)
        return result

    def import_body(self, db, body, force=None):
        if len(json.dumps(body).encode()) > gpt_accounts.MAX_IMPORT_BYTES:
            raise ValueError("导入内容不能超过 10 MB")
        if force is None:
            force = body.get("force", body.get("update_existing", False)) if isinstance(body, dict) else False
        if not isinstance(force, bool):
            raise ValueError("force / update_existing 必须是布尔值")
        return self.import_accounts(db, parse_import_body(body), force)

    def decorate_accounts(self, accounts):
        for account in accounts:
            meta = account.get("meta") or {}
            for key in ("email", "plan_type"):
                account[key] = meta.get(key) or (account.get("secret") or {}).get(key)
            account["chatgpt_account_id"] = meta.get("account_id") or (account.get("secret") or {}).get("account_id")
            account.pop("token", None)
        return accounts

    def describe(self):
        return {"base_url": self.api_base, "chat_path": "/responses", "source": "code",
                "key_concurrency": self.key_concurrency,
                "notes": "原生 GPT OAuth：数据库号池、bit-api 自动续期、直连 Codex"}


register_adapter(GPTAdapter())
