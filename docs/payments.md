# 如何接支付渠道

支付渠道是注册表模式，跟 adapter 同一个路子：写一个文件实现三个方法，`register_provider()`，在管理台或环境变量里启用。**不改核心**。

下面这些环境变量是**默认值**：管理台「站点设置 → 支付设置」把同名项存进库以后，库里的值说话，存库即生效、不必重启。想回到环境变量的值，点该项的「恢复默认」（后端是 `DELETE /api/admin/settings/{key}`）——把输入框清空不是恢复默认，那是「明确设为空」= 停用。

```bash
BITAPI_PAYMENT_PROVIDERS=epay             # 逗号分隔。**默认为空 —— 不配就没有渠道**
BITAPI_SITE_URL=https://your-domain.com    # 回调与跳回地址的根，必须外网可达
BITAPI_ORDER_TTL=1800                      # 订单有效期（秒）。只认环境变量
BITAPI_MIN_TOPUP=1                         # 单笔最低充值（美元），服务端硬限
```

内置两个：

| 渠道 | 说明 |
|---|---|
| `epay` | 易支付（彩虹易支付协议），MD5 签名，国内个人站主流 |
| `mock` | 本地测试：打开链接即视为付款成功。**生产不要启用** |

`mock` 不在默认值里——它把「打开链接」当成付款成功，默认启用等于给站点开一个免费充值口。本地开发要显式写 `BITAPI_PAYMENT_PROVIDERS=mock`。管理台那条路是**硬拒**的：渠道下拉里不列 mock，直接 PATCH 也会被写入校验挡回 400。

## 内置 epay 配置

同样是默认值，管理台「支付设置」里可逐项覆盖（商户密钥只写不读——服务端只回「设了没有」和末 4 位）：

```bash
BITAPI_EPAY_URL=https://pay.example.com    # 易支付站点根
BITAPI_EPAY_PID=1001                       # 商户 ID
BITAPI_EPAY_KEY=your-merchant-key          # 商户密钥
BITAPI_EPAY_USD_RATE=7.2                   # 美元→人民币
```

项目内部以美元计价。**换算只在下单那一刻发生一次**：算出的人民币金额冻进订单行的 `pay_amount_cny`，回调金额比对直接在人民币域进行。用户充 `$10`、汇率 7.2，则应收 ¥72，到账 `$10`。

改汇率不影响在途订单——它们按下单时冻结的数执行。这也是为什么不能在回调里用汇率反算美元：乘一次再除回去，汇率中途改一次两边就永久对不上，金额校验也就不可能成立。

签名口径是**减法**：当前参数表里的一切，减去 `sign`/`sign_type`、减去空值，按 key 升序拼 `a=1&b=2`，末尾接商户密钥取 MD5。空值必须排除——可选字段（`device`/`clientip`/`cid`）「不传」与「传空串」必须产生同一个签名，否则移动端探测参数一开一关就大面积验签失败。上游回大写 hex 也认（宽容口径，`tests/test_epay_sign.py` 有断言锁着这个方向）。

## 写一个新渠道

```python
# payments/mypay.py
import hashlib
import urllib.parse

import config
from core import site_settings as S
from core.payments import NOT_PAID, PaymentProvider, register_provider


class MyPayProvider(PaymentProvider):
    name = "mypay"
    display_name = "MyPay（支付宝/微信）"

    def create_payment(self, order):
        """发起支付。order 是 orders 表的一行 dict。
        返回 {"pay_url": "https://..."}；如果渠道以别的币种收款，
        再带 "amount_cny"（应收人民币），核心会冻进订单行做回调比对基准。

        站点地址走 core.site_settings（管理台可覆盖环境变量），不要直接读
        config.SITE_URL —— 那样管理员在网页上改完地址，回调还发往旧域名。"""
        site = S.site_url().rstrip("/")
        params = {
            "merchant": config.MYPAY_ID,
            "out_trade_no": order["out_trade_no"],
            "amount": f'{order["amount"]:.2f}',
            "notify_url": f"{site}/pay/notify/mypay",
            "return_url": f"{site}/pay/return/mypay",
        }
        params["sign"] = self._sign(params)
        return {"pay_url": "https://mypay.com/pay?" + urllib.parse.urlencode(params)}

    def verify_notify(self, headers, raw_body, params=None):
        """**验签必须在这里做** —— 核心不知道各渠道的签名规则。

        三态返回，别退化成两态：
          None      验签失败/缺凭据
          NOT_PAID  验签通过但这笔没成功（用户取消、超时关闭）
          dict      验签通过且已支付，至少含 out_trade_no
        """
        if not (config.MYPAY_ID and config.MYPAY_KEY):
            return None       # 密钥为空时任何人都能用空 key 算出合法签名
        data = dict(params or {})
        if not data and raw_body:
            data = {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw_body.decode("utf-8", "replace")).items()}
        if not data:
            return None
        if data.get("sign") != self._sign(data):
            return None                       # 验签失败
        if data.get("status") != "SUCCESS":
            return NOT_PAID                   # 签名是真的，只是这笔没成功
        return {"out_trade_no": data.get("out_trade_no"),
                "trade_no": data.get("trade_no", ""),
                "amount_cny": float(data.get("amount") or 0)}

    def query_order(self, out_trade_no):
        """主动查单（回调丢失时的兜底）。返回 {"status": "paid"|"pending"} 或 None。"""
        return None

    @staticmethod
    def _sign(params):
        items = sorted((k, v) for k, v in params.items()
                       if k != "sign" and v not in (None, ""))
        raw = "&".join(f"{k}={v}" for k, v in items) + config.MYPAY_KEY
        return hashlib.md5(raw.encode()).hexdigest()


register_provider(MyPayProvider())
```

