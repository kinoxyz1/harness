# Runtime Update Protocol 设计

> 日期：2026-04-22
> 状态：待评审
> 替代：
> - `docs/superpowers/specs/2026-04-21-query-control-plane-design.md`
> - `docs/superpowers/specs/2026-04-21-query-control-plane-design-review.md`
>
> 相关文档：
> - [`docs/superpowers/specs/2026-04-15-session-query-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md)
> - [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)
> - [`docs/superpowers/specs/2026-04-19-state-assembled-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-19-state-assembled-runtime-design.md)
> - [`docs/query-control-plane-cc-reference.md`](/Users/kino/works/kino/harness/docs/query-control-plane-cc-reference.md)

---

## 1. 摘要

`harness` 当前已经有 `Session / Query / ToolRuntime` 分层，也已经开始把模型输入从 transcript 直接复制，推进到“显式状态 + 视图组装”的方向。

但在最关键的一层，系统仍然存在一个结构性断裂：

> 工具协议、query control plane、runtime truth、模型输入组装，还没有共享同一种运行时语言。

当前系统的问题不是某个 `if batch.barrier is not None` 写得是否优雅，而是：

- 工具返回协议仍然以 `output` 为中心
- `skill` 这类工具还依赖 `barrier` 这种特殊控制流
- query loop 仍然理解工具私有语义
- 一部分“下一轮为什么继续”与“下一轮模型应该看到什么”的逻辑，被拆散在 tool/runtime/policy/overlay 多处

这会直接阻碍后续能力的稳定扩展，包括但不限于：

- compact
- memory
- permission system
- hook
- task
- error recovery
- agent teams
- richer MCP integration

本设计的目标是一次性把这层运行时协议拉正，但**不实现 compact / memory / hook / teams 本身**。

本设计采用的路线是：

> 采用高质量的“方案 B”：
> transcript 保留，runtime truth 显式化；
> 工具统一返回“消息更新 + 状态更新 + 终止/错误信号”；
> `QueryLoop` 只消费统一协议和 query transition；
> 不引入 event sourcing，但把所有状态变更收敛到 reducer-like 入口。

---

## 2. 这次要解决什么

### 2.1 要解决的问题

1. 工具返回协议不统一
2. query loop 理解了工具私有控制语义
3. 运行时事实和 transcript 仍有混用
4. `transition` 缺少明确而可扩展的边界
5. 为未来 `compact / memory / recovery / teams` 铺路时，仍需要再次撕开架构

### 2.2 明确非目标

本设计**不包含**：

- 实现 compact
- 实现 memory
- 实现新的 hook system
- 实现新的 permission system
- 实现 teams / swarm
- 实现 durable workflow engine
- 引入 event sourcing
- 引入跨进程 replay / time-travel debugging

本设计只做一件事：

> 把 agent runtime 的核心协议改成未来可承载这些能力的稳定底座。

---

## 3. 设计结论

### 3.1 transcript 保留，但降级

`conversation_messages` 不删除，也不彻底退出模型输入。

它的定位改为：

- append-only transcript
- 审计和调试记录
- 模型输入中的一条通道（transcript slice）

它**不再承载 runtime truth**。

### 3.2 runtime truth 全部显式化

必须长期保真的运行时事实，只存在于显式状态中，由每轮组装进入模型输入，例如：

- invoked skills
- todo / task state
- read file state
- capability / tool availability overrides
- model / effort overrides
- 未来 memory / compact restore 所需状态

这些事实不能再依赖“某条旧消息还留在 transcript 里”来生效。

### 3.3 工具协议统一为三类输出

所有工具统一返回：

1. **消息更新**
   - 下一轮模型应该看到的工具结果消息、meta 提示、辅助消息

2. **状态更新**
   - 对 `SessionState` 或 `RunState` 的显式更新

3. **终止/错误信号**
   - 成功、失败、取消、不可继续等结果语义

不再保留：

- `ExecutionBarrier`
- `ContextPatch`
- `ToolResult.output + QueryLoop 二次翻译` 这种旧模型

