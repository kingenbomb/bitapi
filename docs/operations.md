# 运维手册

一个人跑站要盯的几件事，以及它们在这个仓里落在哪。原则和 README 一样：core 记事实、不预设策略；能在管理台改的不必重启；能自愈的不靠人。

## 每天开一次的两个页面

管理控制台 → **看板**：今日 / 7 天 / 30 天三个窗口的经营读数，数据全部来自已有的四张表（`usage_logs` / `credit_ledger` / `orders` / `users`），没有新的写路径，所以看板上的每个数都能对回原始记录。

| 读数 | 口径 |
|---|---|
| 请求 / 实扣 / 原价 / 失败率 / 平均响应 | `usage_logs`，失败 = `pricing_snapshot.end_reason` 不是 `done` |
| 充值 | `orders.status='completed'` 的笔数、美元额度、人民币实收（`pay_amount_cny`） |
| 余额负债 | 所有用户 `balance` 之和 —— 这是你对用户的负债，不是收入 |
| 钱的去向 | `credit_ledger` 按 `reason` 求和：`recharge` 充值、`redeem` 兑码、`plan` 套餐（购买是负数，取反）、`usage` 消费（取反）、赠出 = `checkin + signup + affiliate + admin(正数)` |
| 活跃 / 新增用户 | 有用量记录的去重用户数 / `users.created_at` |

管理控制台 → **日志**：全站用量明细，按邮箱（精确）、模型（支持 `kg-*`）、渠道、时间区间、只看异常检索，点用户名可只看该用户，可导出 CSV（单次上限 10000 行，CSV 里把快照的首字延迟 / 结束原因 / 档位平铺成列）。

对应端点：`GET /api/admin/stats?days=14`、`GET /api/admin/usage`、`GET /api/admin/usage/export.csv`、`GET /api/admin/users?q=`。

## 备份与恢复

库文件（`bitapi.db` / 旧名 `poolgate.db`）是用户、密钥、余额、订单、号池的唯一副本。

- 用 `sqlite3` 的 backup API（`core/backup.py`）而不是 copy 文件：WAL 模式下直接拷主文件拿到的是不含最近写入的半份。
- 默认每 24 小时一份、保留 7 份、放在**库文件旁边**的 `backups/`（不是代码目录 —— deploy 是 tar 解压覆盖，这个目录得跟着库走）。
- 到期判断看目录里最新一份的**文件年龄**，不是进程内计时：重启不归零，停了三天再起来第一跳（10 分钟内）就补一份。
- 先写 `.tmp` 再改名：进程中途被杀留下的是一眼能认出的临时文件，不是缺页的 `.db`。
- 清理按份数不按天数（份数是确定的磁盘上界），且只删本模块命名的 `bitapi-*.db`。

```bash
BITAPI_BACKUP_INTERVAL=86400   # 秒;0 = 关闭自动备份(管理台仍可手动)
BITAPI_BACKUP_KEEP=7
BITAPI_BACKUP_DIR=             # 留空 = <库所在目录>/backups;异地就指到挂载的对象存储
```

管理台「站点设置 → 运行状态」显示上次备份时间、份数、总大小，超过 1.5 个周期没备份会亮红；「立即备份」给「要动数据库之前先备一份」。端点：`GET/POST /api/admin/backup`。

**恢复**：停服务 → 把 `backups/bitapi-YYYYmmdd-HHMMSS.db` 复制成库文件的路径（先把现有的挪走，连同 `-wal` / `-shm`）→ 启动。备份是普通 SQLite 文件，`sqlite3 backups/xxx.db "select count(*) from users"` 就能验。

备份失败会 emit `backup.failed`，接了 `alert_webhook` 会收到通知。

## 邮件（SMTP）

标准库 `smtplib`（`core/mailer.py`），参数在管理台「站点设置 → 邮件设置」改，存库即生效，密码只写不读。环境变量只是默认值：

```bash
BITAPI_SMTP_HOST=smtp.example.com
BITAPI_SMTP_PORT=465
BITAPI_SMTP_SECURITY=ssl        # ssl(465) | starttls(587) | none(25)
BITAPI_SMTP_USER=no-reply@example.com
BITAPI_SMTP_PASS=授权码          # QQ / 163 这类填「授权码」不是登录密码
BITAPI_SMTP_FROM=no-reply@example.com
BITAPI_SMTP_FROM_NAME=          # 可空
BITAPI_SITE_NAME=bit-api        # 邮件标题里的站名
BITAPI_PASSWORD_RESET_TTL=1800  # 找回链接有效期(秒)
```

配好先点「发测试邮件」—— 失败会把 SMTP 的原话显示出来（「认证失败」和「连不上 465」是两种要改的东西）。

靠它的三条路：

- **找回密码**：登录页「忘记密码」→ `POST /api/password/forgot` → 邮件里的链接 `/portal#/reset?token=…` → `POST /api/password/reset`。令牌只存 SHA-256、30 分钟有效、一次性、重发即作废旧的；邮箱不存在也回 ok（不可枚举）；SMTP 没配回 503 明说。
- **邮箱验证**：个人资料页发 6 位验证码；没配 SMTP 时退回「写服务端日志」并如实返回 `sent: false`。
- **改密作废旧会话**：自助改密、找回、管理员重置都走 `UserDB.set_password`，推进 `users.password_changed_at`；JWT 的 `iat` 早于它的一律 401。自助改密的响应带新 token，前端换上，不掉线。

