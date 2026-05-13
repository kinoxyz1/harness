# Task / Subagent Runtime 设计

> 日期：2026-05-10
> 状态：待评审
> 相关文档：
> - [2026-04-15-skills-system-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-skills-system-design.md)
> - [2026-04-17-inline-local-skilltool-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)
> - [2026-04-17-skill-and-task-runtime-parity-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md)
> - [2026-04-17-todo-task-parity-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-todo-task-parity-design.md)
> - [2026-04-17-runtime-control-plane-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)
> - [2026-05-10-llm-skill-relevance-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-llm-skill-relevance-design.md)
> - [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md)
> 当前相关实现：
> - [core/tools/builtin/todo.py](/Users/kino/works/kino/harness/core/tools/builtin/todo.py)
> - [core/tools/builtin/skill.py](/Users/kino/works/kino/harness/core/tools/builtin/skill.py)
> - [core/tools/runtime.py](/Users/kino/works/kino/harness/core/tools/runtime.py)
> - [core/query/loop.py](/Users/kino/works/kino/harness/core/query/loop.py)
> - [core/session/state.py](/Users/kino/works/kino/harness/core/session/state.py)
> - [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)
> - [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py)
> Claude Code 参考：
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/AgentTool.tsx`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/runAgent.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/forkSubagent.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/prompt.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/SkillTool.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TodoWriteTool/TodoWriteTool.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TaskCreateTool/TaskCreateTool.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TaskUpdateTool/TaskUpdateTool.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/tasks.ts`

---

## 1. 摘要

本文档提出一个新的主方向：**停止继续强化“主 Agent 自己识别并自己同时执行所有子任务”的路线，转而引入以 `Task` 为中心的运行时模型。**

核心结论是：

1. `TODO` 应只承担**用户可见的进度视图**职责，而不是规划与调度的权威状态。
2. `TASK` 应成为**主 Agent、subagent、skill、执行边界**之间的正式中介对象。
3. `subagent` 的输入边界不应由“当前对话全文 + 一句模糊提示词”决定，而应由结构化的 `TaskPacket` 精确构造。
4. `subagent` 必须区分两类上下文模式：
   - **fresh**：零上下文启动，主 Agent 负责写完整 briefing
   - **fork**：继承父上下文，只附加短 directive
5. 从 `b9ae764` 开始的 `SkillRelevancePolicy` 强化方向，并没有触及真正的问题层级。它解决的是“主模型更容易想到 skill”，但没有解决“任务如何被拆分、边界如何被表达、多个子任务如何不互相污染”。

推荐的新模型是：

```text
User Request
  -> Task Planning
  -> SessionState.task_state (权威)
  -> Todo Projection (用户视图)
  -> Task Dispatch (local / fresh_subagent / fork_subagent)
  -> TaskResult Merge
  -> Replan / Complete
```

这个模型与 Claude Code 的关键启发一致，但不照搬其所有基础设施。本文会明确区分：

- **Claude Code 直接源码证据**
- **基于 harness 当前架构作出的设计选择**

---

## 2. 背景与问题复盘

## 2.1 当前两条近因

最近两份文档：

- [2026-05-10-llm-skill-relevance-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-llm-skill-relevance-design.md)
- [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md)

都在尝试解决“模型没有正确使用 skill”这个表象问题。

对应代码从 `b9ae764` 之后开始：

- `b9ae764`：增加 `SKILL_LLM_MATCH`
- `59cb1e9`：引入 LLM-based skill relevance matching
- `5a15242` / `3898545`：把 `model_gateway` 传给 `SkillRelevancePolicy`

这条路线的隐含假设是：

> 只要主模型更容易注意到 skill，或者更容易被提醒先调用 skill，执行质量就会提升。

现在用户实际观察到：**任务效果仍然不理想。**

这说明问题不在“提醒是否足够强”，而在于：

- 一个请求中的多个真实工作单元没有被显式拆开
- skill、todo、read、bash 仍然混在同一个推理回合里竞争控制权
- 主模型既要理解任务、又要拆任务、又要执行多个子任务、又要控制进度显示
- 任务边界没有被编码成正式运行时对象

## 2.2 当前 harness 为什么会天然混任务

当前主路径里，模型一旦返回 `tool_calls`，`QueryLoop` 会整批执行：

- [core/query/loop.py](/Users/kino/works/kino/harness/core/query/loop.py:351)
- [core/tools/runtime.py](/Users/kino/works/kino/harness/core/tools/runtime.py:152)

`ToolExecutorRuntime._partition()` 只按**只读 / 写入**分批，不认识“任务边界”：

```text
[skill(...), todo(...), read_file(...), bash(...)]
```

在今天的 runtime 里，只会变成：

```text
skill -> 串行
todo -> 串行
read_file -> 并行或串行
bash -> 串行
```

但 runtime 不会表达：

- `skill` 完成后是否必须重评任务
- `todo` 是否应在 skill expansion 之后才允许生成
- 当前是否其实存在两个可以独立执行的子任务
- 哪些工作应该让 subagent 执行

这会产生一种稳定的失败模式：

1. 主模型先从用户输入里形成一个模糊总计划
2. `todo` 在真正工作流进入上下文之前就被写出来
3. skill 成为“背景说明”，不是“执行边界”
4. 任务没有拆成独立执行单元，后续 read/bash/edit 混成一条流水线

## 2.3 之前的 subagent 代码为什么没形成主方案

`harness` 其实已经有子代理运行时：

- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)

但它目前仍是一个**孤立执行器**，而不是主路径的规划骨架。

现状特点：

- 只能通过显式 `SubagentRequest` 启动
- 没有上接到正式 `Task` 规划层
- `EXPLORE` / `PLAN` / `GENERAL` 是静态 agent 类型
- 没有 `fresh/fork` 双协议
- 没有 `TaskPacket`
- 没有 `TaskState`
- 没有“主 Agent 如何把已知边界交给子代理”的协议

因此，当前 subagent 代码虽然存在，但还不能解决本次核心问题。

---

## 3. 当前方案为什么不够

## 3.1 `SkillRelevancePolicy` 强化的是错误层级

当前 `SkillRelevancePolicy`：

- 提醒模型哪些 skill 可能相关
- 最多再通过 LLM 分类匹配提高推荐准确率

相关实现：

- [core/policy/skill_relevance.py](/Users/kino/works/kino/harness/core/policy/skill_relevance.py)

但它始终没有回答这些更关键的问题：

- 一个请求里到底有几个独立工作单元？
- 哪些单元应该串行，哪些可以并行？
- 哪些单元应该使用 local execution，哪些应该用 subagent？
- subagent 具体拿到什么输入？
- 用户看到的 TODO 该如何从这些真实工作单元中投影出来？

所以这条路线即使继续加大 prompt 强度，提升也会非常有限。

不过，这不意味着当前 skill router 可以继续维持宽松边界。即使它只是辅助层，也必须满足最基本的输入输出约束，否则会持续制造假阳性并干扰主流程。

短期内至少应满足：

1. router 输入只看**当前最后一条 user 请求**，不能把最近多轮 `assistant/user` 对话全文拼进去做 LLM 分类
2. classifier prompt 必须显式说明：讨论 skill / agent / workflow / subagent，不等于要激活对应 meta-skill
3. 对 `skill-creator` 这类 meta-skill，host 侧必须做 post-validation；只有用户明确要求“创建 / 更新 skill 或 SKILL.md”时才允许命中
4. 在满足上述约束之前，继续增强全局 skill relevance 只会把误判放大成更强的运行时干预

也就是说：

- `SkillRelevancePolicy` 不是主解
- 但它仍需要先收紧 router 边界，避免污染主 Agent 对真实任务的判断

## 3.2 `todo` 当前承担了过多职责

当前 `todo` 既是：

- 用户可见进度条
- 模型生成的计划
- 任务状态源

见：

- [core/tools/builtin/todo.py](/Users/kino/works/kino/harness/core/tools/builtin/todo.py)
- [core/session/state.py](/Users/kino/works/kino/harness/core/session/state.py)

这会带来三个问题：

1. 给用户看的简洁视图，和给执行器看的完整任务说明，粒度天然不同。
2. `todo` 重写语义适合“当前计划列表”，不适合“可执行子任务对象”。
3. 一旦引入 subagent，`todo` 无法承载输入边界、输出契约、写范围、失败原因等执行语义。

## 3.3 旧 skill 路线与新的任务分解路线是竞争关系

旧路线的主假设是：

> 主 Agent 应尽量在一个共享上下文里自己完成所有事，只是需要更可靠地“先用 skill”。

本设计的新假设是：

> 主 Agent 应负责理解、规划、分配与汇总；具体执行应下沉到 `Task` 单元，并在必要时交给 subagent。

这两个假设不能长期并存，否则 reviewer 会陷入两套模型之间：

- 一套是“主模型自己做完”
- 一套是“主模型拆给任务和子代理”

本设计明确推荐第二套。

---

## 4. 来自 Claude Code 的直接启发

本节只写**直接可验证的源码事实**，不夹带 harness 的实现推断。

## 4.1 Claude Code 把 `Todo` 和 `Task` 视为两层东西

直接证据：

- `/src/tools/TodoWriteTool/TodoWriteTool.ts`
- `/src/tools/TaskCreateTool/TaskCreateTool.ts`
- `/src/tools/TaskUpdateTool/TaskUpdateTool.ts`
- `/src/utils/tasks.ts`

可观察事实：

1. `TodoWriteTool` 仍然存在，负责会话级 checklist。
2. 同时还存在独立的 task subsystem。
3. `Task` 是正式对象，有自己独立的数据结构，不只是 `todo` 的别名。