### 3.4 `QueryLoop` 只理解 query control plane

`QueryLoop` 的职责收敛为：

- 组装当前视图
- 调模型
- 执行工具批次
- 消费统一工具更新协议
- 应用状态 reducer
- 设置 `transition`
- 处理停止和恢复

`QueryLoop` 不再理解：

- `skill_expanded`
- “某个工具需要 barrier”
- “某个工具触发 todo replan flag”

这些都必须被工具消息和显式状态更新协议吸收掉。

### 3.5 状态变更收敛到少数入口

本次不做 event sourcing，但要强制采用 reducer-like 的少数入口：

- `apply_session_update(...)`
- `apply_run_update(...)`
- `apply_transition(...)`

任何运行时状态变更，都必须经过这些入口，而不是散落的直接赋值。

这一步是未来从方案 B 升级到方案 C 的关键铺垫。

---

## 4. 新的运行时分层

### 4.1 `SessionState`

`SessionState` 保存跨 query 持续存在的 runtime truth。

本设计下，它继续包含并扩展：

- `conversation_messages`
- `invoked_skills`
- `todo_state`
- `read_file_state`
- `session_metadata`
- 未来 memory / compact restore 所需状态

约束：

- 可以作为 runtime context 的真相源
- 不依赖 transcript 才能恢复
- 只通过显式更新入口修改

### 4.2 `RunState`

`RunState` 保存一次 query run 的控制状态。

它保留并收敛为：

- `turn_count`
- `stop_reason`
- `last_model_response`
- `tool_calls_executed`
- `files_modified`
- `usage_delta`
- `empty_retry_count`
- `transition`
- query-scoped overrides
  - `allowed_tools_override`
  - `model_override`
  - `effort_override`
- UI / display state

它不再保留旧的工具私有桥接字段：

- `barrier_reason`
- `todo_replan_required`
- `todo_replan_reason`

这些字段的存在，本质上说明 query control plane 正在替某个具体工具擦屁股。

### 4.3 `ToolUseContext`

`ToolUseContext` 继续存在，但角色要变化：

- 读取当前运行时环境
- 暴露 working dir / tool identity / session references
- 为工具提供只读观察入口

`ToolUseContext` 不应再承担“通过共享可变对象直接写回 runtime truth”的职责。

也就是说：

- 工具可以读 `session_state` / `read_file_state`
- 工具可以执行真实副作用（读写文件、跑命令）
- 但工具对 runtime truth 的写入，必须通过返回的状态更新完成

### 4.4 `ModelInputView`

模型输入每轮都由视图组装器重建，至少保留四层：

1. `stable context`
2. `runtime context`
3. `query overlay`
4. `transcript slice`

其中：

- `stable context` 是长期稳定说明
- `runtime context` 是显式状态的当前渲染
- `query overlay` 是单轮控制提示
- `transcript slice` 是最近的对话轨迹

这与 Claude Code 的总体原则一致：**当前视图每轮重建，而不是直接拿原始历史整包回放。**

---

## 5. 新的工具协议

### 5.1 旧协议的问题

当前 `ToolResult` 近似是：

```python
class ToolResult:
    output: str
    success: bool
    injected_messages: list[dict]
    context_patch: ContextPatch | None
    barrier: ExecutionBarrier | None
```

这个结构有三个问题：

1. `output` 和 `injected_messages` 是两套并存的消息通道
2. `context_patch` 只能表达很窄的一类覆盖
3. `barrier` 把工具私有语义硬塞进主循环

### 5.2 新协议目标

新协议要求每个工具只返回一种统一结果对象，这个对象要能表达：

- 要追加给模型的消息
- 要应用的状态更新
- 这次工具调用的完成语义

建议形态：

```python
@dataclass(slots=True)
class ToolInvocationOutcome:
    messages: list[dict[str, Any]] = field(default_factory=list)
    session_updates: list["SessionUpdate"] = field(default_factory=list)
    run_updates: list["RunUpdate"] = field(default_factory=list)
    status: "ToolOutcomeStatus" = ToolOutcomeStatus.SUCCESS
    error: str | None = None
```

