# 把 flow/ 变成一台生成器

给它一张业务流程图，它问清楚该问的，写出方案给你核对，然后生成节点图、规则文件、工具、场景，
自己跑 harness 到可用为止。写代码的引擎是 Claude Code CLI 无头模式。

---

## 这个目录里已经有的东西

四份文档不是过时的笔记，**它们正好是生成器缺的那一半**：

| 文件 | 在生成器里的角色 |
|---|---|
| `CHECKLIST.md` | **interview 阶段问什么** —— 11 节填空清单 |
| `PLAYBOOK.md` 第七节 | **plan 阶段产出什么** —— 9 步建设顺序 |
| `METHOD.md` 第四节 | **test 阶段按什么顺序验** —— 成本阶梯 |
| `ARCHITECTURE.md` | **哪些能搬哪些要重写** —— 三层边界，含上门/到店的差别 |

里面几条经验今天又被独立验证了一遍：

- *"先验证仪器，再验证产品"* —— 今天四个半小时里最贵的错都在测试台
- *"能进工具层的规则，就别进 prompt"* —— `_undone` 收尾闸、`remembers` 机制
- *"一次运行不算证据"* —— `--repeat 4`
- *"验证一个机制不要跑全量套件"* —— 今天做的节点级场景，510 秒变 66 秒
- *"失败按来源分类"* —— 故障分类器
- *"五处一次改完再跑验收"* —— PLAYBOOK 六.23

**这四份要改写到新架构**（节点代替 agent、规则文件代替 prompt、故障分类器代替 judge），
经验全部保留并补上今天新学到的。

**`scripts/new_business.py` 和 `template/*.tmpl` 删掉** —— 它们生成的是老架构（5 个 agent +
orchestrator + 32 个状态）的项目，那套已经被重写掉了。

---

## 通用化的成本

`flow/` 里只有三处耦合：

| | |
|---|---|
| `flow/runner/harness.py` | `from plumbing.llm import LLM`、`from plumbing.paths import load_dotenv` |
| `flow/sim/world.py` | `from plumbing import config` |

加三个写死的路径常量（`assemble.FLOW_DIR`、`graph.FLOW_DIR`、`harness.SCENARIOS`）。

**其余原样可用**，包括今天赚来的：故障分类器、节点耗时表、`remembers`、`_undone` 收尾闸、
`_still_here` 推动、节点级测试。

而且 `flow/world.py` 那个正在进行的重构（两个后端共用一套词汇）**正好是生成器需要的形状**：
工具写一次，模拟器和真实服务都能跑。

---

## 目录

```
business-agent-template/
  PLAN.md  METHOD.md  PLAYBOOK.md  ARCHITECTURE.md  CHECKLIST.md  README.md
  bat/
    runtime/          引擎，从 flow/ 通用化而来
      graph.py engine.py assemble.py memory.py           + project_dir
      harness.py diagnose.py smells.py followup.py
      world.py          两个后端共用的词汇（从 flow/world.py 来）
      sim.py            模拟后端
      tools.py          @tool 注册表 + 预设/自定义工具加载
      llm.py            从 src/plumbing/llm.py 搬来，provider 按项目配
    presets/
      always.md         每个节点都带的通用前言
      tools/            16 个验证过的服务派单工具，按族分文件
      rules/            成型的规则写法：交棒、收尾、拒绝
      harness.yaml model.yaml
    builder/
      claude.py         驱动 claude -p，流式，记账
      session.py        一个项目的构建会话：阶段、记录、花费
      phases.py         interview -> plan -> build -> test -> iterate
      prompts/          生成器自己的系统提示词，每阶段一份
    console/
      server.py         http.server + SSE，不装依赖
      static/
    projects/<name>/    生成出来的 agent：flow.yaml rules/ tools/ scenarios/
                        business_rules.yaml model.yaml harness.yaml
                        PLAN.md runs/ spend.jsonl
  tests/
```

**运行时共享，项目里只有数据。** 每个项目复制一份引擎，等于生成 N 份要各自维护的 bug。

---

## 生成器的五个阶段

| 阶段 | 干什么 | 停不停 |
|---|---|---|
| **interview** | 贴流程图 + 讲生意。按 `CHECKLIST.md` 提问 | 每一问都停 |
| **plan** | 按 `PLAYBOOK.md` 第七节写方案：节点、分支、规则、工具、场景 | **停，等批准** |
| **build** | Claude Code CLI 写文件 | 不停 |
| **test** | 自动跑 harness `--repeat 4` | 不停 |
| **iterate** | 配置类故障自己修；要业务决策的**主动停** | 按需停 |

### 三种停

- **手动暂停** —— 当前这一步做完就停，不打断半截
- **需要人拍板** —— 抛出问题等回答。这是业务决策，不是技术问题
- **达标自停** —— 见下

### 「可用」的判定

```yaml
usable:
  every_scenario_clean: true    # 每条场景 --repeat 4 全过
  min_pass_rate: 0.95
  config_faults: 0              # 不可放宽
  stop_after_flat_rounds: 2
```

**配置故障必须是 0。** 那是规则文件和工具清单互相矛盾——生成器自己的 bug。今天 `no_number`
就是一条：规则让它报价，`always.md` 禁止说没查过的数字，而节点没有查询工具，结果它花 19 秒
编了一段拒绝。