`tasks.ts` 中的字段尤其关键：

```ts
{
  id,
  subject,
  description,
  activeForm,
  owner,
  status,
  blocks,
  blockedBy,
  metadata
}
```

这说明 Claude Code 已经明确区分：

- checklist / progress tracking
- structured task state

## 4.2 Claude Code 的 fresh subagent 默认**没有会话上下文**

直接证据：

- [AgentTool.tsx](/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/AgentTool.tsx:538)
- [AgentTool prompt.ts](/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/prompt.ts:267)

在非 fork 路径里，`promptMessages` 只有：

```ts
promptMessages = [createUserMessage({ content: prompt })]
```

同时工具 prompt 明确要求：

> Each fresh Agent invocation with a subagent_type starts without context — provide a complete task description.

这说明 Claude Code 并没有神秘的“自动上下文切片器”帮主线程把所有必要背景编给 fresh subagent。它的真实做法是：

- runtime 提供严格零上下文边界
- 主 Agent 负责写完整 briefing

## 4.3 Claude Code 的 fork path 明确是“继承父上下文”

直接证据：

- `/src/tools/AgentTool/forkSubagent.ts`
- [AgentTool.tsx](/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/AgentTool.tsx:495)

在 fork 路径里：

- 子代理继承父 system prompt
- 子代理继承父消息前缀
- `buildForkedMessages()` 在父上下文之后只追加一条 directive

也就是说，Claude Code 明确区分：

- **fresh**：完整 briefing，零上下文
- **fork**：完整上下文继承，短 directive

## 4.4 Claude Code 还显式阻断 subagent 自动继续读取主线程 prompt 流

直接证据：

- [query.ts](/Users/kino/works/opensource/Claude-Code-doc/src/query.ts:1561)

源码说明：

- 主线程只消费 `agentId === undefined` 的 prompt
- subagent 只消费发给自己的 `task-notification`
- `subagents never see the prompt stream`

这说明子代理的输入边界不是模糊的共享对话，而是一次性进入、之后隔离。

## 4.5 Claude Code 的 skill fork 执行也遵守同样的输入边界

直接证据：

- `/src/tools/SkillTool/SkillTool.ts`
- [forkedAgent.ts](/Users/kino/works/opensource/Claude-Code-doc/src/utils/forkedAgent.ts:191)

`prepareForkedCommandContext()` 做的事情很简单：

1. 生成 skill 内容
2. 解析 allowedTools
3. 选择 agent type
4. 生成：

```ts
promptMessages = [createUserMessage({ content: skillContent })]
```

这再次说明：

- fresh/forked execution 的任务内容入口是**明确构造的 promptMessages**
- 不是把整个主线程 runtime 模糊复制给子代理

## 4.6 Claude Code 的 verification agent 明确要求传递结构化任务事实

直接证据：

- `/src/tools/AgentTool/built-in/verificationAgent.ts`

其 `whenToUse` 明确要求调用方传：

- original task description
- files changed
- approach taken

这很重要，因为它证明：

> 即便在成熟 agent 框架中，subagent 输入边界仍然主要由“调用方显式写进任务包”的事实来控制，而不是隐含地依赖共享上下文。

## 4.7 证据边界

Claude Code 直接支持的结论只有这些：

- `Todo` 和 `Task` 是两层对象
- fresh agent 是零上下文，需要完整任务描述
- fork agent 继承完整上下文
- subagent 不继续自动消费主线程 prompt
- skill fork 也通过显式 promptMessages 构造输入
- 某些 built-in agent 明确要求结构化任务事实

Claude Code **没有**直接提供的东西：

- 一个通用的“从 TODO 自动精确切出 subagent 输入”的统一编译器
- 一个对所有任务自动做最优拆分的中央 planner

这部分必须由 harness 自己设计。

---

## 5. 设计目标

本设计结束后，应满足：

1. `TODO`、`TASK`、`skill`、`subagent` 四者边界清楚，不再混用。
2. 主 Agent 在复杂任务上先生成正式 `TaskState`，而不是直接开始混合执行。
3. `TODO` 成为从 `TaskState` 投影出的用户视图，而不是权威状态源。
4. `TASK` 成为唯一可调度的执行单元。
5. `subagent` 输入通过 `TaskPacket` 明确构造，不再依赖模糊对话共享。
6. 明确支持两种 subagent 上下文模式：
   - `fresh`
   - `fork`
7. `skill` 绑定到 `TASK` 或 `TaskPacket`，而不是继续依赖全局模糊激活状态。
8. 新模型能自然兼容现有 `core/session/subagent.py`，而不是推翻重来。

---

## 6. 非目标

本阶段明确不包含：

- 完整 swarm / teammate / mailbox 协作系统
- 文件持久化的 task store
- 多层递归 subagent delegation
- 复杂 DAG UI
- 自动从 skill 文件中完整提取 machine-readable workflow graph
- 继续把 `SkillRelevancePolicy` 打磨成主方案
- 任何需要重写现有社区 skill 文件格式的做法

---

## 7. 本阶段的关键实现决策

以下决策不再留作开放问题，而是本 spec 的明确建议：

1. **Planner 不是自由文本 prompt 约定，也不是普通 policy 提醒。**
   第一阶段应引入一个新的 builtin tool，例如 `task_plan`，负责把 `TaskState` 写入 `SessionState`。
2. **第一阶段不实现完整 Claude Code 风格的 `TaskCreate / TaskUpdate / TaskList / TaskGet` 全家桶。**
   那套模型更完整，但对当前 harness 来说过重。第一阶段更适合先实现一个 rewrite-style `task_plan` 工具，类似当前 `todo`，但语义是权威 `TaskState`。
3. **`fork` 只在协议层定义，默认不纳入第一阶段实现范围。**
   第一阶段应优先落地 `fresh_subagent`，`fork_subagent` 作为已定义但延后的模式保留在模型中。
4. **`todo_state` 在 `TaskState` 存在时不再允许模型直接作为权威写入。**
   它必须成为由 `TaskState` 投影生成的兼容视图。

之所以把这些点写成明确决策，是因为如果继续把它们留在“实现时再看”，整个方案会再次退回到 prompt 约定层。

---

## 8. 核心原则

## 8.1 `TODO` 是视图，不是执行权威

对用户有价值的是：

- 当前做到了哪一步
- 还有哪些大项没做

对执行器有价值的是：

- 具体目标是什么
- 输入边界是什么
- 需要哪些技能
- 输出长什么样
- 什么算完成

这两个粒度不同，因此必须拆成：

- `TaskState`：权威
- `TodoProjection`：视图

## 8.2 `TASK` 是执行单元，不是 prompt 文本

`TASK` 必须是 session-scoped 的正式对象，否则：

- 无法稳定追踪状态
- 无法 resume / compact
- 无法调试“为什么派了这个 subagent”
- 无法可靠合并结果

## 8.3 `fresh` 与 `fork` 必须是两种不同协议

它们不是“是否带上下文”的一个布尔参数，而是两种不同的输入构造哲学：

- `fresh`：编译 briefing
- `fork`：编译 directive

## 8.4 主 Agent 不应把“理解任务”外包给 subagent

无论是 Claude Code 还是本设计，主 Agent 都必须先理解任务，再下发 `TaskPacket`。

禁止的坏模式：

```text
“你去研究一下，然后基于你的研究把事情做掉。”
```

推荐模式：

```text
“目标是什么、为什么、已知事实、范围边界、输出格式、完成标准是什么。”
```

## 8.5 skill 是工作流知识，不是任务对象

`skill` 负责：

- 告诉执行器如何做
- 对复杂任务提供结构化步骤和约束

`TASK` 负责：

- 告诉执行器做哪一块
- 这一块的边界、输入、输出、完成标准是什么

所以关系应是：

- 一个 `TASK` 可以绑定 `0..N` 个 skill
- 一个 skill 可以服务多个 `TASK`
- `TASK != skill`

---

## 9. 总体架构

## 9.1 新模型

```text
User Message
  -> Planner decides trivial vs multi-step
  -> If trivial: direct local execution
  -> If multi-step:
       -> create/replace TaskState
       -> derive TodoProjection
       -> select next runnable Task
       -> compile TaskPacket
       -> execute local or subagent
       -> merge TaskResult
       -> refresh TodoProjection
       -> replan if needed
```

## 9.1.1 Planner 的实现机制

本设计明确推荐：

```text
Task Planner = 新的 builtin tool（暂名 task_plan）
```

而不推荐：

- 让模型直接在普通 assistant 文本中输出 `TaskState` JSON
- 只靠 policy 提醒模型“先规划一下”
- 先不加工具，完全依赖 system prompt 约束

### 为什么推荐 `task_plan` tool

1. 当前 harness 已经证明：即使是结构比 `TaskRecord` 简单得多的 `todo`，也需要专门工具和校验逻辑。
2. Claude Code 的 task 子系统也是显式工具，不是让模型自由吐 JSON：
   - `TaskCreateTool`
   - `TaskUpdateTool`
   - `TaskListTool`
   - `TaskGetTool`
3. `task_plan` 作为工具可以天然提供：
   - schema 校验
   - 失败重试
   - 明确的 session update
   - 与 runtime control plane 的稳定对接

### 为什么第一阶段不直接照搬 Claude Code 的 task 全家桶

Claude Code 的 task 子系统已经包含：

- 持久化
- owner
- blockedBy / blocks
- 团队协作
- 与 UI 的直接绑定