其中：

- `messages` 统一承载所有模型可见输出
- `session_updates` 表达长期状态变化
- `run_updates` 表达 query-scoped 状态变化
- `status/error` 表达终止或失败语义

### 5.3 初始更新类型必须在 spec 中落地

本设计的核心不是 `ToolInvocationOutcome` 这个壳，而是它承载的更新指令集。

第一阶段至少定义以下结构化更新变体。

#### `SessionUpdate`

```python
class SessionUpdateKind(str, Enum):
    INVOKE_SKILL = "invoke_skill"
    SET_TODO_ITEMS = "set_todo_items"
    UPSERT_FILE_STATE = "upsert_file_state"
    INVALIDATE_FILE_STATE = "invalidate_file_state"
    APPEND_SKILL_EVENT = "append_skill_event"


@dataclass(slots=True)
class SessionUpdate:
    kind: SessionUpdateKind
    payload: dict[str, Any]
```

第一阶段预期 payload 形状：

- `INVOKE_SKILL`
  - `skill_id`
  - `skill_path`
  - `content_digest`
  - `content`
  - `invoked_at_turn`
- `SET_TODO_ITEMS`
  - `items`
  - `last_write_turn`
- `UPSERT_FILE_STATE`
  - `path`
  - `file_state`
- `INVALIDATE_FILE_STATE`
  - `path`
- `APPEND_SKILL_EVENT`
  - `event`

#### `RunUpdate`

```python
class RunUpdateKind(str, Enum):
    MARK_FILE_MODIFIED = "mark_file_modified"
    NARROW_ALLOWED_TOOLS = "narrow_allowed_tools"
    SET_MODEL_OVERRIDE = "set_model_override"
    SET_EFFORT_OVERRIDE = "set_effort_override"


@dataclass(slots=True)
class RunUpdate:
    kind: RunUpdateKind
    payload: dict[str, Any]
```

第一阶段预期 payload 形状：

- `MARK_FILE_MODIFIED`
  - `path`
- `NARROW_ALLOWED_TOOLS`
  - `allowed_tools`
- `SET_MODEL_OVERRIDE`
  - `model`
- `SET_EFFORT_OVERRIDE`
  - `effort`

说明：

- 不是所有 `RunState` 字段都来自 tool update。
- `turn_count`、`tool_calls_executed`、`empty_retry_count`、`transition` 仍由 `QueryLoop` 自己维护。
- `session_updates` / `run_updates` 只覆盖工具有资格直接影响的状态。

### 5.4 更新必须是结构化的

`session_updates` 和 `run_updates` 不能是任意 Python 回调。

本设计明确不采用“闭包式 modifier”作为最终本地协议，而采用**结构化 update 类型**，原因是：

1. 更容易测试
2. 更容易调试
3. 更容易序列化
4. 更容易在未来升级到 event log / replay

也就是说，方向上借鉴 Claude Code 的 `newMessages + contextModifier`，但在本地实现上选择更适合未来扩展的结构化 update。

### 5.5 统一消息通道

旧的：

- `output`
- `injected_messages`

统一成新的：

- `messages`

规则：

- 工具的普通可见结果，变成 `messages`
- 工具的 meta 提示，变成 `messages`
- “skill 已加载，请重新评估下一步” 这种提示，也变成 `messages`

这样 `QueryLoop` 不再做“某个字段要翻译成 message，某个字段不用”的二次解释。

### 5.6 明确移除 `barrier`

`ExecutionBarrier` 完全删除。

直接影响：

- `skill` 不再中断当前工具批次
- runtime 不再生成 “skipped: superseded by barrier” 结果
- query loop 不再存在 barrier 分支
- `barrier_continuation` 不再是 transition reason

这是本次重构最重要的行为改变之一。

### 5.7 为什么我们改变了主意

2026-04-17 的 runtime-control-plane 设计引入 `ExecutionBarrier` 和 `ContextPatch`，并不是在解决错误的问题。

它抓住的核心矛盾其实是对的：

