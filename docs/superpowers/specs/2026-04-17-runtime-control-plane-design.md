# Runtime Control Plane 设计

> 日期: 2026-04-17
> 状态: 待评审
> 相关文档:
> - [`docs/superpowers/specs/2026-04-15-session-query-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md)
> - [`docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md)
> - [`docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)
> - [`docs/superpowers/specs/2026-04-17-todo-task-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-todo-task-parity-design.md)

## 摘要

`harness` 当前已经有 `Session / Query / ToolRuntime` 三层分工，但它的 `Runtime` 仍主要承担“工具执行器”的角色，还不是一个真正的“运行时控制平面”。

这份文档定义的平台级目标是：

- 工具返回值不再只是字符串结果
- Runtime 不再只负责执行工具
- 工具执行完成后，Runtime 能基于结构化结果重排下一步运行环境

也就是说，工具结果需要同时服务两类消费者：

1. **LLM**
   - 让模型看到发生了什么
   - 让模型在下一轮获得额外上下文或提醒

2. **Runtime**
   - 决定是否打断当前批次
   - 决定下一轮工具集是否收窄
   - 决定是否设置 replan / verify 等 run-scoped flags
   - 决定哪些结果进入长期状态，哪些只留在本次 run

这套能力不是 skill/todo 的局部技巧，而是后续所有专业 agent 的通用平台能力，包括但不限于：

- 智能问数 agent
- 财务稽核 agent
- 合同校验 agent
- 报告分析 agent
- 代码 agent

## 为什么需要这份文档

2026-04-15 的 [`Session / Query / Runtime 重构设计`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md) 解决的是**分层问题**：

- 会话层和单次 query 层拆开
- `QueryLoop.run()` 成为唯一主循环
- `ToolRuntime.execute_batch()` 成为统一工具执行入口

但它没有完全展开另一个更深的问题：

> 工具执行完成之后，系统应该如何“消费”这个结果？

如果消费方式始终只是：

```text
tool -> string output -> append history -> LLM 自己领会下一步
```

那么 Runtime 依然只是执行层，不是控制层。

因此，这份文档是在 4 月 15 日的三层架构之上，继续补出第四个关键问题：

> Runtime 除了执行工具，还应不应该解释工具结果，并重排后续运行条件？

本设计的答案是：**应该，而且这是后续高准确率 agent 的平台基础。**

## 问题定义

当前 `harness` 的工具返回协议偏弱，核心问题不是“返回值是不是 JSON”，而是：

> 工具结果现在主要只是“给 LLM 看的结果文本”，还不是“给 Runtime 看的控制协议”。

当前系统的基本形态更接近：

```text
LLM
  -> call tool
    -> tool returns string
      -> append to history
        -> LLM 自己决定下一步
```

这种模型有三个天然限制：

1. 工具结果只能“建议”模型下一步怎么做，不能显式表达运行时语义。
2. 当前 batch 是否应该中断，无法由工具返回值明确控制。
3. 下一轮运行环境的变化，只能通过 history 间接影响模型，不能由 Runtime 直接应用。

这会直接导致一类已经在 skill/todo 场景中暴露出来的问题：

- skill 虽然被“激活”了，但没有立刻重排当前 run 的行为
- todo 虽然“写成功”了，但无法持续约束后续执行
- reminder 虽然存在，但缺少当前运行状态上下文，因此约束力有限

## 核心结论

`harness` 的目标 Runtime 不应只是：

```text
execute tools
```

而应升级为：

```text
execute tools
+ interpret tool protocol
+ shape next-step runtime environment
```

也就是说，工具结果必须从“结果字符串”升级为“运行时协议对象”。

## 与现有三层架构的关系

本设计不推翻 `Session / Query / Runtime` 三层，只是进一步明确三层边界。

### 关系图

下面这张图描述的是 `harness` 当前更接近的工作形态：

```text
┌─────────────────────────────────────────────────────────────┐
│                          Session                            │
│  - conversation history                                     │
│  - discovered skills                                        │
│  - 其他长期状态                                              │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ 用户问题
                ▼
┌─────────────────────────────────────────────────────────────┐
│                           Query                             │
│  1. 从 Session 取 history                                   │
│  2. 调 LLM                                                  │
│  3. LLM 返回 tool calls / final text                        │
│  4. 执行 tools                                              │
│  5. 把 tool result append 到 history                        │
│  6. 继续下一轮 / 结束                                        │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ tool calls
                ▼
┌─────────────────────────────────────────────────────────────┐
│                    Tool Executor / Runtime                  │
│  - 串行/并行执行工具                                         │
│  - 收集 ToolResult                                           │
│  - ToolResult 主要是 string output                          │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ result.output
                ▼
┌─────────────────────────────────────────────────────────────┐
│                     append back to history                  │
│  "skill activated..."                                       │
│  "todo updated..."                                          │
│  "file read success..."                                     │
└─────────────────────────────────────────────────────────────┘
```

