# 接真实客户之前还差什么

**这份文件回答一个问题：现在把网址给一个真客户，会发生什么坏事？**

每一条都写了「现状 / 为什么要紧 / 怎么做」。做完一条就把它从这里删掉——**这份清单的价值在于它是短的**。

最后核对：2026-08-05。

---

## 已经验证能用的

不是「应该能用」，是当天逐个探针打过的：

| | 状态 | 怎么验的 |
|---|---|---|
| Google Calendar | ✅ 读 + 写 | 建 → 改 → 重读能看见 → 删干净 |
| Twilio | ✅ | 账户 active；`+17782004962` sms=True voice=True |
| Gmail | ✅ 搜索 | 搜到真实邮件。**发信没验** |
| Telegram | ✅ 端到端 | 真派单 `OF-0004` 到师傅手机，带按钮 |
| Stripe | ⚠️ 能连，**测试模式** | `livemode=False` |
| 提醒循环 | ✅ 在跑 | `ReminderLoop` 在 `serve()` 里启动 |
| 生产开关 | ✅ 在 systemd | `live_status()` 报 `source=env` |

**凭据不是瓶颈。** 下面每一条都不是「没有账号」。

---

## 🔴 拦路的

### 1. 8 个工具全是 mock —— agent 会承诺它做不到的事

唯一 live 的 `telegram.send` **不在 agent 路径上**（`notify.py` 直接调）。两个启用的 agent 能碰到的工具，一个都不是真的：

| 工具 | 谁用 | 现在打开会怎样 |
|---|---|---|
| `calendar.find_slots` | intake, small_job | **只读**。不开就是个正在生效的 bug：agent 从空日记里报时间，会把师傅已占的时段给客户 |
| `calendar.create_appointment` / `reschedule` / `cancel` | small_job | 写师傅的真实日历，可见可撤 |
| `phone.call_technician` | small_job | 响师傅的手机，只说一句「查看信息」 |
| `sms.send` | intake, small_job | **真发短信给客户，按条计费** |
| `email.send` | intake | 真发邮件 |

**按风险从小到大开，每步之间跑一次真实会话**：

```
1. calendar.find_slots
2. calendar.create_appointment,calendar.reschedule,calendar.cancel
3. phone.call_technician
4. sms.send          ← 单独一步。开完立刻用自己的号码验一条
5. email.send
```

改 systemd 的 `PLUMBING_LIVE_TOOLS`（逗号分隔）→ `daemon-reload` → `restart`。

> **不要改 `config/tool_catalog.yaml`。** 那是 git 跟踪的文件，`git pull` 会把开关悄悄关回去，而没有任何地方报错。见 [CHANGE_GUIDE.md](CHANGE_GUIDE.md#生产开关不在-git-里)。

### 2. 数据没有备份

`data/plumbing.db` 里是客户、工单、预约、消息、followups、offers。**没有任何自动备份。**

要做：每天 `sqlite3 .backup` 的 systemd timer，留 7 天。

> `.backup` 而不是 `cp` —— 这个库开了 WAL，直接拷会得到一个缺了最近写入的文件，而且看起来完全正常。

---

## 🟠 会咬人的

### 3. 没有监控

服务挂了、Telegram 发不出去、模型报错——**没有任何地方会告诉你**。`notify.py` 刻意从不抛异常（免得一条通知失败拖垮派单），代价就是失败是静默的。

最小可用：cron 每 5 分钟 curl `/health`，失败发 Telegram 给你。用已经 live 的那条通道，不引新依赖。

### 4. 进行中的对话不跨重启

客户、工单、预约、chat session 的号码都在库里。**正在进行的对话在内存**（`LiveConversation.messages`）。重启会让客户说到一半的话丢掉——不丢工单，但读起来像「它把我说的忘了」。

已知取舍，`live/conversation.py` 文件头写明了。**有流量再说。**

### 5. Gmail 发信没验过

搜索验过，`send_email` 没有。第一次真发是在一个客户身上，不理想。

---

## 🟡 记着就行

- **Stripe 是测试模式**。emergency 关着所以不影响，重开 emergency 之前必须处理。
- **两条 flaky 场景**（各 1/4 挂，都是 agent 的）：`live_urgent_goes_to_the_technician` 偶尔转人工后还想自己启动紧急派单（状态机每次都挡住）；`live_warranty_goes_to_the_technician` 偶尔拿到号码就结束会话不说话。按方法暂不交 doctor——**只是运气不好的 prompt 不能被改写**。等它们变成稳定失败。
- **另外 4 个 agent 关着**（emergency / warranty / large_job / complaint）。恰好是收钱和退款的那几个。
- 库里有测试数据：chat session `604-555-0199` / `604-555-0188`，offer `TK-TEST-DEPLOY`。

---

## 链路图（谁转给谁）

```
smartstrategy.services      → cloudflared → localhost:8000   ← 应用本体
                                                              /chat/new /chat/message
                                                              /sms /voice /telegram /health
www.smartstrategy.services  → cloudflared → nginx:80 → /var/www/html   ← 静态页 + chat widget
```

**widget 和后端不同源**，所以 chat 的每一次请求都是跨域的。`config/live.yaml` 的 `web_chat_origins` 是那份白名单——不是 `*`，因为这些端点会花钱调模型并且带 session id。

---

## 每次改完，怎么确认链路还活着

```bash
curl -i -X OPTIONS https://smartstrategy.services/chat/new -H "Origin: https://www.smartstrategy.services" -H "Access-Control-Request-Method: POST"
```

期望 `204` + `Access-Control-Allow-Origin`。换成 `Origin: https://evil.example` 应该 `403`。

**但真正的验证是用浏览器打开 `https://www.smartstrategy.services` 聊一轮。** curl 验不出浏览器满不满意——那次「服务器返回 200、浏览器把它丢掉」的故障，curl 从头到尾都是绿的。
