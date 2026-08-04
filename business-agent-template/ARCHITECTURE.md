# 三层边界：哪些能直接搬，哪些每次都要重写

这套东西约 6600 行。做第二个 business 时，**能原样搬走的比想象中多**——但前提是知道界在哪。

## 分层

```
┌─ 第 1 层：引擎（约 4000 行）───────────────── 原样搬，不用改 ─┐
│  llm.py            按角色调模型、重试、用量与缓存统计          │
│  config.py         YAML 加载与缓存                             │
│  paths.py          路径解析、.env 加载                          │
│  agent.py          通用 agent 循环（prompt + 工具 + tool call） │
│  agent_registry.py 从配置装配任意 agent                         │
│  orchestrator.py   对话编排、交接、收尾阶段、调度器             │
│  livestatus.py     运行状态广播                                 │
│  tools/registry.py 工具注册、白名单、dispatch、审计日志         │
│  testkit/*         runner / assertions / judge / doctor / loop  │
│  dashboard/*       本地控制台                                   │
│  integrations/gate.py  真实服务的双开关                         │
└────────────────────────────────────────────────────────────────┘

┌─ 第 2 层：服务业套件（约 1500 行）──── 改参数和词汇，不改结构 ─┐
│  world.py 的一部分：                                            │
│    · 虚拟时钟 + 工作日/节假日/营业时间          ← 任何行业都要 │
│    · 工单与状态机                                ← 任何行业都要 │
│    · violation 记录                              ← 任何行业都要 │
│    · 客户档案 + 历史工单 + 历史对话              ← 服务业通用   │
│    · 日历与时段查找                              ← 预约类通用   │
│    · 支付/定金/退款                              ← 收订金的通用 │
│    · 现场人员（师傅/技师/医生/理发师）           ← 派工类通用   │
│  tools/ 的大部分：clock rules crm calendar sms email payment   │
│         ticket schedule handoff conversation escalate           │
│  sim/*  三个人类模拟器（客户/现场人员/主管）                    │
└────────────────────────────────────────────────────────────────┘

┌─ 第 3 层：行业（约 1100 行 + 全部 prompt）── 每次重写 ─────────┐
│  config/business_rules.yaml   价格、工时、拒接范围、升级规则    │
│  config/ticket_states.yaml    这个行业的工单状态机              │
│  config/agents.yaml           切几个 agent、各自什么工具        │
│  config/world_seed.yaml       种子数据                          │
│  agents/*.md                  每个 agent 的 prompt              │
│  personas/*.md                模拟人的性格模板（薄）            │
│  scenarios/*.yaml             端到端场景                        │
│  world.py 的行业特化部分       本例：紧急费率档、派单确认判定    │
│  tools/ 的行业特化工具         本例：保修资格、安全提示          │
└────────────────────────────────────────────────────────────────┘
```

## 引擎有多干净

实测：**引擎里只有一行逻辑带行业词汇**（`assertions.py` 里一句
`"{n} technician calling rounds"` 的报错文案）。其余的行业词全在注释举例里。

这不是运气好，是因为一开始就定了「代码里不写 `if 紧急:`」。分流由 agent 调 `handoff.transfer`
自己决定，编排器只负责切换。**这个约束值得在新项目里继续守住**——它是引擎能复用的唯一原因。

> 脚手架不能只改包名：注释里的行业举例也要换，否则新项目一打开满眼都是上一个行业的例子，
> 读代码的人会被误导。

## 「上门」和「到店」的差别在第 2 层

两种履约形态共用客户档案、日历、支付、状态机，差别集中在这几处：

| | 上门（technician goes out） | 到店（customer comes in） |
|---|---|---|
| 地点 | 客户地址，要算服务区、路程 | 我们的门店，要算门店产能 |
| 人员 | 有服务区、有技能、会在路上 | 绑定门店、有专长、不移动 |
| 紧急 | 派单、找人、定金、退款窗口 | 一般没有；改成加急/插队 |
| 提醒 | 出发通知、ETA | 到店提醒、迟到/爽约处理 |
| 时段 | 人员日历的空档 | 门店 × 人员 × 椅位/诊室 的空档 |

**建议**：`world.py` 里把 `Technician` 改叫 `Staff`，加 `travels: bool` 和 `location_id`；
`find_slots` 增加一个可选的 `location` 维度。配置里加：

```yaml
service_delivery: on_site        # on_site | in_store | both
locations:                       # in_store / both 时才需要
  - { id: "main", name: "...", address: "...", chairs: 4 }
```

派单/紧急那一组工具只在 `on_site` 时注册；到店提醒/爽约那一组只在 `in_store` 时注册。
`both` 就都装上，由 agent 按具体单子选。

## 迁移时最容易漏的三件事

1. **状态机是行业的，不是通用的。** 别想着复用上一个行业的状态表，重新画。
   （参见 PLAYBOOK 第 1 条：状态机和 prompt 不同步的代价）
2. **judge 的基础评判项要跟着行业调。** 「语言一致」「不编价格」「不泄露内部信息」
   「不编造行为」这四条通用；其余按行业加。
3. **doctor 的受保护目录清单**（`doctor.py` 的 `PROTECTED_DIRS`）在新项目里要重新确认，
   漏掉一个目录就等于给它开了作弊的口子。
