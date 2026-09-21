#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内滑动窗口限速 + 客户端 IP 取值。

给登录、注册、找回密码这几个「不需要密钥就能打」的入口用。网关那条路的 RPM
限流在 core/billing.py 里有自己的一份,按用户计;这里按 IP 与邮箱计,拦的是
撞库、批量注册刷赠额、用别人邮箱刷验证码。

进程内状态,依赖单 worker(与 RPM 限流、巡检同一前提)。不落库:这是「此刻在被
打」的瞬时判断,重启清零是可接受的 —— 攻击者要靠重启绕过得先能让服务重启。

只记失败不记成功:正常用户输对密码不该消耗额度,而攻击者每次都是失败。
"""
import threading
import time

import config


class SlidingWindow:
    """key 在 window 秒内最多 limit 次。hit() 记一次,blocked() 只看不记。"""

    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self._events = {}
        self._lock = threading.Lock()
        self._sweep_at = 0.0

    def _trim(self, key, now):
        events = [t for t in self._events.get(key, ()) if now - t < self.window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def _sweep(self, now):
        # 每个窗口长度扫一次全表,把静默的 key 清掉 —— 否则被扫过一遍的 IP 段
        # 每个都留一个空列表在字典里,长跑几个月内存只增不减
        if now - self._sweep_at < self.window:
            return
        self._sweep_at = now
        for key in list(self._events):
            self._trim(key, now)

    def retry_after(self, key, now=None):
        """还要等几秒才解封;0 = 现在就能过。"""
        if self.limit <= 0:
            return 0
        now = time.time() if now is None else now
        with self._lock:
            events = self._trim(key, now)
            if len(events) < self.limit:
                return 0
            return max(1, int(events[0] + self.window - now) + 1)

    def hit(self, key, now=None):
        """记一次。返回记完之后是否已经超限(True = 该封了)。"""
        if self.limit <= 0:
            return False
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            events = self._trim(key, now)
            events.append(now)
            self._events[key] = events
            return len(events) > self.limit

    def reset(self, key=None):
        """成功登录后清掉该邮箱的失败计数;不带 key 清空(测试用)。"""
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


class AuthThrottled(Exception):
    def __init__(self, scope, retry_after, key=None):
        super().__init__(f"{scope} rate limited, retry after {retry_after}s")
        self.scope = scope
        self.retry_after = retry_after
        self.key = key


def client_ip(request):
    """密钥 IP 白名单与登录限速共用的取值。反代部署必须读 X-Forwarded-For 的
    第一跳,否则永远拿到 nginx 的 127.0.0.1 —— 那样全站用户共享一个限速桶,
    一个人撞库全站登不上。直接暴露公网时关掉开关,因为客户端可以自己伪造这个头。"""
    if request is None:
        return None
    if config.TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return getattr(getattr(request, "client", None), "host", None)


# ---- 具体入口的限速器(阈值见 config.AUTH_*) ----

LOGIN_IP = SlidingWindow(config.AUTH_LOGIN_FAILS_PER_IP, config.AUTH_LOGIN_WINDOW)
LOGIN_EMAIL = SlidingWindow(config.AUTH_LOGIN_FAILS_PER_EMAIL, config.AUTH_LOGIN_WINDOW)
REGISTER_IP = SlidingWindow(config.AUTH_REGISTER_PER_IP, config.AUTH_REGISTER_WINDOW)
RESET_EMAIL = SlidingWindow(config.AUTH_RESET_PER_EMAIL, config.AUTH_RESET_WINDOW)
RESET_IP = SlidingWindow(config.AUTH_RESET_PER_IP, config.AUTH_RESET_WINDOW)


def check(*pairs):
    """pairs = ((limiter, key, scope), ...)。任一超限抛 AuthThrottled(取最长等待)。
    只看不记 —— 记账在结果出来之后(登录失败才 hit)。"""
    worst = None
    for limiter, key, scope in pairs:
        if not key:
            continue
        wait = limiter.retry_after(key)
        if wait and (worst is None or wait > worst.retry_after):
            worst = AuthThrottled(scope, wait, key=key)
    if worst:
        raise worst


def reset_all():
    """测试用:清空全部限速器。"""
    for lim in (LOGIN_IP, LOGIN_EMAIL, REGISTER_IP, RESET_EMAIL, RESET_IP):
        lim.reset()
