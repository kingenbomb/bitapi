# 如何加一个渠道（adapter）

一个 adapter 把某个上游包装成 bit-api 认识的样子。两条路：

- **OpenAI 兼容的上游（八成情况）**：不写代码。管理台「渠道」页点「新建 OpenAI 兼容渠道」，填 base_url、模型清单、模型映射（可选）、额外请求头（可选），存库即注册；再「导入 key」「测试」，最后去「模型定价」配价。见下一节。
- **别的形状（逆向、非 OpenAI 协议、图片视频）**：写一个 adapter 文件、`register_adapter()`、在 `server.py` 里 import 一行。**不需要改核心代码**。

## 账号管理

入口是管理台「号池管理」（`/portal#/admin?tab=pool`）。交互参照 sub2api 的 `AccountsView`、账号表格、批量编辑、测试与统计组件；实现继续使用当前 Vue/Naive UI、FastAPI、SQLite 和 adapter，不依赖 sub2api 的 Go 服务、Redis 或 PostgreSQL。

- 页面分为「账号池」与「Key 池」：OAuth、密码和会话凭据属于账号池，API Key 属于 Key 池；账号池默认查看 GPT。分类从现有账号的认证类型推导，不复制账号记录。列表、同条件计数、搜索、分页、导入导出和操作预览均遵守当前分类，以 `accounts` 为主体，未产生用量的账号同样可见。未加载渠道的遗留账号也保留在列表中。切换分类清空选择并回到第一页；跨页选择保存明确的账号 ID，执行前预览目标，按批返回失败项。
- 优先级数值越小越先取；账号并发、RPM、用户分组限制、到期暂停、临时暂停及总/日/7 天/5 小时成本限额都进入实际取号路径。未限定分组的账号供所有用户分组使用；管理员和维护请求不受用户分组选择限制。管理员停用标记独立于健康状态，巡检和测试不会清除它。
- `usage_logs.upstream_account_id` 记录实际调度的账号，流式工作线程共享同一请求上下文，切号/切渠道后按最后实际调用账号结算。`account_cost` 为定价原价乘账号倍率；`actual_cost` 仍按用户分组计算，不因账号倍率变更用户扣费。Token 统计包含已记录的缓存读写。旧日志保持账号字段为空，展示为未归属；不按渠道复制、均摊或伪造逐号历史。重置账号额度只移动日志水位，不删除账单。
- API 默认不返回凭据值。导出是单独的管理员操作，会包含凭据及所选账号的代理配置；普通账号、代理列表和错误信息不包含凭据。导入支持本项目备份、sub2api 账号导出、Codex `auth.json`、JSON/JSONL 和逐行凭据；裸凭据必须选择目标渠道，未安装的平台须先接入对应 adapter。预览只读，默认跳过已有账号。GPT 复用专用导入器的账户/成员/凭据去重，避免把 Team 的不同成员误合并。
- HTTP/HTTPS 账号代理接入 GPT、OpenAI 兼容 adapter 的请求和凭据刷新（含后台刷新）。代理弹窗提供名称和地址两个输入框，自动识别 `主机:端口:用户名:密码`、`用户名:密码@主机:端口`、`用户名:密码:主机:端口`、`主机:端口@用户名:密码` 及完整 URL；未带协议默认 HTTP，HTTPS 使用完整 URL。名称可自填，留空时新代理按 ID 自动生成名称，相同代理按标准 URL 去重并保留已有名称；无法唯一识别时提示使用完整 URL。「测试 GPT」通过选定代理请求 GPT 模型目录，10 秒超时，显示接口响应延迟及失败原因；不携带账号凭据，JSON 401 作为端点可达处理，网页验证或拒绝访问不判为成功。测试不改代理启用状态或账号绑定，不产生模型用量。其他 adapter 仍使用各自的网络实现，因此这些账号不能配置本页代理。
- GPT OAuth 使用 PKCE，授权会话绑定发起管理员、30 分钟有效且仅能交换一次。用户在浏览器完成登录后将完整 localhost 回调地址粘贴回管理台；凭据交换与入库在后端完成。现有账号重新授权会核对账号身份。
- GPT 列表逐号展示名称/邮箱/ID、平台/认证类型/套餐、并发和实际 RPM、状态与调度、累计请求/Token/账号与用户计费（汇总全部已归属日志，包含缓存读写）、5h/7d 用量及重置倒计时、优先级、最后使用、账号/订阅/凭据三种到期时间。用量取自 `/backend-api/wham/usage`，按窗口时长映射 5h/7d，缺失为未知而非 0%；本站窗口请求、Token 和费用仅统计对应账号的调用日志。资料取自 `accounts/check`，付费账号必要时再查 `subscriptions`，仅接受此账号选定的工作区。
- GPT 用量卡显示上游额度、Codex 积分和重置券；缺失的 5h/7d 显示未知，不绘制空条。正常请求的 `x-codex-*` 响应头会更新逐号窗口；额度达到账号阈值后暂停取号，窗口到期恢复，429 按模型记录恢复时间。上游限流与管理员停用独立，额度恢复不会解除人工停用。
- 详情「上游操作」提供重置券、Codex 邀请及操作历史。先读取资格并预览，再明确确认执行；邀请逐封发送。`gpt_actions` 保存管理员绑定的请求 ID、状态及必要的上游券标识，浏览器不接收券 ID；同一请求只提交一次，超时等未知结果阻止下一次同类操作，需人工到 Codex 核对。自动重置默认关闭，保存开启即授权该账号按阈值消费券，每次执行前重新读取用量、资格和有效期。
- 可见 GPT 行自动同步，浏览器最多同时同步两号；成功缓存 5 分钟，失败 1 分钟，可强制单号、当前页或已选账号同步。同步会按需续期凭据，不调用对话模型、不产生用户账单；部分失败保留上次成功数据，独立显示错误。关闭训练是单独操作，成功后才标记已关闭。上游网络或 Cloudflare 拦截会如实报错。
- 单号和定时测试固定发送 `hi`，展示并保存模型的文本回答，不产生用户账单，但使用上游额度；空回答视为失败。模型清单从逐账号 Codex manifest 获取，缓存 10 分钟并可手动刷新；已同步目录同时用于公开模型发现、分组过滤和实际路由。临时繁忙、超时或限流不会移除上游已列出或曾验证成功的模型；只有明确的模型不支持错误才排除，最近检测失败仍如实保留。账号编辑支持白名单及对外 ID → 上游 ID 映射。测试可手动输入其他上游模型 ID；支持情况以实际回答为准。计划与回答分别保存在 `pool_test_plans`、`pool_test_results`。
- Spark 为主账号的独立额度维度，每个主账号最多一个影子记录，无独立凭据，继承主账号代理、分组、优先级与共享并发；主账号停用或无效时影子不可调度。创建时填写实际支持的 Spark 模型 ID，不能让主账号接走影子模型；用量读取 `codex_bengalfox` 附加窗口。导出保留主账号引用和策略；只导出影子不会带出主账号凭据，导入需存在或同时导入主账号。删除主账号前需先明确删除影子。
- GPT `/v1/responses` 保留原生工具（namespace、web search、image_generation）与原生事件，不经 Chat 格式转换；`/v1/responses/compact` 支持压缩及专用模型映射。`previous_response_id` 的完整上下文按密钥和用户分组隔离、保留一小时，仅完成的响应可续接，超过 2 MB 的上下文不留存，客户端应改发完整历史。WebSocket v2 在账号编辑中开启：`ctx_pool` 每轮重连并重放完整历史，`passthrough` 保持当前客户端的上游连接；两者逐轮复用鉴权、额度和计费。未实现 sub2api 的跨客户端连接池与 Redis 分布式协调。
- 账号策略还包括 CLI 限制、原生扩展字段、兼容模式命名空间展开、图片工具、请求头标识、自定义非敏感请求头与套餐显示覆盖。HTTP 使用 curl_cffi 固定 TLS 指纹并遵循账号代理；原生 WebSocket 使用 websocket-client，代理需支持 HTTP CONNECT。`/v1/images/generations` 可传 GPT 对话模型，通过原生图片工具返回单张 `b64_json`，原生 Responses 可直接携带图片工具；未增加未经配置的独立图片模型价格。管理页账号测试可切换生图，自定义描述并直接预览、放大图片；`POST /api/admin/pool/accounts/{id}/test-image` 按指定账号流式返回进度及图片，仅长时间无有效输出时超时，不产生用户账单。测试历史保存描述和结果摘要，图片只供当次预览；生图结果不改写对话模型检测记录。实现位于 `core/account_image_test.py`。

