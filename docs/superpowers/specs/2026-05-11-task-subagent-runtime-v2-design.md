# Task / Subagent Runtime V2 设计

> 日期：2026-05-11
> 状态：草案
> 目标：以 Claude Code 的 `Task/Agent` 体验为对标，重构 harness 的 task / subagent 协议、控制平面、可见性与结果契约。
> 前置文档：
> - [2026-05-10-task-subagent-runtime-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-task-subagent-runtime-design.md)
> - [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md)
> - [2026-05-10-llm-skill-relevance-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-llm-skill-relevance-design.md)
> 当前相关实现：
> - [core/tools/builtin/task_plan.py](/Users/kino/works/kino/harness/core/tools/builtin/task_plan.py)
> - [core/tools/builtin/task_execute.py](/Users/kino/works/kino/harness/core/tools/builtin/task_execute.py)
> - [core/tasks/models.py](/Users/kino/works/kino/harness/core/tasks/models.py)
> - [core/tasks/dispatcher.py](/Users/kino/works/kino/harness/core/tasks/dispatcher.py)
> - [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)
> - [core/tools/runtime.py](/Users/kino/works/kino/harness/core/tools/runtime.py)
> - [core/ui/renderer.py](/Users/kino/works/kino/harness/core/ui/renderer.py)

---

## 1. 摘要

`2026-05-10` 版本的设计已经正确指出：`Task` 必须成为主 Agent、subagent、skill 和执行边界之间的正式中介对象，`fresh_subagent` 的输入边界必须由 `TaskPacket` 显式构造，不能依赖模糊共享上下文。

但最近的真实使用和代码排查说明，仅有 `TaskState + TaskPacket + task_execute` 还不够。当前方案在三个用户最敏感的层面仍然失败：

1. 用户看不到派发给 subagent 的真实输入边界。
2. 用户看不到 subagent 的实时执行过程，只能黑盒等待。
3. subagent 的结果返回不是正式契约，主 Agent 难以稳定消费，容易退化成半截自然语言或伪工具语法。

因此，本设计不是对旧方案做局部补丁，而是提出一个 **V2 替换式主路径**：

```text
User Request
  -> Task Control Plane
  -> Dispatch Compiler
  -> Subagent Session (fresh / fork)
  -> Event Stream
  -> Structured Result Envelope
  -> Task Merge
  -> Main Agent Continuation
```

核心变化是：

1. `Task` 与 `Subagent` 升级为一等运行时对象，而不是 `QueryLoop + tool_call` 的附属物。
2. `fresh_subagent` 与 `fork_subagent` 统一纳入正式协议，不再把 `fork` 长期留在“概念已定义、运行时延后”的状态。
3. subagent 执行期间必须对用户持续可见，默认采用“任务卡片 + 关键事件流”的混合终端体验。
4. subagent 的返回值必须先满足结构化消费，再考虑展示层自然语言总结。
5. 旧的 `task_plan / task_execute / SubagentRuntime` 不再是长期中心，只保留最小迁移壳。

---

## 2. 背景与前因后果

## 2.1 为什么旧设计方向是对的

`2026-05-10-task-subagent-runtime-design.md` 已经抓住了两个根因：

1. 旧路线把“主 Agent 自己识别并自己同时执行所有事”当成默认主路径，导致任务边界从未成为正式运行时对象。
2. `subagent` 输入边界必须由调用方显式写进任务包，而不是依赖共享上下文和模糊提示词。

因此，旧文档中关于下列判断仍然成立：

- `TaskState` 应是权威状态，不是 `todo`。
- `TaskPacket` 必须是 compiled execution payload，而不是 `TaskRecord` 的别名。
- `fresh` 与 `fork` 是两种不同的输入构造哲学。
- 搜索、信息收集、高噪声探索类工作天然更适合外包给 subagent。

这些结论在 V2 中全部保留。

## 2.2 为什么旧设计仍然不够

旧设计主要聚焦三个面：

1. planner 与 runtime 的边界
2. `TaskRecord -> TaskPacket` 的编译关系
3. `fresh_subagent` 的最小可执行路径

它没有真正完成下列三件事：

1. 定义用户在终端中看到的 **subagent 前台体验**。
2. 定义 subagent 在运行中的 **事件流协议**。
3. 定义 subagent 完成后的 **结构化结果契约**。

因此，虽然旧设计在控制平面方向上是对的，但一旦用户真正用它来做联网搜索类任务，就会暴露新的失败模式。