对当前 harness 来说，直接全量照搬会引入过多状态管理和兼容性成本。第一阶段更合理的策略是：

```text
先做 task_plan（全量重写当前 TaskState）
后续再视需要拆成 task_create / task_update / task_list / task_get
```

### `task_plan` 的建议输入形态

```json
{
  "tasks": [
    {
      "task_id": "task-1",
      "subject": "审查现有 runtime 断点",
      "goal": "定位任务混杂执行的真正运行时断点",
      "status": "in_progress",
      "execution_mode": "local"
    }
  ]
}
```

第一阶段应优先保证：

- schema 简单
- 可验证
- 能被 runtime 稳定消费

而不是一次性追求完整 task graph。

## 9.1.2 `task_plan` Tool Schema

第一阶段建议把 `task_plan` 明确定义成：

```text
rewrite-style authoritative planner tool
```

也就是说：

- 每次调用都提交一个完整 `tasks[]`
- handler 校验并归一化后，直接替换整个 `SessionState.task_state`
- 不做增量 merge
- 如果只想更新一个 task，第一阶段也必须重提完整列表

这样做的理由很简单：

- 比增量 patch 更容易保证一致性
- 更接近现有 `todo` 的心智模型
- 更适合第一阶段先把 planner/runtime 边界跑通

### 顶层输入 schema

建议对齐当前 `todo` 工具的风格，提供严格 JSON schema，至少包含以下约束：

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["tasks"],
  "properties": {
    "tasks": {
      "type": "array",
      "description": "Full replacement of the current TaskState",
      "maxItems": 20,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["subject", "goal"],
        "properties": {
          "task_id": {
            "type": "string",
            "description": "Optional stable id. Runtime allocates one if omitted."
          },
          "subject": {
            "type": "string",
            "description": "Short user-facing title"
          },
          "goal": {
            "type": "string",
            "description": "What this task must accomplish"
          },
          "status": {
            "type": "string",
            "enum": [
              "pending",
              "in_progress",
              "blocked",
              "completed",
              "failed",
              "cancelled"
            ],
            "default": "pending"
          },
          "execution_mode": {
            "type": "string",
            "enum": [
              "local",
              "fresh_subagent",
              "fork_subagent"
            ],
            "default": "local"
          },
          "parent_todo_id": {
            "type": ["string", "null"]
          },
          "active_form": {
            "type": ["string", "null"]
          },
          "agent_type": {
            "type": ["string", "null"]
          },
          "allowed_tools": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "write_scope": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "inputs": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "known_context": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "out_of_scope": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "required_skills": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "expected_output": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "done_criteria": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "depends_on": {
            "type": "array",
            "items": { "type": "string" },
            "default": []
          },
          "owner": {
            "type": ["string", "null"]
          },
          "blocked_reason": {
            "type": ["string", "null"]
          }
        }
      }
    }
  }
}
```

这也与 Claude Code 的真实倾向一致：

- `TodoWriteTool` 使用严格 schema 管整个 checklist
- `TaskCreateTool` 的输入只要求 `subject`、`description`，其余字段保持克制

因此，本设计虽然把 `TaskRecord` 定义得更完整，但第一阶段的 `task_plan` 输入依然应保持“核心字段最小化，handler 负责补默认值”的思路，而不是要求模型一次性可靠生成生产级全字段对象。

### `task_plan` 的 description 文本草案

第一阶段建议给模型看到的工具说明至少达到当前 `todo` 的清晰度。可直接采用类似下面的文本：

```text
Rewrite the current TaskState for non-trivial multi-step work.
Use this before execution when the request has multiple independent goals, requires research + implementation + verification, needs subagent dispatch, or spans multiple files/modules.
Submit the full replacement task list each time; this tool does not do incremental patching.
When TaskState is active, do not use `todo` as the source of truth — `todo_state` is derived automatically from tasks.
Keep task granularity executable: each task should have one clear goal, a plausible execution mode, and a boundary that could be executed locally or by one subagent.
Prefer a small number of concrete tasks over vague phases. Keep at most one `in_progress` task unless a later phase explicitly enables parallel execution.
If you replan, rewrite the full list with preserved task IDs where the work item is semantically the same.
```

这段 description 至少要覆盖四件事：

- 何时应调用 `task_plan`
- 它与 `todo` 的关系
- 它是 rewrite 语义，不是 incremental patch
- task 粒度要面向执行，不是面向用户展示

### 为什么 `maxItems = 20`

建议第一阶段与现有 `todo` 保持同一数量级：

- 对模型来说，20 已经足够覆盖复杂请求
- 对 UI 来说，不会把用户视图炸成不可读列表
- 对 runtime 校验和投影来说，复杂度可控

后续如果确实出现 task 数量上限不足的问题，再单独扩展。

### `task_id` 的分配规则

第一阶段建议：

- `task_id` 可以省略
- 省略时由 runtime 按顺序分配，例如 `task-1`、`task-2`
- 模型如果显式提供 `task_id`，handler 必须校验唯一性
- rewrite 调用时，如果某个任务在语义上延续旧任务，推荐保留旧 `task_id`

这比要求模型从第一天就稳定管理 ID 更现实。

### `status` 与 `execution_mode` 的默认规则

建议输入层允许省略：

- `status` 默认 `pending`
- `execution_mode` 默认 `local`

但归一化后的 `TaskRecord` 中，两者都必须是显式值。

### 初次 planning 与二次 planning 的额外约束

第一阶段建议把语义分成两类：

1. 当前 `TaskState` 为空：
   - 这是 initial planning
   - 只允许 `pending` 或 `in_progress`
   - 最多一个 `in_progress`
2. 当前 `TaskState` 非空：
   - 这是 rewrite / replan
   - 允许全部 `TaskStatus`
   - 仍然建议最多一个 `in_progress`

这样可以避免一上来就出现“初次规划里一半任务已完成”的反直觉状态。

### `task_plan` 的应用语义

建议 handler 执行顺序写死为：

```python
def apply_task_plan(input, session_state, run_state):
    validate_json_shape(input)
    normalized_tasks = normalize_defaults(input["tasks"])
    validate_unique_task_ids(normalized_tasks, session_state.task_state)
    validate_status_rules(normalized_tasks, session_state.task_state)
    validate_execution_modes(normalized_tasks, phase="A")
    allocated_tasks = allocate_missing_ids(normalized_tasks, session_state.task_state)
    next_state = build_task_state(allocated_tasks, run_state.turn_count)
    return SessionUpdate(
        kind=SessionUpdateKind.SET_TASK_STATE,
        payload={"task_state": next_state},
    )
```

注意这里不应直接在 tool handler 里原地改 `session_state.task_state`。

现有 harness 的状态更新路径是：

```text
tool handler -> SessionUpdate -> reducer -> SessionState
```

因此第一阶段建议新增：

```python
SessionUpdateKind.SET_TASK_STATE = "set_task_state"
```

并在 reducer 里执行：

1. `session_state.task_state = next_state`
2. `sync_todo_projection_from_tasks(session_state)`
3. 更新 `task_state.last_planned_turn`

这样才能与现有 `SET_TODO_ITEMS`、`UPSERT_FILE_STATE` 的 reducer 模式保持一致。

### handler 必须承担的归一化职责

至少包括：

- 给缺失 optional 字段补默认值
- 为缺失 `task_id` 的 task 分配稳定 ID
- 把 `status` / `execution_mode` 归一化成枚举值
- 在 Phase A 拒绝 `fork_subagent` 被实际调度
- 保证 `ordered_task_ids` 与输入顺序一致

### 空 `tasks[]` 的语义

建议允许：

```json
{ "tasks": [] }
```

其语义是：

- 清空当前 `TaskState`
- 清空 `todo_state` 投影

这个入口主要用于：

- 复杂任务完成后的显式收尾
- replan 后确认当前不再需要 session-scoped task tracking

但不建议把它作为复杂请求的初始 planning 结果。

### 为什么第一阶段坚持 rewrite，不做 incremental

Claude Code 有 `TaskCreate / TaskUpdate / TaskGet / TaskList`，这是它成熟 task 子系统的一部分。

本设计第一阶段不直接学习那一层，而是只学习两件事：

- task 是独立对象
- task 应通过显式工具和 schema 管理

在当前 harness 中，先把 rewrite-style `task_plan` 跑稳，比过早引入增量更新协议更重要。

## 9.2 四层概念关系

```text
Skill
  -> defines workflow knowledge

Task
  -> defines executable unit and boundary

Subagent
  -> is one possible executor for a Task

Todo
  -> is the user-facing projection of Task progress
```

## 9.3 SessionState 中的角色分工

建议新增：

```python
SessionState.task_state
```

而保留：

```python
SessionState.todo_state
```

但其来源改为：

```text
todo_state = project(task_state)
```

不再把 `todo_state` 当作主规划权威。

---

## 10. 数据模型

## 10.1 `TaskStatus`

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

说明：

- `blocked` 与 `failed` 必须分开
- `blocked` 代表等待外部条件、依赖或用户输入
- `failed` 代表执行尝试已结束且未成功

## 10.2 `TaskExecutionMode`

```python
class TaskExecutionMode(str, Enum):
    LOCAL = "local"
    FRESH_SUBAGENT = "fresh_subagent"
    FORK_SUBAGENT = "fork_subagent"
