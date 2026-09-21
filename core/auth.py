#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户接入层鉴权工具 —— 密码哈希 / JWT 会话 / API key 与邀请码生成。

无重依赖:密码走标准库 pbkdf2_hmac,JWT 走 PyJWT(唯一新增依赖)。
"""
import hashlib
import hmac
import os
import secrets
import string
import time

import jwt

import config

_PBKDF2_ROUNDS = 200_000
_PBKDF2_ALGO = "sha256"


# ---- 密码 ----

def hash_password(password):
    """返回可存库的字符串: pbkdf2$algo$rounds$salt_hex$hash_hex。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"),
                             salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ALGO}${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    """常量时间校验。stored 为 hash_password 的输出。"""
    try:
        scheme, algo, rounds, salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if scheme != "pbkdf2":
        return False
    dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"),
                             bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---- JWT ----

def issue_token(user_id, role="user", ttl=None, scope=None):
    """会话 token。scope 非空时是一张受限票(如 TOTP 第二步的 5 分钟票),
    decode_session_token 会拒绝它 —— 票不是会话,不能拿去调 /api/me。"""
    ttl = ttl if ttl is not None else config.JWT_TTL
    now = int(time.time())
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": now + ttl}
    if scope:
        payload["scope"] = scope
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def decode_token(token):
    """成功返回 payload dict,失败(过期/篡改)返回 None。不看 scope。"""
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def decode_session_token(token):
    """只接受完整会话:带 scope 的受限票返回 None。鉴权路径一律用这个。"""
    payload = decode_token(token)
    if not payload or payload.get("scope"):
        return None
    return payload


TOTP_TICKET_TTL = 300


# ---- 生成 ----

def generate_api_key():
    """sk- 前缀 + 32 字节随机 hex。"""
    return "sk-" + secrets.token_hex(32)


_AFF_ALPHABET = string.ascii_uppercase + string.digits


def generate_aff_code(length=8):
    return "".join(secrets.choice(_AFF_ALPHABET) for _ in range(length))


def generate_email_code(length=6):
    return "".join(secrets.choice(string.digits) for _ in range(length))


def normalize_aff_code(code):
    """邀请码归一化:去空白 + 大写(与 sub2api 规则一致)。"""
    return (code or "").strip().upper()