回调地址固定是 `POST|GET /pay/notify/{provider_name}`，跳回页是 `GET /pay/return/{provider_name}` —— 核心已经注册好，你不用加路由。

`verify_notify` 的 `params` 形参可选：核心会优先带上（已解析好的 query/form），你的实现也可以只接 `(headers, raw_body)`，核心会自动降级调用。

## 订单状态机

```
pending ──► paid ──► recharging ──► completed
   │         ▲
   ├─► expired ┘        （超时后才付款也能回收到 paid）
   └─► failed  ┘
```

`recharging` 这个中间态是幂等租约的支点，不要为了"简化"删掉它。

## 回调幂等：六层防线

同类项目最容易出错的地方就是回调重放导致重复到账，以及回调被伪造。核心做了六层：

1. **验签**（在你的 provider 里）—— 假回调直接挡在门外
2. **渠道闸门** —— 回调入口的渠道必须在当前启用列表里（管理台「支付设置」的值，回落 `BITAPI_PAYMENT_PROVIDERS`），且订单自己记的 `provider` 必须与入口一致。少了后半条，攻击者可以用弱渠道建单、拿单号去打强渠道的 notify。在管理台关掉一个渠道，回调与查单补账两条路一起关
3. **金额闸门** —— 实付人民币与下单时冻结的 `pay_amount_cny` 比对，容差一分钱，少付不到账
4. **`out_trade_no` 唯一索引** —— 同一订单号不可能建两次
5. **状态条件更新** —— `mark_order_paid` 只在 `pending/expired/failed` 时生效，`rowcount == 0` 即视为已处理
6. **租约乐观锁** —— `paid → recharging` 抢占并递增 `lease_version`，`completed` 时校验版本

之上还有一层：到账**不直接改余额**，而是造一张内部兑换码（`PAY-{out_trade_no}`）再兑付。兑换码的原子占用（`UPDATE ... WHERE status='unused'`）+ `credit_ledger.idem_key` 唯一索引 = 一套并发安全代码服务充值与兑码两条路径。

实测：同一回调重放 10 次，余额只加一次，返佣也只发一次。

迟到的合法回调**不设时间上界**——签名伪造不出来，能进来的就是用户真付了钱。`expired → paid` 是允许的。

## 返回值约定

`/pay/notify/{provider}` 始终返回 HTTP 200 纯文本：

- 成功处理 → `success`
- **验签通过但这笔没成功**（用户取消/超时关闭）→ 也回 `success`。这条容易漏：把它当失败回，渠道会把一笔正常失败的通知无限重试
- 未知订单 → `unknown order (ignored)`，也是 200
- 验签失败 / 渠道不匹配 / 金额不符 → 原因文本（仍 200），同时服务端 stdout 打一行带订单号和原因的拒绝日志

正确性由上面六层保证，不依赖 HTTP 状态码。`/pay/return/{provider}` 只读订单状态展示，**不是入账入口**——若它也能触发到账，用户手动构造一次 return 就成了第二条入账路径。

## 测试你的渠道

先用 `mock` 走通全链路，确认用户层没问题：

```bash
BITAPI_PAYMENT_PROVIDERS=mock python main.py
```

在控制台「钱包 → 充值」下单，点「去支付」会打开一个链接，访问即视为付款成功 —— 余额到账、返佣发放、流水可查。

然后换成你的渠道，重点验证：