## 2.3 本轮实际暴露出来的问题

真实现象集中在三个方面：

### 问题 A：输入边界对用户不可见

用户无法确认主 Agent 到底把什么交给了 subagent，例如：

- 是否把“明天”解析成了具体日期
- 是否把地点、出发点、交通限制、时间约束写进了任务包
- 是否遗漏了关键上下文
- 是否存在信息为空或过薄的问题

当搜索结果不理想时，用户既无法判断是 planner 没写进去，还是 runtime 没传进去，还是 subagent 没用好。

### 问题 B：执行过程对用户不可见

subagent 执行期间，用户常常只看到主线程挂起，而看不到：

- subagent 当前是否已经启动
- 它是否真的在调用工具
- 现在卡在搜索、天气查询、技能加载还是总结阶段
- 为什么复杂任务等待时间明显变长

这会让 subagent 的体验比主线程直接调用工具更差，因为它变成了更长时间的黑盒等待。

### 问题 C：结果回收不稳定

当 subagent 没有真正进入“正式工具调用 + 正式结果归一化”路径时，主线程可能收到：

- 半截自然语言
- 伪工具语法
- 技能加载提示但没有真实结果
- 缺少结构化结论的摘要文本

这会让主 Agent 很难可靠地消费 subagent 输出，也让用户无法区分“运行完成但结果差”与“运行路径本身就坏了”。

---

## 3. 本轮排查与研究结论

本节只记录已经通过当前代码验证过的断点，不做猜测。

## 3.1 当前路径中的具体断点

在当前实现中，subagent 主路径是：

```text
QueryLoop
  -> ToolExecutorRuntime
  -> task_execute
  -> SubagentRuntime
  -> SessionEngine
  -> QueryLoop (subagent)
```

实际排查发现如下问题：

### 断点 1：调用上下文没有稳定携带 renderer

`task_execute` 想访问父级 renderer，但 `ToolExecutorRuntime._build_call_context()` 原本没有把 renderer 复制到真实 call context。

直接后果：

- `task_execute` 拿到的 `context.renderer` 可能为空
- 即使设计上想展示 subagent dispatch panel，也无法稳定生效

### 断点 2：工具事件是批处理后回放，不是实时流

`ToolExecutorRuntime.execute_batch()` 原本是在整批工具执行完后，统一 `show_tool_call()` / `show_tool_result()`。

直接后果：

- 用户看到的是“事后回放”
- 不是“工具已经开始执行”的实时反馈
- 对长时间运行的 subagent，等待体验依然接近黑盒

### 断点 3：subagent SessionEngine 没有稳定拿到 tools schema

`SubagentRuntime` 在创建 `SessionEngine` 时只把 `sub_schemas` 传给了 `MessageViewBuilder`，没有保证 API 调用层总能拿到同一套 schema。

直接后果：

- 模型可能看到了工具描述性上下文，但底层调用层没有正式 tools
- 响应会退化成伪工具语法或自然语言假装调用工具

### 断点 4：`TaskPacket.task_context` 在最终 prompt 中丢失

`compile_task_packet()` 会把 `TaskRecord.inputs` 编译为 `TaskPacket.task_context`，但 `_render_fresh_packet()` 之前没有把它渲染进 subagent 的初始 prompt。

直接后果：

- planner 即使正确填了输入，subagent 也未必真的看得到
- 用户会误以为“包里可能没传”或“信息是不是空的”

## 3.2 更深层的结构性问题

上面四个断点只是症状，背后的结构性问题更重要：

1. `Task` 语义仍然依赖工具链路旁路传递，而不是正式 runtime primitive。
2. subagent 可见性不是事件流设计出来的，而是 renderer 层硬插进去的。
3. subagent 结果没有一等 envelope，仍然把“最终展示给用户的话”和“给主线程消费的结果”混在一起。
4. `fresh` 与 `fork` 还没有统一协议，只是类型名存在。

结论是：

> 现有路径即使继续加补丁，也很难自然演化成 Claude Code 风格的 `Task/Agent` 体验。

因此 V2 选择 **重构优先**，允许重定义工具面和控制平面。

## 3.3 与 Claude Code 对标后的研究结论

本轮不追求抄写 Claude Code 源码实现细节，只提炼对 harness 有约束力的事实：

