# bit-api

轻量 LLM API 网关 —— 一个人也能跑起来的 new-api / sub2api 替代品。

Python + FastAPI + SQLite 单文件，**无 Redis、无 Postgres、无构建步骤**。1 核 1G 的机器足够支撑数千注册用户、数百并发。

设计目标是**做底座**：核心只负责"记录发生了什么"并保证正确性，"该收多少钱、该给谁返佣"这类策略全部交给可插拔的插件。想改行为，改一个文件；不想要某个功能，删掉它。

## 能做什么

- **多渠道聚合**：一个 adapter 接一个上游（官方 API、自建 key 池、逆向渠道皆可），按模型名自动路由，失败自动切号。**同一模型可挂多个渠道**：优先级高的先试，没号 / 换遍号仍失败 / 上游限流就降到下一档，同档按权重分流；流式只在一帧未出时换渠道，计费记实际作答的渠道。每个渠道可在管理台一键上下线、就地改优先级与权重，存库即生效不必重启 —— 下线只对用户生效（模型清单、模型广场、`/v1/*` 路由三处同时消失，调用返回「未知模型」），号池、巡检与管理面板照旧，随时上回来
- **OpenAI 兼容上游不写代码**：管理台「渠道」页填 base_url、模型清单、模型映射、额外请求头，存库即注册；同页导入 key、一键测试（回上游原话）、看号池计数。流式**原样透传**上游 SSE —— tool_calls / usage / finish_reason 一个不丢，只把 `model` 改回对外名；上游 401/402/403 让 key 退场，其余抖动只轮换不罚号。仓库自带的 `grok` 渠道则是自己实现的 Python 子类，不经过这一层
- **三协议**：OpenAI `/v1/chat/completions` + `/v1/responses`（Codex CLI 与新 SDK 的默认接口，function tools 与流式事件齐全，无状态、不存 response）+ Anthropic `/v1/messages`（含 `count_tokens`，图片 block 透传），流式与非流式，工具调用透传；另有 `/v1/embeddings`（OpenAI 兼容的向量渠道：在管理台填 base_url 与模型清单即可用，检索用途由调用方带 `task` 决定）。**响应里的 usage 与账单一致**：上游给了真实值原样透传，没给就按计量层同一套估算回填（Anthropic 的 `message_start` / `message_delta` 也有值），计量层仍如实标 `estimate`
- **用户体系**：邮箱或白嫖社区登录、社区账号绑定、API 密钥、仪表盘、使用记录、每日签到随机赠额（管理台可设区间）、邀请码（可强制，管理台批量发一次性码）、邮箱绑定
- **在线体验**：控制台里挑个对话模型直接聊，流式出字，每轮显示首字延迟、用时与 token。走 `/api/playground/chat`（JWT 鉴权），**计费与 `/v1/chat/completions` 同一条路** —— 扣自己的余额、如实进「使用记录」，所以体验价和真接进去的价是同一个。只收声明了 `CAP_CHAT` 的渠道，图/视频模型不在这里
- **模型广场**：能调的模型一页看全，带厂商官方图标、生效单价与可调用的模型名；卡片 / 表格两种视图，可按供应商、渠道、计费方式筛选并按价格排序。可见范围与网关白名单一致，看得到就调得通。图/视频模型按分辨率、时长折叠成一张卡，规格价在详情里。**未登录也能看**（`GET /api/models` 免鉴权），此时按默认分组的价与可见范围显示 —— 也就是说模型清单与价目表对任何人公开，不想公开就把这条路由改回强制登录
- **站内公告**：管理台发公告（分级 / 置顶 / 草稿 / 定时上下线 / 登录弹窗），用户顶栏铃铛未读提醒，已读按用户存库
- **三种计费策略**（按分组选）：余额扣费 / 配额限额 / 完全免费
- **订阅时长卡**：管理台给任意分组标个价就上架成套餐（小时卡 / 天卡 / 周卡 / 月卡，时长按小时任填），用户在「计费与套餐」页自助购买、从余额扣款；没买卡的人走默认分组按量计费。到期是**读的时候判断**、自动回落默认分组 —— 不靠定时任务改库，漏跑一次不会让人白用过期套餐。同一张卡续费从原到期时间往后叠加，换卡立即生效（原卡剩余时长不折算、不退款）
- **定价**：结对单价制（直接填真实美元单价），支持模型通配、分组专属价、长上下文阶梯价，LiteLLM 价目表兜底
- **支付充值 + 兑换码 + 邀请返佣**：易支付（彩虹协议）开箱可用。商户号 / 密钥 / 汇率 / 最低充值 / 启用渠道都在管理台「站点设置」里改，存库即生效不必重启（环境变量退化成默认值，每项都能一键恢复）。回调六层防线（验签 / 渠道归属 / 金额闸门 / 唯一索引 / 状态条件更新 / 租约乐观锁），回调丢了每 10 分钟主动查单补账；卡在 `paid` / `recharging` 的单同一跳里直接补到账，仍卡住的发 `order.stuck` 事件。返佣策略靠插件自定义
- **运营看板与全站日志**：管理台「看板」给今日 / 7 天 / 30 天的请求、实扣、充值、余额负债、活跃与新增用户、失败率、平均响应，日序列图、渠道消耗占比、钱的去向（充值 / 兑码 / 套餐 / 消费 / 赠送，每一行对应流水表的一个 reason）、用户 / 模型 / 渠道消耗排行；「日志」按用户邮箱 / 模型（支持 `kg-*`）/ 渠道 / 时间 / 只看异常检索全站用量明细，可导出 CSV。用户列表按邮箱 / 昵称 / 返佣码 / ID 服务端检索。用户侧「使用记录」也是服务端分页 + 同条件统计卡 + CSV 导出
- **账号安全**：登录只记失败、按 IP 与邮箱两道滑动窗口限速（撞库与枚举邮箱都拦），注册按 IP 计成功次数（刷号刷赠额），找回密码按邮箱与 IP 各计一道；阈值见 `config.py` 的 `AUTH_*`。改密（自助 / 找回 / 管理员重置）后早于改密时间签发的会话一律作废。**TOTP 二次验证**（RFC 6238，标准库实现，Google Authenticator / 1Password 等全兼容）：密码对了只换一张 5 分钟的受限票，票 + 验证码才换会话，同一时间步的码只能用一次；关闭要密码 + 验证码，手机丢了由另一位管理员替关。管理员没开的在用户列表里标「无 2FA」
- **SMTP 发信 + 找回密码**：标准库 `smtplib`，主机 / 端口 / 加密 / 账号 / 发件人在管理台「邮件设置」里改，存库即生效，带「发测试邮件」。登录页「忘记密码」发一次性链接（只存哈希、30 分钟有效、重发即作废旧的）；邮箱验证码同一条路。没配 SMTP 时入口明说「站点未配置邮件服务」，不假装发了
- **库备份**：`sqlite3` 在线备份 API（WAL 下也是一致快照），默认每天一份、留 7 份、放在库文件旁的 `backups/`；到期判断看目录里最新一份的年龄，重启不归零、停机后第一跳补上。管理台「运行状态」显示上次备份时间与份数，可手动立即备份。恢复 = 停服务、把备份文件改名放回去
- **告警**：core 在该发事实的地方 emit（`pool.empty` 号池打空 / `upstream.failed` 换遍号仍失败 / `order.stuck` / `backup.failed` / `mail.failed` / `auth.throttled`），`plugins/alert_webhook.py` 按 (事件, 键) 去重后推到 Telegram / Bark / 任意 webhook，后台线程投递不阻塞请求。启用：`BITAPI_PLUGINS=alert_webhook` + `BITAPI_ALERT_WEBHOOK_URL`