```

说明：

- `LOCAL`：主 Agent 自己做
- `FRESH_SUBAGENT`：子代理零上下文启动，拿完整 `TaskPacket`
- `FORK_SUBAGENT`：子代理继承父上下文，只拿 directive

### Phase A 限制

虽然枚举里保留了 `FORK_SUBAGENT`，但第一阶段默认只允许：

- `LOCAL`
- `FRESH_SUBAGENT`

`FORK_SUBAGENT` 在第一阶段是**已定义但默认不可调度**的模式。保留它是为了避免后续数据模型改 shape，不等于第一阶段就要把 runtime 跑通。

## 10.3 `TaskRecord`

```python
@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode

    # V1 可选展示字段
    parent_todo_id: str | None = None
    active_form: str | None = None

    # 执行器选择
    agent_type: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)

    # 任务输入与边界
    inputs: list[str] = field(default_factory=list)
    known_context: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)

    # 完成定义
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)

    # 调度与追踪
    depends_on: list[str] = field(default_factory=list)
    owner: str | None = None
    blocked_reason: str | None = None

    # 结果
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    failure_reason: str | None = None

    # 编译与调试
    packet_revision: int = 0
    created_at_turn: int = 0
    updated_at_turn: int = 0
```

### 字段说明

- `subject`
  - 给用户和 UI 看的一行标题
- `goal`
  - 执行目标，通常比 `subject` 更完整
- `active_form`
  - UI spinner 使用
- `known_context`
  - 由主 Agent显式整理出的已知事实，不是原始 transcript dump
- `out_of_scope`
  - 明确告诉执行器什么不要做
- `required_skills`
  - 需要预加载或遵循的工作流知识
- `done_criteria`
  - 判断是否真正完成的标准
- `write_scope`
  - 预期允许改动的文件或目录
- `blocked_reason`
  - 当 `status == BLOCKED` 时记录原因。可用于表达“等待依赖”“等待用户确认”“等待环境条件”。
- `packet_revision`
  - 每次重新编译 `TaskPacket` 时递增，便于调试

### 10.3.1 第一阶段的 required vs optional

第一阶段建议的 **required 字段** 只有：

- `task_id`
- `subject`
- `goal`
- `status`
- `execution_mode`

其余字段全部为 optional 或有默认值。

原因很直接：

- 当前 harness 连更简单的 `todo` 都需要大量验证逻辑
- 如果第一阶段就要求模型可靠填满 20+ 语义字段，方案会迅速退化
- 运行时真正必须知道的，只有“任务是谁、要做什么、现在什么状态、谁来执行”

### 10.3.2 第一阶段最小字段集

因此，第一阶段 planner 可以只稳定产出如下最小结构：

```json
{
  "task_id": "task-1",
  "subject": "审查 runtime 断点",
  "goal": "定位任务混杂执行的运行时根因",
  "status": "in_progress",
  "execution_mode": "local"
}
```

而这些 richer 字段应被视为**逐步增强项**：

- `allowed_tools`
- `write_scope`
- `known_context`
- `out_of_scope`
- `required_skills`
- `expected_output`
- `done_criteria`
- `owner`
- `depends_on`

## 10.4 `TaskState`

```python
@dataclass(slots=True)
class TaskState:
    tasks_by_id: dict[str, TaskRecord] = field(default_factory=dict)
    ordered_task_ids: list[str] = field(default_factory=list)
    last_planned_turn: int | None = None
    last_projection_turn: int | None = None
    current_task_id: str | None = None
```

说明：

- `ordered_task_ids` 决定展示顺序与默认调度顺序
- `current_task_id` 是当前聚焦任务，不等于唯一 in_progress 约束，但通常应只有一个主聚焦项

## 10.5 `TodoProjectionItem`

```python
@dataclass(slots=True)
class TodoProjectionItem:
    todo_id: str
    content: str
    active_form: str
    status: str
    workflow_ref: str | None = None
    task_ids: list[str] = field(default_factory=list)
```

说明：

- 这是视图对象，不是权威执行对象
- `task_ids` 只是反向映射，方便定位用户看到的 TODO 对应哪些任务

### Phase A 兼容性说明

当前 [core/session/state.py](/Users/kino/works/kino/harness/core/session/state.py) 中：

```python
TodoState.items: list[TodoItem]
```

因此第一阶段不要直接把 `TodoProjectionItem` 写进 `session_state.todo_state.items`。

第一阶段建议：

- `TodoProjectionItem` 保留为概念模型 / 未来扩展模型
- 真正写回 `TodoState.items` 时，投影为现有 `TodoItem`
- `todo_id`、`task_ids` 这类 richer 映射信息，第一阶段不进入 `TodoState.items`

这样可以避免同步改 renderer、PromptAssembler、去重逻辑的类型链路。

## 10.6 `TaskPacket`

`TaskPacket` 是真正交给 local executor 或 subagent 的执行契约。

```python
@dataclass(slots=True)
class TaskPacket:
    task_id: str
    mode: TaskExecutionMode
    agent_type: str | None

    title: str
    directive: str

    # fresh 模式使用的结构化 briefing
    task_context: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)

    required_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)

    packet_revision: int = 0
```

### 为什么 `TaskPacket` 不能等于 `TaskRecord`

`TaskRecord` 是 durable state，职责是“记录任务是什么、状态如何”。

`TaskPacket` 是 compiled execution payload，职责是“这次执行具体要怎么把任务讲给执行器听”。

两者分开有三个好处：

1. 同一 `TaskRecord` 可以多次重新编译 packet
2. `fresh` 与 `fork` 可以用不同渲染协议
3. 调试时可以清楚区分“任务本身定义错了”还是“packet 构造错了”

### `TaskPacket.mode` 与上下文继承

`inherits_parent_context` 不单独建字段。

规则直接由 `mode` 决定：

- `LOCAL`：不适用
- `FRESH_SUBAGENT`：不继承父上下文
- `FORK_SUBAGENT`：继承父上下文

这样可以避免 `mode == FRESH_SUBAGENT` 但 `inherits_parent_context == True` 这类自相矛盾状态。

## 10.7 `TaskRunResult`

```python
@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    recommended_next_steps: list[str] = field(default_factory=list)
    failure_reason: str | None = None
```

这个结构必须比“自由文本回答”更强，否则结果合并会继续依赖主模型自由发挥。

### 10.7.1 与现有 `SubagentRunResult` 的关系

当前已有：

- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)

```python
SubagentRunResult(
    request,
    output,
    success,
    stop_reason,
    turns_used,
    files_modified,
)
```

第一阶段建议的关系是：

```text
SubagentRunResult = 底层执行结果
TaskRunResult = 上层任务合并结果
```

也就是说，第一阶段**不是立刻删除** `SubagentRunResult`，而是：

1. 保留 `SubagentRunResult` 作为 subagent runtime 的原生返回值
2. 在 task dispatch 层增加：

```python
def normalize_subagent_result(
    task_id: str,
    result: SubagentRunResult,
) -> TaskRunResult:
    ...
```

3. 把 `SubagentRunResult.stop_reason / turns_used` 暂时视为调试信息，而不是 `TaskRunResult` 的必备字段

这比直接替换底层 dataclass 更稳，也更适合分阶段迁移。

---

## 11. 输入构造协议

本节是整个设计里最关键的部分。

问题不是“subagent 要不要看到一点上下文”，而是：

> subagent 的输入边界到底由谁决定，按什么规则构造？

推荐答案是：

- 由主 Agent 决定
- 通过 `TaskPacket` 明确构造
- `fresh` 与 `fork` 分别遵守不同协议

## 11.1 `fresh` 输入协议

### 核心原则

`fresh` subagent 视为：

```text
一个刚走进房间、完全没看过这段对话的同事
```

因此不能假设它知道：

- 用户最初说了什么
- 主 Agent 已经试过什么
- 当前任务为什么重要
- 哪些文件已经看过
- 哪些方向已经被排除

### `fresh` 必须包含的内容

`fresh` packet 在协议层至少可以容纳：

1. `title`
2. `directive`
3. `task_context`
4. `known_facts`
5. `out_of_scope`
6. `expected_output`
7. `done_criteria`
8. `required_skills`
9. `allowed_tools`
10. `write_scope`

但第一阶段的**最小必需集合**应收敛为：

1. `title`
2. `directive`
3. `expected_output`

以及按需可选的：

- `known_facts`
- `out_of_scope`
- `required_skills`
- `allowed_tools`

原因：

- 协议能力可以提前定义完整
- 但第一阶段实现必须允许 planner 只稳定产出最小有效包
- richer packet 字段可以在后续 phase 逐步提升

### `fresh` 不应自动包含的内容

默认不应自动塞入：

- 整段原始 transcript
- 所有旧的 tool outputs
- 所有 TODO 项
- 无关的用户追问
- 无关 skill

原因：

- token 污染严重
- 边界模糊
- 复现性差
- 让 subagent 自己重新做无意义信息筛选

### `fresh` packet 的推荐渲染顺序

```text
Task: <title>

Directive:
<directive>

Why this task exists:
- ...

Known facts:
- ...

Out of scope:
- ...

Required skills/workflows:
- ...

Allowed tools:
- ...

Write scope:
- ...

Expected output:
- ...

