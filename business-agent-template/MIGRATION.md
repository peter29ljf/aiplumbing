# 整改方案：先修仪器，再换底座

七个问题的研究结论已经在手。这份讲**按什么顺序改、每一步的验收是什么**。

排序原则是这个项目自己的第一条规矩，也是我今天违反了三次的那条：**先验证仪器，再验证产品。**
第 0 阶段一行 LangGraph 都不碰——因为现在的测量装置在撒谎，换底座只会让它用更先进的方式撒谎。

---

## 一处必须先说清的取舍：LangGraph 放在哪一层

生成器的前提是 **agent 是数据，不是代码**。这条前提撑着三件事：

| | 靠什么成立 |
|---|---|
| `graph.load()` 一次报出全部问题 | flow.yaml 是声明式的，可以静态检查 |
| 控制台能安全编辑节点和规则 | 编辑的是 yaml/markdown，不是可执行代码 |
| 生成器的正确性面小一个数量级 | 它写 yaml + markdown，不写 Python |

原生 LangGraph 里节点是 Python 函数、图是 Python 代码。**生成器改写代码之后这三条全部失效。**

**方案：LangGraph 当执行底座，声明层保留。** `flow.yaml` 仍是唯一真相源，加载时编译成
`StateGraph`：

```python
def compile(flow: Flow, checkpointer) -> CompiledStateGraph:
    graph = StateGraph(TicketState)
    for node in flow.nodes.values():
        graph.add_node(node.name, _step(node))          # 闭包，捕获节点声明
    for node in flow.nodes.values():
        if node.branch:
            graph.add_conditional_edges(node.name, _route(node), node.branch)
        elif node.next:
            graph.add_edge(node.name, node.next)
        else:
            graph.add_edge(node.name, END)
    graph.set_entry_point(flow.entry)
    return graph.compile(checkpointer=checkpointer)
```

拿到的：checkpointer、`update_state(as_node=)`、子图、interrupt、LangSmith/Langfuse。
保住的：验证器、生成器的写入面、故障分类器、`_undone`、`remembers`、节点耗时表。

---

## 第 0 阶段：让测量装置可信（不引入任何依赖）

**为什么在最前**：今天三次「仪器坏了产品」——读错报告谎报达标、我加的检查停掉三个能跑的项目、
检测器把正确行为判成撒谎。在这些修好之前，任何通过率数字都不知道是真是假。

### 0.1 `CLAIMS` 子串检测器 → state-delta 判据

**证据**：`accounting` 被判「said 'booked' from a node with no calendar.create_appointment」，
原句是 *"I won't say you're booked just yet, because that confirmation happens in the next
step, but I'm passing it through"* ——**做得完全正确**。14 条模型故障里至少 3 条是这个假阳性，
而模型故障数是「该不该换模型」的唯一依据。

研究结论一致：LLM 裁判在这件事上封顶 AUROC 0.65，因为它 anchor 在收尾措辞上——**和子串匹配
是同一个错误**。

改法两层：

1. **主判据 = 状态变化**。一个节点若在它的回复里断言了某个状态变更，那个变更必须在
   `tickets.tags` / `world` 快照里真的发生过。不看词，看 delta。
2. **文本侧兜底 = protected value 规则**（ProvenanceGuard 式）。只有当宣称里的关键值
   （预约 id、时间、技师名）能在这一步的 trace 里找到时才放行。

`_undone` 保留不动——它本来就走的是状态路线，是这套系统里最领先的一处。

**验收**：`tests/test_diagnose.py`，用真实那句话做反例；`accounting` 的模型故障数应从 14 降到
11 以下，且降下来的都能人工确认是假阳性。

### 0.2 模拟客户的 DONE 也绑到状态（同一个机制，顺带解决 Q6 一半）

现在模拟客户听到「师傅会跟您联系」就结束——而那句话在一半的流程里是中间态。这制造了假的
「conversation never finished」。

改成：DONE 的判据是 world state（有没有真的产生预约/工单状态变更），不是措辞。

**验收**：`dental` 和 `accounting` 里「still moving when the turns ran out」的条数明显下降，
且下降的那些确实是流程已经完成的。

### 0.3 模拟客户换到不同模型家族

最低成本的改动。现在被测 agent 和模拟客户都是 DeepSeek，同族自评偏差无法排除。

**验收**：跑一次对照，看通过率变化幅度——若变化 > 5 个点，说明之前的数字有系统性偏差。