OpenAI 兼容 Key 池也支持在同一渠道上混合发布对话模型及 `gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`。图片型号走 `POST /v1/images/generations`，保留上游 `data` 与 `usage`，使用渠道原有 Key、模型映射、代理和并发限制；不进入对话体验或自动对话巡检。文生图按 `data/pricing_seed.json` 核对的官网标准 Token 价计费：文字输入 $5、缓存输入 $1.25、图片输出 $30/百万 Token。生成结果未知时不自动重复提交。入口规则在 `core/openai_images.py`，HTTP 实现在 `OpenAICompatAdapter.images`。
- Codex PAT 通过管理页专用入口在线读取 whoami 后确认导入，清除不适用的 OAuth 续期字段。Agent Identity 可从完整 JSON 导入 Ed25519 PKCS#8 私钥、runtime ID 和可选 task ID；请求生成签名 assertion，缺少或失效的 task 按账号串行重新注册，支持密封盒 task 解密。认证值不进入普通账号 DTO。

实现入口：`routers/admin_pool.py`，规则及操作在 `core/account_management.py`，请求归属/并发/RPM 在 `core/account_runtime.py`，备份互导在 `core/account_transfer.py`，GPT 授权在 `core/gpt_oauth.py`，GPT 资料与窗口映射在 `core/gpt_profile.py`。数据库字段由 `core/db.py`、`core/user_db.py` 启动迁移，保留已有账号、凭据和日志。需要重启服务才能启用新版后端。

