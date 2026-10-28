# SMTP 邮箱配置指南

告警邮件、恢复通知、每日偏离日报、每 4 小时存活邮件都走同一个 SMTP 通道。
未配置时 monitor 不会崩溃，只是把内容写到日志（`logs/monitor.log`），
所以这一步属于「让告警真正到达你手机」的必要配置。

---

## 1. 配置位置

只有两个文件，都在项目根目录下：

| 文件 | 作用 | 是否含敏感信息 |
|---|---|---|
| `config/config.toml` 的 `[smtp]` 段 | 声明用哪个发件服务器、收件人 | 不放密码，可安全查看 |
| `.env`（项目根） | 存放邮箱账号与授权码 | **敏感，已在 `.gitignore` 中** |

`config/config.toml` 当前内容：

```toml
[smtp]                                    # 告警与日报邮件；未配置时降级为仅日志
host = "${SMTP_HOST:-smtp.qq.com}"        # QQ 邮箱 SMTP
port = 465
username = "${SMTP_USERNAME:-}"           # 你的 QQ 邮箱地址
password = "${SMTP_PASSWORD:-}"           # QQ 邮箱「授权码」，不是登录密码
from_addr = "${SMTP_FROM:-}"              # 留空则用 username
to_addrs = []                             # 收件人列表；填入 QQ 邮箱地址即自己收
use_ssl = true                            # 465 用 true；587 通常改成 false
```

`${VAR:-默认值}` 是环境变量引用语法，项目启动时会自动加载根目录 `.env`：

- `${SMTP_USERNAME:-}`：`.env` 里有 `SMTP_USERNAME` 就用它，没有则用空字符串
- `${SMTP_HOST:-smtp.qq.com}`：没设置时默认 `smtp.qq.com`
- 已存在的系统环境变量优先于 `.env`，二者冲突时以系统环境变量为准

> `to_addrs` 是 TOML 数组，不接受环境变量默认语法，直接写真实地址（收件人不是秘密）。

---

## 2. 需要填的 3 个值

| 值 | 填在哪 | 示例 |
|---|---|---|
| 发件邮箱地址 | `.env` 的 `SMTP_USERNAME` | `12345678@qq.com` |
| 邮箱授权码 | `.env` 的 `SMTP_PASSWORD` | `abcdefghijklmnop`（16 位） |
| 收件邮箱地址 | `config.toml` 的 `to_addrs` | `["12345678@qq.com"]` |

`from_addr` 留空即可，程序会自动用 `username` 作为发件人；
QQ 邮箱要求发件人必须等于登录账号，填其它地址会被拒。

---

## 3. 第一步：获取 QQ 邮箱授权码

QQ 邮箱不接受登录密码，必须用独立的「授权码」：

1. 浏览器登录 <https://mail.qq.com>
2. 右上角 **设置** → **账号**（新版界面在「账号与安全」→「安全设置」）
3. 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务**
4. 打开 **IMAP/SMTP 服务** 开关
5. 按提示用绑定手机发送短信验证
6. 验证通过后弹出一串 **16 位授权码**，复制保存

注意：

- 授权码只在生成时显示一次，关掉窗口就得重新生成
- 授权码与登录密码完全独立，可随时在页面「删除」后重新生成（怀疑泄露时这么做）
- 开启的是 IMAP/SMTP，不是 POP3，二者任一开启即可，但建议只开需要的

---

## 4. 第二步：写入凭据

在项目根目录（`~/Desktop/quant-trading`）执行，把两处中文换成真实值：

```bash
cd ~/Desktop/quant-trading
printf "\nSMTP_USERNAME='12345678@qq.com'\nSMTP_PASSWORD='你的16位授权码'\nSMTP_FROM='12345678@qq.com'\n" >> .env
```

`SMTP_FROM` 与 `SMTP_USERNAME` 一致即可，写与不写都能发；
不写时程序用 `username` 兜底。

也可以直接用编辑器打开 `.env`，在末尾追加这三行（推荐用编辑器，避免手滑）。

`.env` 当前已有 OKX 与 Redis 配置，**不要覆盖原有内容，只在末尾追加**。

---

## 5. 第三步：填写收件人

编辑 `config/config.toml`，把 `to_addrs` 从空数组改成你的邮箱：

```toml
to_addrs = ["12345678@qq.com"]
```

多个收件人用逗号分隔：

```toml
to_addrs = ["a@qq.com", "b@163.com"]
```

---

## 6. 第四步：验证