### 0.4 `max_turns` 按图推导 + 停滞检测

**证据**：`dental` 的 `new_patient_booked` 走到 `offer_times` 就没回合了，还差两步。
生成器把 10 节点参照项目的常数抄给了 18 节点的图。harness 故障占失败的 26–56%，大头是这个。

```
max_turns ≈ 最长路径节点数 × 每节点平均往返 + 余量
```

外加停滞判停：`(tool_name, world_state_hash)` 连续 3 轮无变化 → 判「卡住」并终止，与「撞上限」
分开报。研究里明确说「按图直径推导」没有成熟学术方案，这是工程估算——所以**关键不是公式准，
是把「预算耗尽」和「逻辑错误」在故障分类里分开**。

**验收**：三分类里 harness 故障占比下降，且剩下的能明确归到「真的卡住」或「真的收敛」。

### 0.5 缓存前缀稳定（自建循环 49% → 目标 80%+）

自建循环每轮重发整个消息列表，前缀不稳定 = 频繁 miss。Claude Code 84% 是因为静态前缀固定在最前。

改法：静态系统提示词 + 工具定义永远在最前且逐字节稳定；动态部分（world state、用户输入）放最后；
只追加新 turn，不重建 message list。

**验收**：`bat/builder/agent.py` 的 cache hit 从 49% 到 80% 以上。研究给的量化是省 41–80% 输入成本。

### 0.6 「测试台故障」拆成两类

