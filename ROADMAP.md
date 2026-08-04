# 开发路线图

按优先级排的迭代清单。**每一项都写成"可独立完成 + 可验证"**，适合用 `/loop` 逐条推进。

跑之前先看一眼当前状态：

```bash
python3 -m pytest -q
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite journey --baseline-only --workers 8
```

**基线（2026-08-04）：单测 123 通过 / 端到端约 18 稳定通过 · 2 稳定失败 · 4 抖动。**

**唯一两轮都认定的真失败：`journey_emergency_nobody_available_refund`**（从不调用
`phone.call_technician`）。其余都在 fail 和 flaky 之间摇摆，先别动。

⚠️ **repeat=2 本身还不够**。实测两轮独立运行给出了不同的"真失败"集合：

| 场景 | Run A | Run B |
|---|---|---|
| `warranty_rejected_becomes_paid_work` | **fail (0/2)** | flaky (1/2) |
| `deposit_payment_fails` | flaky (1/2) | **fail (0/2)** |

正是 25% 误判率预测的样子。自适应确认（判成 fail 的再跑 2 次）已经写好并有单测，
**但还没有端到端验证过** —— 见 P0-3b。

---

## P0 — 测试台可信度 ✅ 全部完成

三项都做完了：模拟器不再吐坏 JSON、多轮判定挡掉抖动、失败按来源分类挡掉"不是 prompt 能修的"。
**现在可以相信端到端的结论了**，也可以放心让 doctor 跑。

### [x] 1. 客户模拟器不再依赖 JSON 输出 ✅ 2026-08-04

改成纯文本 + `[END]` 标记（`personas/customer.md` + `src/plumbing/sim/customer.py`）。
解析器刻意宽容：标记可能缺失、跟在句尾、被代码围栏包住、或写成小写。

**结果**：模拟器故障 **6 → 0**，基线 **13/24 → 19/24**。裁判仍用 `chat_json`（温度 0，很稳）。
加了 10 个解析测试。

### [~] 2. 状态机补边 —— 部分完成 2026-08-04

补了 7 条边，**每条在配置里写了为什么**（`config/ticket_states.yaml`）：

- `Needs Assessment → Warranty Eligibility Review` —— 客户想起保修是随时的，不只在第一句
- `Warranty Technician Review → Deposit Link Sent` —— 批准的保修也可能很急，而紧急是先收定金
- `Escalated to Supervisor → {Deposit Link Sent, Awaiting Appointment Selection, Needs Assessment}`
  —— 主管处理完，活要能接着走。**只开具名的几条**：让升级能通向任意状态，状态机就没有意义了
- `Refund Completed → {Awaiting Appointment Selection, Appointment Booked}`
  —— "今晚没人，钱退你，明天来行吗"是失败搜索的正常结局，不是例外

加了 6 个测试，其中一个专门验证**补边没有把该关的闸打开**（跳过付款派单、没发链接就标已付、
保修没经人工审核就预约，全都仍然被拒）。

**结果**：`illegal_ticket_transition` 3 → 2，`emergency_no_taker_switches_to_standard` 通过。

**剩下的 2 条不是状态机的问题**，各自归位到下面：

- `New Inquiry → Warranty Eligibility Review`（`warranty_approved`）—— 客户开口就提保修，
  agent 还没验证电话就想跳去保修评估。**这条边不该加**：没认出客户就没法评估保修。
  是 intake prompt 的歧义（"保修直接转走"被读成了"先于一切"），→ 见 P1-4c，doctor 该能修。
- `Escalated to Supervisor → Appointment Booked`（`deposit_payment_fails`）——
  两跳路径（→ Awaiting Appointment Selection → Appointment Booked）已经存在，agent 想抄近路。
  **故意不加**：这正是"不许跳过要紧步骤"该拦的。但该场景的断言本身也值得复核，见 P1-4d。

### [x] 3. 多轮判定，把抖动和真失败分开 ✅ 2026-08-04

`loop.py` 加了 `--repeat N`（默认 2），三种判定：全过 `pass` / 全挂 `fail`（交 doctor）/
有过有挂 `flaky`（**不交 doctor**，报告单列）。

三处关键改动，都是防止 doctor 对着噪音动手：

1. **doctor 拿到失败的那一次运行**（`representative`）—— 给它碰巧成功的那次，它什么也看不出来
2. **回归判定改成"从稳定通过变成稳定失败"** —— 原来补丁后某场景偶然挂一次就回滚，会误伤好补丁
3. **flaky 既不算通过、也不交 doctor**

