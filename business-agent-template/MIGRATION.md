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

## 第 0 阶段：已完成 —— 以及它教了什么

六条全部做完，`plumbing` 之外的三个项目重跑验证。**最值钱的不是那些修复，是被数据推翻的那条
诊断，和我在修复过程中新造出来的同一个 bug。**

## 做了什么

| | 改动 | 实测 |
|---|---|---|
| 0.1 | 宣称按 world-state delta 判，不按词表 | 假阳性 13 → 0 |
| 0.2 | 模拟客户的 DONE 也按状态判 | dental 8/13 → 16/16 |
| 0.3 | 客户跑在另一个模型家族上 | accounting 67% → 80%（repeat 4） |
| 0.4 | 记录停止原因；预算按图推导；停滞检测 | 判词不再猜 |
| 0.5 | 静态前缀移进 system 消息 | 缓存 49% → 待测 |
| 0.6 | 第四类故障 `grader` | 「改测试，不要改 agent」 |

## 一、方案里 0.4 的诊断是错的

方案说「四分之一到一半的失败是回合预算抄错」。数据不同意：

| 场景 | 用了 | 上限 |
|---|---|---|
| new_patient_booked | 11 | 36 |
| treatment_question | 6 | 34 |
| insurance_question | 15 | 38 |

**一条都没跑到上限。** 而按图推导出来的预算是 26，**比生成器写的还低**——照方案改会让情况更糟。

真正的问题是那句判词自己在撒谎：「still moving when the turns ran out」是**推断**出来的。
harness 知道自己为什么停，只是没说。全部是模拟客户提前走了，也就是 0.2 修的东西。

**教训**：一个从证据推断原因的判词，会在原因变了之后继续自信地说旧答案。让知道的人说。

## 二、新检测器带着旧检测器的病来了，只是换了条路

修完子串匹配之后，第一次真实运行给出 10 条宣称类判决。**全部是假的**，三种方式：

- **按单次调用算 delta，不按节点这一轮。** 一个节点第 1 次调用建单、第 3 次才说「你都安排好
  了」——第 3 次的 delta 是空的。10 条里 7 条。
- **「not booked yet」没被认成对冲**，因为写的是相邻的 `not yet`。中间那个词恰恰是一句谨慎的话
  用来放它不打算宣称的东西的位置。
- **「You're all set」就是普通英语的「这一段完了」。** 5 条是「you're all set, your record is
  open」，那是真的。

三条都是「按表面判句子」，和它替换掉的那个错误一模一样。

**教训**：换掉一个坏机制的时候，坏机制的**思维习惯**会跟着搬家。修完之后必须拿真实数据再跑一次，
不能只看测试绿了。

## 三、一处诚实的让步

有两条我自己写的测试坚持光秃秃的「You're all set」算撒谎。真实数据说那通常是良性的。**数据赢。**

放弃了什么：一个中途节点对着等预约的客户说一句光秃秃的「都好了」，不再被抓。收尾闸仍然抓得住
终端节点的那一种，所以损失是「中途节点说话含糊」——那是 smell 不是 lie。

这条让步写在测试注释里，没有藏起来。

## 四、还没验证的事

**检测器在唯一调过的那次运行上归零，也可能意味着我把它调到了永不触发。** 挡着这个的是测试里那
8 条正例，全是真实 agent 真说过的话。**真正的判据是它在新数据上触发一次真的**——那个结论还没有。

---

# 第 1 阶段：换底座

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

**验收**：45 条引擎测试改写后全过（不花钱，先跑这个）；然后 `dental` 跑回 16/16 同级。
**这条不达标就回退。**

参照项目从 `plumbing` 换成 `dental`：`plumbing` 是手写的，每跑一轮几十万 token，而它证明的
东西 `dental` 也能证明——`dental` 还多证明一件事，它是生成器自己造出来的。

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
- **1.1 之后 `dental` 跑不回 16/16** → 底座换坏了，回退，不要往前走
- **2.1 三轮仍卡 65%** → 按 DD-GEPA 结论，问题在 trace 多样性不在优化器
- **2.2 实验显示拐点远高于 8,000 字符** → 瓶颈是指令条数不是字符数，转向拆节点
- **2.3 之后 `takeaway` 仍建不出** → 图状架构对多意图确实不够，评估 Parlant

---

## 一句话

**第 0 阶段不引入任何依赖，修的全是「测量装置在撒谎」——那是唯一会让整个系统不可信的一类问题，
而且今天发生了三次。第 1 阶段才换底座，验收是参照项目跑回 59/60。**