> 工具结果不只是给模型看的字符串，它们同时也是 runtime 的控制输入。

真正的问题出在抽象层级：

- `ContextPatch` 太窄，只能表达少数 override
- `ExecutionBarrier` 把“工具想影响后续流程”具体化成了“主循环必须跳转到某个私有控制流”

这会让 tool protocol 和 query control plane 相互污染。

因此，这次不是否认 Doc B 的问题定义，而是修正它的抽象选择：

- 保留“工具有双重消费者”这个判断
- 放弃“工具私有 barrier / patch 是正确表达方式”这个判断
- 改为“结构化 update + 统一消息通道 + query-owned transition”

---

## 6. 旧控制模式到新协议的映射

2026-04-17 runtime-control-plane 设计里，隐含了四种控制模式。

本设计要求把它们重新映射到统一协议下。

| 旧模式 | 旧机制 | 新机制 | 即时性 |
|---|---|---|---|
| 扩展-然后-重新规划 | `skill_expanded` barrier | `messages` + `SessionUpdate.INVOKE_SKILL` | 不再强制立即回模型 |
| 观察-然后-约束 | `ContextPatch.allowed_tools` | `RunUpdate.NARROW_ALLOWED_TOOLS` | 对后续串行工具和后续轮次立即生效 |
| 验证-然后-关闭 | barrier / 强制重评估 | `ToolOutcomeStatus` + 普通 query stop/recovery 机制 | 通过统一终止语义表达 |
| 过时状态提醒 | 注入消息 | policy 基于显式状态生成消息 | 保持 |

### 6.1 扩展-然后-重新规划

旧设计中，`skill` 通过 barrier 强制模型立刻回头看新 skill。

新设计中，这个模式改为：

- tool `messages` 告诉模型 skill 已加载
- `SessionUpdate.INVOKE_SKILL` 把 skill 写入长期状态
- 下一轮 `build_runtime_context()` 渲染 skill 内容

本设计**不保留**“skill 一定要立刻打断当前 batch 并重新评估”的语义。

这是有意的，与 Claude Code 的方向一致。

### 6.2 观察-然后-约束

旧的工具集缩小能力必须保留，而且这是安全相关能力，不是可选优化。

`RunUpdate.NARROW_ALLOWED_TOOLS` 保留当前语义：

- 多次缩小只取交集
- 永不放宽
- 对后续轮次持续生效

并新增一条明确约束：

> 在串行执行路径中，一旦某个工具返回 `NARROW_ALLOWED_TOOLS`，
> runtime 必须在执行后续串行工具前重新校验剩余 tool call；
> 不满足限制的调用必须返回显式 rejected tool message，而不是继续执行。

说明：

- 它不会回溯已经执行或已经开始并发执行的工具
- 但它能约束后续串行调用和后续批次

### 6.3 验证-然后-关闭

旧设计把这类需求预留给 barrier，但代码里并没有成熟落地。

新设计不再把“需要停止 / 关闭 / 阻断”建模成 barrier，而是：

- `ToolOutcomeStatus.BLOCKED`
- `ToolOutcomeStatus.NEEDS_USER`
- `ToolOutcomeStatus.FAILURE`

由统一的 query stop / recovery 流程消费。

如果未来真的需要“多个工具都可触发的立即回模型”能力，也必须作为**通用 query 语义**引入，而不是恢复 tool-private barrier。

### 6.4 过时状态提醒

这个模式影响最小。

它继续保留在 policy 中，但 policy 只能读显式状态，不再读桥接 flag。

---

## 7. 新的工具批次执行语义

### 7.1 批次执行仍然保留

工具仍然按 runtime 的调度策略执行：

- concurrency safe 的只读工具可并发
- 非并发安全工具串行

这层调度逻辑保留。

### 7.2 串行块中的更新即时生效

在串行块里，一个工具返回的 `session_updates/run_updates`，要在执行下一个串行工具之前立即应用。

这样后续工具看到的是更新后的 runtime state。

### 7.3 并发块中的更新延后合并

在并发块里，各工具独立执行。

它们产生的状态更新不能边跑边共享修改，而应：

