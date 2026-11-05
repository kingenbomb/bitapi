# 如何配定价

bit-api 用**结对单价制**：直接填真实美元单价，不用倍率制。理由是配置时手边是官网价目表，上面写的是 `$4` 而不是"2 倍"；倍率制要人肉做除法，而且同一模型的输入输出倍数常常不一致（比如 gpt-5.6-sol 输入翻倍但输出只 1.5 倍），事后也无法从配置反推真实价格。

**单位**：token 类为 **美元 / 1M token**；`per_request` 类为 **美元 / 次**。

## 解析链

查价从上往下，**命中即返回**：

```
1. model_pricing 表 · 分组专属 · 精确模型名
2. model_pricing 表 · 分组专属 · prefix* 通配（多个匹配时取最长前缀）
3. model_pricing 表 · 全局(group_id 为空) · 精确名 → prefix*
4. LiteLLM 价目表（定时拉取的本地 JSON，官方模型自动有价）
5. 缺价：在调用上游前返回 400
```

路由、查价与记账统一使用模型的规范名称，兼容大小写和 `[1M]` 展示后缀。
**缺价不能隐式免费**：须补齐价格，免费模型也须显式配置 `billing_mode=free`。
缺价模型不在模型广场展示。

配好之后在管理控制台的「定价列表」能看到所有条目及其生效范围。

`gpt-image-2-web` 未单独配置价格时引用同分组 `gpt-image-2.5-flare` 的价格；
修改 Flare 定价也会影响 Web 生图。Web 返回的 Token 用量由实际图片尺寸估算，
账单标注“估算”，快照保留参考模型与尺寸，不会显示为上游实测用量。

## 三种计费模式

| `billing_mode` | 公式 | 适用 |
|---|---|---|
| `token` | 各类 token × 各自单价 / 1e6 | 对话模型 |
| `per_request` | `per_request_price × units` | 图片/视频生成 |
| `free` | 0 | 白名单模型、内部测试 |

```
actual_cost = cost × group.rate_multiplier
```

`rate_multiplier` 只做折扣（VIP 组 0.8 之类）。`usage_logs` 同时存 `cost`（原价）和 `actual_cost`（折后），所以调整倍率后历史账仍能解释。

## 长上下文阶梯价

缓存读写按上游实际报告值分别计费。支持 OpenAI Chat 的
`prompt_tokens_details.cached_tokens/cache_write_tokens`、Responses 的
`input_tokens_details.cached_tokens/cache_write_tokens`，以及 Anthropic 的
`cache_read_input_tokens/cache_creation_input_tokens`。OpenAI 总输入包含缓存，
结算先扣除缓存部分再分别计价；转为 Anthropic 响应时同样只把普通输入放进 `input_tokens`。
未上报的缓存量记为 `null`，日志显示 `—`；明确上报的 `0` 保留为 `0`。
旧记录中无法区分来源的写入 `0` 显示为未知，不再从首次缓存命中推算写入，也不据此补扣历史费用。

OpenAI `cache_write_tokens_details.ttl_30m_tokens/ttl_12h_tokens` 在 Chat、Responses、
Anthropic 兼容入口与流式响应间保留。30 分钟按配置的缓存写价，12 小时按该价的 2 倍；
长上下文和分组定价先选出实际生效的基础写价，再计算时长档，最后乘分组倍率。
这对应官方 1.25× / 2.5× 普通输入价的比例，但不会覆盖本站已有的自定义单价。
无分档、分档不完整或超过写入合计时，只按实际写入合计和基础写价结算；不根据请求 TTL 猜计量。
只有完整分档且未提供合计时，才从两个上游计数相加取得写入合计。
`cache_usage_version=3` 快照冻结分档有效性、各档 token、未分档余额与两档实际单价，日志可复算；历史账不重计。

部分模型的单价随请求总量跳档：**总 token 超过阈值，本次请求的全部 token 都换用贵的那套单价**（不是分段累进，不像个税）。

判定量：

```
total_ctx = input_tokens + cache_write_tokens + cache_read_tokens   # 不含 output
```

判定用**严格大于** —— 恰好等于阈值仍走便宜档。

五个字段，绝大多数模型留空：