1. `Task` 和 `Todo` 在 Claude Code 中是两层对象，不混用。
2. `Agent` 是正式能力边界，而不是普通工具调用的文字模拟。
3. `fresh` 与 `fork` 的输入构造哲学不同。
4. 用户能够看到任务、派发和执行，而不是只看到黑盒等待。
5. 子执行单元返回的结果必须能被父级稳定消费。

对 harness 的约束是：

- 不能只把 Claude Code 的 prompt 语言搬过来。
- 必须把它背后的对象模型、前台体验和结果契约真正落到 runtime 中。

---

## 4. Goals / Non-Goals / Core Principles

## 4.1 Goals

1. 把 `Task` 和 `Subagent` 提升为一等运行时对象，不再把它们当成 `QueryLoop + tool_call` 的附属拼装物。
2. 让 `fresh_subagent` 和 `fork_subagent` 共享一套正式协议，但明确两者的上下文边界、可见性和结果契约不同。
3. 让联网搜索、资料收集、高噪声探索类工作默认优先落到 `subagent`，避免主上下文被大量中间结果污染。
4. 让用户在终端中清楚看到：
   - 派发给 subagent 的输入边界
   - subagent 当前在做什么
   - subagent 最终返回了什么，以及主 Agent 如何消费它
5. 让整体行为和交互都尽量向 Claude Code 的 `Task/Agent` 靠拢，但保留 harness 可维护、可测试、可迁移的实现边界。

## 4.2 Non-Goals

1. 不把现有 `task_plan / task_execute / SubagentRuntime` 继续当作长期中心接口。
2. 不在本设计中优先解决历史上的 `SkillRelevancePolicy` 路线问题，除非它直接阻塞新主路径。
3. 不要求第一版就把所有 local 任务都改写为 task-centric；优先覆盖 subagent 场景，尤其是联网搜索和信息收集。
4. 不复制 Claude Code 的全部内部代码结构；目标是体验和行为等价，不是源码形态相似。

## 4.3 Core Principles

1. `Task first, tool second`
   `Task` 才是执行边界，工具只是任务内部的执行机制。
2. `Explicit boundary over implicit context`
   subagent 输入必须显式构造，不能依赖它从父对话自己推断。
3. `Observable execution over black-box waiting`
   subagent 运行期间必须持续对用户可见。
4. `Structured result over free-form prose`
   subagent 返回值先服务主线程消费，再服务展示层总结。
5. `Fresh and fork are different protocols`
   两者从协议层就不同，不能用一个布尔开关抽象。
6. `Replace, don’t endlessly patch`
   V2 默认允许重定义 task/subagent 工具面与控制平面，而不是继续叠补丁。

---

## 5. 用户可见目标体验

V2 的终端体验默认采用 **混合模式**：

1. 上层是任务卡片和状态变化
2. 下层是关键事件流

它既要保留 Claude Code 风格的任务前台感，也要避免把所有工具细节都喷到终端。

## 5.1 Dispatch 前台体验

当主 Agent 派发 subagent 时，用户必须立即看到一张任务卡片，至少包含：

- `task_id`
- `dispatch_id`
- `mode`：fresh / fork
- `agent_role`
- `title`
- `why this task exists`
- `task_context`
- `constraints / out_of_scope`
- `expected_output`
- `done_criteria`

### `fresh` 卡片必须额外说明

- 这是一个零上下文子代理
- 它不会继承父会话历史
- 它看到的只有当前这份 dispatch envelope

### `fork` 卡片必须额外说明

- 它继承的是哪个父快照
- 附加了什么 directive / delta facts
- 哪些父上下文事实被显式排除

## 5.2 运行中体验

subagent 运行中，终端必须持续输出关键事件，而不是只显示“正在思考”。

默认要展示的事件类型：

- `dispatch_started`
- `context_prepared`
- `skills_preloaded`
- `tool_started`
- `tool_finished`
- `milestone`
- `blocked`
- `completed`
- `failed`

### 显示原则

1. 工具开始时立即可见，不允许批处理后回放。
2. 工具结果默认只显示摘要，不原样喷长输出。
3. 长时间运行任务要有阶段变化或心跳，不允许用户连续几十秒什么都看不到。
4. 子代理中的细碎内部思考默认不展示；对用户展示状态和关键动作即可。

## 5.3 收尾体验

subagent 完成后，终端必须输出一个 completion card，至少包含：

- 成功 / 失败状态
- 简洁可读的完成摘要
- 关键产出
- 主 Agent 后续可直接消费的结论摘要
- 如失败，给出 failure kind 与用户可理解原因