1. 先收集 outcome
2. 再按原 tool call 顺序应用 updates

这样可保持结果稳定、可测试、可复现。

### 7.4 `skill` 的新语义

`skill` 不再做 barrier。

它的新语义是：

1. 返回 skill 激活成功的消息
2. 返回把 skill 写入 `SessionState.invoked_skills` 的 `session_update`
3. 如有需要，返回 query-scoped 的 `run_update`
   - 例如限制后续可用工具
   - 例如建议切换模型 / effort

如果同一批里还有后续工具：

- 它们照常执行
- 不再被跳过

这是向 Claude Code 靠拢的关键取舍。

### 7.5 `skill` 即时性变化的明确取舍

这次重构后，若模型发出：

```text
[skill, read_file, write_file]
```

则不再有“skill 先触发 barrier，后续调用全部跳过”的旧语义。

这意味着：

- 后续工具可能在模型看到 skill 全文之前执行
- 这是用户可见行为变化

本设计接受这个变化，原因有三点：

1. 它去除了工具私有控制流
2. 它与 Claude Code 的主方向一致
3. 它把“skill 生效”从 loop 技巧改回显式状态和输入组装

但本设计同时保留两条约束来降低风险：

1. `skill` 仍会立即返回模型可见消息，说明 skill 已加载
2. 若 `skill` 同时返回 `NARROW_ALLOWED_TOOLS`，该约束会立即作用于后续串行工具

本设计**不在此阶段引入** `IMMEDIATE_REEVALUATE` 之类的新 query 语义。

理由：

- 这会把“去掉 barrier”又绕回另一种通用 barrier
- 当前目标是把协议和状态边界拉正，而不是保留所有旧行为

如果后续真实需求证明“立即回模型重新评估”是多个工具共享的通用能力，再以 query-level 机制引入。

### 7.6 `todo` 的新语义

`todo` 工具不再依赖 loop 侧的 `todo_replan_required` 桥接字段。

它只做两件事：

1. 返回对 `todo_state` 的显式 `session_update`
2. 返回必要的模型可见消息

而“计划是否过时、是否提醒模型刷新”，由 policy 基于显式状态判断。

---

## 8. Query Control Plane 的新模型

### 8.1 `transition` 的职责

`transition` 只回答一个问题：

> 上一轮为什么继续到了这一轮？

它不承载工具私有语义，也不承载长期状态。

### 8.2 当前应定义的 transition reason

当前实现阶段，建议只定义这些：

```python
class TransitionReason(str, Enum):
    NEXT_TURN = "next_turn"
    MAX_TURNS_RECOVERY = "max_turns_recovery"
    EMPTY_RESPONSE_RETRY = "empty_response_retry"
    MAX_TOKENS_RECOVERY = "max_tokens_recovery"
```

说明：

- `NEXT_TURN`：普通工具执行后继续
- `MAX_TURNS_RECOVERY`：已到安全上限，强制收尾
- `EMPTY_RESPONSE_RETRY`：空响应恢复
- `MAX_TOKENS_RECOVERY`：输出截断恢复

本设计**不再定义**：

- `BARRIER`
- `TOOL_RESULT`

理由：

- 绝大多数工具轮次都只是“进入下一轮”，用 `NEXT_TURN` 即可
- `TOOL_RESULT` 只是旧实现中的过渡命名，不是稳定控制面语言
- barrier 被移除后，不需要对应 reason

### 8.3 过渡状态保留 / 重置矩阵

第一阶段至少明确以下字段在不同 transition 下的行为：

| 字段 | `NEXT_TURN` | `MAX_TURNS_RECOVERY` | `EMPTY_RESPONSE_RETRY` | `MAX_TOKENS_RECOVERY` |
|---|---|---|---|---|
| `turn_count` | 工具批次完成后 `+1` | 保持当前值 | 保持当前值 | 保持当前值 |
| `allowed_tools_override` | 保留 | 保留 | 保留 | 保留 |
| `model_override` | 保留 | 保留 | 保留 | 保留 |
| `effort_override` | 保留 | 保留 | 保留 | 保留 |
| `empty_retry_count` | 重置为 `0` | 保持当前值 | `+1` | 保持当前值 |

