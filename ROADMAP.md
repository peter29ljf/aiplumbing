# 开发路线图

**按成本排序，不按优先级排序。** 免费的检查全做完，再做便宜的，最后才做贵的。

这是从 [METHOD.md](business-agent-template/METHOD.md) 里学到的、而上一版路线图违反了的原则：
上一版按"重要程度"排，结果**用最贵的端到端去发现读一遍就能发现的问题**。8 小时里有 4.5 小时
花在测试台可信度上，中间整份结论作废重来。

四种手段的成本差两个数量级：

| 手段 | 单次成本 | 本项目现状 |
|---|---|---|
| **静态检查** | 几秒，免费 | ❌ **从来没做过** |
| **工具层单测** | 秒级，免费 | ✅ 139 个，全通过 |
| **单 agent + stub** | 中，约端到端 1/5 | ❌ **没有这个模式** |
| **端到端** | 高（一天烧了 39.7M token） | ✅ 24 条，用得过多 |

**两个最便宜的档位一个都没建。** 这一版的 A、B、C 三阶段就是补这个。

---

## 当前状态（2026-08-04 17:30）

| | |
|---|---|
| 单测 | **139 通过** |
| 端到端 | **21/24 通过，0 失败，3 抖动** |
| Agent | 5 个全部实现 |
| 工具 | 50 个**全部 mocked，0 个 live**，8 个 planned 未实现 |
| prompt | 文件 820 行 → 5 个 agent **累计装配 1577 行**，其中约 20% 是跨文件重复 |

**3 条抖动：**

| 场景 | 原因 | 归属 |
|---|---|---|
| `warranty_rejected_becomes_paid_work` | 结构性重复，doctor 三轮修不动 | **改动已做，未验证**（见 D2） |
| `emergency_no_taker_switches_to_standard` | 分类误判，进不了 doctor | 等 D1 拍板 |
| `emergency_cancel_after_confirmation` | 基线通过、终态抖动 | 见 D3 |

**未提交的债**：`agents/warranty.md` + `agents/_shared/technician_handover.md` 有改动，
**测试跑到一半被叫停，未验证**。

---

# A 阶段 — 静态检查（几秒，零测试成本）

**先做完这一整阶段，再碰任何需要跑测试的东西。** 已知至少有一个会赔钱的 bug 藏在这里，
而它是读出来的、不是跑出来的。

### [ ] A1. 矛盾扫描 ⬅ **第一项**

写 `scripts/check_consistency.py`：把全部 10 份 prompt + `business_rules.yaml` +
`ticket_states.yaml` 喂给一个 LLM，**只输出矛盾点，不改任何东西**。

**已知必须被它抓到的一条**（用来验证脚本本身有效）：

> 退款截止时点，三处不一致：
> - `_shared/core_rules.md:60` —— "师傅出发或到场后"（**旧规则，早就废弃**）
> - `agents/emergency.md:120-127` —— "确认短信发出后"
> - `config/business_rules.yaml:186` —— `cut_off: "The emergency confirmation message being sent"`（权威）
>
> 另外 `business_rules.yaml:251` 的升级触发条件里也留着旧措辞。

这是会**赔钱**的矛盾：同一件事 prompt 里有两个说法，模型每次挑哪个看运气。

**验收**：脚本跑出来的报告里包含上面这条；修完之后再跑一次，报告干净。

### [ ] A2. 硬编码数字与旧词扫描

正则 + LLM 两遍：

- prompt 里出现的每个价格 / 时长 / 期限，是不是都该从 `rules.*` 读（`small_job.md:41`
  直接写了 "CAD 100"，`emergency.md:42` 也是）
- 换公司名 / 地区 / 价格之后有没有漏改的文件（**这条真踩过**：改名后 core_rules 漏了一处，
  agent 对着客户说了旧公司名）

**验收**：报告列出所有硬编码数字并逐条判定"该配置化 / 可保留"。

### [ ] A3. prompt 去重

扫出的重复约 165 行 / 820 行（**20%**），其中两处是**一字不差的复制**：

