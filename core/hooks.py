#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件总线 —— core 发事实,plugins 定策略。

core 在关键节点 emit 事件,插件用 @on 挂载。插件异常被捕获并记日志,
绝不影响主流程(用量照记、订单照完成)。

事件清单(稳定 API):
  user.registered   注册成功(含邀请绑定后)      user, inviter
  order.paid        支付回调验签通过、订单转 PAID  order, user, inviter
  code.redeemed     兑换码成功占用并发放后        code, user, inviter
  usage.recorded    一次请求结算完成             log, user, group, cost
  balance.changed   任何 ledger 写入后           user_id, delta, reason, balance_after

运维事实(告警插件订阅;core 只发不判该不该通知):
  pool.empty        某渠道取不到号               channel, reason
  upstream.failed   换遍号后请求仍失败            channel, model, error, kind
  order.stuck       已付订单卡在 paid/recharging  order, age
  backup.failed     库备份失败                   error
  mail.failed       SMTP 发信失败                to, error
  auth.throttled    登录/注册/找回触发限速         scope, key, retry_after
"""
import traceback

_HANDLERS = {}


def on(event):
    """装饰器:把函数注册为某事件的处理器。同一事件可注册多个,按注册顺序执行。"""
    def deco(fn):
        _HANDLERS.setdefault(event, []).append(fn)
        return fn
    return deco


def emit(event, **payload):
    """触发事件。任一处理器抛异常都被吞掉并打印,不中断其余处理器与主流程。"""
    for fn in _HANDLERS.get(event, ()):
        try:
            fn(**payload)
        except Exception:
            print(f"[hooks] handler {getattr(fn, '__name__', fn)} "
                  f"failed on {event}:\n{traceback.format_exc()}", flush=True)


def handlers(event=None):
    """内省用:某事件(或全部)已注册的处理器。"""
    if event is None:
        return {k: list(v) for k, v in _HANDLERS.items()}
    return list(_HANDLERS.get(event, ()))


def clear(event=None):
    """测试用:清空注册表。"""
    if event is None:
        _HANDLERS.clear()
    else:
        _HANDLERS.pop(event, None)