## 数据渠道（管理台建的 OpenAI 兼容上游）

实现是 `adapters/openai_compat.py` 的 `OpenAICompatAdapter`，`core/channels.py` 把 `channels` 表里每一行实例化并注册；代码渠道也只是它的子类（填了上游地址与默认模型）。向量渠道不声明 `CAP_CHAT`，只走 `/v1/embeddings`，模型名用上游短名。渠道形状：

- key 池型：每把 key 一个账号（`accounts.channel` = 渠道名），`Authorization: Bearer <key>` 由渠道自己生成；取号 / 切号 / 失败分类由核心负责
- `stateless_keys = True`：key 没有本地状态可判，上游抖动只轮换不罚号，只有 401/402/403 让 key 退场
- `billing_mode = upstream`：采信上游 usage；流式强开 `stream_options.include_usage`
- **流式原样透传**（`streaming = True` + `stream_chat`）：上游 SSE 一帧不改地转给客户端，只把 `model` 字段从上游名改回对外名。tool_calls 增量、usage、finish_reason 全在。此前 key 池渠道走 `chat(stream=True, on_token=…)` 的文本通道，那条路上工具调用没有位置 —— agent 客户端拿到的是没有 tool_calls 的回答
- 非流式返回 `{"raw": 上游完整 JSON}`，`model` 同样改回对外名
- 上游非 2xx 抛 `UpstreamError(code, body)`：`.code` 给失败分类，正文给管理台「测试渠道」显示原话

校验（`core/channels.validate`）：渠道名 `[a-z0-9_-]{2,32}` 且不能撞代码渠道；模型名可以与别的渠道重名（见下一节的路由），但不能撞任何渠道名（渠道名本身也可作为 model 路由）；`Authorization` / `Content-Type` 不能出现在额外请求头里。渠道名建后不可改：号池挂在名字上。删渠道连号池一起删；临时停用请用「下线」。

## 一模型多渠道：优先级、权重、失败换渠道

同一个模型名可以挂在多个渠道上（代码的、数据的都算）。`core/adapter.model_routes(model)` 给出一次请求该依次尝试的渠道序列：

