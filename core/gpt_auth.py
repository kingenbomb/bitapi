"""Codex PAT validation and the Agent Identity assertion/task protocol.

Wire contract follows sub2api openai_codex_pat_service/openai_agent_identity.
Private keys and generated assertions never enter an account's public DTO.
"""
import base64
import datetime
import json
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nacl.public import SealedBox
from nacl.signing import SigningKey

from core import account_runtime


def private_key(secret):
    try:
        key = serialization.load_der_private_key(base64.b64decode(secret.get("agent_private_key") or "", validate=True), password=None)
    except (TypeError, ValueError):
        raise ValueError("Agent Identity 私钥必须是 base64 编码的 PKCS#8 Ed25519 私钥") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Agent Identity 需要 Ed25519 私钥")
    return key


def agent_credentials(raw):
    nested = raw.get("agent_identity")
    raw = {**raw, **nested} if isinstance(nested, dict) else raw
    secret = {key: raw.get(key) for key in ("agent_private_key", "agent_runtime_id", "task_id", "email", "account_id", "user_id", "plan_type")}
    secret["account_id"] = secret["account_id"] or raw.get("chatgpt_account_id") or ""
    secret["user_id"] = secret["user_id"] or raw.get("chatgpt_user_id") or ""
    if not isinstance(secret["agent_runtime_id"], str) or not secret["agent_runtime_id"].strip():
        raise ValueError("Agent Identity 缺少 agent_runtime_id")
    private_key(secret)
    return {**{key: value or "" for key, value in secret.items()}, "auth_mode": "agentIdentity"}


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assertion(secret):
    runtime, task, stamp = secret["agent_runtime_id"], secret.get("task_id"), timestamp()
    if not task:
        raise ValueError("Agent Identity 尚未注册 task")
    signature = private_key(secret).sign((runtime + ":" + task + ":" + stamp).encode())
    payload = {"agent_runtime_id": runtime, "task_id": task, "timestamp": stamp,
               "signature": base64.b64encode(signature).decode()}
    return "AgentAssertion " + base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()


def register_task(secret):
    stamp = timestamp()
    runtime = secret["agent_runtime_id"]
    signature = base64.b64encode(private_key(secret).sign((runtime + ":" + stamp).encode())).decode()
    request = urllib.request.Request("https://auth.openai.com/api/accounts/v1/agent/" + urllib.parse.quote(runtime, safe="") + "/task/register",
        data=json.dumps({"timestamp": stamp, "signature": signature}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with account_runtime.urlopen(request, timeout=30) as response:
        data = json.loads(response.read(65536))
    task = data.get("task_id") or data.get("taskId")
    if task:
        return task
    encoded = data.get("encrypted_task_id") or data.get("encryptedTaskId")
    if not encoded:
        raise ValueError("Agent Identity 上游未返回 task_id")
    seed = private_key(secret).private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    try:
        task = SealedBox(SigningKey(seed).to_curve25519_private_key()).decrypt(base64.b64decode(encoded)).decode().strip()
    except Exception:
        raise ValueError("Agent Identity task_id 解密失败") from None
    if not task:
        raise ValueError("Agent Identity task_id 为空")
    return task


def invalid_task(error):
    if getattr(error, "code", None) not in (400, 401, 403):
        return False
    text = str(error).lower()
    return any(marker in text for marker in ("invalid task_id", "invalid task id", "task not found", "task expired", "unknown task_id", "task_id is invalid"))
