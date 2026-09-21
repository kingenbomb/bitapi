# 如何写插件

核心只记录事实（谁充了钱、谁用了多少 token），**不预设策略**（该返多少佣、该送多少额度）。策略是插件的事。

启用方式：把模块名加到环境变量。

```bash
BITAPI_PLUGINS=affiliate_percent,my_promo
```

留空则不启用任何插件（系统照常运行，只是没有返佣/活动）。

## 五分钟写一个返佣插件

```python
# plugins/my_affiliate.py
from core.credit import credit
from core.hooks import on

@on("order.paid")                      # 被邀请人充值成功时
def rebate(order, user, inviter, **_):
    if not inviter:
        return
    credit(inviter["id"], order["amount"] * 0.2,
           reason="affiliate", idem_key=f"aff:{order['id']}")
```

完事。**不需要考虑重复回调、并发、事务** —— `credit()` 靠 `idem_key` 唯一索引保证同一个键只生效一次，支付回调重放 100 次也只发一次返佣。

三条必须遵守：

1. **`idem_key` 必须唯一且可复现**。用事件里的稳定 id（`order['id']`、`code['id']`、`log_id`），不要用时间戳或随机数 —— 否则重放会重复发放。
2. **处理器签名要带 `**_`**。核心以后可能往事件里加字段，带上 `**_` 你的插件不会因此崩。
3. **别在插件里抛异常当控制流**。异常会被捕获并记日志，但主流程不会因此中断 —— 也就是说异常等于"这次没发放"，不会回滚别的东西。

## 事件清单

| 事件 | 何时触发 | 载荷 |
|---|---|---|
| `user.registered` | 注册成功（邀请关系已绑定） | `user`, `inviter` |
| `order.paid` | 支付回调验签通过、余额已到账 | `order`, `user`, `inviter` |
| `code.redeemed` | 兑换码占用成功并已入账 | `code`, `user`, `inviter` |
| `usage.recorded` | 一次请求结算完成 | `log_id`, `user`, `group`, `model`, `channel`, `cost`, `actual_cost`, `input_tokens`, `output_tokens`, `snapshot`, `request_id`, `end_reason` |
| `balance.changed` | 任何 ledger 写入后 | `user_id`, `delta`, `reason`, `balance_after`, `ledger` |

`inviter` 可能为 `None`（用户没有邀请人），务必判空。

注意 `order.paid` 触发时余额**已经**到账了 —— 插件是在既成事实之后做加法，不是审批环节。

### 运维事实

core 只发事实、不判该不该通知，也不做去重 —— 号池打空那一刻每个请求都会 emit 一次 `pool.empty`。要通知站长，订阅它们的插件自己决定频率与渠道（内置的 `alert_webhook` 就是这么做的）。

| 事件 | 何时触发 | 载荷 |
|---|---|---|
| `pool.empty` | 某渠道取不到可用账号（没有 active 的，或换了 20 把都不行） | `channel`, `reason`（`no_active` / `all_tries_failed`） |
| `upstream.failed` | 一次请求换遍账号后仍失败（非流 502/429、流式一个字没出、图片生成失败） | `channel`, `model`, `error`, `kind`（`error` / `ratelimit`） |
| `order.stuck` | 对账时发现已付订单停在 `paid` / `recharging` 超过 10 分钟且补到账失败 | `order`, `age` |
| `backup.failed` | 定时库备份抛异常 | `error` |
| `mail.failed` | SMTP 发信失败（找回密码 / 验证码 / 测试邮件） | `to`, `error` |
| `auth.throttled` | 登录 / 注册 / 找回触发限速回了 429 | `scope`, `key`（IP 或邮箱）, `retry_after` |

## 内置示例（可直接用，也可当模板）

### `affiliate_percent` — 比例返现

被邀请人充值或兑码时，按比例给邀请人真实余额。

```python
CONFIG = {
    "rate": 0.20,          # 返佣比例
    "min_amount": 0.0,     # 低于此金额不返
    "max_per_event": 0.0,  # 单笔上限，0=不限
    "on_redeem": True,     # 兑换码入账是否也返（防止绕过支付逃返佣）
    "first_only": False,   # 只返首充
}
```

### `affiliate_fixed` — 固定送额度

每成功邀请一人，给邀请人固定额度（不看对方是否花钱）。可选给被邀请人一笔见面礼。

```python
CONFIG = {
    "inviter_bonus": 1.0,             # 邀请人每邀一人得（美元）
    "invitee_bonus": 0.0,             # 被邀请人见面礼，0=不送
    "require_verified_email": False,  # 仅在邮箱已验证时发放
    "max_invitees": 0,                # 每人最多计几次，0=不限
}
```

