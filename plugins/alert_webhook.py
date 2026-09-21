#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警插件 · Webhook —— 把运维事实推到 Telegram / Bark / 任意 webhook。

core 只发事实(pool.empty / upstream.failed / order.stuck / backup.failed /
mail.failed / auth.throttled / 大额 usage.recorded),这里决定「哪些要通知、多久
通知一次、发到哪」。改 CONFIG 即可;不想要就从 BITAPI_PLUGINS 里移除。

去重:同一 (事件, 键) 在 cooldown 秒内只发一条。号池打空的那一刻每个请求都会
emit 一次 pool.empty,不去重等于每秒一条消息把通知渠道刷爆,真正的第二个告警
反而被淹掉。

投递在后台线程里跑,5 秒超时:hooks 是同步的、跑在请求路径上,通知渠道慢一秒
用户就多等一秒 —— 告警不能成为它要报告的那类故障。

地址来源:BITAPI_ALERT_WEBHOOK_URL。按域名识别形状:
  api.telegram.org/bot<token>/sendMessage?chat_id=<id>  → Telegram(JSON: chat_id, text)
  api.day.app/<key>                                     → Bark(JSON: title, body)
  其它                                                  → 通用 JSON(title, text, content, event, payload)
                                                          text/content 两个键同时给,Slack 与 Discord 各认一个
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from core.hooks import on

CONFIG = {
    "url": os.environ.get("BITAPI_ALERT_WEBHOOK_URL", "").strip(),
    "cooldown": 600,              # 同一 (事件,键) 的最短间隔(秒)
    "timeout": 5,                 # 投递超时(秒)
    "expensive_request": 0.0,     # 单次请求实扣超过此美元数就报;0 = 不报
    "site": os.environ.get("BITAPI_SITE_NAME", "bit-api"),
    "auth_throttle": True,        # 是否报登录限速(撞库信号)
}

_last = {}
_lock = threading.Lock()


def _due(event, key, now=None):
    """(event, key) 是否过了冷却。过了就记下这次,返回 True。"""
    now = time.time() if now is None else now
    k = (event, str(key))
    with _lock:
        prev = _last.get(k, 0)
        if now - prev < CONFIG["cooldown"]:
            return False
        _last[k] = now
        return True


def _payload(url, title, text, event, payload):
    host = urllib.parse.urlsplit(url).netloc.lower()
    if host == "api.telegram.org":
        chat_id = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("chat_id", [""])[0]
        return {"chat_id": chat_id, "text": f"{title}\n{text}",
                "disable_web_page_preview": True}
    if host.endswith("day.app"):
        return {"title": title, "body": text, "group": CONFIG["site"]}
    return {"title": title, "text": f"{title}\n{text}", "content": f"{title}\n{text}",
            "event": event, "payload": payload, "site": CONFIG["site"]}


def _post(url, body):
    data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=CONFIG["timeout"]) as r:
        r.read()


def notify(event, key, title, text, payload=None):
    """去重 + 后台投递。返回是否真的发了(测试与调试用)。"""
    url = CONFIG["url"]
    if not url or not _due(event, key):
        return False
    body = _payload(url, f"[{CONFIG['site']}] {title}", text, event, payload or {})

    def run():
        try:
            _post(url, body)
        except Exception as e:
            print(f"[alert_webhook] 投递失败 {event}/{key}: {e}", flush=True)

    threading.Thread(target=run, daemon=True, name="alert-webhook").start()
    return True


# ---- 订阅 ----

@on("pool.empty")
def on_pool_empty(channel, reason, **_):
    notify("pool.empty", channel, f"渠道 {channel} 号池打空",
           f"取不到可用账号({reason}),该渠道的请求正在返回 503。补号或下线渠道。",
           {"channel": channel, "reason": reason})


@on("upstream.failed")
def on_upstream_failed(channel, model, error, kind, **_):
    label = "上游限流" if kind == "ratelimit" else "上游失败"
    notify("upstream.failed", channel, f"渠道 {channel} {label}",
           f"模型 {model} 换遍账号仍失败:{str(error)[:200]}",
           {"channel": channel, "model": model, "kind": kind, "error": str(error)[:500]})


@on("order.stuck")
def on_order_stuck(order, age, **_):
    otn = order.get("out_trade_no")
    notify("order.stuck", otn, f"订单 {otn} 到账卡住",
           f"用户 #{order.get('user_id')} 已付 ${order.get('amount')},{int(age // 60)} 分钟"
           f"仍停在 {order.get('status')}。去管理台「订单」点查单,或查看服务端日志。",
           {"out_trade_no": otn, "user_id": order.get("user_id"),
            "amount": order.get("amount"), "status": order.get("status"), "age": age})


@on("backup.failed")
def on_backup_failed(error, **_):
    notify("backup.failed", "db", "库备份失败", str(error)[:300], {"error": str(error)[:500]})


@on("mail.failed")
def on_mail_failed(to, error, **_):
    notify("mail.failed", "smtp", "邮件发送失败",
           f"发往 {to} 失败:{str(error)[:200]}。找回密码与邮箱验证此刻都不可用。",
           {"to": to, "error": str(error)[:500]})


@on("auth.throttled")
def on_auth_throttled(scope, key, retry_after, **_):
    if not CONFIG["auth_throttle"]:
        return
    notify("auth.throttled", f"{scope}:{key}", f"{scope} 触发限速",
           f"{key} 在窗口内失败次数过多,已封 {retry_after} 秒。持续出现即有人在撞库。",
           {"scope": scope, "key": key, "retry_after": retry_after})


@on("usage.recorded")
def on_usage_recorded(user, model, channel, actual_cost, request_id, **_):
    threshold = CONFIG["expensive_request"]
    if not threshold or (actual_cost or 0) < threshold:
        return
    notify("usage.expensive", (user or {}).get("id"), "单次大额消费",
           f"{(user or {}).get('email')} 一次请求实扣 ${actual_cost:.4f}"
           f"(模型 {model} / 渠道 {channel},请求 {request_id})",
           {"user_id": (user or {}).get("id"), "model": model, "channel": channel,
            "actual_cost": actual_cost, "request_id": request_id})