| 重复内容 | 位置 | 约 |
|---|---|---|
| **公寓规则** | intake:7-35 · small_job:11-26 · emergency:11-24 | **59 行** |
| 投诉→escalate | 4 个 agent 各一段 + core_rules:66-67 | ~20 行 |
| call-out fee 抵扣 | intake:150 · small_job:39-52 · emergency:41-44 | ~20 行 |
| "同一 turn 完成，没有下一回合" | intake:184-190 · handoff:16-27 | ~19 行 |
| 邮件收资料 | materials.md 全文 · large_job:34-49 重写一遍 | ~16 行 |
| 价格必须查工具 | core_rules:34-38 · tool_protocol:16-27 | ~12 行 |

**不要简单删。** 公寓规则写三遍是**故意的二道防线**，删了会丢保护。抽成
`_shared/apartment_rule.md` 多处 include：保护还在，改一处改全部。

**注意杠杆方向**：公共片段每加 1 行，5 个 agent 实收 4-5 行。所以公共片段要比专属 prompt
**更严格**地控长度。

**验收**：装配总行数从 1577 降到 1300 以下，且 21/24 不退步。

---

# B 阶段 — 规则下沉到工具层（便宜、确定性）

一天下来的硬数据：

> **7 个硬闸，一次都没被绕过。prompt 里的规则，时灵时不灵。**

每一条能在工具层强制的规则，就是一条**不用写进 prompt、不用花 LLM 调用去测、也不会被模型
忘记**的规则。

### [ ] B1. 六条该下沉的规则

现在写在 prompt 里、靠模型自觉的：

| 规则 | 现在在哪 | 下沉成 |
|---|---|---|
| **公寓小活不做** | 三份 prompt **59 行** | 资格闸：`property_type=apartment && category=small` → 拒绝 |
| 保修裁决必须师傅做 | warranty.md | 权限闸 |
| 价格必须来自工具 | core_rules + tool_protocol + judge | 数值来源闸 |
| 不许承诺赔偿 | 4 份 prompt | 权限闸 |
| 大活不许报价 | large_job.md | 权限闸 |
| 没电话不许预约 | core_rules | 前置条件闸 |

公寓那条单独就能**把 59 行 prompt 变成 1 行**，且保证不漏。

**验收**：每条一个单测证明工具层会拒绝；对应 prompt 段落删掉后 21/24 不退步。

### [ ] B2. 声明式闸配置

现有 7 个闸只有 ~60 行代码，但都是硬编码的。抽象成六种模式后写成配置：

| 模式 | 本项目实例 |
|---|---|
| **前置条件闸** X 不能发生除非 Y 已发生 | 没付定金不许派单 |
| **不可逆点闸** X 之后 Y 永久禁止 | 发了确认短信不许自动退款 |
| **上限闸** | 找师傅最多 6 轮 |
| **营业窗口闸** | 周日 / BC 法定假日 |
| **状态机闸** | 工单不许跳步（已是配置驱动） |
| **资格闸** | 公寓小活不做 |

```yaml
gates:
  - type: precondition
    action: calendar.create_appointment
    when: {kind: emergency}
    requires: deposit_paid
    violation: dispatch_before_deposit
    message: "定金未付，不能派单。先发链接并确认到账。"
```

**这也是模版目前缺的一块**——做完同步回 `business-agent-template/`。

**验收**：7 个现有闸全部改成配置驱动，139 个单测不动且全过。

---

# C 阶段 — 让测试变便宜（一次投入，之后每次都省）

### [ ] C1. 单 agent + stub 模式

现在只有端到端一档。加一档：**测单个 agent，下游用 stub**，约端到端 1/5 成本。

端到端**不能省**（分段全绿合起来断链是这类项目的常态失败模式），但它该**只做验收**。
一天 156 次端到端换 3 个 bug，其中大部分是在重复验证已经好了的链路。

**验收**：现有 24 条场景中至少 10 条有单 agent 版本，跑一轮的时间是端到端的 1/4 以下。

### [ ] C2. 录制-回放回归

活模拟器有两个用途被混在一起：

- **探索**——每次措辞不同，能发现没想到的问题。要高温度（现在 0.9）
- **回归**——确认没弄坏别的。要**可复现**

