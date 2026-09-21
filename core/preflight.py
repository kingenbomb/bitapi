#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动前的安全检查 —— 默认密钥不能上公网。

config.py 里 JWT_SECRET / API_KEY / ADMIN_KEY 三个都有可用的默认值,本地起一下很方便,
但监听到 0.0.0.0 还用默认值,等于没设密码:任何人都能伪造管理员会话、用主密钥免费调用、
拿管理密钥导号删号。README 里提醒过一句,可提醒是靠人看的;这里改成拒绝启动。

只在「监听非回环地址」时拒绝:127.0.0.1 上跑开发与测试照旧不需要改任何东西。
BITAPI_ALLOW_DEFAULT_SECRETS=1 可以强行放过 —— 给「我就是在内网裸跑」的人一个出口,
但要他明确写下来。
"""
import ipaddress

import config

DEFAULTS = {
    "BITAPI_JWT_SECRET": "change-me-bitapi-jwt",
    "BITAPI_API_KEY": "sk-bitapi",
    "BITAPI_ADMIN_KEY": "adm-bitapi",
}


def _loopback(host):
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_secrets_in_use():
    """仍是默认值的那几项的环境变量名。"""
    current = {"BITAPI_JWT_SECRET": config.JWT_SECRET,
               "BITAPI_API_KEY": config.API_KEY,
               "BITAPI_ADMIN_KEY": config.ADMIN_KEY}
    return [k for k, v in current.items() if v == DEFAULTS[k]]


def check(host=None, allow_default=None):
    """返回 (ok, problems)。ok=False 时调用方应拒绝启动。"""
    host = config.HOST if host is None else host
    allow = (config.env_bool("ALLOW_DEFAULT_SECRETS", False)
             if allow_default is None else allow_default)
    bad = default_secrets_in_use()
    if not bad or _loopback(host) or allow:
        return True, []
    return False, [
        f"{k} 仍是默认值「{DEFAULTS[k]}」" for k in bad
    ] + [f"监听 {host} 不是回环地址 —— 默认密钥等于没有密码。",
         "改掉上面的环境变量;确实要在内网裸跑,设 BITAPI_ALLOW_DEFAULT_SECRETS=1。"]