## 快速开始

```bash
pip install -r requirements.txt

export BITAPI_JWT_SECRET='换成你自己的随机串'
export BITAPI_API_KEY='sk-内部主密钥'
export BITAPI_ADMIN_KEY='adm-号池管理密钥'
export BITAPI_PAYMENT_PROVIDERS='mock'        # 仅本地测试！见下方说明
export BITAPI_PLUGINS='affiliate_percent,alert_webhook'   # 比例返佣 + 运维告警
export BITAPI_ALERT_WEBHOOK_URL='https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>'
# SMTP 也可以先在这里给默认值,上线后在管理台「邮件设置」里改(存库覆盖环境变量)
export BITAPI_SMTP_HOST='smtp.example.com' BITAPI_SMTP_PORT=465 BITAPI_SMTP_SECURITY=ssl
export BITAPI_SMTP_USER='no-reply@example.com' BITAPI_SMTP_PASS='授权码' BITAPI_SMTP_FROM='no-reply@example.com'

# 社区账号登录(可选)。对接一个 Flarum + community-connect 的论坛;
# 三项都留空则关闭,登录页不显示该入口。redirect_uri 要与社区里登记的值逐字一致。
export BITAPI_SITE_URL='https://your-domain.com'
export BITAPI_COMMUNITY_BASE_URL='https://community.example.com'
export BITAPI_COMMUNITY_CLIENT_ID='社区登记的 client id'
export BITAPI_COMMUNITY_CLIENT_SECRET='社区登记的 client secret'
export BITAPI_COMMUNITY_REDIRECT_URI='https://your-domain.com/oauth/community'

python main.py    # 或 uvicorn server:app --host 0.0.0.0 --port 8080
```