如果 subagent 产生了大型原始材料，例如网页抓取、长搜索日志、细碎工具结果：

- 默认不写回主对话正文
- 应持久化为 artifact / trace reference
- 主线程只消费压缩后的结构化结果

---

## 6. V2 总体架构

V2 不再围绕“模型先决定调用哪个工具”来表达 task/subagent，而是围绕任务控制平面表达：

```text
User Request
  -> Task Control Plane
  -> Dispatch Compiler
  -> Subagent Session
  -> Event Stream
  -> Result Envelope
  -> Task Merge
  -> Main Agent Continuation
```

## 6.1 主要组件

### 1. Task Control Plane

职责：

- 维护权威任务对象
- 决定任务状态迁移
- 触发 dispatch
- 合并 subagent 结果

它不负责：

- 直接执行工具
- 直接渲染终端输出
- 直接拼 prompt

### 2. Dispatch Compiler

职责：

- 从 `TaskRecord` 编译正式 `DispatchEnvelope`
- 根据 `fresh` / `fork` 规则构造上下文
- 执行字段验证、边界补全、约束检查

它是 V2 中最重要的“输入边界编译器”。

### 3. Subagent Session

职责：

- 启动 fresh / fork 子会话
- 执行工具
- 产出事件
- 返回结构化结果

它不负责：

- 决定是否要派发这个任务
- 修改权威 `TaskState`

### 4. Event Stream

职责：

- 把 subagent 执行期间的关键动作变成正式事件
- 同时服务 renderer、task timeline 和调试持久化

### 5. Result Envelope

职责：

- 作为 subagent 的唯一正式回传格式
- 提供结构化结果给主 Agent 消费
- 提供摘要给 renderer 展示

---

## 7. 数据模型与正式协议

V2 不建议继续把 `TaskPacket` 当作最终设计名。它在旧设计中承担了“compiled execution payload”的职责，但 V2 需要更明确地区分：

1. 权威任务对象
2. 派发请求
3. 上下文信封
4. 结果信封

## 7.1 权威任务对象：`TaskRecordV2`

`TaskRecordV2` 仍是权威状态对象，职责是“主线程眼中的任务”。

它至少应包含：

- `task_id`
- `title`
- `objective`
- `status`
- `execution_mode`
- `agent_role`
- `why`
- `inputs`
- `known_facts`
- `constraints`
- `expected_output`
- `done_criteria`
- `skills`
- `tool_policy`
- `write_scope`
- `result_ref`
- `timeline_ref`

与旧版相比，最重要的变化是：

- `goal` 改为更明确的 `objective`
- `out_of_scope` 扩展为更一般化的 `constraints`
- 增加 `why`
- 增加 `result_ref / timeline_ref`

## 7.2 派发请求：`DispatchRequest`

`DispatchRequest` 表达“主线程决定现在启动一次子执行”。

```python
@dataclass
class DispatchRequest:
    task_id: str
    dispatch_id: str
    mode: Literal["fresh", "fork"]
    agent_role: str
    max_turns: int | None
    parent_snapshot_ref: str | None
```

它只描述一次 dispatch，不负责承载完整输入边界。

## 7.3 上下文信封：`DispatchEnvelope`

`DispatchEnvelope` 才是 V2 的核心输入协议。它取代旧版 `TaskPacket` 的角色。

```python
@dataclass
class DispatchEnvelope:
    task_id: str
    dispatch_id: str
    mode: Literal["fresh", "fork"]
    agent_role: str
    title: str
    objective: str
    why: list[str]
    task_context: list[str]
    known_facts: list[str]
    constraints: list[str]
    expected_output: list[str]
    done_criteria: list[str]
    required_skills: list[str]
    allowed_tools: list[str]
    write_scope: list[str]
    parent_snapshot_ref: str | None
    delta_context: list[str]
    excluded_parent_context: list[str]
    envelope_revision: int
```

V2 的关键判断是：

> 旧版 `TaskPacket` 太像“扁平字段袋子”，不足以表达 `fork` 所需的父快照和 delta 语义。

因此 V2 直接改名并扩展为 `DispatchEnvelope`。

## 7.4 结果信封：`SubagentResultEnvelope`

V2 要求 subagent 的正式返回值始终结构化，但第一版结果契约应保持最小而稳定：