- 按渠道的 **priority** 从高到低分档；同档内按 **weight** 随机分流（weight 0 的排到该档末尾只当兜底）
- 缺省 priority 0、weight 1 —— 什么都不配就是「挂在同一模型上的渠道均分」
- 下线的渠道不进序列

网关沿序列从头试。换渠道的信号只有三种：**没号**（503）、**换遍号仍失败**（502）、**上游限流**（429）；400 / 403 这类是这次请求本身的问题，换渠道也一样，直接返回。非流式在任何一个渠道拿到回答就停；流式**一帧都没吐才换**，已经开始往客户端写的流不换上游 —— 那会拼出一份两头缝的回答。普通型渠道的 role 帧由网关在取号前先发、整条流只发一次；透传型渠道的 role 帧在上游第一帧里，从普通型失败切到透传型会出现两个 role 帧，那是这种切换的代价。

计费记**实际作答的渠道**（`usage_holder["channel"]` / `served["channel"]`），不是请求开始时猜的首选。`/v1/models` 与模型广场对同一模型只列一次，`owned_by` / `channel` 是确定性的首选渠道（`primary_channel`：最高 priority，同档取 weight 最大，再同则注册顺序），广场卡片另给 `channels` 全量。

priority / weight 在管理台「渠道」页每行就地改，存进 `settings.channel_routing`（`{渠道名: {priority, weight}}`），立刻推进注册表；环境变量 `BITAPI_CHANNEL_ROUTING` 是 JSON 形式的部署默认值。端点 `PATCH /api/admin/channels/{name}/routing`。门禁：`tests/test_routing.py`。

端点：`GET/POST /api/admin/channels`、`PATCH/DELETE /api/admin/channels/{name}`、`POST /api/admin/channels/{name}/keys`（导 key，与 `/admin/import-keys` 同一份去重）、`POST /api/admin/channels/{name}/test`（取一把号发一句话，不计费）。门禁：`tests/test_openai_compat.py`、`tests/test_channels.py`。

## 最小实现（代码渠道）

```python
# adapters/myprovider.py
import json
import urllib.request

import config
from core.adapter import CAP_CHAT, BILL_UPSTREAM, Adapter, register_adapter


class MyProviderAdapter(Adapter):
    name = "myprovider"                  # channel 名，唯一
    capabilities = [CAP_CHAT]            # 声明能力，调度器据此决定巡检什么
    billing_mode = BILL_UPSTREAM         # 上游返回标准 usage → 直接采信
    models = ["mp-fast", "mp-pro"]       # 对外暴露的模型名
    columns = [                          # 号池面板显示哪些列
        {"key": "identity", "label": "账号", "type": "text"},
        {"key": "status", "label": "状态", "type": "text"},
    ]

    def chat(self, acct, messages, stream=False, model=None, on_token=None, body=None):
        """用某个账号发一次请求。

        非流式 → 返回 {"raw": 上游完整 OpenAI JSON}（含 usage/tool_calls）
        流式   → 逐段调 on_token(text, kind)，kind ∈ {"content", "reasoning"}，返回 None
        """
        key = (acct.get("secret") or {}).get("api_key")
        payload = dict(body or {})
        payload["model"] = model
        payload.setdefault("messages", messages)
        if stream:
            payload["stream"] = True
            # 强开 include_usage，否则流式拿不到真实 token 数
            opts = dict(payload.get("stream_options") or {})
            opts["include_usage"] = True
            payload["stream_options"] = opts

        req = urllib.request.Request(
            "https://api.myprovider.com/v1/chat/completions",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=config.SEND_TIMEOUT)

        if not stream:
            try:
                return {"raw": json.loads(resp.read().decode())}
            finally:
                resp.close()

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                evt = json.loads(data)
                delta = ((evt.get("choices") or [{}])[0].get("delta") or {})
                if delta.get("content"):
                    on_token(delta["content"], "content")
                if delta.get("reasoning_content"):
                    on_token(delta["reasoning_content"], "reasoning")
        finally:
            resp.close()
        return None


register_adapter(MyProviderAdapter())
```

然后在 `server.py` 的 adapter import 区加一行：

