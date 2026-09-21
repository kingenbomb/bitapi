#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bit-api 配置(走环境变量,便于部署)。

环境变量前缀为 BITAPI_*。项目原名 poolgate,为不打断既有部署,读取时会
回落到 POOLGATE_* 同名变量(新前缀优先)。旧前缀属过渡兼容,新部署请用 BITAPI_*。
"""
import os

_LEGACY_PREFIX = "POOLGATE_"


def env(name, default=None):
    """读 BITAPI_<name>,缺失则回落 POOLGATE_<name>,再缺失用 default。"""
    v = os.environ.get("BITAPI_" + name)
    if v is None:
        v = os.environ.get(_LEGACY_PREFIX + name)
    return default if v is None else v


def env_int(name, default):
    return int(env(name, str(default)))


def env_float(name, default):
    return float(env(name, str(default)))


def env_bool(name, default=True):
    return env(name, "1" if default else "0") not in ("0", "false", "")


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


_HERE = os.path.dirname(os.path.abspath(__file__))

# 服务
HOST = env("HOST", "0.0.0.0")
PORT = env_int("PORT", 8080)

# 鉴权
API_KEY = env("API_KEY", "sk-bitapi")        # /v1/* 网关主密钥(内部/兜底)
ADMIN_KEY = env("ADMIN_KEY", "adm-bitapi")   # /admin/* 管理 + 上报

# 用户接入层(portal)
JWT_SECRET = env("JWT_SECRET", "change-me-bitapi-jwt")  # 生产必须改
JWT_TTL = env_int("JWT_TTL", 7 * 24 * 3600)             # 会话有效期(秒)
DEFAULT_GROUP = env("DEFAULT_GROUP", "free")            # 新用户默认套餐组名
AUTH_CACHE_TTL = env_int("AUTH_CACHE_TTL", 10)          # key→user/group 缓存秒数
REQUIRE_INVITE = env_bool("REQUIRE_INVITE", True)       # 强制邀请码注册
# 密钥 IP 白名单的取值来源:反代部署(常态)必须信任 X-Forwarded-For,
# 否则拿到的永远是 nginx 的 127.0.0.1。直接暴露公网时改成 false,
# 否则客户端可以自己伪造该头绕过白名单。
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", True)

# 登录/注册/找回密码的限速(进程内滑动窗口,见 core/throttle.py)。0 = 不限。
# 登录只记失败:同一 IP 在窗口内连续失败 N 次、或同一邮箱被试 M 次,就 429。
# 邮箱那道是防「定向撞一个账号」,IP 那道是防「一个 IP 扫一批账号」,两道缺一不可。
AUTH_LOGIN_FAILS_PER_IP = env_int("AUTH_LOGIN_FAILS_PER_IP", 20)
AUTH_LOGIN_FAILS_PER_EMAIL = env_int("AUTH_LOGIN_FAILS_PER_EMAIL", 8)
AUTH_LOGIN_WINDOW = env_int("AUTH_LOGIN_WINDOW", 600)
# 注册按 IP 计成功次数:正常人一小时注册不了 5 个号,脚本一分钟就能注册 500 个来刷赠额与签到。
AUTH_REGISTER_PER_IP = env_int("AUTH_REGISTER_PER_IP", 5)
AUTH_REGISTER_WINDOW = env_int("AUTH_REGISTER_WINDOW", 3600)
# 找回密码按邮箱与 IP 各计一道:邮箱那道防拿别人邮箱刷验证邮件,IP 那道防扫号。
AUTH_RESET_PER_EMAIL = env_int("AUTH_RESET_PER_EMAIL", 3)
AUTH_RESET_PER_IP = env_int("AUTH_RESET_PER_IP", 10)
AUTH_RESET_WINDOW = env_int("AUTH_RESET_WINDOW", 3600)


def _default_db_path():
    """默认库名 bitapi.db;若它不存在而旧的 poolgate.db 在,沿用旧库(平滑改名)。"""
    new = os.path.join(_HERE, "bitapi.db")
    legacy = os.path.join(_HERE, "poolgate.db")
    if not os.path.exists(new) and os.path.exists(legacy):
        return legacy
    return new


# 存储
DB_PATH = env("DB", _default_db_path())

# 备份。库文件是用户/密钥/余额/订单/号池的唯一副本,默认每天一份、留 7 份,
# 放在库文件旁的 backups/(见 core/backup.py)。0 = 关闭自动备份,管理台仍可手动做。
BACKUP_INTERVAL = env_int("BACKUP_INTERVAL", 24 * 3600)
BACKUP_KEEP = env_int("BACKUP_KEEP", 7)
BACKUP_DIR = env("BACKUP_DIR", "")   # 留空 = <库所在目录>/backups

# 号池策略
MIN_BALANCE = env_float("MIN_BALANCE", 0.05)      # 余额低于此切号
REFRESH_MARGIN = env_int("REFRESH_MARGIN", 600)   # token 提前多少秒刷
SEND_TIMEOUT = env_int("TIMEOUT", 120)

# 巡检
SCAN_INTERVAL = env_int("SCAN_INTERVAL", 1800)    # 巡检周期(秒)
SCAN_CONCURRENCY = env_int("SCAN_CONCURRENCY", 50)  # 巡检并发上限

# ---- grok 渠道 ----
# 对外暴露哪些模型。只列号池真的有额度的那些 —— 实测过:同号 grok-4.6 返回 200,
# 而 grok-code-fast-1 一律 402 spending-limit。把没额度的模型挂在 /v1/models 上,
# 用户每次调都必然失败,而失败会被记成账号级问题。
GROK_MODELS = env("GROK_MODELS", "grok-4.6,grok-4.5")
# 对外名 → 上游名的前缀(留空表示同名)。
GROK_MODEL_PREFIX = env("GROK_MODEL_PREFIX", "")
GROK_BASE_URL = env("GROK_BASE_URL", "https://cli-chat-proxy.grok.com/v1")
GROK_TOKEN_ENDPOINT = env("GROK_TOKEN_ENDPOINT", "https://auth.x.ai/oauth2/token")
GROK_CLIENT_ID = env("GROK_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828")
# token 实测 6 小时(expires_in=21600)。提前一小时刷,留足重试余量。
GROK_REFRESH_MARGIN = max(300, env_int("GROK_REFRESH_MARGIN", 3600))
GROK_TIMEOUT = max(10, env_int("GROK_TIMEOUT", 300))

# MagicPatterns 的 reasoningContent 是上游真实思考流；Anthropic 客户端可选择接收 thinking SSE。
ANTHROPIC_EXPOSE_REASONING = env_bool("ANTHROPIC_EXPOSE_REASONING", True)

# 注册赠额(美元)。0 = 不送。开了强制邀请码之后,能注册进来的都是拿到码的人,
# 所以这笔钱按「每个成功注册的账号」发,不再单独区分是哪种码换来的。
# 走 credit() 记一条 reason=signup 的流水而不是直接改 balance:余额的每一分
# 都要有对应的流水行,否则对不上账时无从查起。幂等键是 signup:<uid>,
# 同一个账号重复调也只加一次。
SIGNUP_BONUS = env_float("SIGNUP_BONUS", 0)

# 每日签到随机赠额(美元)。管理台「站点设置」可存库覆盖这两个默认值；
# 金额按 0.0001 美元一档抽取，既能显示小额也不会留下浮点长尾。
CHECKIN_MIN = env_float("CHECKIN_MIN", 0.01)
CHECKIN_MAX = env_float("CHECKIN_MAX", 0.10)

# 渠道路由:JSON,{"渠道名": {"priority": 0, "weight": 1}}。同一模型挂多个渠道时,
# priority 高的先试、失败降档,同档按 weight 分流。这只是部署默认值,管理台「渠道」页
# 每行的优先级 / 权重存库覆盖。留空 = 全部默认(0 / 1)。
def _routing_default():
    import json
    raw = env("CHANNEL_ROUTING", "")
    if not raw.strip():
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


CHANNEL_ROUTING = _routing_default()

# 下线的渠道(逗号分隔的 channel 名)。下线 = 用户侧看不到、调不到(/v1/models、
# 模型广场、/v1/* 路由三处同时消失),但号池与管理面板照旧 —— 号还在,随时能上回来。
# 这只是部署时的默认值:管理台「站点设置」里的开关存库,优先级更高。
DISABLED_CHANNELS = env_list("DISABLED_CHANNELS", "")

# ---- 计费 ----
# LiteLLM 价目表(内置项目别名价之后的补充兜底源)。
PRICING_CATALOG_URL = env(
    "PRICING_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json")
PRICING_CATALOG_PATH = env(
    "PRICING_PATH", os.path.join(_HERE, "data", "litellm_pricing.json"))
PRICING_REFRESH_INTERVAL = env_int("PRICING_REFRESH", 24 * 3600)  # 0=不自动刷新
PRICING_CACHE_TTL = env_int("PRICING_CACHE_TTL", 30)

# 订单有效期(秒),超时置 expired
ORDER_TTL = env_int("ORDER_TTL", 1800)
# 启用的支付渠道(逗号分隔)。**默认为空 —— 不配就没有任何渠道,而不是悄悄启用 mock。**
# mock 把「打开链接」当成付款成功,默认启用等于给站点开一个免费充值口。
# 本地开发显式写 BITAPI_PAYMENT_PROVIDERS=mock。
PAYMENT_PROVIDERS = env_list("PAYMENT_PROVIDERS", "")
# 单笔最低充值(美元)。服务端硬限 —— 前端那个 min 只是 UI 软约束,直接打接口能绕过。
MIN_TOPUP = env_float("MIN_TOPUP", 1.0)
# 对外可访问的站点根地址(支付回调/跳回用)
SITE_URL = env("SITE_URL", "http://127.0.0.1:8080")

# 生成媒体自托管:生图/生视频渠道返回的是上游 CDN 链接(带防盗链、可能 404),
# 网关把字节抓下来放到 <SITE_URL>/media/gen/ 下短期托管,客户端从自家域名取。
# 生成结果一次性,默认存 30 分钟由 housekeeping 清理。MEDIA_DIR 留空 = 库旁 media/。
MEDIA_TTL = env_int("MEDIA_TTL", 1800)     # 缓存文件存活秒数
MEDIA_DIR = env("MEDIA_DIR", "")           # 留空 = <库所在目录>/media

# SMTP 发信(找回密码 / 邮箱验证 / 提醒)。这些是默认值,管理台「邮件设置」存库覆盖。
# 全空 = 未配置:找回密码入口会明说「站点未配置邮件服务」,而不是假装发了。
SMTP_HOST = env("SMTP_HOST", "")
SMTP_PORT = env_int("SMTP_PORT", 465)
SMTP_USER = env("SMTP_USER", "")
SMTP_PASS = env("SMTP_PASS", "")
SMTP_FROM = env("SMTP_FROM", "")            # 发件地址,如 no-reply@example.com
SMTP_FROM_NAME = env("SMTP_FROM_NAME", "")  # 发件人显示名,空则只有地址
SMTP_SECURITY = env("SMTP_SECURITY", "ssl")  # ssl(465) | starttls(587) | none(25)
SITE_NAME = env("SITE_NAME", "bit-api")     # 邮件标题里的站名
# 找回密码链接有效期(秒)
PASSWORD_RESET_TTL = env_int("PASSWORD_RESET_TTL", 1800)

# 社区账号登录(对接一个 Flarum + community-connect 的论坛)。三项都留空即关闭,
# 登录页不显示「社区账号登录」。client secret 只在服务端换 token,不进站点 settings /
# 管理端响应。redirect_uri 必须与社区里登记的值逐字一致。
COMMUNITY_BASE_URL = env("COMMUNITY_BASE_URL", "")
COMMUNITY_CLIENT_ID = env("COMMUNITY_CLIENT_ID", "")
COMMUNITY_CLIENT_SECRET = env("COMMUNITY_CLIENT_SECRET", "")
COMMUNITY_REDIRECT_URI = env(
    "COMMUNITY_REDIRECT_URI",
    SITE_URL.rstrip("/") + "/oauth/community")
# community-connect 插件的 REST 命名空间(授权端点是 <社区根>/community-connect,
# 换 token / 取用户信息的接口在 <社区根>/api/<这个前缀>/ 下)。各站可能改过名。
COMMUNITY_API_PREFIX = env("COMMUNITY_API_PREFIX", "community-connect")
COMMUNITY_HTTP_TIMEOUT = max(1, env_int("COMMUNITY_HTTP_TIMEOUT", 10))

# 易支付(彩虹易支付协议)
EPAY_API_URL = env("EPAY_URL", "")      # 如 https://pay.example.com
EPAY_PID = env("EPAY_PID", "")
EPAY_KEY = env("EPAY_KEY", "")
# 美元→人民币换算。**只在下单那一刻用一次**:算出的人民币会冻进 orders.pay_amount_cny,
# 回调金额比对直接在人民币域做。改这个值不影响在途订单(它们按下单时冻的数执行)。
EPAY_USD_RATE = env_float("EPAY_USD_RATE", 7.2)

# ---- 插件 ----
# 逗号分隔的插件模块名(plugins 包下)。留空=不启用任何插件。
# 内置示例:affiliate_percent / affiliate_fixed / affiliate_revshare
PLUGINS = env_list("PLUGINS", "")

# 输出洗词 —— 反代出去时抹掉上游品牌名,不暴露上游(content/reasoning/流式 token 三路)
SCRUB_ENABLED = env_bool("SCRUB", True)
# 逗号分隔;留空用内置默认(见 core/scrub.py DEFAULT_TERMS)。长词优先、大小写不敏感、整段删除。
SCRUB_TERMS = env_list("SCRUB_TERMS", "")
