#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adapter 抽象基类 + 注册表。

每个网站号池实现一个 Adapter,声明 capabilities(需要哪些维护)+ 实现方法。
调度器和面板读 capabilities/columns 动态适配,加新网站零改动主程序。
"""
import random
from abc import ABC

# 能力标记
CAP_REFRESH = "refresh_token"   # token 会过期,需要刷新
CAP_BALANCE = "balance"         # 有余额/额度概念
CAP_HEALTH = "health"           # 支持存活检测
CAP_REGISTER = "auto_register"  # 支持自动注册补号
CAP_CHAT = "chat"               # 支持对话(网关转发)

# 计量模式(billing_mode)—— 决定该渠道的用量/token 从哪来,供计量层归一。
#   upstream    = 上游返回标准 usage(官方 API 与正版反代),直接采信
#   estimate    = 上游不返回 usage(自建中转、逆向接口),用 tiktoken 估算 in/out
#   per_request = 无 token 概念(图/视频生成),按次计量,tokens 恒 0
BILL_UPSTREAM = "upstream"
BILL_ESTIMATE = "estimate"
BILL_PER_REQUEST = "per_request"


class GenerationSubmittedError(RuntimeError):
    """生成任务已被上游受理，但最终结果未知；网关不得换号重复提交。"""

    def __init__(self, task_id, channel, message):
        self.task_id = task_id
        self.channel = channel
        super().__init__(message)


class Adapter(ABC):
    # --- 子类必须覆盖 ---
    name = "base"                 # channel 名(唯一)
    capabilities = []             # 见 CAP_*
    models = []                   # 对外暴露的 OpenAI 模型名
    columns = []                  # 面板列: [{"key","label","type"}]
    refresh_interval = 0          # token 刷新周期(秒),0=不刷
    proxy = False                 # True=转发型(自带号池,请求原样透传给上游引擎)
    streaming = False             # True=取号后自产 OpenAI SSE(navos),实现 stream_chat
    exhausted_is_dead = False     # 额度耗尽是否视为永久失效(由渠道声明)
    billing_mode = BILL_ESTIMATE  # 计量模式(见 BILL_* 常量);逆向渠道默认估算
    # True = 透传型 key 池:key 长期有效,没有 token/余额/配额这些我们能判的本地状态。
    # 这种渠道里「上游此刻不好」和「这把 key 坏了」是两件事,而我们只看得见前者 ——
    # 所以除了上游明说的凭据失效(401/402/403 与 terminal 标记),一律不改状态,只轮换。
    # 见 core/pool.mark_failure / mark_exhausted。
    stateless_keys = False

    def has(self, cap):
        return cap in self.capabilities

    # --- 账号生命周期(按 capabilities 选择实现) ---

    def register(self):
        """注册一个新账号。返回 {identity, secret, meta} 或 None。"""
        raise NotImplementedError

    def login(self, acct):
        """用凭证登录换 token。返回 {token, token_exp, ...} 或抛异常。"""
        raise NotImplementedError

    def refresh_token(self, acct):
        """确保 token 有效(过期则重登)。返回 {token, token_exp}。默认走 login。"""
        return self.login(acct)

    def balance(self, acct):
        """查余额。返回 float 或 None。"""
        return None

    def health(self, acct):
        """存活检测。返回 True/False。"""
        return True

    # --- 网关对话 ---

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        """
        普通型 adapter:用某账号发起一次 OpenAI 对话。
          body = 完整 OpenAI 请求(含 tools/tool_choice 等);需要透传 tools 的 adapter 用它。
          返回: str 或 {"content": str, "reasoning": str|None, "raw": dict}
            raw = 完整 OpenAI 响应(含 tool_calls);server 优先用 raw 原样返回。
          stream=True 时逐段调 on_token(text, kind),kind ∈ {"content","reasoning"}
        """
        raise NotImplementedError

    def proxy_chat(self, body, stream=False, on_sse=None):
        """
        转发型 adapter(proxy=True):把完整 OpenAI 请求 body 原样透传给上游引擎。
          非流式: 返回上游的完整 OpenAI JSON dict(含 tool_calls 等,不做任何改写)
          stream=True: 逐条 SSE 行调 on_sse(line_str),原样转发
        """
        raise NotImplementedError


# ---- 注册表 ----

_REGISTRY = {}
# 被下线的渠道名。注册表本身不读库 —— 值由 core/portal_state.apply_channel_switches()
# 从 settings 推进来(启动一次 + 每次管理台改设置),免得把 settings/DB 那条链
# 拉进这个纯模块。
_DISABLED = set()


def register_adapter(adapter):
    _REGISTRY[adapter.name] = adapter


def unregister_adapter(name):
    """数据渠道被删或改名时摘掉。代码渠道不会走这里 —— 它们 import 即注册,
    进程活着就一直在。返回是否真的摘掉了什么。"""
    return _REGISTRY.pop(name, None) is not None


def get_adapter(name):
    return _REGISTRY.get(name)


def all_adapters():
    """注册表全量,含已下线的渠道。号池维护(巡检 / 面板 / 导号)用这个 ——
    「下线」是对用户下线,不是把这批账号从管理面板上藏起来。"""
    return dict(_REGISTRY)


def set_disabled_channels(names):
    """设置下线渠道,返回规范化后的列表。名字的存在性由写入侧
    (site_settings.validate)校验;这里认不出的名字留着也只是没有效果。"""
    global _DISABLED
    _DISABLED = {str(n).strip() for n in (names or []) if str(n).strip()}
    return sorted(_DISABLED)


def disabled_channels():
    return sorted(_DISABLED)


def enabled_adapters():
    """用户侧看得到、调得通的渠道。模型清单与模型广场用这个。"""
    return {n: ad for n, ad in _REGISTRY.items() if n not in _DISABLED}


# ---- 路由:一个模型可以挂多个渠道 ----
#
# 渠道级 priority / weight 由 core/portal_state.apply_channel_routing() 从 settings
# 推进来(与下线开关同一条路),注册表本身不读库。
#   priority 高的先试,失败(没号 / 换遍号仍失败 / 上游限流)再降到下一档;
#   同一档内按 weight 随机分流;weight 0 的排到该档末尾只当兜底。
# 缺省 priority 0、weight 1 —— 什么都不配就是「挂在同一模型上的渠道均分」。

_ROUTING = {}
_RNG = random.Random()
DEFAULT_PRIORITY, DEFAULT_WEIGHT = 0, 1


def set_channel_routing(mapping):
    """设置各渠道的 {priority, weight}。认不出的渠道名留着也只是没有效果。"""
    global _ROUTING
    out = {}
    for name, cfg in (mapping or {}).items():
        if not isinstance(cfg, dict):
            continue
        try:
            pr = int(cfg.get("priority", DEFAULT_PRIORITY))
            wt = int(cfg.get("weight", DEFAULT_WEIGHT))
        except (TypeError, ValueError):
            continue
        out[str(name).strip()] = {"priority": pr, "weight": max(0, wt)}
    _ROUTING = out
    return dict(_ROUTING)


def channel_routing():
    return {k: dict(v) for k, v in _ROUTING.items()}


def routing_of(name):
    """(priority, weight),没配就是默认。"""
    cfg = _ROUTING.get(name) or {}
    return (int(cfg.get("priority", DEFAULT_PRIORITY)),
            int(cfg.get("weight", DEFAULT_WEIGHT)))


def _candidates(model):
    """声明了该模型(或名字就是该渠道)的已上线渠道,按注册顺序。"""
    return [n for n, ad in enabled_adapters().items()
            if model in ad.models or model == n]


def _weighted_order(names, rng):
    """按权重不放回地随机排序;权重 0 的按原顺序垫在最后。"""
    weighted = [(n, routing_of(n)[1]) for n in names]
    live = [(n, w) for n, w in weighted if w > 0]
    zero = [n for n, w in weighted if w <= 0]
    out = []
    while live:
        total = sum(w for _, w in live)
        pick = rng.random() * total
        for i, (n, w) in enumerate(live):
            pick -= w
            if pick < 0:
                out.append(n)
                live.pop(i)
                break
        else:
            out.append(live.pop()[0])
    return out + zero


def model_routes(model, rng=None):
    """一次请求该依次尝试的渠道名。空列表 = 未知模型。

    先按 priority 从高到低分档,每档内按 weight 随机排,拼成一条候选序列。
    调用方从头试,失败换下一个 —— 「A 渠道打空了,B 渠道明明有同一个模型」
    这种事原先只能吃 503。
    """
    names = _candidates(model)
    if not names:
        return []
    rng = rng or _RNG
    tiers = {}
    for n in names:
        tiers.setdefault(routing_of(n)[0], []).append(n)
    out = []
    for pr in sorted(tiers, reverse=True):
        out.extend(_weighted_order(tiers[pr], rng))
    return out


def primary_channel(model):
    """确定性的首选渠道(最高 priority,同档取 weight 最大,再同则注册顺序)。
    给清单与广场标 owned_by 用,不掷随机数,否则同一页两次刷新归属会变。"""
    names = _candidates(model)
    if not names:
        return None
    return max(names, key=lambda n: (routing_of(n)[0], routing_of(n)[1], -names.index(n)))


def model_to_channel():
    """模型名 -> 首选 channel 的扁平映射。

    下线的渠道不进这张表:它的模型在 /v1/* 上就是「未知模型」(404),与清单里
    看不到它保持同一口径 —— 一边看不到另一边还调得通,是最难查的那种不一致。
    同一模型挂了多个渠道时只给首选那个;要完整候选序列用 model_routes()。
    """
    m = {}
    for name, ad in enabled_adapters().items():
        for model in ad.models:
            if model not in m:
                m[model] = primary_channel(model)
        m.setdefault(name, name)  # channel 名本身也可作为 model
    return m