它的核心特征可以压缩成一句话：

```text
tool -> string result -> append history -> LLM 自己领会下一步
```

而本设计希望把三层关系推进到更强的 Runtime 形态：

```text
┌─────────────────────────────────────────────────────────────┐
│                         Session/AppState                    │
│  - transcript / messages                                    │
│  - invoked skills                                           │
│  - todos / tasks                                            │
│  - 其他长期状态                                              │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ 用户问题
                ▼
┌─────────────────────────────────────────────────────────────┐
│                           Query Run                         │
│  1. 构建当前模型调用视图                                      │
│  2. 调 LLM                                                  │
│  3. 收到 tool calls / final text                            │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│                  Runtime Control Plane                      │
│  - 执行 tools                                                │
│  - 消费结构化 tool result                                    │
│  - 决定是否插入 newMessages                                  │
│  - 决定是否修改 allowed tools                                │
│  - 决定是否 barrier / stop current batch                     │
│  - 决定是否设置 run-scoped flags                             │
│  - 决定哪些状态写入 Session/AppState                         │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ structured result protocol
                ▼
┌─────────────────────────────────────────────────────────────┐
│                         Tool Result                         │
│  1. 给 LLM 看:                                               │
│     - tool_result_text                                       │
│     - newMessages                                            │
│     - reminders / nudges                                     │
│                                                             │
│  2. 给 Runtime 用:                                           │
│     - contextModifier                                        │
│     - allowedTools override                                  │
│     - barrier                                                │
│     - model override                                         │
│     - app state updates                                      │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│                    下一轮模型调用环境                        │
│  - history 已被重排/补充                                      │
│  - tools 可能被临时收窄                                       │
│  - 当前批次可能被强制打断                                     │
│  - skill/todo 状态已进入运行时                                │
└─────────────────────────────────────────────────────────────┘
```

这张图强调的是：`Runtime` 不是要替代 `Session` 或 `Query`，而是要在单次 run 内承担“控制解释层”的责任。

也就是说，`Session` 管长期状态，`Query` 管一次用户问题的主循环，而 `Runtime` 管这次 run 内部每一批工具执行完成之后，系统到底该如何解释结果、修改运行条件，并组织下一轮模型可见上下文。

### `Session`

负责长期状态：

- conversation history
- discovered skills / tools
- invoked skills
- todo snapshots / task state
- usage totals
- compact / resume 相关状态

### `Query`

负责单次用户问题：

- 主循环推进
- 模型调用
- 工具调用
- 停止条件
- 结构化 query result

### `Runtime`

负责当前 query run 内的控制平面：

- 执行工具
- 消费结构化 tool result
- 维护 run-scoped state
- 决定 tool result 怎样进入模型上下文
- 决定是否设置 barrier
- 决定是否临时修改 allowed tools / model config

## 最关键的纠偏

因此，更准确的表述不是：

> `harness` 没有 Runtime

而是：

> `harness` 已经有 Runtime，但当前 Runtime 仍主要是工具执行器，而不是运行时控制平面。

## 现状与目标

### 最关键的控制流对比

下面这张图更直接说明为什么 `Runtime` 必须从“工具执行器”升级为“运行时控制平面”：

```text
你现在
======

LLM
  -> call tool
    -> tool returns string
      -> append to history
        -> LLM reads text
          -> LLM decides next step


Claude Code 风格
================

LLM
  -> call tool
    -> tool returns protocol object
      -> Runtime reads protocol
         -> inject messages?
         -> change allowed tools?
         -> set barrier?
         -> update app state?
      -> build next-step context
        -> LLM continues inside new runtime state
```

这里最重要的变化不是“tool 返回 JSON”，而是 `ToolResult` 不再直接等于 history 追加内容。它先成为 Runtime 要解释的协议对象，之后 Runtime 再决定：

- 哪些内容需要让 LLM 看见
- 哪些内容只应进入运行时状态
- 哪些内容需要写回长期会话状态
- 是否应该立即改变下一轮规划空间

这也是 skill / todo 问题背后的共同根因：如果工具结果只能变成一段字符串 history，那么系统对后续流程的控制权仍然主要掌握在模型“自己领会”这一层，而不在平台运行时。

