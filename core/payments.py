#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支付渠道抽象 + 注册表 —— 与 core/adapter.py 同一个路子。

接一个新渠道 = 在 payments/ 下加一个文件实现三个方法并 register_provider(),
不改 core。

  create_payment(order)              → {"pay_url": ...},可带 amount_cny
  verify_notify(headers, raw_body)   → dict | NOT_PAID | None(见下)
  query_order(out_trade_no)          → {"status": "paid"|"pending"} 或 None(轮询兜底)

verify_notify 的三态很重要,别退化成两态:

  None      验签失败/缺凭据 —— 端点回失败文本
  NOT_PAID  验签通过但这笔没成功(用户取消、超时关闭)—— 端点必须回 "success",
            否则渠道会把一笔**正常失败**的通知无限重试
  dict      验签通过且已支付。至少含 out_trade_no;有金额校验能力的渠道
            再带 amount_cny(应收人民币,与订单冻结值比对)
"""
from abc import ABC


class _NotPaid:
    """哨兵:验签通过但不是已支付事件。刻意不用 False —— 它和 None 太容易混。"""

    def __bool__(self):
        return False

    def __repr__(self):
        return "NOT_PAID"


NOT_PAID = _NotPaid()


class PaymentProvider(ABC):
    name = "base"
    display_name = "Base"

    def create_payment(self, order):
        """发起支付。order 为 orders 表的一行 dict。
        返回 {"pay_url": "https://..."};如果渠道以别的币种收款,
        再带 "amount_cny"(应收人民币),core 会把它冻进订单行做回调比对基准。"""
        raise NotImplementedError

    def verify_notify(self, headers, raw_body):
        """校验回调签名并解析。返回 dict / NOT_PAID / None,语义见模块头。
        **验签必须在此方法内完成** —— core 不知道各渠道的签名规则。"""
        raise NotImplementedError

    def query_order(self, out_trade_no):
        """主动查单(回调丢失时的兜底)。返回 {"status": ...} 或 None。"""
        return None


_REGISTRY = {}


def register_provider(provider):
    _REGISTRY[provider.name] = provider


def get_provider(name):
    return _REGISTRY.get(name)


def all_providers():
    return dict(_REGISTRY)


def enabled_providers(names):
    """按配置过滤出启用且已注册的渠道。"""
    return [_REGISTRY[n] for n in names if n in _REGISTRY]