高温度做回归是自相矛盾的：结果本身不稳，分不清是改坏了还是运气差。**这正是
`emergency_cancel_after_confirmation` 那条退化查不清的原因。**

把跑通的对话存成脚本，回归时直接回放、不启动模拟器。成本降一个数量级，可以每次改动都跑。

**验收**：21 条通过的场景全部录下来；回放跑一轮 < 2 分钟且结果 100% 可复现。

### [ ] C3. 降低单次成本

- **模拟人换便宜模型**——客户/师傅/主管的任务是"按人设说人话"，扮演质量对结论的影响远小于
  被测 agent 本身
- **judge 抽样跑**——今天 **3 个 bug 全部是硬断言抓到的，judge 一个都没发现**。它该跑在
  硬断言全过的运行上，或者抽样

**验收**：同样一轮全量端到端，token 消耗降 50% 以上，21/24 不变。

---

# D 阶段 — 才轮到修剩下的失败

**现在 0 硬失败、只有 3 条抖动，不急。** 放在 A/B/C 之后是因为：A 会直接消掉一部分，
B 会消掉一部分，C 会让剩下的调试便宜 5 倍。

### [ ] D1. 判定分类：`no_rule_violations` 硬编码成 framework ⬅ **需要你拍板**

[assertions.py:100](src/plumbing/testkit/assertions.py:100) 把所有硬闸触发一律标成
`FRAMEWORK`（= 不交 doctor），理由写在模块开头：

> every illegal transition seen so far has been a missing edge rather than a misbehaving agent

**全量基线证伪了这句话。** 那 2 条紧急场景的非法迁移，合法路径全都存在，是 agent 抄近路：

- `Emergency Technician Search → Appointment Booked` —— 合法路径 `→ Awaiting Appointment
  Selection → Appointment Booked`，或 `→ Refund Pending → Refund Completed → Appointment
  Booked`。agent **跳过了退款**，正是该场景 judge 要拦的事
- `Emergency Technician Search → Deposit Link Sent` —— 合法路径也在

硬闸拦对了，**是分类错了**。

| | 做法 | 代价 |
|---|---|---|
| **A（倾向）** | 非法迁移改判 `agent`，其余硬闸保持 `framework` | 判得准；但真缺边时 doctor 会改 prompt 绕路而不是报告缺边 |
| **B** | 全部改判 `agent` | 风险最大，doctor 可能被诱导绕开硬闸 |
| **C** | 维持现状，这 2 条手工改 prompt | 不动判定逻辑；自愈跑不到全绿 |

理由：**「走错路」和「撞底线」本来是两类东西**，前者 prompt 能修，后者 prompt 不该修，
现在混在一个 check 里才是根子上的问题。

**但这属于判定逻辑，我立过约束：再动这块要先问你。**

**验收**：选定后那 2 条 `--repeat 4` 跑绿，且 6 个硬闸单测不受影响。

### [ ] D2. 验证 warranty 合并改动（**当前未提交**）

doctor 三轮修不动的根因不是措辞，是 `_shared/technician_handover.md` 原文写
**"The technician reports one of two things"**，两个都是终局 —— **"师傅拒赔"在共享片段里
根本没有表示法**。doctor 每次往 warranty.md 写"被拒之后怎么办"，都被这份片段"只有两种结果"
顶回去。

已做的改动：

- `_shared/technician_handover.md`：两种结果 → **三种**，第三种明确"不是终局"，给两条通用
  原则后**委派**给各 agent 自己的章节，不复述流程
- `agents/warranty.md`：新增 `## When a claim is refused`，**按结果归属而不是按触发条件**
  （原来叫 "If the record rules it out"，师傅拒赔时 agent 认不出这节适用于自己）

**部分数据**（跑到 10/24 被叫停）：`standard_repair_completed` 4/4、
`standard_repair_customer_declined` 3/4、`warranty_approved` 2/2 —— 共享片段的改动没弄坏
那两个终局分支。**目标场景没轮到。**