### Skill -> Todo 差异图

如果专门看当前最痛的 `skill -> todo` 场景，差异会更明显：

```text
你现在
======

LLM
  -> activate_skill
    -> "Skill activated"
      -> append history
        -> LLM 也许去建 todo
           -> todo 可能还是 1~2 个泛化项


目标架构
========

LLM
  -> skill
    -> Runtime 拿到 structured result
       -> 注入 skill body
       -> 设置 barrier
       -> 标记 todo_replan_required
       -> 可选收窄下一轮 tools
    -> 下一轮 LLM
       -> 在 skill 已生效的上下文里建 todo
       -> todo 更可能跟随 1 / 2 / 2.5 / 3
```

这也是为什么本文把 skill 和 todo 的问题归到同一个 Runtime Control Plane 问题，而不是把它们看成两个互不相关的提示词问题。

### 当前缺的那一层

再换一种方式描述，当前三层和目标三层的差异并不是“有没有 Runtime”，而是 Runtime 的强弱不同：

```text
当前三层
========

[Session]  长期状态
    |
[Query]    单次问题循环
    |
[Runtime]  工具执行器（薄）


目标三层
========

[Session]  长期状态
    |
[Query]    单次问题循环
    |
[Runtime]  运行时控制平面（强）
           - tool protocol interpreter
           - barrier handling
           - context injection
           - run-scoped state
           - next-step environment shaping
```

## 设计原则

### 1. Tool result 既服务 LLM，也服务 Runtime

工具结果不能只是一段自然语言文本。
它必须允许 Runtime 读取结构化字段，并据此修改下一步行为。

### 2. History 不是唯一控制载体

并不是所有重要信息都应该通过追加到 history 来表达。
有些信息只应存在于 Runtime 或 Session 的结构化状态中。

### 3. Runtime 先解释协议，再决定 history 与下一轮环境

控制的关键不在“最终 history 长什么样”，而在：

- Runtime 在 history 形成之前，是否已经解释并应用了工具结果中的控制语义

### 4. 长期状态与短期状态必须分离

并不是所有控制信息都应该回写到 `SessionState`。

例如：

- `todo_replan_required`
- `verify_before_close`
- `barrier_reason`
- `allowed_tools_override`

这些都更适合作为 run-scoped state，而不是长期会话历史。

### 5. 平台能力要优先于单场景技巧

skill、todo、verification、domain agents 都应复用同一套 Runtime 控制协议，而不是每个工具各造一套特殊逻辑。

## Tool Result 的三类信息

本设计建议把 tool result 中的信息划分成三类。

## 第一类：只给 LLM 看

这类内容的职责是改变模型的下一轮理解，不直接改变 Runtime 行为。

典型字段：

- `tool_result_text`
- `injected_messages`
- `reminders`
- `nudges`

示例：

- `TodoWrite` 的“继续使用 todo 跟踪进度”
- stale plan reminder
- skill 的补充说明文本

## 第二类：只给 Runtime 用

这类内容不一定直接出现在普通 history 中，但会改变系统运行条件。

典型字段：

- `barrier`
- `allowed_tools`
- `model_override`
- `effort_override`
- `state_updates`
- `run_flags`

示例：

- `skill_expanded` 之后打断当前 batch
- 暂时只允许 `find/read_file/todo`
- 设置 `todo_replan_required = True`

## 第三类：两边都要看到

这类内容既要让模型感知，也要让系统持久化或结构化记录。

典型字段：

- todo plan snapshot
- invoked skill summary
- verification requirement summary
- domain-specific evidence coverage summary

示例：

- 当前 todo 列表要既能显示给模型，也能写入 `SessionState.todo_state`
- invoked skill 要既能进入模型上下文，也能进入 `SessionState.invoked_skills`

## 推荐协议形状

本设计不要求一步到位复制 Claude Code 的具体接口，但建议在 `harness` 中逐步收敛到下面这种语义形状：

```python
@dataclass
class ToolResult:
    output: str
    success: bool
    error: str | None = None
    truncated: bool = False

    injected_messages: list[dict[str, Any]] = field(default_factory=list)
    context_patch: ContextPatch | None = None
    barrier: ExecutionBarrier | None = None
    state_updates: dict[str, Any] = field(default_factory=dict)
    run_flags: dict[str, Any] = field(default_factory=dict)
```

说明：