发信失败 emit `mail.failed`。同步发送、10 秒超时：找回密码这条路上「发不出去」必须立刻告诉用户，而不是让人守着收件箱等一封永远不来的信。

## 登录 / 注册限速

进程内滑动窗口（`core/throttle.py`），依赖单 worker（与 RPM 限流、巡检同一前提）。

| 入口 | 计什么 | 默认 |
|---|---|---|
| 登录 | 只记**失败**，IP 与邮箱各一道 | IP 20 次 / 10 分钟，邮箱 8 次 / 10 分钟 |
| 注册 | 按 IP 计**成功**次数（被邀请码挡回的不计） | 5 个 / 小时 |
| 找回密码 | 邮箱与 IP 各一道 | 邮箱 3 次 / 小时，IP 10 次 / 小时 |

两道缺一不可：邮箱那道防「定向撞一个账号」，IP 那道防「一个 IP 扫一批账号」。不存在的邮箱也记账，否则「不限速 = 邮箱不存在」能枚举出注册过的邮箱。成功登录清掉该邮箱的失败计数。超限回 429 + `Retry-After`，同时 emit `auth.throttled`。

反代部署必须信任 `X-Forwarded-For`（`BITAPI_TRUST_PROXY_HEADERS=1`，默认开），否则全站共享 nginx 那个 127.0.0.1 一个桶，一个人撞库全站登不上；直接暴露公网时关掉，因为客户端能伪造这个头。

阈值：`BITAPI_AUTH_LOGIN_FAILS_PER_IP` / `AUTH_LOGIN_FAILS_PER_EMAIL` / `AUTH_LOGIN_WINDOW` / `AUTH_REGISTER_PER_IP` / `AUTH_REGISTER_WINDOW` / `AUTH_RESET_PER_EMAIL` / `AUTH_RESET_PER_IP` / `AUTH_RESET_WINDOW`，0 = 不限。

## 二次验证(TOTP)

管理台管钱:调额、发码、改价、删渠道。一个被钓走的密码不该直接换来这些权限。`core/totp.py` 是 RFC 6238 的标准库实现(SHA-1、30 秒、6 位),Google Authenticator / 1Password / Aegis 等全部兼容,校验允许前后各一步的时钟漂移。

- 开启:个人资料页「开启二次验证」→ 把密钥手输进 authenticator(或导入 otpauth 链接)→ 填一个当前码确认。没确认前不算开启,密钥只显示这一次
- 登录:密码对了只换一张 5 分钟的受限票(`scope=totp` 的 JWT,不能当会话用),票 + 验证码才换会话。错码计入登录限速 —— 6 位码只有一百万种,没有限速就是可穷举的
- 防重放:同一时间步的码只能成功一次(`users.totp_last_step`),开启那一下也算用掉了当前这一步,紧接着登录要用下一个码
- 关闭:要密码 + 当前验证码两样都对 —— 偷到会话的人不该能把锁拆了
- 手机丢了:另一位管理员在「用户」页替他关(`POST /api/admin/users/{id}/2fa/disable`);不能替自己关,自己的走个人资料页
- 管理员没开的在用户列表里标「无 2FA」

## 上线守门

- 三个密钥(`BITAPI_JWT_SECRET` / `API_KEY` / `ADMIN_KEY`)仍是默认值又监听非回环地址 → 进程拒绝启动(`core/preflight.py`)。`BITAPI_ALLOW_DEFAULT_SECRETS=1` 可强行放过,给内网裸跑的人一个出口,但要明确写下来
- `/admin/*` 号池端点认管理密钥也认管理员 JWT(完整会话,不认 TOTP 受限票;改密后的旧会话同样拒)。管理台自己就能导 key、看号池,不再需要 nginx 层注入密钥
- 部署方式见 [deploy.md](deploy.md)

## 告警

core 只发事实（见 [plugins.md](plugins.md) 的「运维事实」表），通知策略在 `plugins/alert_webhook.py`：按 `(事件, 键)` 去重、后台投递、Telegram / Bark / 通用 webhook 三种形状。

```bash
BITAPI_PLUGINS=affiliate_percent,alert_webhook
BITAPI_ALERT_WEBHOOK_URL='https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>'
```

会收到什么：某渠道号池打空（用户此刻全在吃 503）、某渠道换遍号仍失败、已付订单卡住没到账、备份失败、SMTP 坏了、有人在撞库。想报单次大额消费，把插件里 `expensive_request` 设成阈值。

## 订单卡单自愈

`paid`（回调 `mark_order_paid` 之后、`fulfil` 之前进程死了）和 `recharging`（`fulfil` 跑到一半死了）原先没有任何路径再碰它们 —— 钱收了、额度没到、只有站长手点「查单」能救。现在每 10 分钟的对账（`core/orders.reconcile_orders`）会：

1. 把付款时间超过 10 分钟仍在 `recharging` 的单翻回 `paid`（`lease_version` 重新抢占时再加一，僵尸执行流过不了 `complete` 的版本校验）；
2. 对所有 `paid` 单直接 `fulfil`（不问上游，已经知道付了；`fulfil` 本身幂等）；
3. 仍完成不了的 emit `order.stuck`。

刚付的单（10 分钟内）不碰 —— 那是回调线程正在处理的。

## 单 worker

限流、限速、渠道开关、告警去重全是进程内状态，`uvicorn --workers 1` 是前提。要横向扩就得把这些搬进外部存储，那不是这个项目的方向（见 README「设计目标」）。
