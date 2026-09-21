#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行时站点设置 —— settings 表覆盖环境变量的**唯一**解析入口。

为什么单独一个模块:payments/epay.py 与 core/orders.py 都要读这些值,而它们
不能 import core.portal_state(那条链会拉进 billing/pricing)。这里只依赖
config 与一个被 bind 进来的 db,照 core/credit.py 的先例。

三态,不是两态:
  键不存在        → 回落环境变量
  键存在(含空串)  → 库里的值说话
  显式删除        → 回到第一种

「空串视为未设置、回落 env」是会丢钱的那个方向:管理员清空 EPAY_KEY 的意图
九成是「先把收款停掉」,而那样代码会继续用 env 里的旧 key —— 验签照样能过,
入账口是敞开的,界面上却显示为空。所以 present-wins,清空即停。要恢复环境变量
得走显式的删除动作(delete_setting)。

写入侧的校验在 validate() 里,**必须在写库之前跑**。读取侧不做静默兜底:
兜底会造出「界面显示 7.5、实际按 7.2 收」这种查不出来的账。
"""
import math
import urllib.parse

import config

_MISSING = object()
_DB = None

# 管理台可配的键 → 环境变量兜底值的来源(config 属性名)
ENV_SOURCE = {
    "payment_providers": "PAYMENT_PROVIDERS",
    "min_topup": "MIN_TOPUP",
    "site_url": "SITE_URL",
    "epay_api_url": "EPAY_API_URL",
    "epay_pid": "EPAY_PID",
    "epay_key": "EPAY_KEY",
    "epay_usd_rate": "EPAY_USD_RATE",
    "disabled_channels": "DISABLED_CHANNELS",
    "channel_routing": "CHANNEL_ROUTING",
    "checkin_min": "CHECKIN_MIN",
    "checkin_max": "CHECKIN_MAX",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_pass": "SMTP_PASS",
    "smtp_from": "SMTP_FROM",
    "smtp_from_name": "SMTP_FROM_NAME",
    "smtp_security": "SMTP_SECURITY",
    "site_name": "SITE_NAME",
}

SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from",
             "smtp_from_name", "smtp_security", "site_name")
SMTP_SECURITY_MODES = ("ssl", "starttls", "none")

# 汇率的合理区间。不是「是正数就行」—— 把 7.2 写成 0.72 是稳定的九九折,
# 用户真付 0.72 元拿全额额度,而应收与实付自洽,一行日志都不会报警。
RATE_MIN, RATE_MAX = 1.0, 50.0
TOPUP_MIN, TOPUP_MAX = 0.5, 10000.0
CHECKIN_MIN_ALLOWED, CHECKIN_MAX_ALLOWED = 0.0001, 100.0


def bind(db):
    """由 core/portal_state.py 在建好 UserDB 之后调一次。"""
    global _DB
    _DB = db


def _raw(key):
    """库里的原始值;键不存在返回 _MISSING。"""
    if _DB is None:
        return _MISSING
    return _DB.get_setting(key, _MISSING)


def _env(key):
    return getattr(config, ENV_SOURCE[key])


def is_from_db(key):
    return _raw(key) is not _MISSING


def text(key):
    """字符串项。库里有就用库里的(strip 后),否则回落环境变量。

    strip 是必须的:商户 PID/KEY 从后台粘过来常带尾部空白,带空格的 PID
    进签名之后上游拒,而回调侧比对又不符 —— 表现是「验签失败」,排查方向全歪。
    """
    v = _raw(key)
    if v is _MISSING:
        return str(_env(key) or "").strip()
    return str(v if v is not None else "").strip()


def number(key):
    """数值项。解析不出来就回落环境变量 —— 但写入侧已经挡过一次,
    这里的回落只为「有人手改了库」这种情况留一条不崩的路。"""
    v = _raw(key)
    if v is _MISSING:
        return float(_env(key))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(_env(key))
    return f if math.isfinite(f) else float(_env(key))


def names(key):
    """列表项(启用的渠道)。

    形状不可信:get_setting 是「json.loads 成功用解析值、失败用原始串」,
    所以手写进库的 epay 回来是 str、["epay"] 回来是 list。逐字符迭代一个 str
    会去 import payments.e / payments.p …,一个渠道都注册不上,而
    `"pay" in "epay"` 又让渠道闸门从成员判断退化成子串判断。
    """
    v = _raw(key)
    if v is _MISSING:
        v = _env(key)
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",")]
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


# ---- 具体项(调用方只用这些,不直接碰 config) ----

def payment_providers():
    return names("payment_providers")


def min_topup():
    return number("min_topup")


def site_url():
    return text("site_url")


def epay_api_url():
    return text("epay_api_url")


def epay_pid():
    return text("epay_pid")


def epay_key():
    return text("epay_key")


def epay_usd_rate():
    return number("epay_usd_rate")


def disabled_channels():
    """下线的渠道名。真正生效的地方是 core/adapter 的注册表 ——
    portal_state.apply_channel_switches() 把这份值推进去。"""
    return names("disabled_channels")


def channel_routing():
    """各渠道的 {priority, weight}。形状不可信(手改库可能写成别的),不是 dict 就当空。
    真正生效的地方同样是注册表,由 portal_state.apply_channel_routing() 推进去。"""
    v = _raw("channel_routing")
    if v is _MISSING:
        v = _env("channel_routing")
    return v if isinstance(v, dict) else {}


def checkin_min():
    return number("checkin_min")


def checkin_max():
    return number("checkin_max")


def smtp_host():
    return text("smtp_host")


def smtp_port():
    return int(number("smtp_port"))


def smtp_user():
    return text("smtp_user")


def smtp_pass():
    return text("smtp_pass")


def smtp_from():
    return text("smtp_from")


def smtp_from_name():
    return text("smtp_from_name")


def smtp_security():
    v = text("smtp_security").lower()
    return v if v in SMTP_SECURITY_MODES else "ssl"


def site_name():
    return text("site_name") or "bit-api"


# ---- 写入侧校验 ----

class SettingError(ValueError):
    pass


def _check_url(label, value, allow_loopback):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingError(f"{label} 需要带 http:// 或 https:// 的完整地址")
    if parts.scheme == "http" and not allow_loopback:
        # 商户密钥会随查单请求发到这个地址,明文不行
        raise SettingError(f"{label} 必须用 https")
    if parts.path.rstrip("/"):
        # 拼接是 site + "/pay/notify/epay",带子路径会拼出 404,
        # 而回调打不进来的表现是「用户付了钱不到账」
        raise SettingError(f"{label} 不能带子路径")


def validate(key, value, known_providers=(), known_channels=()):
    """返回规范化后的值;不合法抛 SettingError。写库之前必须过这里。"""
    if key == "payment_providers":
        vals = value if isinstance(value, list) else [
            x.strip() for x in str(value or "").split(",")]
        vals = [str(v).strip() for v in vals if str(v).strip()]
        for v in vals:
            if v == "mock":
                raise SettingError(
                    "mock 把「打开链接」当成付款成功,不能从管理台启用 —— "
                    "本地开发用 BITAPI_PAYMENT_PROVIDERS=mock")
            if known_providers and v not in known_providers:
                raise SettingError(f"未知支付渠道: {v}")
        return vals

    if key == "disabled_channels":
        vals = value if isinstance(value, list) else [
            x.strip() for x in str(value or "").split(",")]
        vals = sorted({str(v).strip() for v in vals if str(v).strip()})
        for v in vals:
            # 拼错的渠道名会静默不生效:界面上写着「已下线」,而那个渠道照旧
            # 在卖。这里必须拦住,不能靠管理员自己看清单。
            if known_channels and v not in known_channels:
                raise SettingError(f"未知渠道: {v}(可选:{'/'.join(known_channels)})")
        return vals

    if key == "channel_routing":
        if not isinstance(value, dict):
            raise SettingError("渠道路由需是 {渠道名: {priority, weight}} 的对象")
        out = {}
        for name, cfg in value.items():
            n = str(name).strip()
            if known_channels and n not in known_channels:
                raise SettingError(f"未知渠道: {n}")
            if not isinstance(cfg, dict):
                raise SettingError(f"{n} 的路由配置需是对象")
            try:
                pr = int(cfg.get("priority", 0))
                wt = int(cfg.get("weight", 1))
            except (TypeError, ValueError) as e:
                raise SettingError(f"{n} 的优先级 / 权重需是整数") from e
            if not (-1000 <= pr <= 1000) or not (0 <= wt <= 1000):
                raise SettingError(f"{n} 的优先级需在 -1000~1000、权重需在 0~1000")
            out[n] = {"priority": pr, "weight": wt}
        return out

    if key == "min_topup":
        f = _as_finite(value, "单笔最低充值")
        if not (TOPUP_MIN <= f <= TOPUP_MAX):
            raise SettingError(
                f"单笔最低充值需在 {TOPUP_MIN:g}~{TOPUP_MAX:g} 美元之间")
        return f

    if key in ("checkin_min", "checkin_max"):
        label = "签到最低额度" if key == "checkin_min" else "签到最高额度"
        f = _as_finite(value, label)
        if not (CHECKIN_MIN_ALLOWED <= f <= CHECKIN_MAX_ALLOWED):
            raise SettingError(
                f"{label}需在 {CHECKIN_MIN_ALLOWED:g}~{CHECKIN_MAX_ALLOWED:g} 美元之间")
        return round(f, 4)

    if key == "epay_usd_rate":
        f = _as_finite(value, "汇率")
        if not (RATE_MIN <= f <= RATE_MAX):
            # 区间不是洁癖:0.72 这种少写一位的值会让用户真付 0.72 元拿全额额度,
            # 而应收与实付自洽,金额闸门形同虚设,也不会有任何日志报警。
            raise SettingError(
                f"汇率需在 {RATE_MIN:g}~{RATE_MAX:g} 之间"
                f"(把 7.2 写成 0.72 会让用户按一折付款)")
        return f

    if key in ("site_url", "epay_api_url"):
        s = str(value or "").strip().rstrip("/")
        if not s:
            return ""      # 清空 = 停用,合法
        label = "站点公网地址" if key == "site_url" else "易支付站点地址"
        # 站点地址允许 http 回环(本地开发要用);易支付地址一律 https
        _check_url(label, s, allow_loopback=(key == "site_url"))
        return s

    if key in ("epay_pid", "epay_key"):
        return str(value or "").strip()

    if key == "smtp_port":
        f = _as_finite(value, "SMTP 端口")
        if not (1 <= f <= 65535) or int(f) != f:
            raise SettingError("SMTP 端口需是 1~65535 的整数")
        return int(f)

    if key == "smtp_security":
        v = str(value or "").strip().lower()
        if v not in SMTP_SECURITY_MODES:
            raise SettingError("SMTP 加密方式只能是 ssl / starttls / none")
        return v

    if key == "smtp_from":
        s = str(value or "").strip()
        if s and ("@" not in s or " " in s):
            raise SettingError("发件地址不是有效邮箱")
        return s

    if key in ("smtp_host", "smtp_user", "smtp_pass", "smtp_from_name", "site_name"):
        return str(value or "").strip()

    raise SettingError(f"未知设置项: {key}")


def _as_finite(value, label):
    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        raise SettingError(f"{label} 必须是数字") from e
    if not math.isfinite(f):
        # nan 最阴:float() 不报错,写进 REAL 列被 SQLite 存成 NULL,
        # 金额闸门的「没冻结值就跳过比对」分支就把整条校验静默关掉了
        raise SettingError(f"{label} 不是有效数字")
    return f