附加说明：

- `NEXT_TURN` 是“普通成功推进”的基线，因此要重置空响应恢复计数。
- recovery 类 transition 不应悄悄清掉 tool-imposed overrides。
- 未来如果 compact / token recovery 引入额外计数器，也必须补这张矩阵。

### 8.4 `before_model_call` policy 的新边界

policy 不再依赖工具私有桥接字段，例如：

- `todo_replan_required`
- `barrier_reason`

policy 只能基于：

- `SessionState`
- `RunState`
- `transition`

做自己的判断。

例如：

- stale todo reminder 继续保留
- max turns recovery 轮次保持静默
- future compact recovery 轮次保持静默

而 post-skill replan 这种东西，应来自 skill 自己的 `messages`，不是 policy 代劳。

---

## 9. 模型输入组装的目标形态

### 9.1 transcript 不再负责“让状态生效”

以后模型是否“知道一个 skill 已被激活”，不取决于 transcript 里是否还有那条旧消息。

它取决于：

- `SessionState.invoked_skills`
- `PromptAssembler.build_runtime_context(...)`

以后模型是否“知道 todo 当前长什么样”，不取决于历史里是否还保留之前的 todo 输出。

它取决于：

- `SessionState.todo_state`
- `PromptAssembler.build_runtime_context(...)`

### 9.2 overlay 要收缩

`query overlay` 应只放单轮控制信号。

本次重构后，overlay 不再承担：

- skill 激活结果桥接
- barrier 原因桥接
- todo replan 桥接

也就是说，当前 `build_query_overlay()` 应显著收缩。

第一阶段的明确结论是：

> 在本次重构完成后，overlay 允许为空，而且在大多数轮次应该为空。

它只保留为一个 query-owned 扩展点，供未来真正的单轮恢复信号使用。

换句话说：

- skill 不再使用 overlay
- todo 不再使用 overlay
- barrier 被删除后也不再需要 overlay

如果后续 `compact` 或特殊 recovery 需要一个“不应写入 transcript、但只影响下一轮”的信号，再单独放回 overlay。

### 9.3 未来 compact 的受益点

虽然这次不实现 compact，但这次重构完成后：

- compact 可以只压 transcript slice
- runtime truth 仍可由显式状态重建
- skill / todo / file state 不会因为旧消息丢失而失效

这就是这次改造真正的收益。

---

## 10. reducer-like 状态更新入口

### 10.1 必须收敛到少数入口

为了避免本次方案 B 以后再难升到方案 C，本设计要求把状态修改收敛到少数统一入口。

建议最少有：

```python
def apply_session_update(session_state: SessionState, update: SessionUpdate) -> None: ...
def apply_run_update(run_state: RunState, update: RunUpdate) -> None: ...
def apply_transition(run_state: RunState, reason: TransitionReason, **fields) -> RunState: ...
```

### 10.2 禁止散落写状态

重构后应尽量消除这类模式：

```python
state.foo = ...
state.bar = ...
session_state.xxx.append(...)
```

尤其是跨模块散落的直接写入。

允许的例外只应是：

- 纯局部临时变量
- UI 展示缓存字段
- 不属于 runtime truth 的低风险内部计数

但即使这些例外保留，也不能污染主数据流。

### 10.3 为什么现在就要这么做

因为未来若要升级到方案 C，最贵的成本不是“多写 event log”，而是：

> 先把散落在系统各处的直接状态修改找出来并重新收口。

这次把入口收拢，未来升级成本会明显下降。

---

## 11. 直接替换与删除项

本设计采用直接替换，不保留双轨兼容。

### 11.1 删除

- `ExecutionBarrier`
- `ContextPatch`
- `ToolResult.output`
- `ToolResult.injected_messages`
- `ToolResult.context_patch`
- `ToolResult.barrier`
- barrier-aware runtime 执行路径
- skipped-by-barrier tool result 语义
- `RunState.barrier_reason`
- `RunState.todo_replan_required`
- `RunState.todo_replan_reason`
- query loop 中针对 barrier 的 continue 分支