```python
import adapters.myprovider  # noqa: F401
```

## 四种 adapter 形态

| 形态 | 标记 | 适用 | 实现 |
|---|---|---|---|
| **普通型** | 默认 | 直接调上游 HTTP，只要文字 | `chat()`；流式经 `on_token(text, kind)` 文本通道，**没有 tool_calls** |
| **透传型** | `streaming = True` | 上游本身就是 OpenAI 兼容 SSE，要保留 tool_calls / usage | 非流式 `chat()` 返回 `{"raw": …}`；流式 `stream_chat(acct, model, body, on_sse)` 逐帧原样转 —— 继承 `OpenAICompatAdapter` 通常一行代码都不用写 |
| **转发型** | `proxy = True` | 上游已是完整网关（自带号池/重试） | `proxy_chat(body, stream, on_sse)` 原样透传，**不经号池** |
| **生成型** | `kind = "generation"` | 图片/视频，走 `/v1/images\|videos/generations` | `generate(acct, model, prompt, size, n)` 返回**上游 CDN 的 URL 列表** |

key 池 + OpenAI 兼容的上游一律用透传型（数据渠道就是它）；普通型留给上游只给文字、要自己拼帧的逆向渠道。

生成型的 `generate` 只管返回上游 CDN 的 URL，**不用自己托管**。网关（`server._rehost`）会把每个 URL 的字节抓到自家 `/media/gen/<token>` 下再给客户端 —— 上游链接常带 referer 防盗链或很快过期，直接透传等于给客户端一串打不开的链接。默认返回自家域名的 `url`；客户端传 `response_format: "b64_json"` 时改内联 base64（与 OpenAI 一致）。缓存文件按 `BITAPI_MEDIA_TTL`（默认 30 分钟）由 housekeeping 清理，是短期分发不是长期图床。若 adapter 的出图 URL 需要 referer 才能抓，在类上声明 `media_referer = "<站点根>/"`。

转发型只需把 SSE 原样吐回来，核心会自己嗅探 usage：

```python
class MyProxyAdapter(Adapter):
    name = "myproxy"
    proxy = True
    capabilities = [CAP_CHAT]

    def proxy_chat(self, body, stream=False, on_sse=None):
        resp = urllib.request.urlopen(...)
        if stream:
            for raw in resp:
                on_sse(raw.decode("utf-8", "replace"))
            return None
        return json.loads(resp.read().decode())
```

## capabilities：声明你需要什么维护

| 常量 | 含义 | 要实现 |
|---|---|---|
| `CAP_CHAT` | 支持对话 | `chat()` 或 `proxy_chat()` |
| `CAP_REFRESH` | token 会过期 | `refresh_token(acct)` → `{token, token_exp}` |
| `CAP_BALANCE` | 有余额/额度概念 | `balance(acct)` → float 或 None |
| `CAP_HEALTH` | 支持存活检测 | `health(acct)` → bool |
| `CAP_REGISTER` | 支持自动注册补号 | `register()` → `{identity, secret, meta}` |

巡检器（`core/scheduler.py`）读这些标记决定做什么：声明了 `CAP_REFRESH` 才会去刷 token，声明了 `CAP_BALANCE` 才会查余额。**没声明的一律不碰** —— 这就是万级号池 CPU 只占 35% 的原因。

`proxy = True` 或实现了 `sync_pool()` 的 adapter 会跳过逐号网络巡检（健康由上游引擎负责）。

## billing_mode：告诉计量层 token 从哪来

这个字段管**计量**（token 数怎么得到），跟定价表里的 `billing_mode`（钱怎么算）是两件事。

| 值 | 适用 | 行为 |
|---|---|---|
| `BILL_UPSTREAM` | 官方 API、正规反代 | 采信上游 usage |
| `BILL_ESTIMATE`（默认） | 逆向渠道，上游不给 usage | tiktoken 本地估算 |
| `BILL_PER_REQUEST` | 图片/视频生成 | tokens 记 0，按次计费 |

即便标了 `BILL_ESTIMATE`，只要某次上游意外给了有效 usage，核心也会优先采信 —— 所以标错方向的代价很小。

