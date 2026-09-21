#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOTP(RFC 6238)—— 标准库实现,给管理员会话加第二道锁。

管理台管钱:调额、发码、改价、删渠道。一个被钓走的密码不该直接换来这些权限。
30 秒一步、6 位、SHA-1,与 Google Authenticator / 1Password / Aegis 等全部兼容;
校验允许前后各一步的时钟漂移。

防重放:同一个时间步的码只能成功一次(users.totp_last_step)。6 位码本身只有
一百万种,再叠登录限速(IP 与邮箱两道)才够 —— 这两条缺一不可。
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6
WINDOW = 1          # 允许的时钟漂移:前后各一步


def generate_secret():
    """20 字节随机 → base32(不带 =),这是所有 authenticator 认的手输格式。"""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    s = secret.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s, casefold=True)


def step_of(now=None):
    return int((time.time() if now is None else now) // STEP)


def code_at(secret, step):
    mac = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(n % (10 ** DIGITS)).zfill(DIGITS)


def code(secret, now=None):
    return code_at(secret, step_of(now))


def verify(secret, given, now=None, last_step=0):
    """返回命中的时间步,没命中返回 None。

    last_step 是该用户上一次成功用过的步:命中的步必须比它新,否则同一个码在 30 秒
    内可以被抓包重放一次。调用方拿返回值写回 users.totp_last_step。"""
    given = (given or "").strip().replace(" ", "")
    if not given.isdigit() or len(given) != DIGITS:
        return None
    center = step_of(now)
    for delta in range(-WINDOW, WINDOW + 1):
        step = center + delta
        if step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), given):
            return step
    return None


def otpauth_uri(secret, account, issuer):
    """authenticator 扫码 / 导入用的 otpauth://。"""
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret, "issuer": issuer,
                                "algorithm": "SHA1", "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"