### 11.2 替换

- `ToolResult` → `ToolInvocationOutcome`
- `_apply_batch_control_plane(...)` → reducer-like update application
- `TOOL_RESULT` / `BARRIER` transition → `NEXT_TURN`

### 11.3 保留但重构

- `TodoPlanningPolicy`
- `MessageViewBuilder`
- `PromptAssembler`
- `ToolUseContext`
- `RunState`
- `ToolExecutorRuntime`

这些组件保留，但职责边界要调整。

---

## 12. 影响到的核心文件

本设计会影响至少以下模块：

- `core/tools/context.py`
- `core/tools/runtime.py`
- `core/tools/builtin/skill.py`
- `core/tools/builtin/todo.py`
- `core/tools/builtin/read_file.py`
- `core/tools/builtin/write_file.py`
- `core/tools/builtin/edit_file.py`
- `core/query/state.py`
- `core/query/loop.py`
- `core/query/recovery.py`
- `core/policy/todo_tracking.py`
- `core/session/state.py`
- `core/session/view_builder.py`
- `core/prompt/assembler.py`
- 相关测试

这次不是局部 patch，而是运行时协议层的重构。

---

## 13. 行为变化与取舍

### 13.1 接受的行为变化

1. `skill` 不再中断当前 batch
2. 同批后续工具不再被“skill barrier”跳过
3. post-skill replan 提醒来自 tool message，而不是 policy flag
4. transcript 仍参与模型输入，但不再负责状态生效

### 13.2 为什么这是可接受的

因为我们追求的不是“保留现有技巧”，而是：

> 把工具语义从主循环里拿掉，把控制权重新收敛到统一协议和显式状态。

这比保留当前 barrier 语义更重要。

---

## 14. 测试迁移

本设计采用直接替换，不保留双轨兼容，因此测试必须同步迁移。

迁移范围至少包括：

- 直接引用 `ExecutionBarrier` 的测试
- 直接引用 `ContextPatch` 的测试
- 直接引用 `ToolResult.output` / `injected_messages` / `barrier` 的测试
- barrier-aware runtime 跳过行为测试
- query loop 里 barrier continuation 的测试

迁移策略：

1. 不保留旧夹具
2. 统一改为围绕 `ToolInvocationOutcome`、`SessionUpdate`、`RunUpdate` 断言
3. 旧的 barrier skip 测试改写为：
   - outcome messages 断言
   - update application 断言
   - allowed-tools narrowing 对剩余串行工具的拒绝断言
4. 测试迁移作为实施任务的一部分完成，不单独开兼容阶段

---

## 15. 测试要求

实施后至少要能验证：

1. 工具统一通过 `messages` 把结果返回给模型
2. `skill` 激活后不再触发 barrier 或 skipped results
3. skill 激活仍会在下一轮通过 runtime context 生效
4. `todo` 状态变化不再依赖 query flag 生效
5. `transition` 能准确记录 `NEXT_TURN / recovery / max_turns`
6. transcript slice 被压缩时，skill / todo / file state 仍能通过显式状态重建输入
7. 并发块中的 update 按原调用顺序稳定合并
8. 串行块中的 update 能影响后续串行工具

---

## 16. 最终结论

本设计把 `harness` 的运行时协议调整为：

- transcript 只是 transcript
- runtime truth 显式化
- 工具统一返回消息更新、状态更新、终止/错误信号
- `QueryLoop` 只理解 query control plane
- 状态写入收敛到 reducer-like 入口

这不是 event-sourcing，也不是方案 C。

但它会把系统带到一个足够稳定的位置，让后续的：

- compact
- memory
- permission system
- hook
- task
- error recovery
- MCP

都能在不再次推翻 runtime 的前提下继续增长。

而如果未来真的需要走向方案 C，这份设计也已经提前把最昂贵的准备工作做完了：

> 统一协议、显式状态、收敛写入口、弱化 transcript 真相源。