## stateless_keys：上游抖动别罚号

默认 `False`。上游报错时核心按性质落状态：`429` 只轮换，`401/402/403` 与凭据/额度类报错让号退场，**其余一切落 `cooldown`** —— 要等下一轮巡检（默认 30 分钟）才回场。

透传型 key 池（一把长期有效的 key，没有 token、没有余额、没有配额）该声明 `stateless_keys = True`。这种渠道里我们手上没有任何「这把 key 的状态」可判，只有「上游此刻怎么样」，而这两件事经常被同一个报错码盖住：实测同一把好 key 会先回 `502 Model gateway is unavailable`、再回 `400 Unsupported model`，一分钟后自愈（模型是动态挂载的）。落 cooldown 等于一次抖动 park 整个渠道半小时。

声明之后，除 `terminal` 外的失败（含空回复）都只推进 `last_check` 换下一把号，状态不动。`401/402/403` 不在豁免范围 —— 那说的就是这把 key 本身不行。

判据是「有没有本地状态可判」，不是「是不是 key 池」：一旦给这个渠道加了余额或存活巡检，豁免就该收回。行为口径由 `tests/test_pool_failure.py` 与 `tests/test_gateway_stateless_keys.py` 守着。

## 账号怎么进池

三条路，选一条：

**1. 批量导入 API key**（key 池型渠道）

管理台「渠道」页每一行都有「导入 key」；脚本用管理密钥：

```bash
curl -X POST http://127.0.0.1:8080/admin/import-keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"channel":"myprovider","keys":["k1","k2","k3"]}'
```

两条入口共用 `core/pool_state.import_keys` —— identity 由渠道的 `key_identity(key)` 派生（脱敏），同 identity 跳过。

**2. 注册脚本上报**（自动注册补号）

```bash
curl -X POST http://127.0.0.1:8080/admin/accounts \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"channel":"myprovider","identity":"a@b.com",
       "secret":{"password":"x"},"token":"...","token_exp":1799999999}'
```

**3. 实现 `sync_pool(db)`**（号在文件系统或外部系统里，adapter 自己同步进 DB）

文件型号池渠道的做法：号是磁盘上的 JSON 文件，用 mtime 做增量同步、批量 upsert，万级文件不卡 CPU。

### GPT / Codex OAuth 导入

管理员控制台「渠道与资源」→「导入 GPT 账号」（或 gpt 行的「导入账号」）。
支持粘贴多行 accessToken、Codex `auth.json`、ChatGPT session JSON、JSON 数组、
JSONL，以及带 `accounts` 的 sub2api 导出文件；也可一次选择多份 `.json/.jsonl/.txt`。
GPT 另支持每行 `邮箱----密码----二步验证密钥----access_token` 四段文本，仅提取邮箱与 access_token，密码和二步验证密钥不入库；账号池导入可自动识别此格式为 GPT。
每批最多 1000 个账号、10 MB。返回逐条新增/更新/跳过/失败与警告。

- `tokens.access_token` / `access_token` / `accessToken` 均可；JWT 只提取身份和期限，
  不验证签名，也不等于验活。`sessionToken` 不当作 OAuth `refresh_token`。
- 默认跳过已有账号；需要更新时勾选「覆盖已有」。同 Team 不同成员、同邮箱不同
  account_id 不合并。覆盖时缺失的 refresh_token 从数据库当前凭据保留。
- token 已过期且没有 refresh_token 会拒绝；有 refresh_token 时由 bit-api 自动续期。
- 普通 AT 既无 refresh_token、也没有可解析的到期时间时拒绝入池，请使用完整 `auth.json`。
- API key 应导入 OpenAI 兼容渠道；此入口不支持 agent identity 凭据。

