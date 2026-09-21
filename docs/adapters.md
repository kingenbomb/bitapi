# 如何加一个渠道（adapter）

一个 adapter 把某个上游包装成 bit-api 认识的样子。两条路：

- **OpenAI 兼容的上游（八成情况）**：不写代码。管理台「渠道」页点「新建 OpenAI 兼容渠道」，填 base_url、模型清单、模型映射（可选）、额外请求头（可选），存库即注册；再「导入 key」「测试」，最后去「模型定价」配价。见下一节。
- **别的形状（逆向、非 OpenAI 协议、图片视频）**：写一个 adapter 文件、`register_adapter()`、在 `server.py` 里 import 一行。**不需要改核心代码**。

## 数据渠道（管理台建的 OpenAI 兼容上游）

实现是 `adapters/openai_compat.py` 的 `OpenAICompatAdapter`，`core/channels.py` 把 `channels` 表里每一行实例化并注册。向量渠道也走这个类：不声明 `CAP_CHAT`，只走 `/v1/embeddings`，模型名填上游认的短名（有的上游带厂商前缀会被拒收）。渠道形状：

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

透传型 key 池（一把长期有效的 key，没有 token、没有余额、没有配额）该声明 `stateless_keys = True`。这种渠道里我们手上没有任何「这把 key 的状态」可判，只有「上游此刻怎么样」，而这两件事经常被同一个报错码盖住：实测有的网关会对同一把好 key 先回 `502 Model gateway is unavailable`、再回 `400 Unsupported model`，一分钟后自愈（模型是动态挂载的）。落 cooldown 等于一次抖动 park 整个渠道半小时。

声明之后，除 `terminal` 外的失败（含空回复）都只推进 `last_check` 换下一把号，状态不动。`401/402/403` 不在豁免范围 —— 那说的就是这把 key 本身不行。

判据是「有没有本地状态可判」，不是「是不是 key 池」：一旦给这个渠道加了余额或存活巡检，豁免就该收回。行为口径由 `tests/test_pool_failure.py` 与 `tests/test_gateway_stateless_keys.py` 守着。

## 账号怎么进池

三条路，选一条：

**1. 批量导入 API key**（key 池型渠道，如 NVIDIA 的 242 个 key）

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

若要接一个有外部号源的渠道（比如号是一批本地文件），可以让 adapter 提供 `sync_pool(db)`：用 mtime 做增量同步、批量 upsert，万级文件也不卡 CPU，巡检会跳过它的逐号网络探测。

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