```python
@dataclass
class SubagentResultEnvelope:
    task_id: str
    dispatch_id: str
    success: bool
    completion_kind: Literal["completed", "blocked", "failed", "cancelled"]
    summary: str
    raw_text: str
    artifacts: list[str]
    files_modified: list[str]
    open_questions: list[str]
    recommended_next_steps: list[str]
    failure_reason: str | None
    trace_ref: str | None
    metadata: dict[str, Any]
```

要求：

1. `summary` 给用户看
2. `raw_text / open_questions / recommended_next_steps` 给主线程消费
3. `trace_ref / metadata` 指向完整原始执行痕迹与执行元信息

不再接受“只有自然语言字符串”的结果协议。

---

## 8. `fresh` 与 `fork` 的统一协议

## 8.1 总原则

V2 中：

- `fresh` 与 `fork` 共享统一 envelope 类型
- 但输入构造规则不同

统一的是：

- 都由 `DispatchEnvelope` 表达
- 都产生同构 `SubagentResultEnvelope`
- 都走同一事件流与 renderer 机制

不同的是：

- 上下文来源
- 风险模型
- 验证规则

## 8.2 `fresh` 协议

`fresh` 表示：

```text
子代理不继承父对话历史，只接受一份编译好的任务信封。
```

### `fresh` 的强约束

1. 不继承父 transcript
2. 不继承父 working transcript slice
3. 只继承工作目录、必要 runtime policy、必要 skill registry
4. 所有任务相关事实必须显式写进 envelope

### `fresh` 必须回答的问题

对于搜索类任务，`DispatchEnvelope` 至少应明确：

- 查什么
- 对象是什么
- 时间范围是什么
- 空间范围是什么
- 约束条件是什么
- 结果要以什么形式返回

例如天气 / 行程类任务，如果缺失这些字段，dispatch compiler 应视为高风险低质量派发：

- 具体日期
- 起点 / 终点
- 交通限制
- 天数
- 输出粒度

V2 不允许“只写一句泛化目标就起 fresh_subagent”。

第一版实现中，这类检查默认应先以 `warning` 形式暴露给用户和主线程，而不是一律 hard fail。只有在明确配置了严格策略时，才应升级为阻断。

## 8.3 `fork` 协议

`fork` 表示：

```text
子代理继承父快照，但仍然要通过显式 envelope 描述这次分叉任务的边界。
```

### `fork` 的强约束

1. 必须有 `parent_snapshot_ref`
2. 必须有 `objective`
3. 必须有 `delta_context` 或明确的 `constraints`
4. 必须能够回答“为什么不能直接由主线程继续做”

### `fork` 不允许退化成什么

不允许把 `fork` 退化成：

```text
把父上下文全扔给子代理，再附一句“你去做一下”
```

V2 要求 `fork` 至少说明：

- 继承了哪个父快照
- 新增了什么目标
- 哪些父事实仍然 relevant
- 哪些父事实要忽略

## 8.4 为什么 V2 不再延后 `fork`

旧设计把 `fork` 定义为协议先稳定、runtime 延后，是合理的第一阶段策略。

但 V2 的目标已经不是“先把 fresh 跑通”，而是：

> 让 `Task/Agent` 真的成为主路径能力。

如果 `fork` 长期缺位，会导致：

1. 需要继承当前上下文的分析型任务继续被主线程硬塞执行。
2. 搜索类任务和分析类任务被迫使用不同、不一致的派发语义。
3. `subagent` 无法成为统一能力边界。

因此 V2 直接把 `fork` 纳入正式主路径。

---

## 9. 事件流与可见性协议

V2 的前台体验必须基于正式事件流，而不是 renderer 的旁路技巧。

## 9.1 事件对象：`SubagentEvent`

```python
@dataclass
class SubagentEvent:
    dispatch_id: str
    task_id: str
    seq: int
    kind: str
    status: str
    title: str
    detail: dict[str, Any]
    created_at: float
```

建议的 `kind`：

- `dispatch_started`
- `tool_started`
- `tool_finished`
- `blocked`
- `completed`
- `failed`

说明：

1. 第一版用户可见事件应收敛为最小集合：`dispatch_started`、`tool_started`、`tool_finished`、`completed/failed/blocked`
2. `context_prepared`、`skill_preloaded`、`assistant_status`、`milestone` 可作为内部保留事件类型，但不应默认展示给用户

## 9.2 事件生产原则

1. 事件必须由 subagent session 正式发出。
2. renderer 只消费事件，不自己发明事件。
3. `tool_started` 必须在真实工具执行前发出。
4. 长任务必须周期性产生可见状态变化。
5. 第一版 transport 应优先使用同步 callback；是否升级到独立总线是后续实现决策，不是本阶段前提。