Done criteria:
- ...
```

### `fresh` `directive` 的写法要求

禁止写法：

```text
去研究一下这个问题，然后顺手把它修了。
```

推荐写法：

```text
定位 why `SkillRelevancePolicy` 无法解决两个独立用户意图混杂执行的问题。
只分析 task planning 与 subagent dispatch 层，不改 skill frontmatter。
输出一段结论，列出需要的 runtime changes 和风险点。
```

### `fresh` 与 skill 的关系

对于 `required_skills`，推荐不是简单把 skill ID 放进 prompt 文本里，而是：

1. 在 `TaskPacket` 中声明 `required_skills`
2. dispatcher 在 subagent 启动前预加载这些 skill
3. packet 的 `Required skills/workflows` 段只列出 skill 名称和为什么需要它

这样比“把整个 skill 内容硬贴进 directive”更稳定，也更接近现有 skill runtime 方向。

### `required_skills` 不存在或预加载失败时的行为

必须定义清楚：

1. 如果 `required_skills` 中的 skill 不存在：
   - 不应静默忽略
   - 应把当前 task 标记为 `FAILED` 或 `BLOCKED`
   - `failure_reason` / `blocked_reason` 需明确写出缺失 skill id
2. 如果 skill 存在但预加载失败：
   - 不启动 subagent
   - 返回结构化错误，允许主 Agent replan
3. 如果 task 只是“建议使用 skill”，而不是强依赖：
   - 不应放进 `required_skills`
   - 可以只写入 `known_context` 或 `directive`

也就是说，`required_skills` 的语义应是：

```text
缺了它就不该启动这次执行
```

## 11.2 `fork` 输入协议

### 核心原则

`fork` subagent 视为：

```text
一个继承当前上下文的临时分身
```

因此 `fork` 的目标不是重复解释背景，而是：

- 缩短主线程上下文污染
- 把中间探索或实现细节从主线程剥离
- 在继承上下文的前提下执行一个局部 directive

### `fork` 必须包含的内容

`fork` 只应包含：

1. `title`
2. `directive`
3. 可选的 `out_of_scope` 短约束

其上下文继承语义不通过额外字段表达，而直接由：

```python
mode = TaskExecutionMode.FORK_SUBAGENT
```

决定。

### `fork` 明确不应重复塞入的内容

不应再重复渲染：

- 整个用户背景
- 全部 known facts
- 大段文件摘要
- skill 正文大段拷贝

因为这些内容已经在父上下文中存在，重复塞入只会：

- 浪费 token
- 制造不一致风险
- 让 fork 失去“短 directive”的优势

### `fork` packet 的推荐形态

```text
Directive:
只调查为什么 `todo` 在 skill expansion 之前被写出。
不要提出完整重构方案，只输出证据链和相关文件。
```

### `fork` 适用条件

只有满足以下条件时，才应选 `fork`：

1. 当前上下文已经包含足够完整的任务背景
2. 中间结果不值得污染主线程
3. 子任务主要是研究、核查、局部实现，而不是需要重新建立独立任务世界观
4. 子任务不需要再被转述成完整 briefing

## 11.3 选择 `fresh` 还是 `fork`

推荐硬规则：

### 选 `fresh`

- 子任务是独立问题
- 需要明确输入/输出契约
- 需要受控工具白名单
- 需要把范围说清楚
- 需要一个独立“同事”心智模型

### 选 `fork`

- 子任务只是父问题中的一个局部分支
- 父上下文已经很完整
- 中间推理不值得留在主线程
- 目标是降低主线程噪音，而不是重新建模任务

### 不要选 `fork`

- 父上下文本身混乱或尚未收束
- 需要严格写范围和完成标准
- 任务会改动多个不相关区域
- 任务将被长期追踪和多次恢复

---

## 12. Task Planning 与 Todo Projection

## 12.1 推荐新的权威顺序

正确顺序应改成：

```text
Skill / intent understanding
  -> Task planning
  -> Todo projection
  -> Dispatch
```

而不是今天的：

```text
Skill hint
  -> Todo first
  -> 混合执行
```

## 12.2 `TODO` 的来源

`TODO` 不再直接由模型作为权威状态维护，而应由 `TaskState` 投影生成。

投影规则建议：

- 从长期模型看，一个 `TODO` 可以对应 `1..N` 个 `TASK`
- 但 **Phase A / B 的默认实现应明确收敛为 `1 TASK -> 1 TODO`**
- `active_form` 优先来自 task 自身的 `active_form`，否则回退到 `subject`

例如：

```text
TODO: 重新设计 task/subagent runtime
  -> TASK-1: 阅读旧设计与现状代码
  -> TASK-2: 提炼 Claude Code 证据
  -> TASK-3: 设计 TaskPacket fresh/fork 协议
  -> TASK-4: 写 spec
```

用户看见的是：

```text
- [in_progress] 正在设计 TaskPacket fresh/fork 协议
```

而不是所有执行细节。

### 12.2.1 为什么第一阶段先固定 `1 TASK -> 1 TODO`

虽然概念上 `TODO -> 1..N TASK` 更完整，但第一阶段如果立刻支持分组投影，会多出一整层实现歧义：

- 分组键是什么
- `TODO.content` 取 `subject` 还是高层摘要
- `BLOCKED` / `FAILED` 如何汇总成单个用户视图状态

因此 Phase A / B 建议把规则定死：

```text
一个 TaskRecord 直接投影成一个 TodoProjectionItem
```

等 TaskState、dispatcher、subagent packet 跑稳后，再引入真正的 grouped projection。

## 12.3 为什么不建议 `1 TODO = 1 subagent`

因为 `TODO` 是视图粒度，不是调度粒度。

很多 TODO 对用户很自然，但对执行器太粗或太细：

- 太粗：一个 TODO 里其实有多个独立 TASK
- 太细：一个 TODO 只是“读取一个文件”级别，不值得单开 agent

所以推荐关系是：

- `TODO -> 1..N TASK`
- `TASK -> 0..1 active executor`

---

## 13. 调度规则

## 13.1 何时必须先生成 `TaskState`

满足以下任一条件时，主 Agent 应先规划 `TaskState`：

1. 用户请求明显包含多个独立目标
2. 任务需要研究 + 实现 + 验证三段式
3. 存在多个可并行的问题域
4. 需要调用 subagent
5. 需要跨多个文件或模块协调
6. 当前 skill 提供了明显的多阶段工作流

## 13.2 何时允许直接 local 执行

只有满足以下条件，才允许绕过 `TaskState`：

1. 单一目标
2. 单步或极短链路
3. 不需要 subagent
4. 不需要持续进度跟踪
5. 不会对已有计划造成混淆

### 13.2.1 `task_plan` 之后如何进入第一个 task

这里必须明确选一种闭环机制。第一阶段推荐：

```text
方案 A：task_plan 只写状态；下一轮仍由模型选择执行哪个 task
```

也就是说：

1. 模型调用 `task_plan`
2. handler 通过 `SessionUpdateKind.SET_TASK_STATE` 写入 `TaskState`
3. reducer 同步 `todo_state` 投影
4. `PromptAssembler` 在下一轮把 `TaskState` 渲染回 runtime context
5. 模型基于当前 `TaskState` 决定：
   - 直接 local 执行某个 task
   - 或通过 dispatcher / subagent path 执行某个 task

第一阶段**不推荐**：

- `task_plan` 执行完立即自动调度第一个 task
- 额外引入一个独立 `task_dispatch` tool 让模型显式点火

推荐方案 A 的原因：

- 最接近当前 `QueryLoop` 的运行模型
- 不需要再引入第二个新工具
- `task_plan` handler 的职责更单一，只负责写权威状态
- “选哪个 task 先执行”仍保留在模型侧，便于利用刚写出的 `TaskState` 做局部判断

## 13.3 何时适合 subagent

建议的判定条件：

1. 可以写成独立 briefing
2. 读写范围可圈定
3. 产出形式清楚
4. 不依赖主 Agent 持续来回交互
5. 中间细节不值得污染主线程

## 13.4 并行约束

允许并行的前提：

1. `write_scope` 不重叠
2. `depends_on` 无冲突

第一阶段不要再增加更复杂的静态判定条件。

也就是说，Phase A / B 的并行约束只看：

- `write_scope`
- `depends_on`

其余更复杂的互斥环境条件留待后续扩展，否则 runtime 很难在第一阶段就稳定判断。

## 13.5 subagent 不再继续 spawn

第一阶段建议保持：

- subagent 默认禁止再派生 subagent

原因：

- 否则边界会失控
- 当前 harness 还没有成熟的多层 delegation 可观测性

## 13.6 `TaskPlanningPolicy` 的定位

这不是一句“请先规划”的 prompt 约定，而应是一个正式 policy：

- 建议新模块：`core/policy/task_planning.py`
- 接入方式：和 `SkillRelevancePolicy`、`MaxTurnsPolicy` 一样挂到 `PolicyRunner`
- 主要职责：在“应先规划但尚未规划”时注入强提醒，并配合 runtime gate 阻止直接进入执行

### 与现有 policy 栈的关系

推荐顺序：

1. `TaskPlanningPolicy`
2. `SkillRelevancePolicy`
3. 其他 reminder policy
4. `MaxTurnsPolicy`

理由：

- 先决定“要不要先建立 TaskState”
- 再决定“某个 task 需要哪些 skill”
- 最后才是普通执行期提醒

如果没有先建 `TaskState`，单纯强化 skill relevance 只会继续服务旧的“主线程混合执行”路径。

### 建议新增的 `RunState` 字段

建议在 [core/query/state.py](/Users/kino/works/kino/harness/core/query/state.py) 中新增：

```python
task_planning_required: bool = False
task_planning_reason: str | None = None
task_plan_invoked_this_turn: bool = False
```

这样 policy、QueryLoop、tool runtime 才能共享同一组事实，而不是互相猜测。

### 触发条件

第一阶段不要依赖 LLM 再做一轮“是否复杂”的结构化判断，而应用确定性规则。

推荐把触发分成两层：

1. **Soft trigger**
   - `RunState.task_planning_required == True`
   - 在 `before_model_call()` 注入提醒
2. **Hard trigger**
   - 模型跳过 `task_plan`
   - 直接调用执行类工具
   - runtime 拒绝本批工具，并把 `task_planning_required` 置为 `True`

### Hard trigger 的判定规则

第一阶段建议只拦截“执行类工具”，不拦截轻量发现类工具。

可继续允许的 pre-plan 工具：

- `task_plan`
- `skill`
- `find`
- `read_file`

应被拦截并要求先 `task_plan` 的工具：

- `todo`
- `edit_file`
- `write_file`
- `bash`
- 任何后续新增的 subagent / agent 派发入口

这样做的原因是：

- 主 Agent 仍然可以先读少量上下文来形成规划
- 但不能在没有 `TaskState` 的情况下直接进入执行或派发

### 为什么第一阶段不用 `readonly` 注解自动推导

当前 registry 里虽然可以读取 `ANNOTATIONS["readonly"]`，但这还不够：

- `skill` 不是只读工具，却必须允许 pre-plan 使用
- `todo` 也不是只读工具，但在 task planning 路径里必须被拦截

因此第一阶段建议采用明确常量：

```python
PREPLAN_ALLOWED_TOOLS = {"task_plan", "skill", "find", "read_file"}
```

其余工具名默认拦截。

后续如果工具面继续扩大，再考虑引入更细的注解，例如 `preplan_safe=True`，而不是误用 `readonly` 近似代替。

### `before_model_call()` 注入内容

建议 `TaskPlanningPolicy` 注入一段强提醒 user-message，而不是改 system prompt：

```text
<system-reminder type="task_planning">
Current request requires task planning before execution.
Call `task_plan` with the full task list first.
You may still use lightweight read-only discovery tools if needed, but do not edit files, dispatch subagents, or start execution until TaskState exists.
</system-reminder>
```

这与现有 `SkillRelevancePolicy` 的注入方式一致，更容易复用当前 `PolicyRunner` 机制。

### `after_tool_batch()` 的职责

第一阶段建议最小化：

- 如果本轮成功调用了 `task_plan`：
  - 清空 `task_planning_required`
  - 清空 `task_planning_reason`
- 如果本轮工具被 runtime 因“缺少 task planning”拒绝：
  - 保持标志位
  - 让下一轮 `before_model_call()` 再次注入提醒

### 为什么 policy 本身不负责直接写 `TaskState`

因为 policy 的职责是：

- 注入控制信息
- 改变运行约束

而不是：

- 代替工具修改权威状态

真正写 `TaskState` 的仍必须是 `task_plan` handler。否则 planner 会重新退回“隐式 side effect”。

---

## 14. 与现有 harness 架构的对接建议

## 14.1 `SessionState` 增加 `task_state`

建议在 [core/session/state.py](/Users/kino/works/kino/harness/core/session/state.py) 中新增：

```python
task_state: TaskState = field(default_factory=TaskState)
```

## 14.1.1 task dispatch 的模块归属

这里建议不要把所有 task 逻辑都堆回：

- `core/query/loop.py`
- `core/session/subagent.py`

更合理的拆分是新增一个小的 task runtime 模块层，例如：

```text
core/tasks/models.py
  - TaskStatus
  - TaskExecutionMode
  - TaskRecord
  - TaskState
  - TaskPacket
  - TaskRunResult

