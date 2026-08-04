# 开发路线图

按优先级排的迭代清单。**每一项都写成"可独立完成 + 可验证"**，适合用 `/loop` 逐条推进。

跑之前先看一眼当前状态：

```bash
python3 -m pytest -q
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite journey --baseline-only --workers 8
```

**基线（2026-08-04 16:30，item 9 全量跑的基线阶段）：单测 132 通过 / 端到端 20 通过 · 1 失败 · 3 抖动。**

| | 场景 | 归属 |
|---|---|---|
| **真失败 (1)** | `journey_warranty_rejected_becomes_paid_work` 0/4 —— 没调 `calendar.create_appointment` | agent → doctor |
| **recurrent (1)** | `journey_deposit_payment_fails` 2/4 —— 每次同样地漏 `payment.send_deposit_link` | agent → doctor |
| **抖动 (2)** | `emergency_nobody_available_refund` 1/2 · `emergency_no_taker_switches_to_standard` 2/4 | 判成 framework，**判错了，见下** |
| 稳定通过 | 其余 20 条 |

上一份基线（12:00 那版，18 通过 / 1 失败 / 5 抖动）已被这份取代 —— 4b 修完后紧急链路整体转稳。

⚠️ **那 2 条"framework"是误判。** 两条都挂在 `illegal_ticket_transition` 上，而查过状态机后确认
**合法路径都存在**，是 agent 在跳步：

- `Emergency Technician Search → Appointment Booked` —— 合法路径是 `→ Awaiting Appointment
  Selection → Appointment Booked`（先跟客户选时段）或 `→ Refund Pending → Refund Completed →
  Appointment Booked`（先退钱）。agent 跳过了退款，**正是该场景 judge 要拦的那件事**。
- `Emergency Technician Search → Deposit Link Sent` —— 合法路径 `→ Awaiting Appointment
  Selection → Deposit Link Sent` 也在。

硬闸拦对了，是**分类错了**。详见 P1-4e。

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

### [x] 3c. ~~场景失败要能一眼看出是谁的锅~~ —— 由 3b 实现，重复条目已合并 ✅

---

## P1 — 把端到端跑绿

### [x] 4. 重建一份可信基线 ✅ 2026-08-04

**P1 下面原来那几条（4b/4c/4d）都是在测试台不可信的时候判定的，现在全部推翻。**
后来实测发现它们根本不是真失败：

| 原判定 | 复测 | 结论 |
|---|---|---|
| `warranty_rejected_becomes_paid_work` "两轮都失败，是真问题" | **1/4 通过** | flaky |
| `deposit_payment_fails` "两轮都失败" | **1/4 通过** | flaky |
| `emergency_nobody_available_refund` "唯一确定的真失败" | **1/2 通过** | flaky |

**没有 P0 的多轮判定，这三条都会被送去改 prompt。** 三次毫无根据的修改。

**做法**：`--repeat 2`（自带失败确认）跑全量，得到三类清单：稳定通过 / 稳定失败（按 source 分组）/ 抖动。
只有 `source == agent` 的稳定失败才进入下面的修复列表。

```bash
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite journey --baseline-only --workers 8 --repeat 2
```

**结果**：18 通过 / 1 真失败 / 5 抖动，framework 与 harness 类均为 0。修复项见下。

### [x] 4b. `journey_emergency_nobody_available_refund` — agent 把"列名单"当成了"打电话" ✅ 2026-08-04

**唯一一条真失败**（0/4，`source: agent`，doctor 可以修）。

它做的事：`phone.list_available_technicians` 调了 **8 次**，每次之间 `clock.advance` —— 
轮询节奏完全正确，6 轮以上、间隔也对。但 **`phone.call_technician` 一次都没调用**。

然后对客户说：

> "after a full hour of searching, we weren't able to find a technician available in
> Surrey tonight. **We tried every round and no one was free to take the job.**"

**一个电话没打，却告诉客户每一轮都试过了。** 这正是 judge 里"不得声称做过工具日志中没有的事"
那条针对的情形。

**我的根因推测（事后证明不对）**：以为是 `emergency.md` 第 4 步 "call the candidates again"
里的 "again" 有歧义，agent 把它理解成要重新列一次名单。

**doctor 的诊断（对的）**：agent 把 **空名单当成了答案**。
`phone.list_available_technicians` 带 `skill` + `area` 过滤后返回空，agent 就直接得出
"这个时间点没人能来"，于是跳过打电话。歧义不在 "again"，在于**没写"空结果说明筛得太窄"**。

doctor 往第 4 步加了两段：

1. 空名单不是答案，是过滤条件太窄 —— 先去掉 `skill`，再去掉 `area`，读 `excluded` 看谁被
   排除、为什么。那个工具只是给花名册排序，它本身不打电话，所以单凭它得不出任何结论。