`stop_after_flat_rounds` 是今天那个 loop 的教训：分数在 84–86% 之间跳了三轮，真进步来自看
故障**移动**到哪里，不是看总分。

### 现成的验证器就是编译器

`graph.load()` 已经一次报出全部问题——分支指向不存在的节点、规则文件缺失、工具不存在、
节点不可达、既有 `next` 又有 `branch`、非终端节点没有 `step.finished`。

**生成器写完 flow.yaml 就跑它，报错原样喂回 Claude Code CLI。** 不用另写校验。

---

## 控制台：7 个页面

`http.server` + SSE + 原生 JS，不装依赖。生成器输出是流式的，SSE 正好。

| 页面 | 内容 |
|---|---|
| **Build** | 和生成器聊天。流式输出、当前阶段。按钮：暂停 / 批准计划 / 回答问题 |
| **Flow** | 节点图。点节点看目标、规则、工具、**装配后的字节数**、`sets_status`。可编辑 |
| **Rules** | 规则文件可编辑，每次保存留历史 |
| **Tools** | 预设库勾选 + 自定义工具增删。显示每个工具被哪些节点用 |
| **Test chat** | 直接和生成的 agent 聊。显示当前节点、工单状态、调用的工具、耗时 |
| **Harness** | 场景列表、参数、跑测试 |
| **Dashboard** | 通过矩阵、故障分类、节点排行、耗时表、token/缓存/花费 |

**字节数那一列是验收指标**，不是装饰：哪个节点悄悄胖回去一眼可见。今天 `greeting` 被这么
抓到三次。

---

## 开发测试用 Bailian 的 deepseek，定论才换官网端点

`bat/presets/model.yaml` 默认 `active: bailian-deepseek`，新项目继承这个。

同一个模型家族，实测不是同一回事：28 次运行的套件上 82% 对 84–90%，缓存命中 60% 对 84%。
**这个差距正是要在便宜端点上开发的理由**——在它上面暴露的失败，几乎全是我们自己的规则文件
或场景写错了，而那正是值得找出来的那一类。今天整轮下来，Bailian 找出的每一条都是这样。

叫「完成」之前换回 `deepseek` 跑一遍定论。这一步写进 harness 的达标判定里。

---

## 两个计费表，分开算

| | 谁 | 从哪来 |
|---|---|---|
| **builder** | Anthropic（Claude Code CLI） | stream-json 的 result 消息，写进 `spend.jsonl` |
| **agent** | 生成的 agent 用的 provider | `runtime/llm.py` 现成的 `Usage`，已在算 `cache_hit_rate` |

**缓存命中率是要盯的数。** 今天同一个模型换端点，60% → 84%，输入成本直接减半。

*字段名先用一次最便宜的调用打印出来确认，不要照记忆写。*

---

## 权限边界

```bash
claude -p "<prompt>" \
  --output-format stream-json --include-partial-messages \
  --dangerously-skip-permissions \
  --add-dir business-agent-template/projects/<name> \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash(python3 -m bat.runtime.harness*)" \
  --resume <session-id>
```

cwd 设在项目目录，`--add-dir` 只给那一个项目。能自由建节点、写规则、加工具、跑 harness，
碰不到仓库其他地方。Bash 只放 harness 一条命令——无人值守的会话不需要通用 shell。

---

## 一条竖线一条竖线

### 竖线 1：跑通全流程

runtime 通用化 + builder 驱动 claude -p + 控制台只有 Build 页和 Dashboard 页 + 自动跑 harness。

**验收：把 plumbing agent 重新生成一遍。** 有现成的 59/60 做对照，生成出来的好不好一眼可见。
一个没有测试对象的生成器是没法验证的。

### 竖线 2：Flow 页 + Rules 页
验收：故意把 `flow.yaml` 改坏，确认存不进去且报错说清是哪一条。

### 竖线 3：Tools 页 + 节点增删
验收：加一个自定义工具挂到节点上，在 Test chat 里跑到它被调用。

### 竖线 4：Test chat 页
验收：手聊一遍，每轮显示当前节点、工单状态、工具、耗时。

### 竖线 5：Harness 页 + Dashboard 补全
验收：把 `min_pass_rate` 调到 0.5，确认提前自停；调回 0.95 确认继续跑。

---

## 验证

```bash
python3 -m pytest business-agent-template/tests -q
```

runtime 通用化之后，`flow/tests` 那 45 条要能**原样搬过来跑通**——它们测的是引擎机制，
和哪门生意无关。搬过去还能过，就是没改坏东西的证据。

```bash
cd business-agent-template && python3 -m bat.console
```

---

## 已知风险

1. **Claude Code CLI 的 result JSON 字段名**靠记忆写会错。第一步先打印出来。
2. **无人值守 + skip-permissions**：靠 `--add-dir` 和 `--allowedTools` 限住，不靠自觉。
3. **生成器写出跑不起来的 flow**：`graph.load()` 一次报全部问题，原样喂回去。现成的。
4. **`flow/` 正在被重构**（`flow/world.py` 两后端拆分，未提交）。搬运时以当时的 HEAD 为准，
   不要搬半截。