| 字段 | 含义 |
|---|---|
| `long_threshold` | 阈值（token）。0 或留空 = 不启用阶梯 |
| `long_input_price` | 超阈值后的输入单价 |
| `long_output_price` | 超阈值后的输出单价 |
| `long_cache_read_price` | 超阈值后的缓存读单价 |
| `long_cache_write_price` | 超阈值后的缓存写单价 |

**逐项回落**：配了阈值但某项长档单价留空时，该项单独回落到普通单价，**绝不算成 0**。所以只想给输入输出配阶梯、缓存维持原价，是被支持的。

`pricing_snapshot` 里会记 `tier`（`official` / `long`）、`total_ctx` 与本次实际生效的四项单价 —— 用户在使用记录里能看到「长文本」标记，出争议时可精确复算。

## 一键导入内置阶梯价

`data/pricing_seed.json` 附了 16 条真实定价（gpt-5.6 系列、minimax-m3、gemma-4、jina 向量等），来自生产环境实测。管理控制台「定价列表」里点「导入内置阶梯价」即可。

也可以用 API：

```bash
curl -X POST http://127.0.0.1:8080/api/admin/pricing/import \
  -H "Authorization: Bearer $JWT" \
  -d "$(cat data/pricing_seed.json)"
```

## 手动配一条

管理控制台的「新增 / 修改定价」表单，或：

```bash
curl -X POST http://127.0.0.1:8080/api/admin/pricing \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{
    "model_pattern": "kg-gpt-5.6-sol",
    "group_id": null,
    "billing_mode": "token",
    "input_price": 5, "output_price": 30,
    "cache_read_price": 0.5, "cache_write_price": 6.25,
    "long_threshold": 272000,
    "long_input_price": 10, "long_output_price": 45,
    "long_cache_read_price": 1, "long_cache_write_price": 12.5
  }'
```

按 `(model_pattern, group_id)` upsert，重复提交是修改而非新增。

## 导出与备份

```bash
curl http://127.0.0.1:8080/api/admin/pricing -H "Authorization: Bearer $JWT"
```

返回的 `pricing` 数组可直接喂回 `/api/admin/pricing/import` —— 往返格式一致，五个阶梯字段不丢。管理页也有「导出 JSON」按钮。

## LiteLLM 价目表

官方模型（`gpt-*`、`claude-*`、`gemini-*` 等）的价不用手配，价目表兜底。

```bash
BITAPI_PRICING_URL=https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
BITAPI_PRICING_PATH=./data/litellm_pricing.json   # 落盘位置
BITAPI_PRICING_REFRESH=86400                      # 刷新间隔（秒），0=不自动刷
```

启动时优先读本地文件，没有则试拉一次；拉不到静默降级（那些模型按免费计），**不影响启动**。价目表不进数据库，所以定价的导入导出保持干净 —— 库里只有你手配的部分。

想完全离线：把 `BITAPI_PRICING_URL` 设为空，全部手配。

## 三种计费策略（分组级）

定价决定"一次请求值多少钱"，策略决定"这钱怎么收"。在分组上设 `billing_policy`：

| 策略 | 预检 | 结算 |
|---|---|---|
| `balance` | `balance <= 0` → 402 | 扣 `users.balance`（走 credit_ledger） |
| `quota` | 日/周/月用量超限 → 429 | 只记日志，不扣钱 |
| `free` | 只查并发上限 | 只记日志，不扣钱 |

`balance` 策略**允许透支**：本次请求可以把余额扣成负数，保证进行中的请求能完成；下一次请求由预检拦住。这是刻意的取舍 —— 见 README 的「关键设计决策」。

## 常见问题

**改了价，为什么没立即生效？**
定价有秒级内存缓存（`BITAPI_PRICING_CACHE_TTL`，默认 30s）。通过管理 API 改价会自动失效缓存；直接改数据库则要等 TTL 过期或重启。

**通配符怎么优先？**
精确名 > 最长前缀 > `*`。所以 `kg-gpt-*` 会盖过 `kg-*`，`kg-*` 会盖过 `*`。

**逆向渠道拿不到真实 token，定价还有意义吗？**
有。核心用 tiktoken 本地估算 input/output，估算值参与计价。`usage_logs.token_source` 标 `estimate` 与 `upstream` 区分，用户能看出哪些是估算。

**怎么让某个模型只对 VIP 组便宜？**
配一条 `group_id` 指向 VIP 组的定价，它会盖过全局那条。