- [ ] 下单能跳到支付页，金额正确
- [ ] 支付成功后回调能进来（检查 `BITAPI_SITE_URL` 外网可达、防火墙与反代放行 `/pay/notify/`）
- [ ] 验签逻辑正确（故意改一个字符应返回验签失败）
- [ ] **重放同一回调 5 次，余额只加一次**
- [ ] 订单状态最终是 `completed`（管理控制台「订单」面板可查）

epay 这条路已经有测试守着，不用手工验：`tests/test_epay_sign.py` 覆盖签名边界与查单状态映射（不发网络），`tests/e2e/test_billing_flow.py` 的 11-20 步用同一把 key 算签名打本站 `/pay/notify/epay`，覆盖坏签名、少付、外来商户号、失败通知的响应契约、重放、跨渠道、最低额、订单归属。跑法：

```bash
python -m pytest tests/test_epay_sign.py -q
python -m pytest tests/e2e -q
```

## 主动查单兜底

**回调一定会丢**（网络抖动、服务重启、`SITE_URL` 一时不可达）。这不是可选项——回调丢了而没有兜底，用户的钱扣了、额度没到，订单三十分钟后被翻成「已过期」，页面上看到的是「你没付款」。

核心每 600 秒做一跳对账，落点在 `server.py` 的 `_housekeeping_loop()`：

```python
from core.orders import reconcile_orders
credited, expired = reconcile_orders(USER_DB)   # 先查上游，后写过期
```

两个细节是有意的：

- **顺序**：先查单再过期。反过来的话，同一跳里刚被翻成 `expired` 的单要等下一跳才被查。
- **扫描范围含 `expired`**：`ORDER_TTL` 默认 1800 秒而对账 600 秒一跳，用户扫码后去吃饭、四十分钟回来才付款，订单早已过期——只扫 `pending` 的话这种单永久失联，而钱是真付了的。

管理控制台「订单」面板每行还有一个「查单」按钮（`POST /api/admin/orders/{out_trade_no}/requery`），给「用户在线催单」和「自动路径也没救回来」两种情况。它比手工调额可审计：到账走订单自己的 `recharge_code`，流水 `reason=recharge` 且带订单号；手工调额那笔 `reason=admin`，事后跟任何订单都对不上。

前端在付款窗口打开后会轮询 `GET /api/orders/{out_trade_no}`（2 秒一跳，最多 5 分钟），用户付完款回来不用手动刷新。

`query_order` 的状态映射有个坑：字段优先级是 顶层 `trade_status` → `data.trade_status` → 顶层数字 `status` → `data.status`，且 **`trade_status` 一旦存在就不再看数字 `status`**——部分易支付克隆站的 `status=1` 只表示「接口调用成功」而不是「订单已付」，只看它会给未付订单到账。这段逻辑抽成了纯函数 `payments.epay.map_query_status`，测试直接喂字典，不发网络。

## 不做的事

以下功能核心刻意不实现，需要的人自己加：

- **退款**：涉及资金流出，各渠道差异大，且要考虑已消费部分怎么算。想做的话在 provider 里加 `refund()`，然后写一条负数 ledger。
- **多商户号负载均衡**：单机场景配一个渠道够用。真要日限额，把「单笔上下限 + 日累计」放进建单校验，不引入分流层。
- **管理台增删渠道代码**：渠道的启用列表与参数（商户号、密钥、汇率、最低充值、站点地址）已经能在管理台改，存库即生效、不必重启；但**新增一个渠道仍要写文件并重启一次**——注册表只在 import 时填充，启动时把 `payments/` 下所有模块都 import 进来，「启用」只是每次调用时查一遍那份列表。做成上传即加载要连沙箱与签名一起付，单机场景不值。
- **手续费与多币种**：只收人民币、不收手续费。真要加，两条纪律先记住——向上取整、全程 decimal。
- **审计表**：幂等已经有物理载体（按单唯一的 `recharge_code` + `credit_ledger.idem_key`），拒绝记录走 stdout。现在建表就是一张只写不读的表。等第一次出现「用户说付了钱、日志已经滚掉、查不出发生过什么」的时候再建，那时它有确定的消费者。
- **二维码渠道**：`create_payment` 只承诺 `pay_url`。真有扫码渠道时再加，别为一个不存在的渠道先写前端分支。
- **订阅套餐商品**：用兑换码或插件表达。
- **发票/对账单**：`credit_ledger` + `usage_logs` 有全部原始数据，自己导出。
- **微信/支付宝拆成两个选项**：`create_payment` 的 `pay_type` 形参还没接到前端（`OrderReq` 里没有这个字段），所以渠道名写的是「易支付(支付宝)」而不是「支付宝/微信」——口径与行为一致优先。