`adapters/gpt.py` 是**原生号池型渠道**（`proxy=False`、`CAP_REFRESH`）：
`/v1/chat/completions`、`/v1/messages`、`/v1/responses` 复用既有协议、路由和计费，
`core/codex.py` 把 Chat 转成 Codex Responses，再由本站直连
`https://chatgpt.com/backend-api/codex/responses`。不需要安装其它转发项目。
Chat、Responses（含关闭扩展透传的账号）和 Anthropic 兼容入口保留客户端的
`prompt_cache_options`、`prompt_cache_key`、`prompt_cache_retention` 及内容块的
`prompt_cache_breakpoint`；带断点的 system/developer 不折叠到纯文本 instructions。
是否支持由实际上游决定：Codex OAuth 可能拒绝新缓存选项，此时非流式返回 HTTP 400，
已建立的流返回带 `status_code=400` 的错误事件，不伪装成回答或成功终态；
不删除参数重试、不把参数错误计入账号冷却，也不产生用户扣费。未报告的缓存写入量不估造。

Web 生图使用模型 `gpt-image-2-web`，请求 `POST /v1/images/generations`，
传 `prompt`、`n: 1`、`response_format: "b64_json"`；仅支持文字生成单张图片。
可选 `size` 为 `auto`、`1024x1024`、`1024x1536`、`1536x1024`，作为尺寸提示发送，
上游不保证精确像素；暂不支持其它图片参数。管理台「账号测试 → 生图测试 →
ChatGPT Web 生图」使用指定账号，直接预览、放大结果。
Web 链路直接复用现有 GPT 主账号的 access_token、刷新锁、分组、代理、并发和金额额度，
不需要浏览器登录或额外服务；不使用 PAT、Agent Identity 或 Spark 影子账号。
Codex 的冷却状态及用量窗口不代替 Web 生图资格，手动停用、临时暂停、账号到期仍有效。
Web 检测结果单独写入账号 `meta.gpt_web_image`，不清除 Codex 错误、不激活被暂停的账号；
Cloudflare 网络校验会明确报错，不当作 token 失效。已提交的生成请求不会自动换号重发。
Web 不返回真实 token usage；输入文字沿用本站文本估算，输出按实际图片尺寸分成 32×32 像素块，
auto 质量按上游项目的中等质量口径估算：`ceil(ceil(宽/32) × ceil(高/32) × 33/32)`。
`gpt-image-2-web` 默认引用 `gpt-image-2.5-flare` 的同分组价格（输入 $5、图片输出 $30/百万 Token），
复用分组倍率与扣费规则，账单计量来源为“估算”，API 返回 `usage.token_source=estimate`。
账单快照保留参考模型、估算方法及实际尺寸；管理台生图测试仍不扣用户余额，失败请求不收费。
协议代码在 `core/gpt_web/`，改编自 MIT 项目
[chatgpt2api](https://github.com/basketikun/chatgpt2api/tree/92dd80e91543157d36a4ddad60e765c5293760e9)，
保留目录内 LICENSE。`BITAPI_GPT_WEB_IMAGE_UPSTREAM_MODEL` 可覆盖网页上游会话模型。
Web HTTP 与 SSE 使用一致的 Chrome 152 TLS 指纹和请求头（`curl_cffi==0.17.0`），
通过 HTTP 首页请求获取网络 Cookie 与当前脚本信息后再准备生图；不依赖浏览器或额外校验服务。
Codex 请求继续使用原有 Chrome 136 指纹。

凭据唯一真源是现有 `accounts` 表的 `secret` / `token` / `token_exp`。
本站使用官方 Codex client_id，向 `https://auth.openai.com/oauth/token` 续期；
轮换后的 refresh_token 在解锁前写回数据库，取号器和后台巡检复用同一实现。
同账号并发刷新读取最新凭据；取号阶段的凭据刷新共享 30 秒预算，同一次取号中每号只刷新一次。
后台巡检遇临时刷新故障保留账号状态；冷却账号需通过真实上游鉴权查询后才恢复可用。
HTTP、WebSocket、账号测试与生图共用账号编辑页的并发值，0 表示不限，Spark 影子号共享主号容量。
先选择有空闲容量的账号，全部繁忙时进入有界排队，默认最多 30 秒、每号最多等待 100 个请求。
排队请求不计入实际在途数量；原 `BITAPI_GPT_KEY_CONCURRENCY` 隐藏闸门已移除。

上游请求固定 `stream=true`、`store=false`；非流式收集完成事件，流式逐帧转换
文本、reasoning、function tools 和 usage（包括缓存 token），异常 EOF 不伪造完成。
GPT OAuth 的 HTTP 与原生 WebSocket 共用请求策略：持续有有效输出不限制总时长，
连续 180 秒无有效输出才断开，心跳不续期；不再套用通用的 60/90 秒首帧预算。
首输出和高推理首输出默认不限时（可分别配置），响应头默认仅受空闲上限约束。
上游明确过载在尚无有效输出时同号最多重试 3 次，遵循 Retry-After（最多 8 秒）；
已经开始输出则不重放。重试仍是一笔本站请求和结算；管理页测试保留独立 30 秒预算。
首输出前的事件按 sub2 使用 8 MB 暂存上限，超过 64 KB 转入随请求关闭的临时文件；
达到本站暂存上限只终止本次请求，不冷却账号。
过载不冷却账号；流输出超时默认也不改变账号调度。可显式开启超时惩罚，
10 分钟内累计 3 次后暂缓调度 5 分钟，届时自动恢复；认证和额度错误仍按原规则处理。
尚无输出时，同号重试耗尽后可切换账号：普通故障默认最多切换 10 次，首输出超时最多切换 1 次，
OAuth 429 最多尝试 3 个账号；已经输出后所有入口均停止重试和换号。
429 只有窗口达到 100% 或上游明确给出额度重置时间才立即限流到重置；瞬时 429 先在同号
最多退避重试 2 分钟，遵循 Retry-After（每次最多 8 秒），用尽后短暂避让 5 秒再尝试其他账号。
管理页 30 秒测试预算同时约束排队和重试。400 等请求错误不处罚账号，401/403 等凭据错误
在 Chat、Anthropic、Responses 和 WebSocket 中按同一规则落状态。失败、空闲超时和首输出超时
保留日志但不扣用户余额或密钥额度；最终成功的换号请求只结算一次，归属实际回答的账号。
客户端仍使用 bit-api 原有协议入口，Responses 入口维持既有无状态/function-tools 契约。

配置见 [`.env.example`](../.env.example)：`BITAPI_GPT_BASE_URL`、
`BITAPI_GPT_TOKEN_URL`、`BITAPI_GPT_CLIENT_VERSION`、`BITAPI_GPT_ACCOUNT_WAIT_TIMEOUT`、
`BITAPI_GPT_MODELS`；默认直接使用官方端点。导入成功表示凭据已保存，
在 gpt 行点「测试」确认所选账号/套餐/模型能真实响应。

脚本入口 `POST /admin/gpt/import`（管理密钥或管理员 JWT），控制台入口
`POST /api/admin/gpt/import`（管理员 JWT），共用同一实现：

```json
{"content":"粘贴内容","contents":["文件一内容","文件二内容"],"force":false}
```

也接受单个 OAuth JSON、数组或 `{"accounts":[...]}`。默认模型为
`gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna`，价格沿用既有
`kg-gpt-5.6-*` 种子；实际可用性以账号套餐为准。

## 让用户能用上新模型

模型名 → channel 的映射由 `adapter.models` 自动建立。用户能否调用还要过两道：

1. **分组白名单**：`groups.supported_models`（支持 `mp-*` 通配或 `*` 全放开）
2. **定价**：见 [pricing.md](pricing.md)。没配价的模型按免费计，不会拒绝请求

## 检查清单

- [ ] `name` 唯一，`models` 里的名字不与其他 adapter 冲突
- [ ] 流式强开了 `include_usage`
- [ ] `chat()` 非流式返回 `{"raw": ...}` 而不是裸字符串（这样 usage/tool_calls 不丢）
- [ ] `capabilities` 只声明真正实现了的
- [ ] `billing_mode` 与上游是否给 usage 一致
- [ ] 上游报错时抛异常（核心会切号重试），别静默返回空
- [ ] 抛出的异常带得上上游状态码（`e.code`），失败分类靠它区分「key 坏了」和「上游此刻不好」
- [ ] key 长期有效、无 token/余额/配额的渠道声明了 `stateless_keys = True`