> `mock` 渠道把「打开链接」当成付款成功，**生产环境绝不能启用** —— 那等于给站点开一个免费充值口。它不在默认值里（默认无任何渠道），要用必须像上面那样显式写。接真渠道见 [docs/payments.md](docs/payments.md)。

打开 `http://127.0.0.1:8080/portal` —— **第一个注册的用户自动成为管理员**（免邀请码），后续用户需要邀请码。

部署到服务器见 [docs/deploy.md](docs/deploy.md)（Docker / systemd / nginx 三份样板都在仓里）。

注册邀请码与返佣码作用不同。管理台「邀请码」页批量生成的**一次性注册邀请码**是一号一账号、用后失效；管理台开启“强制邀请码”后，只有这种码能注册。用户在「邀请返佣」页拿到的**个人返佣码**仅在开放注册时记录邀请关系并触发返佣插件，不能绕过邀请制。

登录页也可选择「社区账号登录」。已绑定的社区账号直接进入控制台；社区账号第一次创建本站账号时始终要求管理端发放的一次性注册邀请码（不受管理台“强制邀请码”开关影响），个人返佣码无效。已有本站账号请先登录，再到「个人资料 → 登录方式绑定」绑定社区，系统只按社区用户 ID 认人，不会按相同邮箱自动合并账号。

| 入口 | 用途 |
|---|---|
| `/` | 站点首页（公开落地页：定位、在册厂商、接入示例、计费口径，读数实时取自 `/api/models`） |
| `/portal` | 用户控制台 + 管理控制台 |
| `/admin/ui` | 号池监控面板（账号池状态、巡检、导入） |
| `/v1/*` | OpenAI / Anthropic 兼容 API |
| `/health` | 健康检查 |

> 生产部署必须修改 `BITAPI_JWT_SECRET`、`BITAPI_API_KEY`、`BITAPI_ADMIN_KEY`（三者都有可用的默认值）：**监听非回环地址还用默认值，进程会拒绝启动**（`core/preflight.py`）。服务只监听回环、由 nginx 反代加 TLS —— Dockerfile / docker-compose / systemd 单元 / nginx 样板都在仓里，见 [docs/deploy.md](docs/deploy.md)。
> `/admin/*` 号池端点认管理密钥也认管理员 JWT；`/admin/ui` 面板页本身是公开的外壳，数据都要鉴权。
> 限流与巡检依赖进程内状态，**必须保持单 worker**（`uvicorn --workers 1`）。

## 架构：事实与策略分离

```
                   ┌───────────────── core（事实层，一般不改）
客户端 ──► /v1/*  ──┤  鉴权 → 限流/余额预检 → 模型白名单
                   │  路由到 adapter → 取号 → 转发（流式边转边计量）
                   │  结算：定价解析 → 写 usage_log → credit_ledger 扣款
                   │  事件：user.registered / order.paid / code.redeemed
                   │        usage.recorded / balance.changed
                   └───────────────── plugins（策略层，随便改）
                        返佣比例、赠额活动、消费分成……
```

**关键抽象：`credit_ledger` 是余额变动的唯一入口。** 充值、兑码、返佣、管理员调额、消费扣费全部写这张流水，`users.balance` 只是它的投影。每条流水带 `idem_key` 唯一索引 —— **幂等由核心保证**，插件作者调 `credit()` 就是安全的，不必自己处理重复回调与并发。

所以"按自己的方式实现返佣"就是写一个文件：

```python
# plugins/my_affiliate.py
from core.hooks import on
from core.credit import credit

@on("order.paid")                      # 被邀请人充值成功
def rebate(order, user, inviter, **_):
    if inviter:
        credit(inviter["id"], order["amount"] * 0.2,
               reason="affiliate", idem_key=f"aff:{order['id']}")
```

放进 `plugins/`，加到 `BITAPI_PLUGINS`，完事。插件抛异常会被捕获并记日志，绝不影响主流程。

## 上手文档

| 文档 | 讲什么 |
|---|---|
| [docs/adapters.md](docs/adapters.md) | 如何加一个渠道（上游） |
| [docs/pricing.md](docs/pricing.md) | 如何配定价（含长上下文阶梯） |
| [docs/plugins.md](docs/plugins.md) | 如何写返佣/活动插件 |
| [docs/payments.md](docs/payments.md) | 如何接支付渠道 |
| [docs/deploy.md](docs/deploy.md) | 部署：Docker / systemd / 裸跑、nginx 样板、上线核对、恢复 |
| [docs/operations.md](docs/operations.md) | 运维手册：备份与恢复、SMTP、告警、限速、看板口径 |
| [docs/incidents/](docs/incidents/) | 事故账：出过什么问题、哪个门禁守着它 |

