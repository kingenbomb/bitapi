#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户接入层单例 —— UserDB / Pricing / Billing 全局实例,供 portal 路由与网关热路径共用。

分离到独立模块避免 server.py ↔ routers.portal 循环 import。
"""
import config
from core import credit as credit_mod
from core import pricing_catalog
from core import site_settings
from core.billing import Billing
from core.pricing import Pricing
from core.pricing_catalog import Catalog
from core.user_db import UserDB

USER_DB = UserDB(config.DB_PATH)
PRICING = Pricing(USER_DB, catalog=Catalog(), cache_ttl=config.PRICING_CACHE_TTL)
BILLING = Billing(USER_DB, pricing=PRICING)


def rebind(db):
    """把所有自己存了一份 db 引用的东西指向同一个实例。

    core/credit.py 与 core/site_settings.py 都存了自己的 _DB 引用,漏掉一个的表现是
    「写进去了但读不到」—— 测试里换隔离库时最容易踩,而且不报错、只是行为不对。
    PRICING 是第三处:它在构造时收下 USER_DB,重指模块级 USER_DB 不会带上它,
    结果定价读的还是上一个库(测试里就是开发机的真库),写进去的价读不到。
    再加这类模块时改这里一处。
    """
    credit_mod.bind(db)
    site_settings.bind(db)
    PRICING.db = db
    PRICING.invalidate()


rebind(USER_DB)


def ensure_default_group():
    """确保存在默认套餐组(新用户注册时分配)。首次启动自动建一个宽松的 free 组。
    组名优先取运行时设置 settings.default_group,回退 config.DEFAULT_GROUP。"""
    name = USER_DB.get_setting("default_group") or config.DEFAULT_GROUP
    g = USER_DB.get_group_by_name(name)
    if g:
        return g
    gid = USER_DB.create_group(
        name=name,
        rate_multiplier=1.0,
        supported_models=["*"],   # 默认放开全部模型,管理员可后续收紧
        billing_policy="free",    # 默认不计费,管理员改为 balance/quota
        rpm_limit=20,
        daily_limit=0, weekly_limit=0, monthly_limit=0,
        limit_unit="requests",
        is_default=1)
    return USER_DB.get_group(gid)


def load_plugins():
    """按 config.PLUGINS 导入插件模块(import 即注册 hooks)。"""
    loaded = []
    for name in config.PLUGINS:
        try:
            __import__(f"plugins.{name}")
            loaded.append(name)
        except Exception as e:
            print(f"[plugins] 加载 {name} 失败: {e}", flush=True)
    return loaded


def load_payment_providers():
    """导入支付渠道模块(import 即注册)。

    注册表只在模块 import 时被填充,所以「启用一个渠道」= 「import 它」。
    这里刻意把 payments/ 下所有渠道都 import 进来,而不是只 import 启用的那几个:
    否则管理员在网页上启用一个启动时没 import 的渠道,get_provider 会返回 None,
    界面显示「已启用」而用户一直看到「站点未启用任何支付渠道」。
    是否启用由 site_settings.payment_providers() 每次调用时判断,不由 import 决定。

    mock 只在环境变量里显式要求时才 import —— 它不验签也不比金额,
    注册进来就等于给站点留一个免费充值口(管理台那条路已经在校验里硬拒)。
    """
    import os
    import pkgutil

    import payments as _pkg
    loaded, want_mock = [], "mock" in config.PAYMENT_PROVIDERS
    for mod in pkgutil.iter_modules([os.path.dirname(_pkg.__file__)]):
        if mod.name == "mock" and not want_mock:
            continue
        try:
            __import__(f"payments.{mod.name}")
            loaded.append(mod.name)
        except Exception as e:
            print(f"[payments] 加载 {mod.name} 失败: {e}", flush=True)
    return loaded


def load_data_channels():
    """把 channels 表里的数据渠道注册进 adapter 注册表。启动时调一次(在渠道开关
    之前 —— 开关校验要认得这些名字),管理台每次增删改后再调一次。"""
    from core import channels
    return channels.reload(USER_DB)


def apply_channel_switches():
    """把 settings 里的「下线渠道」与「渠道路由」推进 adapter 注册表,返回下线清单。

    启动时调一次,管理台每次改设置后再调一次 —— 注册表是纯内存的,不会自己去读库。
    单 worker 是前提(限流与巡检本来就依赖它),多 worker 时另一个进程的注册表不会
    跟着变,那份不一致比没有开关更难查。
    """
    from core.adapter import set_channel_routing, set_disabled_channels
    set_channel_routing(site_settings.channel_routing())
    return set_disabled_channels(site_settings.disabled_channels())


def init_pricing_catalog():
    """启动加载内置目录价与 LiteLLM 补充价。"""
    st = pricing_catalog.ensure_loaded()
    PRICING.invalidate()
    return st


def refresh_pricing_catalog():
    pricing_catalog.refresh()
    PRICING.invalidate()