研究里学界的三分法（[arXiv 2607.28802](https://arxiv.org/abs/2607.28802) Model or Harness?）
把它拆成 environment 故障 vs grader 故障。我今天遇到的正是后者（模拟客户过早 DONE、断言写错）。
拆开后诊断更精准。

---

## 第 1 阶段：换底座

前提：第 0 阶段做完，通过率数字可信了。

### 1.1 `flow.yaml` → `StateGraph` 编译器

新增 `bat/runtime/compile.py`。`graph.py`（验证器）、`assemble.py`（提示词装配）、
`registry.py`（工具）全部不动——它们在编译之后仍然是同一套。

`engine.py` 的循环被 LangGraph 的 `invoke`/`stream` 取代，但**三个机制必须显式移植**，
它们是 LangGraph 不提供的：

| 机制 | 移植成什么 |
|---|---|
| `_undone` 收尾闸 | 终端节点函数内的 postcondition 检查，不满足就 `Command(goto=自己)` |
| `_still_here` 推动 | 同上，回复计数超限就注入 system 消息 |
| `remembers` | 工具调用后写进 `TicketState`，机制不变 |

**验收**：45 条引擎测试改写后全过；`plumbing` 参照项目跑出 59/60 同级的分数。**这条不达标就
回退**——参照项目是唯一能证明底座没换坏的东西。

### 1.2 Checkpointer：进行中对话可恢复

这是 LangGraph 带来的、我自己没有的东西。行业数据说 60%+ 的生产事故来自状态管理。

用 `SqliteSaver`（本地）/ `PostgresSaver`（生产）。每个对话一个 thread id。

**验收**：跑到一半 kill 掉进程，用同一个 thread id 恢复，对话从中断处继续且工单状态完整。

### 1.3 节点级场景：生成器强制产出

**证据**：`dental` 0/13 条节点级场景，手写的 `plumbing` 是 4/15。这是最有效的诊断工具
（15 秒对 250 秒），而且**「单节点 4/4、整链路 2/4」那道鸿沟本身就是诊断**。

底座换了之后用 `update_state(config, values=..., as_node=...)`，官方文档明确支持在全新 thread
上做（原话 *"No execution history: Setting up state on a fresh thread (common in testing)"*）。

再加一条自动诊断规则：**某节点单测全过但其下游整链路失败 → 自动标记「上游漏写了下游依赖的
状态」**。这就是 consumer-driven contract。

**验收**：生成器为每个节点至少产出一条节点级场景；上述自动诊断规则在 `dental` 上能定位到真实
的漏写。

---

## 第 2 阶段：更难的三件

### 2.1 iterate 换成真 GEPA

现在的 iterate 是 GEPA 的手工版。换成 `dspy.GEPA`，拿 Pareto 前沿 + 长度正则。

**但要带着这条警告做**：[DD-GEPA](https://arxiv.org/abs/2606.07894) 观察到「best program 很早
出现后就不再提升」，诊断是 trace 多样性不足 + reflection prompt 3 条 trace 就超 20,000 token。
**这和我 63% → 65% → 65% 的平台期几乎一模一样**——说明平台期可能是这类方法的固有瓶颈，不是
实现问题。

所以：不要只喂 3 条 trace，把分支判断失败（投诉/非投诉那类）的边界样本单独成 minibatch。

**验收**：`dental` 从 62% 起步，三轮内若仍卡在 65% 附近，按 DD-GEPA 的结论转向人工 curate
边界样本，而不是继续调优化器。

### 2.2 提示词长度：把 8,000 的硬线换成实测拐点

8,000 是我拍的，没有实验支撑。`dental` 的 `urgency` 已经 9,611。

研究给的定量：ManyIFEval 的 curse of instructions（prompt-level accuracy ≈
instruction-level^n，GPT-4o 遵循 10 条指令只有 15%）、IFScale 的三种退化曲线 + primacy
effect（早写的指令更被遵循）、LLMLingua 的 20× 压缩掉 1.5 分。

两条具体动作：
1. **关键规则前置**（primacy effect），postcondition 和分支规则放最前，样例和工具描述靠后
2. **主指标从字符数换成指令条数**——curse of instructions 说明伤害遵循率的是独立指令数量。
   单节点指令太多就拆节点，这比压字符有效

**验收**：跑一次「压缩率 vs 通过率」小实验，找到本项目的拐点，把 8,000 换成实测数字。

### 2.3 多意图：子图

**证据**：`takeaway`（订位 vs 外卖）两轮都产不出 flow.yaml。研究说 routed graph 在意图路由变多
时越来越脆，超过 8–10 个意图就难管。

改法：生成器先检测简报里的意图数，> 1 时切换形状——意图路由 backbone + 每意图一个子图，而不是
硬塞一条线。LangGraph 原生支持嵌套子图。

**caveat**：默认子图没有自己的 checkpointer，只能从父层做 time-travel；要在子图内部做节点级
测试，子图要 `checkpointer=True`。

**验收**：`takeaway` 能建出来并跑通订位和外卖两条路。**这条同时定义了架构的适用边界**——写进
生成器的自检：单意图线性业务用现在的形状，多意图用 backbone + 子图。

---

## 不做 / 缓做，及理由

**学习式 false-success 检测器**（TF-IDF + 逻辑回归，AUROC 0.83/0.95）。需要**本域标注数据**，
我有大量轨迹但没有标签，而且研究明确说跨域迁移要重新校准。0.1 的 state-delta 判据免费拿到大部分
价值——先做那个，还不够再上这个。

**Parlant**。guideline + journey 架构确实对多意图更合适，但在 LangGraph 之上再引入第二个框架
是很大的面。2.3 的子图路线在 LangGraph 内部就能拿到多意图的收益。若 2.3 做完 `takeaway` 仍然
不行，再评估 Parlant。

**Temporal**。研究给的判断标准是「失败的一步可能重复一个不可逆的业务动作」时才需要。我的 24
小时回访循环正是这种场景，但 LangGraph 的 checkpointer + 一个 idempotency key 先够用。真到了
「漏回访或重复回访」出事，再上 DBOS（零新基础设施，只要 Postgres）。

**形式化 postcondition（Lean4 / SMT）**。`_undone` 已经在做这件事的实用版本。升级成 Hoare
三值语义（satisfied / violated / inconclusive）值得做，但排在 non-atomic failure
（超时后延迟成功）真的咬到之前不急。

---

## 会推翻上面排序的信号

- **0.3 换模型家族后通过率掉 > 5 个点** → 之前所有数字都偏高，优先级从「提通过率」转向「提模拟
  保真度」，第 1 阶段往后推
- **1.1 之后 `plumbing` 跑不回 59/60** → 底座换坏了，回退，不要往前走
- **2.1 三轮仍卡 65%** → 按 DD-GEPA 结论，问题在 trace 多样性不在优化器
- **2.2 实验显示拐点远高于 8,000 字符** → 瓶颈是指令条数不是字符数，转向拆节点
- **2.3 之后 `takeaway` 仍建不出** → 图状架构对多意图确实不够，评估 Parlant

---

## 一句话

**第 0 阶段不引入任何依赖，修的全是「测量装置在撒谎」——那是唯一会让整个系统不可信的一类问题，
而且今天发生了三次。第 1 阶段才换底座，验收是参照项目跑回 59/60。**