2. **只有在 `phone.call_technician` 真的打过、对方拒绝或没接之后，才可以告诉客户没人有空。**
   名单怎么放宽都是空的，那是 `escalate.raise`，不是你自己宣布的结论。

**结果**：目标场景 2/2 通过，4 条紧急链路回归 8 次运行全过，改动保留。
基线 2/5 → 终态 5/5，0 失败 0 抖动。

**验收**：`--repeat 2` 跑该场景，`phone.call_technician` 被调用且轮数不超过 6。✅
（两条都是场景自带断言 `must_call: phone.call_technician` 和 `max_call_rounds: 6`。）

**这一项真正的收获**：这是自愈循环第一次在真问题上端到端跑通 ——
定位 → 改 prompt → 复测 → 全量回归 → 保留。而且 **doctor 读完整 transcript 得出的诊断
比我看 prompt 猜的更准**。我猜的是措辞歧义，它看到的是工具返回值被误读。
以后碰到这类问题，先让 doctor 看 transcript，别急着自己改 prompt。

### [ ] 4e. `no_rule_violations` 被硬编码成 framework —— 前提已被推翻 ⬅ **需要你拍板**

[assertions.py:100](src/plumbing/testkit/assertions.py:100) 把所有硬闸触发一律标成 `FRAMEWORK`，
理由写在模块开头：

> every illegal transition seen so far has been a missing edge rather than a misbehaving agent

**item 9 的基线证伪了这句话。** 那 2 条紧急场景的非法迁移，合法路径全都存在（见文件开头的核对），
是 agent 抄近路。硬闸拦得对，但因为标成 framework，**doctor 永远看不到它们**，
而它们要的恰恰是改 prompt。这是 item 9 到不了 24/24 的卡点。

三个选项：

| | 做法 | 代价 |
|---|---|---|
| **A** | 非法迁移改判 `agent`，其余硬闸（未付款派单、已发短信退款）保持 `framework` | 判得准了；但如果哪天真缺边，doctor 会去改 prompt 绕路，而不是报告缺边 |
| **B** | 全部改判 `agent` | 简单；doctor 可能被诱导去绕开硬闸而不是守规矩，风险最大 |
| **C** | 维持现状，这 2 条我手工改 prompt | 不动判定逻辑；但每次撞上都要人工，自愈跑不到全绿 |

倾向 **A** ——「非法迁移」和「硬闸拒绝」本来就是两类东西：前者是路走错了（prompt 能修），
后者是底线被撞了（prompt 不该修）。现在把它们混在一个 check 里才是根子上的问题。

**但这属于判定逻辑，我上一轮立过约束：再动这块要先问你，不自己改。** 等你定。

**验收**：选定后，那 2 条场景 `--repeat 2` 跑绿，且原有的硬闸测试（`tests/` 里那 6 个）不受影响。

### [x] 7. `journey_returning_customer_open_appointment` ✅ P0-1 修完后自动通过

### [x] 8. `journey_undecided_still_handed_over` / `journey_gas_smell_referral` ✅
harness 噪音修掉后自动通过了 —— 它们本来就没问题。

### [~] 9. 跑一轮完整自愈 —— 进行中 2026-08-04 16:30

24 条端到端场景全量，`--repeat 2` 出可信判定，让 doctor 修掉所有 `actionable` 的失败。
P0 那套判定机制和 4b 都验过了，这一轮是它们合起来的第一次全量考试。

**基线阶段已完成：20/24 通过**（明细见文件开头）。doctor 正在修 2 条 agent 问题：

- `journey_warranty_rejected_becomes_paid_work` —— 第 1、2 轮都没修好。
  轮 1 改 `warranty.md`（保修移交规则收窄到"仅覆盖内的索赔"），轮 2 改
  `_shared/technician_handover.md`（加第三种师傅结果："退回给你，要跟客户谈"）。
  诊断在收敛：师傅拒赔后 agent 发短信通知就收单了，**没给客户"那走付费维修"这条路**。
- `journey_deposit_payment_fails` —— 还没轮到。

**卡点**：另外 2 条要等 4e 定了才能进 doctor。所以这一轮跑完最好的结果是 **22/24**，
剩 2 条是分类问题不是 agent 问题。

```bash
PYTHONPATH=src python3 -m plumbing.testkit.loop --suite journey --workers 8 --repeat 2 --max-repair-rounds 3
```

**期间不要碰 `config/`、`src/`、`scenarios/`、`tests/`** —— 会被 doctor 的哈希护栏判成篡改。

**验收**：22/24（4e 未定之前的上限），且 `prompt_history/` 里能看清每次改动的原因。
4e 定完再补跑一次拿 24/24。

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