## 目录结构

```
core/
  adapter.py        Adapter 基类 + 注册表（渠道抽象）
  channels.py       数据渠道：channels 表 → OpenAICompatAdapter 实例，校验 + 重放注册
  db.py             号池存储（accounts 表）
  pool.py           通用取号（token 惰性刷新 + 余额切号）
  pool_state.py     号池单例（DB / POOL）+ 导 key 的共用逻辑
  sse.py            有界 SSE 读取（总时长 / 单行长度 / 未结束行超时）
  scheduler.py      asyncio 巡检器（刷 token / 查余额 / 测活）
  user_db.py        用户层存储（users/api_keys/groups/usage_logs/
                    credit_ledger/redeem_codes/orders/model_pricing/settings/
                    announcement_reads）
  auth.py           密码哈希(pbkdf2) + JWT + key 生成
  hooks.py          事件总线（@on / emit）
  credit.py         余额变动唯一入口（幂等）
  pricing.py        定价解析链 + 计价（含长上下文阶梯）
  pricing_catalog.py LiteLLM 价目表（定时拉取，兜底）
  metering.py       用量归一（上游真实 usage 优先，tiktoken 估算兜底）
  billing.py        计费编排（策略分流 + 结算）
  redeem.py         兑换码（原子占用）
  orders.py         订单状态机 + 回调六层防线 + 主动查单对账 + 卡单自愈
  payments.py       支付渠道抽象 + 注册表
  backup.py         SQLite 在线备份（到期判断 / 份数保留 / 手动触发）
  mailer.py         SMTP 发信（找回密码 / 邮箱验证 / 测试邮件）
  throttle.py       登录 / 注册 / 找回的滑动窗口限速 + 客户端 IP 取值
adapters/           各上游实现
  grok.py           grok 渠道（xAI 接口,自持 OAuth 续期,OpenAI Responses 转 chat）
  openai_compat.py  通用 OpenAI 兼容 key 池渠道（数据渠道的实现）
payments/           各支付渠道实现（epay/mock）
plugins/            策略插件（返佣示例三个 + alert_webhook 告警）
routers/portal.py   用户与管理 REST API
server.py           FastAPI 应用：/v1 网关 + /admin 号池 + 页面
home.html           站点首页（单文件，零依赖零构建）
portal.html         用户控制台（原生 JS，无构建）
dashboard.html      号池监控面板
static/model-icons/ 厂商官方图标（挑自 @lobehub/icons-static-svg，MIT，自托管不走 CDN）
data/pricing_seed.json  定价种子（可一键导入）
```

## 关键设计决策

**不做额度预占。** 流式请求发起时不预扣，请求结束后按实际用量一次结算，允许把余额扣成负数以保证进行中的请求能完成；下一次请求由预检的 `balance <= 0` 拦住。风险上界是单个 in-flight 批次，不随时间累积。预占那套"预扣多少 / 结算差额 / 失败退款 / 泄漏回收"的状态机是同类项目里最容易出错的部分，不值得。

**流式协议完整性。** 边转发边嗅探 usage（不缓存整流）；强制打开 `stream_options.include_usage` 并原样透传给下游；**客户端断连后继续排空上游**把 usage 合并完再结算（否则上游照常计费而平台漏记）；`message_start` 未发出时不补任何结束事件（宁可空流也不给残缺流）；上游重复发 usage 时取最近一次。每条日志记 `end_reason`（done / eof / client_gone / scanner_error）与首字延迟，计费出争议时这是唯一能复盘的依据。

**用量三级归一。** 上游返回真实 usage 就采信；逆向渠道拿不到 usage 时用 tiktoken 本地估算；图片视频等按次计费的渠道 tokens 记 0。每条日志标 `token_source` 区分真实与估算。

**定价快照。** 每条 usage_log 冻结一份当次生效的单价、档位、倍率。改价之后历史账单仍可精确复算。

## 测试

```bash
pip install -r requirements-dev.txt   # httpx 是 TestClient 的依赖，不是运行时依赖
python -m pytest tests/ -q        # 单元与集成，秒级
python -m pytest tests/e2e -q     # 端到端计费闭环：起真 uvicorn，占 127.0.0.1:8123
```

`tests/e2e/` 不进默认收集（见 `tests/conftest.py`）——它要起真服务器占端口，
跟单元测试放一起会拖慢每次改动。显式点名才跑。

依赖版本全部钉死。上游浮动过一次就够了：CI 抓到 starlette 1.6 而本机是 1.0，
两台机器跑的不是同一份代码。升版本单独提一个 commit。

## License

MIT