core/tasks/planner_runtime.py
  - task_plan schema
  - validate / normalize / allocate ids
  - apply_task_plan()

core/tasks/projection.py
  - map_task_status_to_todo_status()
  - sync_todo_projection_from_tasks()

core/tasks/dispatcher.py
  - select_next_runnable_task()
  - compile_task_packet()
  - dispatch_task()
  - normalize_subagent_result()
```

### dispatcher 的分期归属

虽然从模块边界上看这些函数都属于 `core/tasks/dispatcher.py`，但第一阶段不要求全部实现。

建议分成两批：

**Phase A / B 必需：**

- `compile_task_packet()`
- `normalize_subagent_result()` 的数据结构定义或最小实现

**Phase C 才接入：**

- `select_next_runnable_task()`
- `dispatch_task()`

原因是当前设计已经在 `13.2.1 task_plan 之后如何进入第一个 task` 明确：
```text
Phase A 采用“模型自主选择执行哪个 task”
```

既然第一阶段不是 runtime 自动调度，就不需要先落地真正的 task scheduler / dispatcher loop。

### Phase A `compile_task_packet()` 字段映射

实现者需要的不是抽象原则，而是明确的编译规则。第一阶段建议固定为：

| `TaskPacket` 字段 | 来源规则 |
| --- | --- |
| `task_id` | `TaskRecord.task_id` |
| `mode` | `TaskRecord.execution_mode` |
| `agent_type` | `TaskRecord.agent_type` |
| `title` | `TaskRecord.subject` |
| `directive` | `TaskRecord.goal` |
| `task_context` | `TaskRecord.inputs` |
| `known_facts` | `TaskRecord.known_context` |
| `out_of_scope` | `TaskRecord.out_of_scope` |
| `expected_output` | `TaskRecord.expected_output`；若为空则回退为 `[TaskRecord.goal]` |
| `done_criteria` | `TaskRecord.done_criteria` |
| `required_skills` | `TaskRecord.required_skills` |
| `allowed_tools` | `TaskRecord.allowed_tools` |
| `write_scope` | `TaskRecord.write_scope` |
| `packet_revision` | `TaskRecord.packet_revision + 1` |

第一阶段不建议引入额外的 planner-only packet 字段。也就是说，`compile_task_packet()` 默认是：

```text
TaskRecord 大多数字段直接复制到 TaskPacket，只对少数字段做最小合成
```

最关键的合成规则只有两条：

1. `directive = goal`
2. `expected_output` 为空时，至少回退成 `[goal]`

这样即使 planner 只稳定产出最小 required 字段，dispatcher 仍然能编出一个最小可执行 packet。

### Phase A `compile_task_packet()` 伪代码

```python
def compile_task_packet(task: TaskRecord) -> TaskPacket:
    expected_output = task.expected_output or [task.goal]
    return TaskPacket(
        task_id=task.task_id,
        mode=task.execution_mode,
        agent_type=task.agent_type,
        title=task.subject,
        directive=task.goal,
        task_context=list(task.inputs),
        known_facts=list(task.known_context),
        out_of_scope=list(task.out_of_scope),
        expected_output=expected_output,
        done_criteria=list(task.done_criteria),
        required_skills=list(task.required_skills),
        allowed_tools=list(task.allowed_tools),
        write_scope=list(task.write_scope),
        packet_revision=task.packet_revision + 1,
    )
```

如果后续需要更复杂的 packet 合成，例如把 `goal + out_of_scope` 拼成更长 directive，应作为 Phase 2 优化，而不是第一阶段必需行为。

### 为什么不建议把 dispatch 直接写进 `QueryLoop`

`QueryLoop` 的职责应继续保持为：

- 跑模型循环
- 接收工具调用
- 执行批次
- 处理 policy hooks

如果把下列逻辑直接写进去：

- task 选择
- packet 编译
- subagent 派发
- 结果归一化

那么 `QueryLoop` 很快会再次变成“大一统调度器”，后续很难测试和复用。

### 为什么不建议把 dispatch 直接写进 `SubagentRuntime`

`SubagentRuntime` 的职责应是：

- 已知 `TaskPacket` 后，按上下文模式执行子代理

它不应决定：

- 是否需要 task planning
- 当前该跑哪个 task
- task 与 todo 如何投影
- task result 如何更新权威状态

这些职责更接近 control plane，而不是 executor。

### `QueryLoop`、task dispatcher、subagent runtime 的推荐关系

建议主路径变成：

```text
QueryLoop
  -> task_plan tool handler / planner_runtime
  -> task dispatcher
  -> SubagentRuntime
  -> task result merge
```

换句话说：

- `QueryLoop` 是 orchestrator
- `dispatcher` 是 task control plane
- `SubagentRuntime` 是 executor

这个分层比“在 QueryLoop 里顺手判断一下要不要起 subagent”稳定得多。

### `execution_mode = fresh_subagent` 的路由机制

这里也需要明确，不然 `execution_mode` 会退化成一个没人消费的 planning 字段。

推荐方案是：

```text
Phase A / B：execution_mode 只是规划元数据，不自动触发任何执行
Phase C：增加显式 task-centric 执行入口，例如 `task_execute(task_id)`
```

具体建议：

1. **Phase A / B**
   - `execution_mode` 只用于表达 planner 的执行建议
   - 模型仍然自己决定是 local 继续推进，还是先不执行只做重规划
   - QueryLoop 不根据 `execution_mode` 自动派发 subagent
2. **Phase C**
   - 新增一个显式执行入口，例如：

```json
{ "task_id": "task-2" }
```

   - handler 读取 `TaskState[task_id]`
   - 若 `execution_mode == fresh_subagent`：
     - 调用 `compile_task_packet(task)`
     - 进入 `SubagentRuntime.run(...)`
   - 若 `execution_mode == local`：
     - 拒绝该调用，并提示模型直接使用普通工具在主线程执行

### 为什么不推荐 QueryLoop 自动按 `execution_mode` 路由

- 会把“模型决定何时执行”偷偷搬回 runtime
- 让执行时机变得不可观察
- 会让 `task_plan` 调用后的副作用过重

### 为什么不推荐让模型直接调用一个自由文本 subagent 工具

如果模型要自己把 `TaskRecord` 再手工改写成一段 subagent prompt，再调用一个 generic subagent tool，那么：

- `TaskPacket` 编译边界会再次泄漏回 prompt bricolage
- 主线程和子线程之间的边界又会变得不可复现

因此更推荐的长期形态是：

```text
模型表达“执行 task-2”
runtime 负责“按 task-2 的 execution_mode 编译 packet 并路由”
```

## 14.2 `todo_state` 改为投影视图

当前 `todo_state` 可保留，以尽量复用 UI 和 renderer。

但数据来源应改为：

```text
TaskState -> project_to_todo_items() -> todo_state
```

而不是继续把 `todo` 当主规划写入点。

### 必须避免的双写问题

这是一个运行时约束，不是文档约定即可解决。

第一阶段建议这样处理：

1. 当 `task_state.tasks_by_id` 为空时：
   - 允许现有 `todo` tool 继续直接工作
   - 用于 trivial / legacy 路径
2. 当 `task_state.tasks_by_id` 非空时：
   - `todo` tool 不再被视为权威写入源
   - `todo` tool 若被调用，应失败并返回明确提示：

```text
TaskState is active. Update tasks via `task_plan` / task runtime, not `todo`.
```

3. `todo_state` 的刷新只允许由一个内部投影函数完成，例如：

```python
def sync_todo_projection_from_tasks(session_state) -> None:
    ...