## 9.3 渲染原则

renderer 默认输出两层内容：

### 层 1：任务卡片

- 创建时显示完整边界
- 状态变化时更新

### 层 2：关键事件流

示例：

```text
[task-2] 已派发 fresh 子代理：收集深圳天气与公共交通信息
  $ Weather(Shenzhen, 2026-05-12)
  $ Search(桔钓沙 公共交通)
  · 已获取天气摘要
  · 正在整理景点与交通信息
  ✓ 子代理完成：返回天气、交通、景点、推荐活动
```

默认渲染约束：

1. 工具开始时立即可见，不允许批处理后回放。
2. 工具结果默认只显示摘要，不原样喷长输出。
3. 长时间运行任务要有阶段变化或心跳，不允许用户连续几十秒什么都看不到。
4. 子代理中的细碎内部思考默认不展示；对用户展示状态和关键动作即可。
5. 默认采用 compact 模式：`tool_started` 只显示工具名和一行摘要，`tool_finished` 只显示结果摘要。
6. dispatch preview 默认只展示关键字段；完整 envelope 通过 verbose 模式查看。

## 9.4 为什么不能继续只靠 ToolExecutorRuntime 渲染

因为那样会持续把“子代理可见性”绑定在“工具是否在主线程 runtime 中被当成通用工具执行”上，结构是反的。

正确关系应是：

```text
Subagent Session
  -> Event Sink / Callback
  -> Renderer / Timeline Store
```

而不是：

```text
Tool Runtime
  -> 尝试顺手把一些子代理事件 print 出来
```

---

## 10. 结果回收与主线程消费协议

这是 V2 最重要但旧方案最缺失的一层。

## 10.1 结果回收目标

subagent 完成后，主线程必须拿到：

1. 可用于终端展示的完成摘要
2. 可用于后续推理的结构化结论
3. 可用于调试或追溯的原始执行引用

三者不能混在一个字符串里。

## 10.2 最小结果信封

V2 仍然要求正式结果契约，但第一版不应把语义字段拆得过细。推荐最小结果结构：

```python
@dataclass
class SubagentResultEnvelope:
    task_id: str
    dispatch_id: str
    success: bool
    completion_kind: Literal["completed", "blocked", "failed", "cancelled"]
    summary: str
    raw_text: str
    artifacts: list[str]
    files_modified: list[str]
    open_questions: list[str]
    recommended_next_steps: list[str]
    failure_reason: str | None
    trace_ref: str | None
    metadata: dict[str, Any]
```

解释：

1. `summary` 用于终端展示和主线程压缩引用
2. `raw_text` 保留 subagent 的原始文本结论，避免过度结构化带来的脆弱性
3. `open_questions` / `recommended_next_steps` 保留主线程后续规划真正需要的结构
4. `metadata` 容纳 `turns_used`、`duration_ms`、token 用量等执行信息

这比“只有字符串”更可靠，也比把结果拆成过多语义字段更稳。

## 10.3 主线程合并规则

主线程收到 `SubagentResultEnvelope` 后：

1. 写回权威 `TaskState`
2. 更新任务状态与 timeline
3. 只把 `summary` 与必要结论写入用户可见主对话
4. 把详细 trace 留在 artifact / trace store
5. 允许主 Agent 基于 `raw_text / open_questions / recommended_next_steps` 继续规划下一步

## 10.4 为什么 V2 不再接受“自然语言字符串就是结果”

因为那会持续造成三个问题：

1. 主线程难以可靠消费
2. subagent 出错时主线程无法分辨 failure kind
3. 用户看到的是混杂的 prompt residue、工具语法、摘要文本

V2 明确规定：

> 自然语言文本只是 `SubagentResultEnvelope.summary / raw_text` 的表现形式，不是协议本身。

---

## 11. 工具面与控制平面重定义

用户已经明确允许调整工具面，因此 V2 不再以 `task_plan / task_execute` 为设计中心。

## 11.1 目标状态

V2 建议最终形成三类一等接口：

1. `task_sync`
   维护权威任务图
2. `agent_dispatch`
   显式启动一次 fresh / fork subagent
3. `task_merge`
   把结果合入任务状态

其中 `task_merge` 可以对模型不可见，作为 host 内部控制平面动作存在。

## 11.2 对旧接口的处理

### `task_plan`

短期可以保留，但语义应转向：

