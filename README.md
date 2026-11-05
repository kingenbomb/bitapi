# bit-api

轻量 LLM API 网关 —— 一个人也能跑起来的 new-api / sub2api 替代品。

Python + FastAPI + SQLite 单文件，**无 Redis、无 Postgres、无构建步骤**。1 核 1G 的机器足够支撑数千注册用户、数百并发。

设计目标是**做底座**：核心只负责"记录发生了什么"并保证正确性，"该收多少钱、该给谁返佣"这类策略全部交给可插拔的插件。想改行为，改一个文件；不想要某个功能，删掉它。

## 能做什么

- **账号管理**：分为账号池与 Key 池，账号池默认查看 GPT；按渠道、认证类型、状态、分组与调度筛选，支持跨页选择、批量预览、编辑、导入导出、并发/RPM、优先级、成本额度、到期/临时暂停及单号/定时测试。GPT 直接同步上游账号资料、套餐和 5h/7d 用量及重置时间，逐号展示；支持 PKCE OAuth 及重新授权、关闭模型训练。逐号统计记录实际作答账号，旧日志不推算到账号；账号计费与用户扣费分别展示。使用及边界见 [账号管理](docs/adapters.md#账号管理)。

- **GPT 原生账号导入**：管理员控制台「渠道与资源」导入 accessToken、Codex `auth.json`、ChatGPT session 或 sub2api 导出文件，支持多文件、批量去重和凭据更新。账号只存在现有数据库，bit-api 自行 OAuth 续期和轮换，直连 Codex Responses，保留流式工具调用与缓存用量；无需额外转发服务。详见 [适配器文档](docs/adapters.md)

- **多渠道聚合**：一个 adapter 接一个上游（官方 API、自建 key 池、逆向渠道皆可），按模型名自动路由，失败自动切号。**同一模型可挂多个渠道**：优先级高的先试，没号 / 换遍号仍失败 / 上游限流就降到下一档，同档按权重分流；流式只在一帧未出时换渠道，计费记实际作答的渠道。每个渠道可在管理台一键上下线、就地改优先级与权重，存库即生效不必重启 —— 下线只对用户生效（模型清单、模型广场、`/v1/*` 路由三处同时消失，调用返回「未知模型」），号池、巡检与管理面板照旧，随时上回来
- **OpenAI 兼容上游不写代码**：管理台「渠道」页填 base_url、模型清单、模型映射、额外请求头，存库即注册；同页导入 key、一键测试（回上游原话）、看号池计数。流式**原样透传**上游 SSE —— tool_calls / usage / finish_reason 一个不丢，只把 `model` 改回对外名；上游 401/402/403 让 key 退场，其余抖动只轮换不罚号。`gpt` 这类账号池渠道复用它做协议转换
- **三协议**：OpenAI `/v1/chat/completions` + `/v1/responses`（Codex CLI 与新 SDK 的默认接口，function tools 与流式事件齐全，无状态、不存 response）+ Anthropic `/v1/messages`（含 `count_tokens`，图片 block 透传），流式与非流式，工具调用透传；另有 `/v1/embeddings`（OpenAI 兼容渠道，现成接好 jina 的 v4 / v5 文本向量：拿 key 导进号池即可用，检索用途由调用方带 `task` 决定）。**响应里的 usage 与账单一致**：上游给了真实值原样透传，没给就按计量层同一套估算回填（Anthropic 的 `message_start` / `message_delta` 也有值），计量层仍如实标 `estimate`
- **用户体系**：邮箱或白嫖社区登录、社区账号绑定、API 密钥、仪表盘、使用记录、每日签到随机赠额（管理台可设区间）、社区等级注册（门槛后台可调，默认 Lv3+，无需邀请码）、邮箱绑定
- **共享 Key**：用户把自己在某条渠道上的 key 交上来换余额奖励。服务端先拿这把 key 真发一次最小请求验活 —— 上游拒收的当场返回原话，不入库、不发奖；通过才进号池当号用，并按把记一笔 `share_key` 奖励流水。同一把 key 全站只发一次（幂等键是 `sharekey:<渠道>:<identity>`，自己重复交或别人先交过都拿不到第二笔）；可限制每人奖励把数与允许共享的渠道。规则存库、管理台「邀请与奖励」里改，存库即生效。控制台「共享 Key」页自助提交与查看。默认关闭（`BITAPI_SHARE_REWARD_ENABLED`）
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
- **模型状态巡检**:后台每小时把在营渠道的模型各实测一遍(对话/向量发最小真实请求,出图/出视频按号池推定不烧额度),结果落库;管理台「模型状态」页看每个模型的正常/异常/耗时/失败原话,可手动「立即检测」;状态翻转发 model.failed / model.recovered 事件供告警插件推送。周期用 BITAPI_MODEL_STATUS_INTERVAL 调(默认 3600 秒)

## 快速开始

```bash
pip install -r requirements.txt

export BITAPI_JWT_SECRET='换成你自己的随机串'
export BITAPI_API_KEY='sk-内部主密钥'
export BITAPI_ADMIN_KEY='adm-号池管理密钥'
export BITAPI_PAYMENT_PROVIDERS='mock'        # 仅本地测试！见下方说明
export BITAPI_PLUGINS='affiliate_percent,alert_webhook'   # 比例返佣 + 运维告警
export BITAPI_ALERT_WEBHOOK_URL='https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>'
# 首次注册前必须配好 SMTP；上线后可在管理台「邮件设置」里改(存库覆盖环境变量)
export BITAPI_SMTP_HOST='smtp.example.com' BITAPI_SMTP_PORT=465 BITAPI_SMTP_SECURITY=ssl
export BITAPI_SMTP_USER='no-reply@example.com' BITAPI_SMTP_PASS='授权码' BITAPI_SMTP_FROM='no-reply@example.com'

# 白嫖社区 OAuth（在 /bbs/connect/apps 新建应用；授权码 + PKCE S256）
export BITAPI_SITE_URL='https://ai.baipiao.co'
export BITAPI_COMMUNITY_CLIENT_ID='社区登记的 client id'
export BITAPI_COMMUNITY_CLIENT_SECRET='社区登记的 client secret'
export BITAPI_COMMUNITY_REDIRECT_URI='https://ai.baipiao.co/oauth/baipiao-community'

# 毛支付可在管理台「站点设置 → 支付设置」配置，默认 1 毛购买 $1 API 余额。
# 环境变量兜底：BITAPI_PAYMENT_PROVIDERS=epay,baipiao，
# BITAPI_BAIPIAO_PID / BITAPI_BAIPIAO_KEY / BITAPI_MAO_PER_USD=1。

python main.py    # 或 uvicorn server:app --host 0.0.0.0 --port 8080
```

> `mock` 渠道把「打开链接」当成付款成功，**生产环境绝不能启用** —— 那等于给站点开一个免费充值口。它不在默认值里（默认无任何渠道），要用必须像上面那样显式写。接真渠道见 [docs/payments.md](docs/payments.md)。

白嫖社区登录使用 `https://baipiao.org/bbs/connect/authorize`、
`/bbs/api/connect/token` 与 `/bbs/api/connect/userinfo`，申请 `openid profile email`。
Client Secret 只在服务端使用；随机 state 和 HttpOnly 流程 cookie 校验回调，
cookie 中的随机值同时作为 PKCE verifier，授权码只消费一次。
身份按固定白嫖社区的 `sub` 绑定（等同旧版 `id`，已有绑定继续可用），不按邮箱自动合并。
登录页点「社区账号登录」；已有本站账号可在个人设置绑定。首次创建本站账号默认要求社区页面等级 **Lv3 及以上**，不再需要邀请码；
仍要求 QQ/Gmail 主邮箱、已验证且未封禁的社区身份。`community_level` 来自社区等级计算器，
不是 `trust_level`；缺失等级时拒绝建号。已有绑定登录和已有账号绑定不受新注册等级限制。
管理员在「管理后台 → 站点设置 → 注册与分组 → 社区最低注册等级」选择 Lv0–Lv6，预览允许注册的等级后保存，立即生效，无需重启。Lv0 仍须取得有效社区等级并满足其他身份条件。设置存入数据库，优先于 `BITAPI_COMMUNITY_MIN_LEVEL`（默认 3）；注册页提示与建号事务使用当前门槛，授权后尚未完成的注册也按最新值校验。
旧 `/community-connect` 应用凭据不能直接当新 OAuth 凭据使用，需在社区应用管理登记上述完整回调。
复制 `.env.example` 后按部署方式注入环境变量；本程序不会自动加载 `.env` 文件。

本站设置 `BITAPI_COMMUNITY_REGISTRATION_ONLY=1`：注册页仅提供社区授权，
`/api/register/code` 和 `/api/register` 拒绝邮箱注册（包括旧验证码、邀请码及首用户免码路径）。
`GET /api/registration-policy` 是注册页和管理台使用的公开策略，只返回注册方式与最低等级。
社区新建账号始终是普通用户。独立部署未启用此配置时保留原邮箱注册模式；
该模式第一个邮箱注册用户自动成为管理员，邀请码设置仅对该模式生效。

控制台侧栏按「工作台」「接入与用量」「账户」分组；管理员可在侧栏顶部切换到管理后台，按运行管理、访问与计费、站点运营处理任务。顶部「快速前往」（`Ctrl/Cmd + K`）可搜索当前账号有权访问的页面。管理页面支持可收藏的地址，例如 `/portal#/admin?tab=stats`、`/portal#/admin?tab=settings`，并记住上次访问的分区。

站点设置按注册、签到、渠道、支付、邮件、备份六类组织，切换设置分类会保留未保存输入，各区独立保存。用户与管理界面共用明暗主题；手机使用抽屉导航，宽数据表在表格区域内横向滑动。

控制台的英文与数字使用本地托管的 Manrope 可变字体（Latin WOFF2 约 25 KB，来源为 Google Fonts 的 Manrope，许可证见 `static/fonts/OFL-Manrope.txt`），中文使用系统黑体，无外部字体请求。金额保留原有精度，货币符号与单位弱化；响应耗时 ≤10 秒为绿色、10–25 秒为黄色、超过 25 秒为红色。页面和图表的短入场动效遵循系统「减少动态效果」，金额不做滚动计数。

未启用社区专用注册的独立部署，邮箱注册须先通过 `POST /api/register/code` 获取邮件中的 6 位验证码，再向
`POST /api/register` 提交 `verification_code`。只接受数字 `@qq.com` 和 Gmail
无点号、无加号的主地址；既有账号登录不受这项新注册限制。验证码 15 分钟有效、
最多错误 5 次、每邮箱至少间隔 60 秒重发；新验证码使旧验证码失效。SMTP 未配置或
发送失败会明确报错，不创建账号。验证、一次性邀请码消耗与建号在同一事务内完成。
社区首次建号同样限制邮箱类型，复用社区已经验证的邮箱身份；已绑定账号照常登录。

## 一键部署

Windows 上双击仓库根目录的 `deploy.cmd`，或在 PowerShell 运行：

```powershell
.\deploy.ps1
```

先提交要发布的代码，再运行脚本。脚本会把 `master` 当前提交推送到 GitHub，再将该提交直接发布到
`dedirock:/root/bit-api`，同步固定版本依赖、重启 `bit-api` 服务，并等待
`http://127.0.0.1:8081/health` 返回正常。未提交文件只会提示，**不会被带到线上**。

首次使用前需要安装 Git for Windows 和 GitHub CLI，并保证 `dedirock` SSH
别名可用。只检查流程、不执行推送或部署时运行 `.\deploy.ps1 -WhatIf`。

邮箱注册模式下，注册邀请码与返佣码作用不同。管理台「邀请码」页批量生成的**一次性注册邀请码**是一号一账号、用后失效；管理台开启“强制邀请码”后，只有这种码能注册。用户在「邀请返佣」页拿到的**个人返佣码**仅在开放注册时记录邀请关系并触发返佣插件，不能绕过邀请制。

登录页也可选择「社区账号登录」。已绑定的社区账号直接进入控制台；社区账号第一次创建本站账号时核验真实社区等级达到后台设置的门槛（默认 Lv3），邀请码和返佣码均不能绕过等级门槛。已有本站账号请先登录，再到「个人资料 → 登录方式绑定」绑定社区，系统只按社区用户 ID 认人，不会按相同邮箱自动合并账号。

| 入口 | 用途 |
|---|---|
| `/` | 公开落地页：橙色立体主视觉、实时模型目录、能力介绍、可复制端点与三种 SDK 示例；顶部导航直达模型广场，支持手机和深色模式，读数取自 `/api/models` |
| `/portal` | 用户控制台 + 管理控制台 |
| `/v1/*` | OpenAI / Anthropic 兼容 API |
| `/health` | 健康检查 |

> 生产部署必须修改 `BITAPI_JWT_SECRET`、`BITAPI_API_KEY`、`BITAPI_ADMIN_KEY`（三者都有可用的默认值）：**监听非回环地址还用默认值，进程会拒绝启动**（`core/preflight.py`）。服务只监听回环、由 nginx 反代加 TLS —— Dockerfile / docker-compose / systemd 单元 / nginx 样板都在仓里，见 [docs/deploy.md](docs/deploy.md)。
> `/admin/*` 号池端点认管理密钥也认管理员 JWT；号池管理统一从 `/portal#/admin?tab=pool` 进入。
> 限流与巡检依赖进程内状态，**必须保持单 worker**（`uvicorn --workers 1`）。

浏览器会话使用 HttpOnly cookie 和绑定会话的 CSRF token，升级后需重新登录；脚本继续使用 Bearer。
代理头默认不信任，反代部署须按 [部署文档](docs/deploy.md#nginx) 显式启用并覆盖 XFF。
免费模型也须显式配价；缺价模型会拒绝调用。管理写操作记录在同一数据库的 `admin_audit`，
管理员通过 `GET /api/admin/audit` 查询操作者、目标、结果与用户状态变化，不返回凭据。

管理后台「套餐分组」设置的是**每用户并发上限**（`concurrency_limit`），0 表示不限。
设为 10 时，同一用户所有 API Key 和在线体验合计最多同时处理 10 个请求；
已完成的请求立即释放名额，因此每分钟可以完成超过 10 次请求。流式请求持续占用到
结束；客户端断连后，网关会先排空上游并结算，再释放名额。超限返回 HTTP 429，
错误码 `CONCURRENCY_LIMIT_EXCEEDED`，无需等待一分钟，有空位即可重试。
旧分组的 `rpm_limit` 在启动时原值迁移为 `concurrency_limit`，例如原来的 10 改为 10 并发；
分组管理、用户信息、用量和套餐 API 均改用新字段。登录、邮件、MCP 及上游渠道自身的
独立限速仍各自生效。并发与迁移验证入口为 CI 的 `tests/test_concurrency.py`。

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

### 改 `routers/` 时的三条硬规矩

**1. `/api` 的 router 只在 `portal_shared.py` 建一次。** 子模块一律 `from routers.portal_shared import router` 往同一个实例上注册。谁自己新建 `APIRouter`，谁的 `prefix="/api"` 和 `route_class=SessionRoute`（CSRF / Origin 校验）就都没了 —— 而且**不报错**，只是那条路由不再带会话鉴权。

**2. 别在新模块里缓存 `USER_DB` / `BILLING`。** 换库有两条路径：`portal_state.rebind(db)` 和测试里直接 `mod.USER_DB = db`。模块级缓存一份自己的引用，第二条路径就会漏掉，表现是「写进去了但读不到」。用 `portal_shared._db()` 现取。

**3. 按行号搬代码时，装饰器算在块内。** `@router.post(...)` 在 `def` 的上一行，只按 `def` 切会把装饰器留在原地，函数搬走了却不再注册成路由。

## 上手文档

用户控制台的「Token 消耗榜」提供累计、近 7 天、近 30 天排名。按概览相同口径汇总输入、输出与可计算的缓存 Token；所有启用账号均在统计范围内，无调用账号保留在列表末尾，相同用量并列。榜单仅登录后可见，用户名脱敏，自己的排名不受当前分页影响。

管理后台「站点设置 → 邀请奖励」可设置开关、每位有效新用户给邀请人的美元余额奖励、每位邀请人的累计奖励人数上限（0 为不限），保存前可预览。邮箱验证注册、满足既有社区等级和邮箱核验的社区注册均支持邀请关系；社区授权往返保留邀请链接中的返佣码，老账号登录或绑定社区不重新绑定邀请人。奖励与建号共同提交，重复请求不重复发奖，上限按已发奖人数计算；新规则仅影响后续注册，不补发历史邀请。未配置时默认关闭；已启用固定邀请插件的部署沿用原配置，保存后台规则后以后台为准。

| 文档 | 讲什么 |
|---|---|
| [MCP 接入与使用指南](https://ai.baipiao.co/mcp-guide) | 用本站 API 密钥接入 TinyFish；客户端配置、搜索与抓取示例、费用及排错（页面源文件 `mcp.html`） |
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
adapters/           各上游实现（gpt/openai_compat/...）
  openai_compat.py  通用 OpenAI 兼容 key 池渠道（数据渠道的实现，各代码渠道的基类）
payments/           各支付渠道实现（epay/mock）
plugins/            策略插件（返佣示例三个 + alert_webhook 告警）
routers/portal.py   用户与管理 REST API
server.py           FastAPI 应用：/v1 网关 + /admin 号池 + 页面
home.html           公开落地页（零依赖、无构建，样式与交互见 static/landing.css、landing.js）
portal.html         用户控制台（原生 JS，无构建）
static/model-icons/ 厂商官方图标（挑自 @lobehub/icons-static-svg，MIT，自托管不走 CDN）
data/pricing_seed.json  定价种子（可一键导入）
```

## 关键设计决策

**不做额度预占。** 流式请求发起时不预扣，请求结束后按实际用量一次结算，允许把余额扣成负数以保证进行中的请求能完成；下一次请求由预检的 `balance <= 0` 拦住。风险上界是单个 in-flight 批次，不随时间累积。预占那套"预扣多少 / 结算差额 / 失败退款 / 泄漏回收"的状态机是同类项目里最容易出错的部分，不值得。

**流式协议完整性。** 边转发边嗅探 usage（不缓存整流）；强制打开 `stream_options.include_usage` 并原样透传给下游；**客户端断连后继续排空上游**把 usage 合并完再结算；`message_start` 未发出时不补任何结束事件（宁可空流也不给残缺流）；上游重复发 usage 时取最近一次。每条日志记 `end_reason`（done / eof / client_gone / scanner_error / error）与首字延迟，计费出争议时这是唯一能复盘的依据。

**失败不扣费。** 上游报错、断流、解析失败保留请求日志与用量，但费用记 0，不扣余额、
密钥额度，也不产生消费返佣。错误帧后附带的 `[DONE]` 不会把失败变成成功。
用户主动中断时继续排空上游：上游正常完成且已有输出，按实际用量计费；没有输出、
或排空过程中上游也失败则免费。非流式上游在 HTTP 200 中返回错误对象也按失败处理。

**用量三级归一。** 上游返回真实 usage 就采信；逆向渠道拿不到 usage 时用 tiktoken 本地估算；图片视频等按次计费的渠道 tokens 记 0。每条日志标 `token_source` 区分真实与估算。

**定价快照。** 每条 usage_log 冻结一份当次生效的单价、档位、倍率。改价之后历史账单仍可精确复算。

**缓存明细。** 使用记录与管理日志分别显示缓存读取、缓存写入 token，CSV 导出追加
`cache_read_tokens` / `cache_write_tokens` 两列。新请求在 `pricing_snapshot` 保存计费使用的
缓存读写量；没有上游用量、或旧日志没有保存明细时显示「—／未记录」，不把未知当成 0。
历史缓存合计仍保留供核对，无法追溯拆分；本次展示调整不改变计费方式。
同一条记录保存请求 IP，并在列表、详情及 CSV 的 `client_ip` 列展示；网关与在线体验
沿用 `TRUST_PROXY_HEADERS` 的客户端 IP 识别规则，仅保存有效 IPv4/IPv6，旧记录不补猜。

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