```

### `sync_todo_projection_from_tasks` 的第一阶段算法

第一阶段建议把投影算法写成简单、无歧义的规则：

```python
def map_task_status_to_todo_status(status: TaskStatus) -> str:
    if status == TaskStatus.IN_PROGRESS:
        return "in_progress"
    if status == TaskStatus.COMPLETED:
        return "completed"
    return "pending"


def sync_todo_projection_from_tasks(session_state) -> None:
    task_state = session_state.task_state
    if not task_state.tasks_by_id:
        session_state.todo_state.items = []
        return

    projected_items = []
    for task_id in task_state.ordered_task_ids:
        task = task_state.tasks_by_id[task_id]
        projected_items.append(
            TodoItem(
                content=task.subject,
                active_form=task.active_form or task.subject,
                status=map_task_status_to_todo_status(task.status),
                workflow_ref=None,
            )
        )

    session_state.todo_state.items = projected_items
```

`last_projection_turn` 的记账建议由调用方按当前 query turn 更新，不要简单复用 `last_planned_turn`。

### 字段映射规则

第一阶段固定为：

- `TodoItem.content = TaskRecord.subject`
- `TodoItem.active_form = TaskRecord.active_form or TaskRecord.subject`
- `TodoItem.workflow_ref = None`

而 `todo_id` / `task_ids` 这类 richer 映射字段：

- 第一阶段不写入 `TodoState.items`
- 如确实需要，可留待后续单独增加 projection metadata 容器

### 状态映射规则

当前 `todo` 只有：

- `pending`
- `in_progress`
- `completed`

而 `TaskStatus` 更多，所以第一阶段建议这样投影：

- `pending -> pending`
- `in_progress -> in_progress`
- `completed -> completed`
- `blocked -> pending`
- `failed -> pending`
- `cancelled -> pending`

理由不是这些状态“等于 pending”，而是：

- 当前 `todo` renderer 还没有更细粒度状态
- `todo_state` 在新模型里已不是权威状态
- 真实失败/阻塞原因继续保存在 `TaskState` 中

如果后续 UI 要暴露 `blocked` / `failed`，应扩展 `TodoProjectionItem` 和 renderer，而不是在第一阶段里把投影逻辑做复杂。

### 为什么第一阶段不建议让 `todo` 反向写 `TaskState`

因为那会重新引入歧义：

- `todo` 是给用户看的视图
- `TaskState` 是给执行器看的权威

允许视图层反向写权威层，等于把这次分层重新打穿。

### 14.2.1 `PromptAssembler` 需要感知 `TaskState`

当前 [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py) 的 runtime context 只渲染：

- `<active-skills>`
- `<todo-state>`
- `<file-runtime>`

引入 `TaskState` 后，第一阶段建议增加一个新的 runtime block：

```text
<task-state>
```

并明确渲染规则：

1. 当 `task_state.tasks_by_id` 为空：
   - 保持当前行为
   - 继续渲染 `<todo-state>`
2. 当 `task_state.tasks_by_id` 非空：
   - 渲染 `<task-state>`
   - 不再渲染 `<todo-state>`，避免把 projection 视图和权威状态重复塞给模型
   - 保留 `<active-skills>` 和 `<file-runtime>`

### `<task-state>` 第一阶段建议渲染的最小信息

至少包括：

- `current_task_id`
- `ordered_task_ids`
- 每个 task 的：
  - `task_id`
  - `status`
  - `execution_mode`
  - `active_form or subject`

推荐形态例如：

```xml
<task-state current_task_id="task-2">
  <task id="task-1" status="completed" mode="local">审查现有 runtime 断点</task>
  <task id="task-2" status="in_progress" mode="fresh_subagent">正在设计 TaskPacket 编译规则</task>
  <task id="task-3" status="pending" mode="local">写 spec</task>
</task-state>
```

### `task_planning_required` 是否需要 assembler 特殊渲染

第一阶段建议：

- **不需要**在 `PromptAssembler` 里单独渲染 `task_planning_required`
- 这类提醒继续由 `TaskPlanningPolicy` 的注入消息承担

原因是：

- policy 已经能精确控制何时提醒
- 把“强提醒”也塞进 stable/runtime prompt，容易造成重复和钝化
- assembler 更适合渲染状态事实，不适合承担控制指令

## 14.3 `SubagentRequest` 需要升级

当前：

- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)

只有：

```python
task: str
agent_type: SubagentType
description: str | None
max_turns: int | None
```

建议升级为：

```python
@dataclass
class SubagentRequest:
    task_packet: TaskPacket
    agent_type: SubagentType
    description: str | None = None
    max_turns: int | None = None
    preloaded_skill_ids: list[str] = field(default_factory=list)
```

### 为什么不能继续只传 `task: str`

因为那样会丢掉：

- execution_mode
- required_skills
- allowed_tools
- write_scope
- done_criteria
- packet_revision

这些都是本次设计想要解决的核心边界。

## 14.4 subagent runtime 需要支持 `fresh/fork`

当前 `SubagentContextMode` 只有类型定义，没有真正接入主路径。

建议把 `TaskExecutionMode` 与 `SubagentContextMode` 对齐：

- `FRESH_SUBAGENT` -> `SubagentContextMode.FRESH`
- `FORK_SUBAGENT` -> 新增真正的 `SubagentContextMode.FORK`

### `fresh` 路径

- subagent 初始消息只来自 compiled `TaskPacket`
- 预加载 `required_skills`
- 继承工作目录，但不继承主对话消息

### `fork` 路径

- 继承父 query 当前消息视图
- 只追加 directive
- 可选附带 worktree notice

### 第一阶段建议

虽然协议层定义 `fork`，但第一阶段建议只真正接入：

- `SubagentContextMode.FRESH`

`FORK` 需要解决的问题明显更多：

- 快照父 `MessageViewBuilder` 当前视图
- 明确哪些 session-scoped 事实要复制，哪些只读共享
- 处理 `read_file_state`、`invoked_skills`、tool permission 的继承
- 处理写操作对父状态的可见性与隔离

因此，本 spec 现在把 `fork` 定义成：

```text
协议先稳定，runtime 默认延后
```

而不是“第一阶段承诺一定实现”。

## 14.5 skill 应绑定到任务，而不是继续靠全局提醒

新的优先顺序应该是：

1. planner 生成 `TaskRecord.required_skills`
2. dispatcher 编译 packet 并预加载 skill
3. 只有在 planner 尚未形成任务时，`SkillRelevancePolicy` 才作为弱提醒存在

这意味着：

- `SkillRelevancePolicy` 不再是主方案
- 至少从 `b9ae764..HEAD` 这条路线开始，不应继续加码为中心方向
- 但在它退回辅助层之前，仍应先做最小修正：只看当前 user 请求、加强 meta-skill classifier 规则、增加 host 侧 post-validation

---

## 15. 输入输出协议细节

## 15.1 Planner 输出

planner 的输出至少应能表达：

```json
{
  "tasks": [
    {
      "task_id": "task-1",
      "parent_todo_id": "todo-runtime-redesign",
      "subject": "审查现有 skill/todo/runtime 断点",
      "goal": "确定当前实现中任务混杂执行的真正断点",
      "active_form": "正在审查 skill/todo/runtime 断点",
      "execution_mode": "local",
      "required_skills": [],
      "known_context": [
        "skill 工具当前只在下一轮可见",
        "tool batch 只按只读/写入分批"
      ],
      "out_of_scope": [
        "不直接修改社区 skill 文件"
      ],
      "expected_output": [
        "一段问题定位结论",
        "关键代码文件列表"
      ],
      "done_criteria": [
        "指出至少一个真正运行时断点"
      ],
      "write_scope": []
    }
  ]
}
```

### 15.1.1 为什么这里仍然展示 richer 示例

这个示例的作用是：

- 说明最终 `TaskState` 能承载什么信息
- 让 reviewer 看清 `TaskRecord` 的目标形状

它**不等于**第一阶段模型必须一次性稳定吐出所有字段。

第一阶段应允许 planner 只产出最小 required 字段；tool handler 负责：

- 填默认值
- 补空列表
- 拒绝缺失必需字段

### 15.1.2 模型输出不合规时的处理

这是第一阶段必须定义的行为：

1. 模型调用 `task_plan` 且参数不合法：
   - tool 返回 `validation_failed`
   - 保持当前 `TaskState` 不变
   - 模型下一轮根据错误信息重试
2. 模型在需要 task planning 的情况下跳过 `task_plan`，直接开始执行：
   - `TaskPlanningPolicy` 注入强提醒
   - 如仍尝试调用执行类工具，可由 runtime 拒绝并返回：

```text
Task planning required before execution. Call `task_plan` first.
```

3. 模型产出部分填充的任务：
   - 若缺 required 字段：拒绝
   - 若只缺 optional 字段：接受并填默认值

这也是为什么本 spec 坚持第一阶段必须走 tool，而不是自由 assistant 输出。

## 15.2 `TaskPacket` -> subagent 输入

### `fresh` 示例

```text
Task: 审查任务边界设计