- 只是旧名兼容壳
- 内部调用新的 task control plane

### `task_execute`

短期也可以保留，但必须改造成：

- 只是 `agent_dispatch(task_id=...)` 的兼容入口
- 不再承载长期语义设计

### `SubagentRuntime`

不再把它理解成“一个被工具 handler 顺手 new 出来的执行器”，而要升级为正式 `SubagentSessionRuntime`。

---

## 12. 分阶段处理方案

以下阶段顺序是本设计的核心要求，必须按顺序推进。

## 12.1 Phase 0：Bug Fixes 与回归基线

目标：

- 先修掉当前已确认的 4 个断点，建立稳定回归基线

### Phase 0 必做项

1. 修复 subagent `SessionEngine` 未传 `tools=` 的 P0 问题
2. 修复 `task_context` 已编译但未渲染的问题
3. 修复 `_build_call_context()` 未传递 renderer 的问题
4. 修复工具事件批处理后回放的问题，让开始事件在执行前可见
5. 同步补齐对应 regression tests

### Phase 0 说明

Phase 0 是必要前置，不替代后续协议和控制平面工作。它解决的是已确认 bug，不等于自动解决了 `fresh/fork` 的正式协议、结果契约和长期控制平面问题。

## 12.2 Phase 1：派发质量

目标：

- 先彻底解决“主 Agent 给 subagent 什么”的问题
- 让输入边界可编译、可验证、可展示、可复现

### Phase 1 必做项

1. 用 `DispatchEnvelope` 取代旧版 `TaskPacket` 作为新正式协议。
2. 为 `fresh` 与 `fork` 定义不同的 envelope compile rules。
3. 引入 dispatch validation：
   - 字段缺失
   - 搜索类任务缺日期 / 地点 / 约束
   - `fork` 缺父快照或 delta
4. 在派发前向用户展示完整 dispatch preview。
5. 强制 renderer 与 prompt renderer 使用同一份 envelope 数据源，避免“展示看到的边界”和“真正发给模型的边界”不一致。
6. 第一版 validation 默认先发 warning；只有 strict policy 才做阻断。

### Phase 1 直接解决的问题

- 用户不知道输入是不是空的
- planner 填了字段但 runtime 丢字段
- `fresh` 和 `fork` 启动语义不统一

### Phase 1 暂不解决的问题

- 完整实时事件流
- 正式结果 envelope 的全量消费

## 12.3 Phase 2：执行可见性

目标：

- 让 subagent 运行不再是黑盒等待

### Phase 2 必做项

1. 引入 `SubagentEvent` 正式事件模型。
2. 第一版 transport 使用 `on_event` callback，保持同步直传。
3. 子代理工具调用开始时立即发 `tool_started`。
4. renderer 改为消费事件流，而不是直接读 tool runtime 内部状态。
5. 终端默认采用“任务卡片 + 关键事件流”混合模式。

### Phase 2 直接解决的问题

- 用户只能干等
- 复杂任务越久越像挂住
- 子代理有没有真的在做事不可见

## 12.4 Phase 3：结果回收

目标：

- 让 subagent 成为主线程真正可依赖的执行单元，而不是摘要黑箱

### Phase 3 必做项

1. 引入最小 `SubagentResultEnvelope` 正式结果契约。
2. 主线程按 envelope 结构化消费，不再依赖自然语言字符串解析。
3. 大型原始材料默认落 trace/artifact，不污染主对话。
4. 对失败、阻塞、取消状态给出正式 completion kind。
5. 支持主线程基于 `open_questions / recommended_next_steps` 继续规划。
6. 第一版保留 `raw_text`，不强制把结果拆成过多语义字段。

### Phase 3 直接解决的问题

- 结果不稳定
- 主线程消费困难
- 搜索类任务回收时仍污染上下文

---

## 13. End-to-End Roadmap

这里给出替换式主路径的全量路线图。

## 13.1 Stage A：协议先行

- 定义 `TaskRecordV2`
- 定义 `DispatchRequest`
- 定义 `DispatchEnvelope`
- 定义 `SubagentEvent`
- 定义 `SubagentResultEnvelope`

交付标准：

- 类型、序列化、验证规则全部稳定

## 13.2 Stage B：派发编译器接管

- 引入 `DispatchCompiler`
- 旧 `compile_task_packet()` 退为兼容层
- `fresh` / `fork` 都能编译出可展示、可执行 envelope

交付标准：

- 用户可见的 dispatch preview 与真实模型输入一致