这种形态容易被小号刷，建议配合 `require_verified_email` 或人工审核。

### `affiliate_revshare` — 消费分成

被邀请人每次调用 API 消费，按比例持续分给邀请人。与比例返现的区别：返现只在充值那一刻发生一次，分成会随对方长期使用持续产生。

```python
CONFIG = {
    "rate": 0.05,
    "min_payout": 0.000001,   # 低于此不记账，避免大量 0 值流水
}
```

注意分成是**额外支出**，不是从对方消费里扣 —— 相当于你为拉新支付的持续成本。

### `alert_webhook` — 运维告警

订阅上面那组运维事实，按 `(事件, 键)` 去重（默认同一渠道的同一种告警 10 分钟一条），后台线程投递、5 秒超时，不阻塞请求路径。地址按域名识别形状：

| 地址 | 发出去的 JSON |
|---|---|
| `https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>` | `{chat_id, text}` |
| `https://api.day.app/<key>` | `{title, body, group}`（Bark） |
| 其它 | `{title, text, content, event, payload, site}` —— `text` 与 `content` 都给，Slack 与 Discord 各认一个 |

```python
CONFIG = {
    "url": os.environ.get("BITAPI_ALERT_WEBHOOK_URL", ""),
    "cooldown": 600,            # 同一 (事件,键) 的最短间隔(秒)
    "timeout": 5,
    "expensive_request": 0.0,   # 单次实扣超过此美元数就报;0 = 不报
    "auth_throttle": True,      # 是否报登录限速(撞库信号)
}
```

启用：`BITAPI_PLUGINS=alert_webhook` 并设 `BITAPI_ALERT_WEBHOOK_URL`。投递失败只打日志，不抛 —— 告警不能成为它要报告的那类故障。

## 更多花样

**注册送体验额度**

```python
@on("user.registered")
def welcome(user, **_):
    credit(user["id"], 0.5, reason="promo", idem_key=f"welcome:{user['id']}")
```

**首充双倍**

```python
@on("order.paid")
def double_first(order, user, **_):
    from core.portal_state import USER_DB
    prior = [e for e in USER_DB.list_ledger(user["id"], limit=500)
             if e["reason"] == "recharge"
             and e["idem_key"] != f"recharge:{order['out_trade_no']}"]
    if prior:
        return                       # 不是首充
    credit(user["id"], order["amount"], reason="promo",
           idem_key=f"first:{order['id']}")
```

**先进返佣池、需手动提现**

核心不实现"返佣池"这个概念 —— 但你可以用两条 ledger 自己表达：

```python
@on("order.paid")
def to_pool(order, user, inviter, **_):
    if inviter:
        # 记一笔 0 金额的"待提现"凭证，金额存 meta
        credit(inviter["id"], 0, reason="affiliate_pending",
               idem_key=f"pending:{order['id']}",
               meta={"pending_amount": order["amount"] * 0.2})
```

然后加一个提现接口，把 `affiliate_pending` 的 `meta.pending_amount` 汇总后 `credit(..., reason="affiliate_withdraw")`。核心不需要知道这套逻辑的存在。

**用量异常告警**

```python
@on("usage.recorded")
def alert(user, actual_cost, model, **_):
    if actual_cost > 5:
        print(f"[alert] {user['email']} 单次消费 ${actual_cost:.2f} on {model}")
```

**接自己的对账系统**

```python
@on("balance.changed")
def sync_to_erp(user_id, delta, reason, balance_after, **_):
    requests.post("https://erp.internal/ledger",
                  json={"user": user_id, "delta": delta, "reason": reason},
                  timeout=3)
```

外部调用建议设短超时 —— 插件跑在请求路径上，慢了会拖累响应。真要做重的事，插件里只入队，另起进程消费。

## credit() 参数

```python
credit(user_id, amount, reason, idem_key, meta=None) -> (ledger_row, created)
```

- `amount`：正数入账，负数扣费
- `reason`：任意字符串。约定值 `recharge` / `redeem` / `affiliate` / `admin` / `usage`，自定义的也会正常显示在流水里
- `idem_key`：幂等键，唯一索引。**必填**
- `created`：`False` 表示这个键已记账过，本次是空操作

## 调试

插件加载情况在启动日志里：

```
[bit-api] 插件已加载: affiliate_percent
```

加载失败也会打出来（不会导致启动失败）：

```
[plugins] 加载 my_broken 失败: No module named 'foo'
```

处理器抛异常时会打完整 traceback，但主流程继续：

```
[hooks] handler rebate failed on order.paid:
Traceback ...
```

单测里想隔离插件，用 `core.hooks.clear()` 清空注册表，再手动 `hooks.on(...)` 注册要测的那个。参考 `tests/test_orders.py` 的 `PluginTest`。