**还加了自适应确认**：判成 `fail` 的场景会再跑 `repeat` 次才作数。因为 repeat=2 时，
一个真实通过率 50% 的场景有 **25%** 概率被误判成 fail 并送去改 prompt；加了确认降到 **6.2%**，
而稳定通过的场景一次额外运行都不用付。

**结果**：判定比以前有信息量得多（18/2/4 而不是笼统的 "19/24"），加了 9 个测试。

### [x] 3a. 验证自适应确认真的管用 ✅ 2026-08-04

只跑那 4 条已知抖动的（8 次运行，不是全量 48 次）。两项都通过：

1. `confirming 2 failure(s) with 2 more run(s) each` —— 触发了，**且只对那 2 条 0/2 的触发**，
   稳定通过的和已判 flaky 的一次额外运行都没付
2. **两条都被改判**：`warranty_rejected_becomes_paid_work` 0/2 → **1/4**，
   `deposit_payment_fails` 0/2 → **1/4**，双双 `fail → flaky`

没有这一步的话，doctor 这会儿正在改两个本来没错的 prompt。

顺带修了一个真 bug：报告把 flaky 算进了 passing，导致 "4 passing, 0 failing, 3 flaky (of 4)"
加起来是 7。

### [x] 3b. 失败按来源分类（harness / framework / agent）✅ 2026-08-04

每个断言带一个 `source`，只回答一个问题：**doctor 能不能靠改 prompt 修好它？**

| 来源 | 含义 | doctor |
|---|---|---|
| `harness` | 模拟器或模型坏了 | 不碰 |
| `framework` | 硬闸/状态机/工具权限拦的 | 不碰，需要人判断是规则错还是流程错 |
| `agent` | agent 做错了事 | **只修这类** |

一个场景同时有多类失败时**取最严重的**——被框架卡住的 agent 后面该调的工具自然都调不成，
先修框架是唯一说得通的顺序。

`heal()` 遇到非 agent 类会打印 `Skipping X (framework) — Not something a prompt edit can fix`
并跳过；报告按来源分三节，每节写明谁该负责。

**验收用确定性测试完成**（8 个），因为等真实套件恰好产生 framework 失败是靠运气 ——
正是 PLAYBOOK 第 10c 条说的那个反面。其中两个直接穿过 `heal()`：一个断言 doctor
**一次都没被调用**，另一个断言真 agent 失败仍然照常交给它。

### [ ] 3. 场景失败要能一眼看出是谁的锅（仍然值得做）

**改法**：报告里把失败分成三类并分开统计 —— `harness`（模拟器/LLM 故障）、
`framework`（硬闸、状态机、编排器）、`agent`（prompt 行为）。
`loop.py` 的 `write_report` 里加分类。

**验收**：`report.md` 顶部出现三类计数，doctor 只对 `agent` 类失败出手。

---

## P1 — 把端到端跑绿

按上面分类修完 P0 之后重跑，再逐条处理剩下的真失败。当前已知的真失败：

### [x] 4. `journey_small_job_reschedule` ✅ P0-1 修完后自动通过

### [ ] 4b. `journey_warranty_rejected_becomes_paid_work` — 保修被驳回后没建预约

两轮都失败，是真问题。`calendar.create_appointment` 从未调用 —— 客户已经同意按付费服务做了，
链路却停在那里。先看 transcript 判断是 warranty→small_job 的交接没发生，还是交接后没建。

### [ ] 4c. `journey_warranty_approved` — intake 跳过身份验证直奔保修

intake prompt 里保修是 Step 3（在"认人"之后），但"保修直接转走"这句被 agent 读成了
"先于一切"。**没认出客户就评估不了保修**。改 prompt 讲清顺序，doctor 该能修。

### [ ] 4d. `journey_deposit_payment_fails` — 断言可能太严

场景断言 `no_appointment`，但支付失败后**给客户排一个普通预约其实是合理服务**。
先看 transcript：如果 agent 是征得客户同意才排的，那是断言写错了；如果是自作主张，才是 agent 的错。

### [ ] 5. （已并入 P0-2）

### [x] 6. `journey_deposit_payment_fails` — 定金链接问题已解决 ✅
现在只剩状态机缺边（见 P0-2）。

### [x] 7. `journey_returning_customer_open_appointment` ✅ P0-1 修完后自动通过

### [x] 8. `journey_undecided_still_handed_over` / `journey_gas_smell_referral` ✅
harness 噪音修掉后自动通过了 —— 它们本来就没问题。

### [ ] 9. 跑一轮完整自愈

```bash
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite journey --workers 8 --max-repair-rounds 3
```

**期间不要碰 `config/`、`src/`、`scenarios/`、`tests/`** —— 会被 doctor 的哈希护栏判成篡改。