Directive:
分析为什么当前 harness 无法稳定把用户请求拆成两个独立执行单元。
只聚焦 task planning、task packet、subagent dispatch 三层。

Why this task exists:
- 主 Agent 当前会把多个独立工作混在一轮 tool batch 里执行
- 仅强化 skill relevance 没有改善根因

Known facts:
- skill 工具当前在下一轮才可见
- QueryLoop 不认识 task boundary
- 现有 subagent runtime 尚未接入主规划路径

Out of scope:
- 不讨论远程 skill
- 不要求改现有 skill frontmatter

Required skills/workflows:
- code-explorer（若存在）用于阅读局部实现

Allowed tools:
- find
- read_file

Write scope:
- none

Expected output:
- 给出当前断点列表
- 给出对 TaskPacket 的最小字段建议

Done criteria:
- 输出必须能直接支撑 spec 编写
```

### `fork` 示例

```text
Directive:
只核对 Claude Code 里 fresh/fork subagent 的真实输入边界。
不要做 harness 方案设计，只返回源码证据和文件位置。
```

## 15.3 `TaskRunResult` -> 主 Agent 合并

推荐结果格式：

```json
{
  "task_id": "task-2",
  "success": true,
  "status": "completed",
  "summary": "Claude Code 对 fresh agent 使用零上下文 prompt，对 fork agent 继承父上下文并仅追加 directive。",
  "artifacts": [],
  "files_modified": [],
  "open_questions": [
    "harness 是否需要第一阶段就支持真正的 fork context mode"
  ],
  "recommended_next_steps": [
    "在 spec 中单独定义 fresh 与 fork packet 编译规则"
  ]
}
```

主 Agent 合并时应只消费这些结构化字段，而不是重新解析整段自由文本。

---

## 16. 迁移建议

## 16.1 分阶段迁移

### Phase A：定模型，不先追求全链路自动化

- 引入最小 `TaskState`
- 引入最小 `TaskRecord` 必需字段
- 引入 `TaskPacket` 协议
- 新增 `task_plan` tool
- 定义 `TodoProjection`

### Phase B：让主 Agent 先基于 Task 规划

- 复杂任务先产出 `TaskState`
- 当前 UI 继续使用 `todo_state`
- `todo_state` 改由 `TaskState` 投影生成

### Phase C：把 subagent 接到 Task dispatch

- `SubagentRequest` 改为接收 `TaskPacket`
- 真正接入 `fresh`
- `fork` 只保留协议和接口，不默认实现
- 支持 `preloaded_skill_ids`

### Phase D：弱化旧 skill relevance 路线

- `SkillRelevancePolicy` 退回为辅助提醒
- 不再继续把 `b9ae764..HEAD` 那条路线当主改进方向
- 在退回辅助提醒之前，先完成短期止血：
  - router 仅消费最后一条 user 请求
  - LLM classifier 明确约束 meta-skill 触发条件
  - `skill-creator` 等高风险 skill 增加 host 侧 post-validation

## 16.2 对现有 `todo` 的兼容

第一阶段不要求立刻删掉 `todo` 工具。

兼容策略：

- `todo` 仍可保留 UI 和 renderer 语义
- 但真正的计划权威来自 `TaskState`
- `todo` 最终应成为 projection / compatibility layer

---

## 17. 风险与缓解

## 风险 1：Task 模型过重，简单任务也被迫走复杂流程

缓解：

- 明确 trivial bypass 规则
- 仅对多步骤 / 多目标 / subagent 场景强制 `TaskState`

## 风险 2：fresh packet 质量不足，变成“换个 prompt 再失败一次”

缓解：

- `TaskPacket` 必须有结构化字段
- 不允许只传一段自由文本
- 主 Agent 必须先完成任务理解

## 风险 3：fork 被滥用，导致上下文继承失控

缓解：

- 仅在明确适用条件下允许 fork
- fork packet 只能是短 directive
- 默认不允许 subagent 再 spawn

阶段说明：

- 这是 **Phase 2+ 风险**
- Phase A / B / C 因为默认不落地 `fork` runtime，实现优先级应低于其他风险

## 风险 4：TODO 与 TASK 双写，状态再次分裂

缓解：

- 规定 `TaskState` 为唯一权威
- `todo_state` 只从 `TaskState` 投影
- `todo` tool 在 `TaskState` 激活时必须拒绝直接写入

## 风险 5：skill 再次漂回全局模糊状态

缓解：

- `required_skills` 写入 `TaskRecord`
- packet 编译与 dispatcher 负责加载 skill
- relevance policy 退回辅助角色

---

## 18. 方案比较

## 方案 A：继续强化 `SkillRelevancePolicy`

改动：

- 更强的匹配
- 更强的 prompt
- 更强的 enforcement

优点：

- 代码改动少
- 对现有路径扰动小

缺点：

- 仍然没有正式任务边界
- 仍然没有 subagent 输入协议
- 仍然没有解决 `TODO` 与执行语义混淆

结论：

- 不推荐作为主方向

## 方案 B：`TODO` 继续当权威，再在 TODO 上加 subagent

改动：

- 让一个 TODO 对应一个 subagent

优点：

- 表面直觉简单

缺点：

- `TODO` 粒度不等于调度粒度
- 无法承载 execution_mode / write_scope / done_criteria
- 会继续混淆视图与执行对象

结论：

- 不推荐

## 方案 C：引入 `TaskState + TaskPacket + fresh/fork`

优点：

- 边界清楚
- 能自然承接 subagent
- 能与 Claude Code 的关键经验对齐
- 不需要一次性搬入它全部 task infra

缺点：

- 需要新增 session state 与调度层
- 比纯 prompt 修补更重

结论：

- 推荐

---

## 19. Reviewer 应能看清的结论

reviewer 在读完这份 spec 后，应能明确判断：

1. 为什么 `b9ae764..HEAD` 这条 skill relevance 强化路线不是主解。
2. 为什么 `TODO` 不能继续同时扮演视图和执行权威。
3. 为什么必须引入正式 `Task` 对象。
4. 为什么 subagent 输入边界必须通过 `TaskPacket` 构造。
5. 为什么 `fresh` 与 `fork` 必须是两套协议，而不是一个开关。
6. Claude Code 到底提供了哪些直接证据，哪些地方需要 harness 自己做设计。
7. 第一阶段最值得实现的，是 `TaskState`、`TaskPacket` 和 `fresh/fork` 输入规则，而不是继续打磨 skill relevance。

---

## 20. 建议的第一批验收标准

1. 对一个包含两个独立目标的复杂请求，系统会先产出 `TaskState`，而不是直接混合执行。
2. 用户看到的 TODO 来自 `TaskState` 投影，而不是模型随手写出的自由计划。
3. `task_plan` 具备完整 schema 校验、rewrite 语义、默认值归一化和自动分配缺失 `task_id` 的能力。
4. 当 `TaskState` 存在时，`todo` tool 会拒绝直接写入，`todo_state` 只能来自 `sync_todo_projection_from_tasks()`。
5. `fresh_subagent` 启动时拿到的是完整 `TaskPacket`，不依赖主线程完整 transcript。
6. subagent 完成后返回 `TaskRunResult`，主线程能结构化合并。
7. `TaskPlanningPolicy` 能在需要时注入提醒，并阻止模型绕过 `task_plan` 直接进入执行。
8. `SkillRelevancePolicy` 即使保留，也不再是任务拆分与执行边界的主机制。
9. 现有 `subagent.py` 不再只是孤立执行器，而成为 `Task` 调度路径的一部分。

### Phase 2+ 验收项

以下不属于第一批实现验收，但协议必须在 spec 中定义清楚：

1. `fork_subagent` 启动时继承父上下文，只追加短 directive。
2. grouped todo projection 支持 `1 TODO -> N TASK`。

---

## 21. 开放问题

以下问题可以留到后续实现或评审进一步收敛：

1. `TaskState` 第一阶段是否只放 session memory，还是要同步落盘。
2. `required_skills` 的预加载是走“伪调用 skill 工具”还是直接写 `invoked_skills`。
3. Phase 2 里 `TodoProjection` 何时从默认的 `1 TASK -> 1 TODO` 升级到 grouped projection。
4. `TaskPlan` 未来是否要拆成 `task_plan` + `task_update` 两个工具。
5. `TaskRunResult` 何时完全替代底层 `SubagentRunResult` 成为唯一 public 结果对象。

本 spec 的建议是：

- `task_plan` tool 不再是开放问题，而是当前推荐方案
- `fork` 第一阶段默认延后，不再作为当前必做项
- 其余问题可以后续评审
- 但 `TaskState`、`TaskPacket`、`fresh/fork` 的概念边界必须先定住

因为这三者一旦不清楚，后面无论是主 Agent 还是 subagent，都会重新退回 prompt bricolage。
