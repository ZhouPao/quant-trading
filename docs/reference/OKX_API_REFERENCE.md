# OKX V5 API 速查参考

> 文档版本：v1.0
> 创建日期：2026-10-17
> 用途：本项目的开发参考，不替代官方文档
> 官方文档：[https://www.okx.com/docs-v5/en/](https://www.okx.com/docs-v5/en/)

---

## 1. REST API 基础

| 项 | 值 |
|---|---|
| 基础域名 | `https://www.okx.com` |
| API前缀 | `/api/v5/` |
| 返回格式 | JSON，统一信封 `{"code": "0", "msg": "", "data": [...]}` |
| `code: "0"` | 成功 |
| `code != "0"` | 错误，查 `msg` |
| `data` | 始终为数组，即使单对象 |

### 模拟盘

- 同域名，请求头加 `x-simulated-trading: 1`
- 模拟盘与实盘的 `instIdCode` 不同，必须独立拉取 instrument 配置
- 模拟盘Key与实盘Key**严禁复用**

---

## 2. WebSocket 基础

| 频道类型 | 地址 |
|---|---|
| 公开频道 | `wss://ws.okx.com:8443/ws/v5/public` |
| 私有频道 | `wss://ws.okx.com:8443/ws/v5/private` |

### 订单簿频道粒度

| 频道 | 频率 | 类型 | 权限 |
|---|---|---|---|
| `bbo-tbt` | 10ms | 快照 | 公开 |
| `books5` | 100ms | 快照 | 公开 |
| `books` | 100ms | 增量 | 公开 |
| `books-l2-tbt` | 10ms | 增量 | 需登录 + VIP5+ |
| `books50-l2-tbt` | 10ms | 增量 | 需登录 + VIP4+ |

> 若深度在间隔内变化 A→B→A，不会发送更新。长时间无更新时，快照频道会重推当前深度，增量频道会推"无变化"消息以保持心跳。

---

## 3. 认证机制

### 3.1 三种 API Key 权限

| 权限 | 用途 |
|---|---|
| `Read` | 查询账户、持仓、订单、市场数据 |
| `Trade` | 下单、撤单、改单 |
| `Withdraw` | 提币 — **本项目的实盘Key严禁开启** |

### 3.2 Key 安全规则

- 每Key最多绑定 **20 个IP**
- 未绑IP的 Trade/Withdraw Key **14天不活跃会过期**
- 模拟盘Key与实盘Key必须**完全分离**
- 模拟盘Key在 OKX 网页"模拟盘 → API"独立创建

### 3.3 私有 REST 请求的 4 个 Header

| Header | 值 |
|---|---|
| `OK-ACCESS-KEY` | API Key |
| `OK-ACCESS-SIGN` | Base64(HMAC_SHA256(prehash, SecretKey)) |
| `OK-ACCESS-TIMESTAMP` | ISO 8601 UTC 毫秒，如 `2024-01-15T10:30:00.123Z` |
| `OK-ACCESS-PASSPHRASE` | 创建Key时设的passphrase |
| `Content-Type` | `application/json`（POST必加） |

### 3.4 签名公式

```
prehash = timestamp + METHOD(大写) + requestPath(含query) + body(GET为空串)
signature = base64(hmac_sha256(prehash, secret_key))
```

**字段说明**：
- `timestamp`：与 `OK-ACCESS-TIMESTAMP` 同值，必须 ISO 8601 UTC 毫秒精度
- `METHOD`：`GET` / `POST` / `DELETE` 等，大写
- `requestPath`：含 query string，如 `/api/v5/market/ticker?instId=BTC-USDT`
- `body`：POST 的 JSON 字符串；GET 为空字符串 `""`

### 3.5 时间约束

- timestamp 与服务端时差必须 **< 30 秒**
- 启动时应先 `GET /api/v5/public/time` 校时
- 客户端建议每 5 分钟校时一次

### 3.6 Python 签名示例

```python
import hmac, hashlib, base64
from datetime import datetime, timezone

def get_timestamp() -> str:
    """ISO 8601 UTC 毫秒"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def sign(timestamp: str, method: str, path: str, body: str = '', secret: str = '') -> str:
    prehash = timestamp + method.upper() + path + body
    mac = hmac.new(secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def build_headers(method: str, path: str, body: str = '',
                  api_key: str = '', secret: str = '', passphrase: str = '',
                  demo: bool = False) -> dict:
    ts = get_timestamp()
    headers = {
        'OK-ACCESS-KEY': api_key,
        'OK-ACCESS-SIGN': sign(ts, method, path, body, secret),
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': passphrase,
        'Content-Type': 'application/json',
    }
    if demo:
        headers['x-simulated-trading'] = '1'
    return headers
```

---

## 4. 限频策略

### 4.1 限频维度

| 维度 | 说明 |
|---|---|
| per IP | 公开行情端点，按出口IP计 |
| per UserID | 私有端点，按账户计 |
| per UserID + instId | 下单/撤单端点（除期权外） |
| per UserID（期权） | 期权下单按UserID，不含instId |

### 4.2 主要端点限频表

| 端点组 | 限频 | 维度 |
|---|---|---|
| 公开行情（tickers/candles/books/trades/instruments） | **20 req/2s** | per IP |
| 账户接口（balance/positions/config） | **10 req/2s** | per UserID |
| 单笔下单（POST /trade/order） | **60 req/2s** | per UserID+instId |
| 批量下单（POST /trade/batch-orders，≤20单/次） | **300 req/2s** | per UserID |
| 单笔撤单 | **60 req/2s** | per UserID+instId |
| 批量撤单 | **300 req/2s** | per UserID |
| 改单 | **60 req/2s** | per UserID+instId |
| 历史订单查询（orders-history） | 10 req/2s | per UserID |
| 成交明细（fills-history，3个月内） | 10 req/2s | per UserID |
| 策略委托（orders-algo） | 20 req/2s | per UserID |
| 资金费率（funding-rate） | 20 req/2s | per IP |
| 公开产品配置（instruments） | 20 req/2s | per IP |

### 4.3 限频处理

- 命中返回 `code: "50011"` → `msg: "Rate limit reached"`
- 处理：sleep 2 秒后重试
- 退避策略：指数退避，最多 3 次重试
- 批量操作优先用 `batch-orders`，1次请求≈20次单下
- 实时行情优先用 WebSocket，减少 REST 压力

### 4.4 客户端限频器建议

- 本地按"端点组"维护独立令牌桶
- 每桶配置：容量（如20）、补充速率（如10/s）
- 请求前 `acquire()`，超时则排队或拒绝
- 命中 50011 时强制阻塞该桶 2 秒

---

## 5. 错误码速查

| Code | 含义 | 处理建议 |
|---|---|---|
| `0` | 成功 | — |
| `50001` | 维护中 | 暂停策略，30秒探测 |
| `50011` | 限频 | sleep 2s 重试 |
| `50013` | 系统错误 | 暂停，告警 |
| `50101` | API Key与环境不匹配 | 检查 live/demo Key 配对 |
| `50102` | timestamp请求过期 | 客户端校时 |
| `50105` | passphrase错误 | 检查配置 |
| `50110` | IP不在白名单 | 检查Key绑定的IP |
| `50111` | API Key无效 | 立即停所有策略，告警 |
| `50113` | 签名无效 | 排查 prehash 拼接 |
| `50114` | timestamp超±30s | 客户端校时 |
| `51000` | 参数错误 | 校验请求参数 |
| `51001` | instId不存在 | 检查 instrument 列表 |
| `51008` | 余额不足 | 停该策略下单，告警 |
| `51009` | 持仓不足 | 检查持仓状态 |
| `51100` | 下单失败（盘口原因） | 检查价格/数量 |
| `54051` | RPI订单低于最小名义金额 | SPOT≥500/ FUT≥2000/ SWAP≥5000 USD |

---

## 6. 常用端点速查

### 6.1 公开行情（无需认证）

| 操作 | Method | Path |
|---|---|---|
| 获取服务器时间 | GET | `/api/v5/public/time` |
| 获取产品配置 | GET | `/api/v5/public/instruments?instType=SPOT` |
| 获取最新价 | GET | `/api/v5/market/ticker?instId=BTC-USDT` |
| 获取K线 | GET | `/api/v5/market/candles?instId=BTC-USDT&bar=1H` |
| 获取历史K线（含分页） | GET | `/api/v5/market/history-candles?instId=BTC-USDT&bar=1H` |
| 获取订单簿 | GET | `/api/v5/market/books?instId=BTC-USDT&sz=20` |
| 获取最近成交 | GET | `/api/v5/market/trades?instId=BTC-USDT` |
| 获取资金费率 | GET | `/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` |
| 获取持仓量 | GET | `/api/v5/public/open-interest?instType=SWAP` |

> **K线分页**：单次返回 ≤ 100 根，用 `after`（最早时间戳）+ `before`（最晚时间戳）参数翻页。`history-candles` 可获取更早数据，但返回字段不含确认标记。

### 6.2 私有账户

| 操作 | Method | Path |
|---|---|---|
| 账户余额 | GET | `/api/v5/account/balance` |
| 持仓信息 | GET | `/api/v5/account/positions` |
| 账户配置 | GET | `/api/v5/account/config` |
| 杠杆信息 | GET | `/api/v5/account/leverage-info` |
| 设置杠杆 | POST | `/api/v5/account/set-leverage` |
| 设置持仓模式 | POST | `/api/v5/account/set-position-mode` |

### 6.3 交易

| 操作 | Method | Path |
|---|---|---|
| 下单 | POST | `/api/v5/trade/order` |
| 批量下单（≤20） | POST | `/api/v5/trade/batch-orders` |
| 撤单 | POST | `/api/v5/trade/cancel-order` |
| 批量撤单 | POST | `/api/v5/trade/cancel-batch-orders` |
| 改单 | POST | `/api/v5/trade/amend-order` |
| 当前挂单 | GET | `/api/v5/trade/orders-pending` |
| 订单历史（7天） | GET | `/api/v5/trade/orders-history` |
| 订单历史（3个月） | GET | `/api/v5/trade/orders-history-archive` |
| 成交明细（3天） | GET | `/api/v5/trade/fills` |
| 成交明细（3个月） | GET | `/api/v5/trade/fills-history` |

---

## 7. K线下载要点

### 7.1 端点选择

| 端点 | 范围 | 字段差异 |
|---|---|---|
| `/market/candles` | 近 1500 根 | 含 `confirm` 字段（0=未确认，1=已确认） |
| `/market/history-candles` | 更早的历史 | 不含 `confirm` |

**推荐流程**：先用 `candles` 拉到最早可用的1500根，再用 `history-candles` 向前翻。

### 7.2 分页参数

| 参数 | 含义 |
|---|---|
| `before` | 早于此时间戳的数据（向前翻页） |
| `after` | 晚于此时间戳的数据（向后翻页） |
| `limit` | 单次返回数量，最大 100 |

> OKX 返回的K线时间戳是**开盘时间**（UTC毫秒）。

### 7.3 K线字段

返回数组，每根K线字段顺序：

```
[ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
```

| 字段 | 含义 |
|---|---|
| `ts` | 开盘时间戳（UTC ms） |
| `open/high/low/close` | OHLC |
| `vol` | 成交量（基础币） |
| `volCcy` | 成交量（计价币） |
| `volCcyQuote` | 成交额（计价币） |
| `confirm` | 0=K线未结束，1=K线已结束 |

---

## 8. 近期 API 行为变更（2026年）

### 8.1 USD → USDC 现货迁移（2026-09-30）

- `Crypto-USD` 现货产品已下线，需改用 `Crypto-USDC`
- `instId` 不会自动映射，必须显式更新
- 若需 USD 计价，下单时显式传 `tradeQuoteCcy=USD`
- 影响所有含 `instId` 的 REST/WS 请求

### 8.2 RPI 挂单最小名义金额（2026-09-15）

- `ordType: rpi` 或 `elp` 订单有最小金额门槛
- SPOT: ≥ 500 USD
- FUTURES: ≥ 2,000 USD
- SWAP: ≥ 5,000 USD
- 低于门槛返回 `54051`

### 8.3 post-only/rpi 订单 state 行为（2026-08-20）

- 挂单失败（穿越BBO被撤）时**只推 `state: canceled`**，不再推 `state: live`
- 挂单成功的 `state: live` 推送延后约 1ms（订单进入订单簿后才推）
- **影响策略状态机**：不能假设先收到 `live` 再收到 `canceled`

### 8.4 交易产品频道增量推送（2026-09-30）

- WebSocket `instruments` 频道由全量推送改为增量推送
- 客户端应根据 `instId` 维护本地产品缓存
- 不能假设每次推送包含全量产品

---

## 9. 模拟盘与实盘对照

| 项 | 模拟盘 | 实盘 |
|---|---|---|
| 域名 | `https://www.okx.com` | `https://www.okx.com` |
| 鉴权Header | 加 `x-simulated-trading: 1` | 不加 |
| API Key 创建位置 | 模拟盘 → API | 主账户 → API |
| `instIdCode` | 与实盘**不同**，需独立拉 | 标准值 |
| 资金 | 虚拟资金 | 真实资金 |
| 适用阶段 | M3 模拟盘 | M5 实盘 |

---

## 10. 客户端实现要点（本项目）

按需求文档 F1.1-F1.6，M1 阶段的 OKX 客户端需实现：

1. **统一鉴权层**：4 个 Header 自动生成，模拟盘/实盘通过配置切换
2. **校时**：启动时 `GET /api/v5/public/time`，每 5 分钟校时一次
3. **限频器**：按端点组维护令牌桶，命中 50011 时阻塞 2s
4. **重试**：网络异常 / 5xx / 50011 重试 3 次，指数退避
5. **签名**：HMAC-SHA256 + Base64，prehash 拼接严格按公式
6. **配置**：API Key / Secret / Passphrase / 模拟盘开关 走配置文件
7. **日志**：请求/响应明文日志（**Secret/Passphrase 必须脱敏**）
8. **断点续传**：K线下载记录已下载时间戳，中断后从断点续拉

---

## 11. 变更记录

| 日期 | 版本 | 修订人 | 说明 |
|---|---|---|---|
| 2026-10-17 | v1.0 | AI起草 | 初版。基于OKX官方文档+多个第三方参考交叉验证。包含REST/WS基础、认证、限频、错误码、常用端点、K线分页、近期变更 |
