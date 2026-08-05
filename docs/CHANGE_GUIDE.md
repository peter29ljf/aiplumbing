# 改动规范：一条规则变了，要动哪些文件

业务规则会一直变。这份文档回答一个问题：**你说"这条规则改一下"，我该动哪几个文件？**

写它的直接原因是一次真实的漏改。你当初定的是"大小活判断不出来一律转小额"，这条落进了
`agents/intake.md`，**但 `config/business_rules.yaml` 里还留着旧策略"转免费报价"**。
两份文件互相矛盾地并存了很久，端到端测试跑了 150 多次都没发现，最后是静态扫描读出来的。

所以核心规矩只有一条：

> **每种改动都有"必须一起改的伙伴文件"。漏一个，系统就有两个互相矛盾的说法，
> 而模型每次照哪个走看运气。**

---

## 规则可能住在哪

| 位置 | 放什么 | 谁能改 |
|---|---|---|
| `config/business_rules.yaml` | **唯一真相源**：价格、工时、假期、接不接、期限、间隔、升级触发条件 | 只有人 |
| `config/ticket_states.yaml` | 工单状态 + 哪些迁移合法 | 只有人 |
| `config/agents.yaml` | 有哪几个 agent、各自的 prompt、工具白名单 | 只有人 |
| `config/tool_catalog.yaml` | 工具状态、真实服务开关 | 只有人 |
| `agents/*.md` | 怎么说、怎么判断、什么顺序 | 人 + doctor |
| `agents/_shared/*.md` | 所有 agent 共用的规则 | 人 + doctor |
| `src/plumbing/world.py`、`tools/*.py` | **硬闸**：撞了就拒绝 | 只有人 |
| `scenarios/journey/*.yaml` | 什么必须成立 | 只有人 |
| `tests/` | 硬闸的单测 | 只有人 |

**doctor 只能改 `agents/**.md`。** 其余目录跑自愈时会做 SHA256 快照比对，动了就整次修复作废。

---

## 按改动类型查表

### 1. 改一个数字（价格、时长、期限、阈值、间隔）

> 例：call-out 费从 100 改成 120；保修从 1 年改成 2 年；报价跟进从 48 小时改成 72

**只改 `config/business_rules.yaml`。**

prompt 里**不应该出现这个数字**。如果出现了，那是个 bug——说明 agent 在背数字而不是查工具，
改了配置它还会说旧的。

- ✅ 改完跑 `python3 scripts/check_consistency.py`，它会报出所有写死的数字
- ❌ 不要顺手去 prompt 里"也改一下"——那是在制造第二个真相源

### 2. 改营业时间、假期、服务区域

**改 `config/business_rules.yaml`。**

伙伴文件：`tests/` —— 如果新增了"某天不接单"这类规则，硬闸的单测要跟上。

- ✅ `python3 -m pytest -q`
- ✅ 一致性扫描

### 3. 改"接不接某类活"（资格规则）

> 例：公寓小活不做；某个区域不去；某类设备不修

这是**三处一起改**：

1. `config/business_rules.yaml` 的 `service_policy` —— 规则本身和给客户的说法
2. **工具层硬闸** —— 让它在工具里被拒绝，而不是靠 prompt 自觉
3. 相关 agent 的 prompt —— 只留一句提示，不要抄整段规则

**优先级：能进工具层就别只写 prompt。** 实测数据：7 个硬闸至今一次都没被绕过，
prompt 规则时灵时不灵。

- ✅ 每条硬闸配一个单测
- ✅ 端到端场景覆盖被拒的那条路

### 4. 改流程顺序 / 增删步骤

> 例：紧急服务改成先派单后收定金；保修多一道审核

**四处一起改**，漏一个 agent 就会被自己的系统卡死：

1. `config/ticket_states.yaml` —— 新状态、新的合法迁移边
2. 相关 agent 的 prompt —— 步骤顺序
3. `scenarios/journey/*.yaml` —— 断言要跟着改
4. 可能还有硬闸（如果新顺序有前置条件）

**状态机和 prompt 必须同时改。** 不同步的表现是 agent 反复撞状态机，
看起来像它不听话，其实是你少开了一条边。

- ✅ `python3 -m pytest -q`
- ✅ 相关场景 `--repeat 4`

### 5. 改"谁做决定"

> 例：保修裁决从主管改成当班师傅；某个判断从 agent 改成人工

1. 相关 agent 的 prompt
2. 可能要加工具（比如 `review.request_warranty`）
3. `config/agents.yaml` 的工具白名单
4. 场景断言

- ✅ 端到端

### 6. 改话术（怎么说，不改做什么）

**只改 prompt。** 这是最便宜的一类。

- ✅ 一致性扫描（确认没顺手引入新数字）

### 7. 增删一个服务类型 / agent

1. `config/agents.yaml` —— 注册
2. 新的 `agents/<name>.md`
3. `config/ticket_states.yaml` —— 它的状态
4. 新场景
5. 上游 agent 的分流逻辑

**代码不用动。** 这是"新增 agent = 写一个 md + 加一段配置"这条约束的意义——
守住它，引擎才能一直复用。

### 8. 改禁止行为（硬闸）

1. `src/plumbing/world.py` 或 `tools/*.py` —— 闸本身
2. `tests/` —— 单测
3. `config/business_rules.yaml` —— 如果闸的参数来自配置
4. `agents/_shared/core_rules.md` —— 「绝不能做」清单里那一行

**闸的拒绝信息是给 agent 看的**，要写清楚"为什么被拒 + 该怎么做"，
它有机会自我纠正。

- ✅ 单测必须覆盖"闸真的会拒绝"

---

## 每次改完，按这个顺序验证

**从便宜到贵。前面的过不了，别跑后面的。**

| | 命令 | 成本 |
|---|---|---|
| 1 | `python3 scripts/check_consistency.py` | 几分钟一次调用，抓漏改和矛盾 |
| 2 | `python3 -m pytest -q` | 秒级，抓硬闸和状态机 |
| 3 | 相关场景 `--repeat 4` | 中 |
| 4 | 全量端到端 | 贵，只做验收 |

第 1 步是专门为"改了一处漏了另一处"设计的，也是这份文档存在的原因。**它几秒钟就能跑，
每次改完都跑。**

---

## 三条容易犯的错

**1. 把数字同时写进配置和 prompt。**
以为是"双保险"，实际是两个真相源。改配置的时候忘了 prompt，agent 就说旧价格。
**prompt 里出现的每个数字都该被质疑一次。**

**2. 只改 prompt，不改状态机。**
prompt 说"接下来去 X 状态"，状态机没这条边，agent 撞墙、重试、放弃。
表现出来像模型不听话。

**3. 跑自愈期间改受保护的文件。**
`config/`、`src/`、`scenarios/`、`tests/` 在 doctor 跑的时候有哈希护栏，
它分不清是你改的还是它自己改的，会把这次修复判成篡改作废。**要么等跑完，要么先停。**

---

## doctor 改了什么，去哪看

控制台 **Doctor changes** 标签页，或 `prompt_history/INDEX.md`。

每条标着 `live` 还是 `reverted`、触发场景、doctor 自己写的原因，点开有改动前后全文。
**你审过的 prompt 被自动改过什么，这里是唯一的账。**