- `output` 仍保留，用于兼容现有工具与普通 tool result 文本
- `injected_messages` 负责进入模型可见上下文
- `context_patch` 负责临时修改下一轮运行环境
- `barrier` 负责改变当前 batch 控制流
- `state_updates` 负责结构化写回 session state
- `run_flags` 负责本次 query run 的瞬时控制信息

这里的关键点不是字段名字，而是语义分层。

补充：

- `ExecutionBarrier`、`ContextPatch`、`run_flags` 都是 `harness` 的运行时协议抽象
- 它们可以借鉴 Claude Code 的行为证据，但不应被表述为 Claude Code 源码里已经存在的同名类型

## Runtime 允许执行的通用动作

Runtime 在消费 tool result 后，应该有一组通用动作，而不是每个工具单独写分支。

建议最小动作集合如下：

### 1. `append_tool_result`

把普通工具结果作为标准 `tool` 消息追加给模型。

### 1.5. `append_skipped_tool_result`

当某个 `tool_call` 因为 barrier 被跳过时，Runtime 仍应生成显式 `tool` 结果消息。

原因：

- 当前 `harness` 的 [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 会在缺失 `tool_result` 时自动补 `(cancelled)` 占位
- 但那只是协议修补，不足以表达“该调用是因为新的运行时边界而被废弃”

因此，控制平面应显式输出类似：

```text
(skipped: superseded by barrier; re-issue after re-evaluation if still needed)
```

让模型知道：

- 这个调用不是完成了
- 也不是普通失败
- 而是被控制平面中止，需要在新上下文里重新决定

### 2. `inject_messages`

把结构化 `injected_messages` 放入 conversation history 或 model-visible message view。

### 3. `set_barrier`

设置当前 batch 的中断边界，例如：

- `skill_expanded`
- `verify_before_close`
- `context_shift`

### 4. `patch_context`

临时修改下一轮运行上下文，例如：

- 收窄 allowed tools
- 覆盖 model
- 覆盖 effort

### 5. `update_run_flags`

更新仅对本次 run 生效的标志，例如：

- `todo_replan_required`
- `verification_required`
- `evidence_gap_detected`

### 6. `update_session_state`

把需要跨轮持久化的信息写入 `SessionState`，例如：

- invoked skill records
- todo state
- evidence snapshots

## Runtime Control Plane 的标准流程

工具执行完成后，推荐的标准流程不是“立刻 append output”，而是：

```text
1. tool 执行完成
2. runtime 收到 structured result
3. runtime 先解释控制字段
   - barrier?
   - context patch?
   - run flags?
   - session updates?
4. runtime 再决定模型可见部分
   - tool result text
   - injected messages
   - reminders
5. 构建下一轮 model view
6. 进入下一轮 LLM
```

这一步顺序非常重要。

如果把第 4 步提前到第 3 步之前，就又会退回“只靠 history 驱动”的旧模型。

## 四种通用控制模式

这套 control plane 不只服务 skill/todo，而是应该抽象成可复用模式。

### 模式 1: Expand-Then-Replan

流程：

```text
tool 扩展新上下文
-> runtime 注入新消息
-> runtime 设置 barrier
-> runtime 设置 replan flag
-> 下一轮模型在新上下文中重新规划
```

适用场景：

- skill expansion
- 切换工作流模板
- 引入大型 reference context

### 模式 2: Observe-Then-Constrain

流程：

```text
tool 先进行少量观察
-> runtime 根据观察结果收窄下一轮工具集
-> 模型继续在受限环境中推进
```

适用场景：

- 智能问数中的 schema 探查后再开放 query
- 合同校验中先定位条款后再开放 clause compare
- 财务稽核中先识别报表结构后再开放异常判定

### 模式 3: Verify-Before-Close

流程：

```text
任务接近完成
-> runtime 检测 verification flag
-> 如果未验证，则设置 barrier
-> 下一轮模型先进行验证，再允许结束
```

适用场景：

- 代码 agent
- 财务稽核
- 合同风险输出
- 报告生成

### 模式 4: Stale-State Reminder

流程：

```text
已有结构化计划/证据链
-> 多轮未更新
-> runtime 注入带当前状态快照的 reminder
-> 模型据此刷新计划或补全证据
```

适用场景：

- todo stale reminder
- 证据覆盖 stale reminder
- 问数口径确认 reminder

## Skill / Todo 场景中的具体落地

## Skill

`skill` 工具应成为第一类真正使用 Runtime control plane 的工具。

它不应只返回：

```text
Skill activated
```

而应允许表达：

- 注入 skill body
- 设置 `skill_expanded` barrier
- 可选收窄下一轮工具集
- 写入 invoked skill records

## Todo

`todo` 工具不应只返回：

```text
计划已更新
```

而应允许表达：

- 更新 `SessionState.todo_state`
- 返回继续跟踪计划的 nudge
- 在必要时设置 verification-related flag
- 让 stale reminder 有结构化 plan snapshot 可用

## 对其他专业 Agent 的帮助

这部分是本设计最值得保留的平台价值。

## 智能问数 Agent

当前常见风险：

- 指标口径没闭合就直接出答案
- schema 还没理解清楚就开始写 SQL
- 已经发现字段冲突但没有强制 replan

Runtime control plane 可提供：

- `metric_definition_required`
- `schema_scope_locked`
- `allowed_tools = ["schema_inspect", "query", "todo"]`
- `evidence_gap_detected`
- query 结果摘要同时进入 LLM 与 session state

预期收益：

- 降低口径误解率
- 降低“看似回答正确、实则口径错误”的风险

## 财务稽核 Agent

当前常见风险：

- 找到几个异常后就直接给结论
- 证据链未闭合
- verification 没做完就进入 final summary

Runtime control plane 可提供：

- `verification_required`
- `evidence_chain_snapshot`
- `barrier = "verify_before_close"`
- 对下一轮 allowed tools 收窄为核验相关工具

预期收益：

- 降低“异常发现不充分”与“证据不足即下结论”的错误

## 合同校验 Agent

当前常见风险：

- 只抓到显眼条款，遗漏 coverage
- 高风险条款没有二次核对
- 输出结论时缺少条款级证据定位

Runtime control plane 可提供：

- `clause_coverage_map`
- `high_risk_clause_pending_review`
- `barrier = "context_shift"` 或 `verify_before_close`
- clause-level reminders

预期收益：

- 提高覆盖率
- 提高风险结论与证据引用的一致性

## 为什么这会提升准确率

这套能力提升的不是单一 prompt 命中率，而是三类准确率：

### 1. 流程准确率

模型更不容易：

- 漏步骤
- 抢步骤
- 在错误时机进入下一阶段

### 2. 证据准确率

模型更容易在证据不足时停下来，而不是直接生成看似合理的结论。

### 3. 结束准确率

模型更不容易在没有完成验证、覆盖或核对时提前宣称完成。

对于高要求 agent，这三类准确率往往比“单轮回答好不好看”更重要。

## 与现有 4 月 15 日重构设计的关系

这份文档不是替代 2026-04-15 的三层架构文档，而是补充它。

可以把两者关系理解为：

- 2026-04-15 文档解决 **“层怎么拆”**
- 本文解决 **“Runtime 拆出来之后，到底要不要承担控制平面职责”**

也就是说：

- 4 月 15 日定义了 `ToolRuntime.execute_batch(...)`
- 今天这份文档定义的是：`ToolRuntime.execute_batch(...)` 返回之后，Runtime 应如何消费结构化结果并重排后续运行环境

## 对现有代码的直接启发

结合当前仓库，平台级落点主要会在以下位置：

- [`core/tools/context.py`](/Users/kino/works/kino/harness/core/tools/context.py)
  - 扩展 `ToolResult`

- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
  - 从“执行器”升级为“执行器 + 协议解释器”

- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
  - 消费 barrier / run flags / injected messages

- [`core/query/state.py`](/Users/kino/works/kino/harness/core/query/state.py)
  - 承载 run-scoped control flags

- [`core/session/state.py`](/Users/kino/works/kino/harness/core/session/state.py)
  - 承载需要跨轮持久化的结构化状态

## 非目标

本设计明确不要求：

- 一次性复制 Claude Code 的全部 tool protocol 细节
- 一次性引入完整 task graph
- 一次性支持 compact / swarm / hooks / remote session
- 把所有工具都改成复杂协议工具

正确做法应是：

- 先让 `skill`、`todo`、verification 这类高价值工具吃到协议升级
- 再逐步推广到其他 agent domain

## 成功标准

如果以下条件成立，则说明 Runtime control plane 设计成功：

1. `harness` 内部不再把 tool result 仅仅视为字符串输出。
2. Runtime 能区分“给 LLM 看”和“给 Runtime 用”的结果信息。
3. Runtime 能基于结构化结果设置 barrier、patch context、更新 run flags。
4. Session 与 Query 之间的边界依旧清晰，没有因为 control plane 回退成巨型总控模块。
5. `skill` 与 `todo` 可以作为第一批采用者，验证这套平台能力。
6. 这套能力能自然迁移到智能问数、财务稽核、合同校验等其他专业 agent。