**验收**：24/24，且 `prompt_history/` 里能看清每次改动的原因。

---

## P2 — 补齐还没做的业务能力

### [ ] 10. 大额工程的正式报价流程

`quote.*` 五个工具已经写好、有测试、**没接给任何 agent**。当前 `large_job` 的流程是
"通知师傅去邮箱看资料"就结束，报价由人在系统外做。

**要决定**：报价单是否要回到系统里（这样才能做 24/48/72 跟进和"客户同意→安排施工"）。
要，就把 `quote.*` 接给 `large_job` 并补场景；不要，就把这组工具和
`business_rules.yaml` 里的 `quote_followup` 一起删掉，别留半截。

### [ ] 11. 还没实现的 planned 工具

`config/tool_catalog.yaml` 里 8 个 planned。按价值排序：

- `payment.charge_balance` —— 收检查费差额/维修尾款。**目前系统完全不收尾款**，
  师傅在现场收的钱没有回到工单里。
- `calendar.hold_slot` / `release_hold` —— 客户犹豫时占位保留时段
- `technician.get_live_location` —— 客户问"人到哪了"
- `crm.merge_duplicates` —— 同一客户不同号码
- `media.analyse_photo` —— 用图片辅助判断规模（要设信心阈值，别让它左右分流）
- `review.request_feedback` —— 完工后评价，**绝不能对升级过的工单发**

### [ ] 12. 多渠道接入

现在只有一条文字对话通道。真实场景是电话/短信/网页/WhatsApp 同一套流程。
内部的 `aiphone` 项目已经做了 Twilio ConversationRelay 语音 + 短信 + Telegram + 网页
四通道，可以直接借鉴其 `app/routes` 与 `app/ws`（服务器地址见内部记录，不写在公开仓库里）。

---

## P3 — 接真实服务（全绿之前不做）

### [ ] 13. 逐个工具切 live

顺序建议：`email.send` → `sms.send` → `payment.*`。每切一个跑一遍全量。

```yaml
# config/tool_catalog.yaml
live_tools_enabled: true
statuses:
  email.send: live
```

**验收**：切之前先跑 `python3 scripts/check_llm.py` 确认凭据可用；切之后第一条真实消息
发给自己的号码/邮箱，不要发给场景里的假客户。

### [ ] 14. Google Calendar 真实适配器

现在日历是纯模拟。`aiphone` 的 `app/integrations/google_calendar.py` 可直接借鉴，
凭据 `GOOGLE_CALENDAR_ID` 已经在 `.env` 里。

### [ ] 15. 真实 CRM / 工单持久化

现在整个 world 是内存对象，进程结束就没了。真上线要落库。
`aiphone` 用 SQLite（`app/db`），够用。

---

## P4 — 打磨

### [ ] 16. prompt 长度预算

`intake` 已经 18000 字符（起初 2000）。定期检查，超过 20000 就考虑把
"服务选择 + 报价说明"拆成独立 agent。

```bash
PYTHONPATH=src python3 -c "
from plumbing import agent_registry, config
cfg=config.agents_config()
for n in cfg['agents']:
    print(n, len(agent_registry.build_system_prompt(n,cfg)))"
```

### [ ] 17. 控制台加"跑测试"按钮

现在控制台只能看和改，不能触发运行。加一个按钮跑单场景并实时显示对话。

### [ ] 18. 成本看板

`report.md` 已有 token 统计和缓存命中率。加一个按场景的成本排序，找出最贵的链路。

### [ ] 19. 场景覆盖审计

写个脚本，检查每个硬闸、每个 agent 的每个分支是否都有场景覆盖，列出没覆盖的。

---

## 用 /loop 迭代的建议

```
/loop 读 ROADMAP.md，挑第一个未完成项做掉，跑验收，勾掉，然后停下来报告
```

**注意事项：**

0. **用 `--repeat 2`（默认）跑验收。** 单轮结果指导不了任何决策。
1. **一次只做一项。** 这个项目的改动往往要同时落到「配置 + prompt + 状态机 + 场景 + 测试」
   五处，一次改两项很容易互相干扰。
2. **改完先跑单测再跑端到端。** 单测 3 秒，端到端 15 分钟。
3. **跑自愈期间不要改任何被保护的文件。**
4. **端到端有抖动。** 客户模拟器温度 0.9，同一场景两次跑结果可能不同。
   **单次失败不足以判定 prompt 有问题，连续两次才值得动手。**
5. **只对 `Failing every run` 那一组动手。** `Flaky` 那组不要碰 —— 它们不是 prompt 的问题，
   改了只会让 prompt 越来越长。