```bash
quant test-email
```

三种可能的结果：

| 输出 | 含义 | 处理 |
|---|---|---|
| `test email sent`，退出码 0 | 成功，去收件箱确认 | 无 |
| `SMTP 未配置：...`，退出码 1 | 三要素缺一个 | 检查 `.env` 的 `SMTP_USERNAME`、`SMTP_PASSWORD` 是否生效 |
| `email send failed: ...`，退出码 1 | 连上了但登录/发送失败 | 看第 8 节错误对照表 |

临时换收件人（不改配置文件）：

```bash
quant test-email --to other@example.com
```

成功与否都以退出码为准，方便放进脚本：

```bash
quant test-email && echo OK || echo FAILED
```

---

## 7. 第五步：重启 monitor 生效

配置只在进程启动时读取，正在跑的 monitor 不会自动感知。supervisor
托管时会自动重启子进程，所以只需杀掉旧的 monitor 进程：

```bash
# 查看当前 monitor 进程
pgrep -fl "quant monitor"

# 杀掉（supervisor 会在几秒内用新配置拉起）
kill $(pgrep -f "quant monitor")

# 确认日志出现 smtp=on
sleep 10 && tail -n 5 logs/monitor.log
```

期望看到：

```
monitor started interval=60s smtp=on engine_heartbeat=on daily_report=on retention=on
```

`smtp=off` 说明配置没生效，回到第 4 步检查 `.env` 路径与变量名。

---

## 8. 配置完成后会收到哪些邮件

| 邮件 | 触发条件 | 频率 |
|---|---|---|
| 监控告警 | 10 条规则任一命中（心跳丢失、队列堆积、死信、保证金率、余额突变、撤单率、连亏、API 错误率、kill switch、engine 心跳） | 命中即发，同一规则有去重 |
| 恢复通知 | 持续型规则从异常恢复正常 | 每次恢复一封（`INFO 已恢复`） |
| 每日偏离日报 | `[report] enabled = true`，本地时间过 `at`（默认 08:00） | 每天一封 |
| 存活邮件 | 固定心跳，证明 monitor 还活着 | 每 4 小时一封 |

存活邮件的时间戳存在 Redis 键 `quant:monitor:alive-last`，
monitor 重启不会立刻补发，避免崩溃循环刷满邮箱。

---

## 9. 常见错误对照表

| 报错关键字 | 原因 | 处理 |
|---|---|---|
| `SMTPAuthenticationError` / `535 Login Fail` | 用了登录密码而非授权码；或授权码已失效 | 重新生成授权码，确认没有多余空格 |
| `SMTPNotSupportedError` / `530` 需要 TLS | 端口与 SSL 开关不匹配 | 465 配 `use_ssl = true`；587 配 `use_ssl = false` |
| `ConnectionRefusedError` / 连接超时 | 端口错、被墙、或公司网络限制 | 换 465/587 试；确认能 `telnet smtp.qq.com 465` |
| `554` / `550` 被拒 | 内容被判垃圾邮件，或新账号发送受限 | 换标题与正文再试；QQ 新账号有时限制频率 |
| 发送成功但收不到 | 进了垃圾箱 / 被规则拦截 | 检查垃圾箱，把发件人加入白名单 |
| `SMTP 未配置` | `.env` 未生效或变量名拼错 | 确认文件在项目根、变量名全大写、无 BOM |
| 邮件里发件人乱码 | `from_addr` 与登录账号不一致 | 留空 `SMTP_FROM`，让程序用 `username` |

---

## 10. 安全提醒

- 授权码只写进 `.env`，不要写进 `config/config.toml`（后者是给人看和备份的）
- `.env` 已在 `.gitignore` 中，不要 `git add -f` 强制提交
- 授权码泄露时，QQ 邮箱页面「删除」后重新生成即可，旧码立即失效
- 建议单独注册一个邮箱专用于告警，与日常邮箱隔离

---

## 11. 相关命令速查

```bash
quant test-email                        # 用 config.toml 里的 to_addrs 发测试邮件
quant test-email --to a@b.com,c@d.com   # 临时改收件人
pgrep -fl "quant monitor"               # 查 monitor 进程
kill $(pgrep -f "quant monitor")        # 重启 monitor（supervisor 自动拉起）
tail -f logs/monitor.log                # 观察告警与邮件日志
```

相关文档：[模拟盘运行手册](RUNBOOK_PAPER_TRADING.md) 第 2.3 节「监控」。