**验收**：`journey_warranty_rejected_becomes_paid_work` `--repeat 4` 至少 3/4，
且 4 条师傅移交相关场景不退步。**用 C1 的单 agent 模式跑，别用端到端。**

### [ ] D3. 查 `emergency_cancel_after_confirmation` 的退化

基线通过 → 终态抖动，而它在 doctor 那次改动的 23 场景回归里**是通过的**。

两种可能：纯方差，或 **回归的 `--repeat 2` 强度不够**。

**C2 做完之后这条几乎自动有答案**——回放可复现，方差和真退化一眼分开。所以别急着单独查。

---

# E 阶段 — 补业务能力

### [ ] E1. 大额工程的正式报价流程
`quote.*` 四个工具已实现但没接进流程。师傅报价后怎么发给客户、客户接受/拒绝怎么走。

### [ ] E2. 8 个 planned 工具
`config/tool_catalog.yaml` 里声明了但没实现的。

### [ ] E3. 多渠道接入
现在只有文字聊天。短信/邮件入站没做。

---

# F 阶段 — 接真实服务（**全绿之前不做**）

`live_tools_enabled: false` 是总闸，每个工具还有自己的 `live` 开关，**两把都开才走真实**。

### [ ] F1. 逐个工具切 live

**上线顺序**（按风险面从小到大）：

1. **intake 的短信**——最稳、最上游，且只发不做危险动作（不派单、不收钱、不退款）
2. **small_job 的日历**——需要 Google Calendar 真实适配器（F2）
3. **large_job / emergency / warranty 都别急**——E1 没做完；emergency 有跳步会赔钱；
   warranty 断链刚修、未验证

### [ ] F2. Google Calendar 真实适配器
### [ ] F3. 真实 CRM / 工单持久化

---

# G 阶段 — 打磨

### [ ] G1. prompt 长度预算
装配后每个 agent 的行数设上限，超了报警。**公共片段按 ×5 计权。**

### [ ] G2. 控制台加"跑测试"按钮
### [ ] G3. 成本看板
### [ ] G4. 场景覆盖审计
从 `ticket_states.yaml` 的边自动生成路径，覆盖率变成可报的数字。

---

# 已完成

| | 做了什么 |
|---|---|
| ✅ 客户模拟器改纯文本输出 | 坏 JSON 伪装成客户挂断 |
| ✅ 状态机补 7 条边 | 每条在配置里写了为什么；`illegal_ticket_transition` 3 → 2 |
| ✅ 多轮判定 + 自适应确认 | 一次运行不算证据 |
| ✅ 失败按来源分类 + `recurrent` | 决定什么交 doctor、什么报给人 |
| ✅ 重建可信基线 | **前一份"真失败清单"全部作废重来** |
| ✅ `emergency_nobody_available_refund` | 自愈循环第一次在真问题上端到端跑通 |
| ✅ `deposit_payment_fails` | doctor 修好：core_rules"支付问题一律 escalate"太宽，刷卡被拒也去找主管 |
| ✅ 全量自愈第一次跑完 | 21/24，0 失败；doctor 改 4 留 1，回滚机制连续正确工作 3 次；护栏校验 SHA256 完全一致 |

**经验都在这两份里，不在这里**：

- [business-agent-template/METHOD.md](business-agent-template/METHOD.md) —— 按什么顺序做、每步值不值
- [business-agent-template/PLAYBOOK.md](business-agent-template/PLAYBOOK.md) —— 28 条经验，每条对应一次真实失败

---

# 用 /loop 迭代时

1. **A 阶段做完之前，别跑端到端。** 它贵，而且 A 会先消掉一部分问题。
2. **跑自愈期间不要碰 `config/`、`src/`、`scenarios/`、`tests/`** —— 哈希护栏分不清是你改的
   还是 doctor 改的，会把它的修复判成篡改。
3. **抖动的场景不要送 doctor**，只碰运气失败的 prompt 不该被重写。
4. **验证一个机制不要跑全量套件**，挑最相关的几条。
5. **macOS 没有 `timeout`**，用 `until` 循环等，且**先确认没有同名进程在跑**——
   踩过一次，两个基线并发跑在旧代码上，浪费 40 分钟还得出了错结论。
