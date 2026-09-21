#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
插件包 —— 策略层。core 发事实(事件),插件定策略。

启用方式:环境变量 BITAPI_PLUGINS=affiliate_percent,my_plugin
不想要某个内置插件,从该列表移除即可(或直接删文件)。

写一个自己的返佣插件:

    from core.hooks import on
    from core.credit import credit

    @on("order.paid")
    def rebate(order, user, inviter, **_):
        if inviter:
            credit(inviter["id"], order["amount"] * 0.2,
                   reason="affiliate", idem_key=f"aff:{order['id']}")

credit() 的 idem_key 保证幂等 —— 重复回调、并发都不会重复发放,插件作者不必操心。
所有事件处理器的异常都被 core 捕获,不会影响主流程。
"""