## 13.3 Stage C：Subagent Session 与事件流接管

- 引入正式 `SubagentSessionRuntime`
- 事件流成为 renderer 的唯一来源
- 去掉“批处理后回放工具调用”的旧可见性路径

交付标准：

- 长时间运行任务有持续可见性

## 13.4 Stage D：结果 envelope 接管主线程消费

- 主线程只接受 `SubagentResultEnvelope`
- 旧字符串结果路径退为兼容层

交付标准：

- 搜索类任务的主上下文污染明显下降
- 主线程可基于结构化结果继续推理

## 13.5 Stage E：旧路径退场

- `task_execute` 退化为薄兼容壳或删除
- `TaskPacket` 删除或仅留 alias
- 旧 renderer hack 删除
- 旧结果字符串路径删除

交付标准：

- V2 成为默认唯一主路径

---

## 14. 测试与验证策略

V2 必须采用“协议测试 + 事件测试 + 端到端体验测试”的组合验证。

## 14.1 协议测试

至少覆盖：

1. `fresh` envelope 编译
2. `fork` envelope 编译
3. 搜索任务缺关键字段时 validation warning / strict fail
4. display preview 与 model input 使用同一份 envelope
5. `compile_task_packet` 或其兼容层的字段映射、默认值回退、revision 递增

## 14.2 事件测试

至少覆盖：

1. `tool_started` 必须在真实工具完成前可见
2. 长任务必须产生可见状态变化
3. renderer 只消费事件，不旁路读取 runtime 内部状态
4. 第一版 callback transport 能稳定把事件传到 renderer

## 14.3 结果测试

至少覆盖：

1. subagent 成功返回 envelope
2. blocked / failed / cancelled 路径
3. 大型 trace 不污染主对话
4. 主线程能消费 `raw_text / open_questions / recommended_next_steps`

## 14.4 Regression Tests（Phase 0 必需）

至少补齐以下回归测试：

1. `SubagentRuntime.run()` 创建的 `SessionEngine` 收到 `tools` 参数，且下游模型调用不会再出现 `tools=None`
2. `_render_fresh_packet()` 的输出包含 `task_context`
3. `_build_call_context()` 传递 renderer
4. 工具开始事件在真实工具完成前可见，不再批处理后回放
5. `task_execute.handle()` 的错误路径：unknown task、subagent failure

## 14.5 端到端场景测试

V2 至少应保留一个高噪声真实场景作为回归基线，例如：

- 天气 + 行程 + 交通 + 景点的 3 天旅行规划

验证点：

1. dispatch preview 明确显示日期、地点、约束
2. 运行中能看到天气查询、搜索、整理等关键阶段
3. 主线程最终拿到结构化结果，不被细碎搜索输出污染

---

## 15. 风险与防御

## 15.1 风险：重构范围过大

防御：

- 先定正式协议
- 再替换编译器
- 再替换事件流
- 最后替换结果消费

## 15.2 风险：fork 复杂度高

防御：

- 把 `fork` 也纳入正式协议，但先限制第一版适用范围
- 明确父快照引用和 delta 规则
- 不允许“全量父上下文无界继承”

## 15.3 风险：终端事件流过于嘈杂

防御：

- 默认只显示关键事件
- 原始 trace 持久化到 artifact
- renderer 提供 compact / verbose 层级

## 15.4 风险：兼容层长期不退场

防御：

- 本设计明确把旧路径定义为迁移壳
- 路线图中单独设置 Stage E 清退阶段

---

## 16. 结论

对于 harness 而言，subagent 不是“再加一个更会联网搜索的工具”，而是解决主上下文污染、任务边界模糊、长任务黑盒等待的主机制。

`2026-05-10` 版本解决了第一层问题：必须引入 `TaskState`、`TaskPacket` 和 `fresh/fork` 语义。

本 V2 设计解决的是第二层问题：

1. 子代理到底看到了什么
2. 用户在等待期间到底看到了什么
3. 主线程到底拿回了什么

如果这三层没有一起变成正式协议，那么 `subagent` 仍然只会是“一个看起来先进、实际比主线程更难调试的黑盒”。

V2 的判断非常明确：

> 要让联网搜索和高噪声探索任务真正成为 subagent 的最优解，就必须把 `Task`、`DispatchEnvelope`、`SubagentEvent` 和 `SubagentResultEnvelope` 一起升级为主路径能力，而不是继续在旧 runtime 上叠体验补丁。


