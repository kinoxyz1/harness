# Inline Local SkillTool 设计

> 日期: 2026-04-17
> 状态: 待评审
> 依赖:
> - [`docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md)
> - [`docs/superpowers/specs/2026-04-15-skills-system-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-skills-system-design.md)
> - [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)
> 替代旧 skill 激活路径:
> - [`core/tools/builtin/activate_skill.py`](/Users/kino/works/kino/harness/core/tools/builtin/activate_skill.py)

## 摘要

Phase 1 的目标，是把当前延迟生效的 `activate_skill` 路径，替换成一种更接近 Claude Code 的 inline local skill 执行模型。

新的运行方式是：

- 模型调用 `skill`
- `skill` 立即加载并展开本地 skill
- 工具返回 injected messages、可选 runtime overrides、以及 barrier
- 当前 tool batch 在这里停止
- 下一次模型调用在已经展开的 skill 上下文里重新评估下一步

从平台视角看，`skill` 是第一类真正依赖 Runtime control plane 的工具：它不只是返回结果文本，还需要通过 injected messages、context patch、barrier 改写下一步运行环境。详见：
[`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)

第一阶段明确 **不** 实现：

- `fork` skills
- 远程 skills
- compact 后 invoked skill 恢复
- 超出时序改进之外的 todo/task parity

## 来自 Claude Code 的直接证据

本设计基于本地 Claude Code 源码镜像中的直接观察：
`/Users/kino/works/opensource/Claude-Code-doc`

### 证据 1: Skill 执行是专门的工具

Claude Code 定义了 `SkillTool`：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/SkillTool.ts`

这不是单纯的状态位，也不是系统提示词的副作用。

### 证据 2: Inline skill 执行会展开成消息

`SkillTool.call()` 会委托给 `processPromptSlashCommand(...)`，然后返回 `newMessages`。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/SkillTool.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/utils/processUserInput/processSlashCommand.tsx`

可观察行为是：

- skill prompt 内容被加载
- skill 内容被转换成 messages
- 这些 messages 被送回主 query path

### 证据 3: Claude Code 明确把 skill 执行视为时序边界

`SkillTool.ts` 中有一句非常关键的注释：

> Only one skill/command should run at a time, since the tool expands the command into a full prompt that Claude must process before continuing.

这里的直接源码依据是：

- skill 执行不应和其他命令混在同一个继续推进的动作链中
- skill expansion 之后需要模型重新处理新 prompt

但 Claude Code 源码里并没有一个现成的 `ExecutionBarrier` 类型。因此，phase 1 中把这一时序边界具体实现成 `ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")`，属于 `harness` 的设计推断，而不是对 Claude Code 类型系统的直接复刻。

### 证据 4: Invoked skills 会被记录，以支持 compact

Claude Code 会记录 invoked skill，并在 compact 时打包成附件。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/bootstrap/state.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/compact.ts`

Phase 1 不实现恢复流程，但必须保留后续需要的 record 结构与生命周期。

## 当前 Harness 的问题

当前 `harness` 的 skill 流程是：

1. 启动时发现 skill catalog
2. 模型调用 `activate_skill`
3. 修改 `SessionState.active_skills`
4. 后续由 `PromptAssembler.build_active_skill_messages()` 再把 skill 注入上下文

相关文件：

- [`core/tools/builtin/activate_skill.py`](/Users/kino/works/kino/harness/core/tools/builtin/activate_skill.py)
- [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py)
- [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py)
- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)

这个流程会导致两个直接问题：

1. **Skill expansion 被延后。**
   第一批真正影响执行的工具决策，可能已经发生了。

2. **Skill 与 todo 在抢同一个规划时机。**
   todo 往往基于 pre-skill 的推理状态生成，把真正的工作流压缩掉。

## Phase 1 目标

用“立即展开的 inline skill”替代“延迟激活的 active skill”。

本阶段结束后，应满足：

- 主路径不再依赖 `activate_skill`
- inline local skill 的执行入口只有 `skill`
- runtime 可以在 skill expansion 之后立刻停止当前批次，并重新请求模型

## 非目标

Phase 1 明确不包含：

- `fork` skill 执行
- 后台 skill 执行
- 现有本地 catalog 之外的 skill 搜索/发现
- compact 时对 injected messages 的回放或重建
- 更强的 todo/task 模型
- 从 skill 段落自动生成 todos

## 设计概览

### 旧模型

```text
model -> activate_skill
      -> state.active_skills += skill
      -> next model turn sees skill via prompt assembler
```

### 新模型

```text
model -> skill(skill=...)
      -> load local skill content
      -> inline reference bodies
      -> return injected_messages + context_patch + barrier
      -> query loop appends injected messages
      -> current batch stops
      -> next model call sees expanded skill immediately
```

## 新工具 API

引入新的 builtin tool：

- 文件: `core/tools/builtin/skill.py`
- 对外工具名: `skill`

术语说明：

- 文档里提到 **SkillTool** 时，指的是“Claude-Code-like 的架构角色”
- 在 `harness` 里，实际工具名仍然应为小写 `skill`，以保持与 `read_file`、`write_file`、`todo`、`activate_skill` 一致

### 输入 schema

```json
{
  "type": "object",
  "properties": {
    "skill": { "type": "string" },
    "args": { "type": "string" }
  },
  "required": ["skill"]
}
```

### 第一阶段语义

- `skill`: 本地 skill id，例如 `analysis-report`
- `args`: 可选，先作为未来扩展预留；phase 1 可以忽略或只用于日志

## 数据模型变更

### 1. 保留现有 registry 模型

继续保留：

- `SkillReference`
- `SkillMeta`
- `SkillContent`

并继续沿用当前已经完成的 inline reference loading 行为。

原因：

- 当前 reference inline 已经解决了真实问题
- phase 1 的重点是时序与控制流，不是把已有进展推翻

### 2. 从 activation-oriented state 改为 invocation-oriented state

新增：

```python
@dataclass(slots=True)
class InvokedSkillRecord:
    skill_id: str
    skill_path: str
    content_digest: str
    content: str
    invoked_at_turn: int
```

在 `SessionState` 中增加：

```python
invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
```

从主逻辑中弃用：

```python
active_skills: dict[str, ActiveSkillState]
```

`active_skills` 在迁移期可以临时保留，但 phase 1 的主路径不得再依赖它。

### 3. 扩展 `ToolResult`

当前的 `ToolResult` 太窄，应扩展为：

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
```

### 4. 新增 `ContextPatch`

```python
@dataclass(slots=True)
class ContextPatch:
    allowed_tools: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
```

Phase 1 中它的语义是：

- patch 是 **run-scoped**
- 只影响当前 query run 中后续的模型调用
- 不会立刻变成 session-wide sticky state

### 5. 新增 `ExecutionBarrier`

```python
@dataclass(slots=True)
class ExecutionBarrier:
    stop_after_tool: bool = True
    reason: str | None = None
```

Phase 1 合法的 `reason` 值只有：

- `"skill_expanded"`

### 6. 扩展 `ToolBatchResult`

```python
@dataclass(slots=True)
class ToolBatchResult:
    tool_results: list[dict[str, Any]]
    files_modified: list[str]
    tool_names: list[str]

    injected_messages: list[dict[str, Any]]
    context_patches: list[ContextPatch]
    barrier: ExecutionBarrier | None
```

补充：

- phase 1 中如果某个工具调用因为 `skill_expanded` barrier 被跳过，`ToolBatchResult.tool_results` 仍必须为该 `tool_call_id` 生成一个显式的 `tool` 结果消息
- 不应依赖当前 [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 中“缺失 tool_result 时自动补 `(cancelled)` 占位”的兜底逻辑
- 推荐跳过消息内容为：

```text
(skipped: superseded by skill_expanded barrier; re-issue after re-evaluation if still needed)
```

原因：

- 当前 `QueryLoop` 会把 assistant message 中的全部 `tool_use` 记入 transcript
- 如果只执行其中一部分，而另外一部分没有明确结果，模型虽然最终会收到一个合成的 `(cancelled)`，但那是协议修补，不是有意设计的运行时语义
- phase 1 应显式告诉模型：这些调用不是失败，也不是完成，而是因为 skill barrier 被废弃，模型需要在下一轮重新决定

## Phase 1 之后的模块职责

### `core/skills/registry.py`

继续负责：

- skill discovery
- 本地 skill loading
- inline reference loading

不再负责：

- 决定 skill 何时生效
- prompt 注入时机

### `core/tools/builtin/skill.py`

负责：

- 校验本地 skill id
- 通过 registry 加载 skill 内容
- 生成 injected skill messages
- 生成 runtime patch metadata
- 生成 barrier metadata
- 写入 invoked-skill records
- 输出 `[Skill]` 日志

### `core/tools/runtime.py`

负责：

- 执行工具
- 聚合 richer tool result fields
- 在 barrier 时提前停止

不负责：

- 直接改写 conversation history
- 读取 skill 文件
- 对 prompt assembly 做 skill 特判

### `core/query/loop.py`

负责：

- 追加普通 tool result messages
- 追加 injected messages
- 应用 run-scoped patches
- 遵循 barrier 语义并重新进入模型评估

### `core/prompt/assembler.py`

Phase 1 之后它只应负责：

- 稳定 system prompt
- environment message 构建

它不应继续承担主路径上的 active skill 注入。

### `core/session/view_builder.py`

负责：

- 从 `conversation_messages` 构建模型可见消息
- 在 model call 时应用 filtered tools 与 overrides

它不应继续根据 `SessionState.active_skills` 合成 skill messages。

## 工具输出

### 1. Injected messages

Phase 1 推荐使用一条结构化 system message：

```xml
<skill-runtime>
  <skill id="analysis-report" source="local-inline">
    <instruction>
      ...SKILL.md body...
    </instruction>
    <reference-files>
      <file path=".harness/skills/analysis-report/analysis-pipeline.md">
        ...
      </file>
    </reference-files>
  </skill>
</skill-runtime>
```

为什么不复用 `<active-skills>`：

- 这个标签和旧的 delayed-state 模型绑定得太紧
- phase 1 需要一个能明确区分新旧路径的 runtime artifact
- reviewer 必须能一眼看出这是“tool-driven expansion”，不是旧的 prompt assembler 注入

为什么在 `harness` 里把它作为中途插入的 `system` message 是可行的：

- `core/llm/protocol.py` 已经会把内部所有 `system` 消息合并到 Anthropic 顶层 `system` 参数
- 因此 phase 1 可以把 injected skill message 放进 `conversation_messages`，同时兼顾可观察性与 provider 兼容性

但这也意味着一个必须明确写出的副作用：

- injected skill message 一旦写入 `conversation_messages`，就不只是“当前 run 可见”，而是会在后续整个会话中继续参与 `system` 合并

这不是 bug，而是 phase 1 对 inline local skills 的有意选择：

- `ContextPatch` 是 run-scoped
- injected skill message 本身则是 session-visible 的持久对话事实

这一区分必须写清楚，否则 reviewer 会误以为 “run-scoped patch” 也自动意味着 “skill 注入内容只活一轮”。

### 2. 普通文本输出

工具常规输出应该简短、程序化，例如：

```text
Skill loaded: analysis-report. The skill instructions have been injected into context. Re-evaluate your next action using the skill guidance.
```

它不应该重复输出整个 skill body。它的作用是提示模型重新评估下一步。

### 3. Barrier

`skill` 必须返回：

```python
ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
```

这是 phase 1 的强制要求。

## 控制流变更

## `ToolExecutorRuntime`

### 当前问题

当前 runtime 认为“完整的 tool-call batch”天然就是执行单位。

对 skill expansion 来说，这是错误前提，因为 skill expansion 不是一个普通副作用，而是会改写后续动作语义的上下文切换点。

这在当前 [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 里是一个真实的实现约束：

- `_partition()` 目前只按 `readonly` / `write` 划分 batch
- 它并不知道 `skill` 这种“虽然可能是只读，但会重排控制流”的工具类别

因此 phase 1 必须显式修改分批规则，而不是假设现有 partition 逻辑天然适配 skill。

### Phase 1 规则

如果一个 tool-call batch 里包含 `skill`，那么正确性优先于并行性。

推荐行为：

- 按顺序执行 batch
- 如果原始 tool_calls 中出现 `skill`，当前批次的执行策略应退化为“从该位置开始串行解释”
- 一旦 `skill` 返回 barrier，就停止后续工具调用
- 不自动重放被跳过的调用
- 让模型在 skill 已经可见的下一步里重新决定是否还要调用它们

原因：

- 凡是在 skill expansion 之前形成的后续工具决策，此时都已经过时

### 为什么不自动 replay 被跳过的调用

因为 replay 会保留 pre-skill 的决策结构，正好违背 barrier 的目的。

如果模型仍然想调用 `todo`、`read_file`、`bash`，它应该在 skill 已经进入上下文后重新做出这个决策。

这会带来一个可接受但必须明说的用户体验 tradeoff：

- 某些原本可能“也有价值”的后续工具调用，例如同一批中的一次 `read_file`，会因为 barrier 被跳过
- 这看起来像浪费了一轮工具调用
- 但 phase 1 的设计仍然优先保证“skill 接管后再重规划”的正确性，而不是最大化复用 pre-skill 决策

## `QueryLoop`

Phase 1 之后，主循环应变成：

1. 调用模型
2. 追加 assistant message
3. 执行 tool batch
4. 追加普通 tool results
5. 追加 injected messages
6. 合并并应用 context patches
7. 如果存在 barrier：
   - 停止当前正常下游推进
   - 立即继续外层 query loop
8. 下一次模型调用基于更新后的 conversation 与当前 run patch state 构建

这里必须保证的核心不变量是：

> skill 的 injected messages 必须在下一次动作规划型模型调用之前可见。

## View 构建

### Conversation history

Injected skill messages 必须写入 `conversation_messages`。

原因：

- 模型真正看到过什么，历史里就应该能看见
- 调试不应该依赖隐藏状态
- phase 1 明确避免出现两套并行 skill 注入机制

### Context patch

`ContextPatch` 本身不进入 conversation history。

它只影响 model view 构建，例如：

- filtered tool schemas
- model override
- effort override

### 生命周期

Phase 1 的 patch 生命周期是 **run-scoped**：

- 从产出它的工具调用之后开始
- 持续到当前 query run 结束
- 不保证跨后续用户轮次持久化

这个边界比 session-scoped 更窄，也更容易审查。

## 可观察性

Phase 1 必须留下两类证据：

1. **runtime 日志证据**
2. **conversation history 证据**

### Runtime 日志

推荐日志格式：

```text
[Skill] 展开 analysis-report (6 refs, 18,240 chars, barrier=true)
```

可选附加信息：

- `allowed_tools=...`
- `model_override=...`
- `effort_override=...`

推荐 barrier 日志：

```text
[Runtime] skill barrier triggered; deferring remaining tool calls to model re-evaluation
```

### Conversation 证据

被注入的 `<skill-runtime>` message，就是“模型真的收到了 skill 展开内容”的证据。

### 为什么两者都需要

日志证明工具运行过。
Conversation message 证明 skill 真正进入了模型上下文。

缺了任意一边，用户都会回到老问题：

“模型嘴上说用了 skill，但它到底有没有真的看到 skill？”

## 对 Todo 的直接影响

Phase 1 不重新设计 todo，但它应该立刻改善 todo 行为。

原因：

- 今天 todo 往往先抢到第一版规划时机
- phase 1 之后，skill 会先展开
- todo 再发生时，就会处在更正确的工作流上下文中

预期结果：

- todo 会明显更具 skill-aware 的特征
- 但不会因此自动变成完美方案

如果 phase 1 之后 todo 仍然偏粗，那说明剩余问题确实属于 phase 2 的 task-parity 设计，而不是 skill timing。

## 弃用组件

### `activate_skill`

Phase 1 弃用：

- `core/tools/builtin/activate_skill.py`

迁移建议：

- 如有必要可短期保留，确保迁移安全
- 但一旦 `skill` 接通，必须从主工具集合移除
- 后续文档与测试都只应该针对 `skill`

### `/skills use`

当前 [`core/session/commands.py`](/Users/kino/works/kino/harness/core/session/commands.py) 中的 `/skills use <id>` 直接修改 `state.active_skills`。

Phase 1 之后，这条命令不能继续作为旧 delayed-state 路径的保留入口。

推荐迁移方式：

- `/skills use` 保留为用户兼容命令
- 但实现改为复用和 `core/tools/builtin/skill.py` 相同的 shared expansion helper
- 命令执行后直接写入：
  - injected skill runtime message
  - `invoked_skills`
  - 必要的 skill events

并且：

- 不再写入 `state.active_skills`
- 不再依赖 `PromptAssembler.build_active_skill_messages()`

由于 `/skills use` 是显式用户命令，而不是模型在当前 batch 中的工具调用，因此它不需要生成 `skill_expanded` barrier；它的效果应在下一次正常 query run 开始前已经可见。

### `SessionState.active_skills`

弃用其“主运行时状态”角色。

新的语义载体应变成：

- injected skill runtime messages
- `invoked_skills` records

### `PromptAssembler.build_active_skill_messages()`

从主路径移除。

如果迁移期暂时保留，也应明确标记为 deprecated，并确保 phase 1 执行路径不再调用它。

## 文件级变更计划

### 新文件

- `core/tools/builtin/skill.py`

### 修改文件

- `core/tools/context.py`
  - 扩展 `ToolResult`
  - 新增 `ContextPatch`
  - 新增 `ExecutionBarrier`

- `core/tools/runtime.py`
  - 聚合 richer tool result fields
  - 在 barrier 时停止执行

- `core/query/loop.py`
  - 追加 injected messages
  - 应用 run-scoped patches
  - 遵循 barrier 并重新进入模型评估

- `core/session/state.py`
  - 增加 `invoked_skills`
  - 弃用 `active_skills`

- `core/session/view_builder.py`
  - 消费 run-scoped patch state
  - 停止合成 active skill messages

- `core/prompt/assembler.py`
  - 从主路径移除 active skill injection
  - 仅保留 stable prompt 与 environment assembly
  - 修复 stable prompt cache key，只用 `skills_revision` 会漏掉 `_FRAMEWORK_PROMPT` 与其他 system prompt 文本变更

- `core/session/engine.py`
  - 串起新的 runtime/state 字段

- `core/skills/models.py`
  - 增加 `InvokedSkillRecord`

### 从主路径移除

- `core/tools/builtin/activate_skill.py`

## 测试策略

### 单元测试

1. `skill` 能校验已存在的本地 skills，并拒绝未知 skill
2. `skill` 能加载 body 与 inline references
3. `skill` 会返回 injected messages
4. `skill` 会返回 barrier
5. `skill` 会记录 `invoked_skills`

### Runtime 测试

1. 如果模型响应只包含 `skill`，下一次模型调用必须能看到 `<skill-runtime>`
2. 如果模型响应同时包含 `skill` 与其他工具调用，执行必须在 `skill` 处停止
3. 被跳过的调用不会被自动 replay，但会收到显式 `skipped` tool result
4. `ContextPatch` 中的 filtered tools 会作用于下一次模型调用
5. injected skill message 在后续会话轮次中仍会继续参与 `system` 合并

### 回归测试

1. 非 skill 工具在 richer `ToolResult` 协议下仍正常工作
2. 现有 `read_file`、`write_file`、`todo`、`bash` 若不返回 richer fields，行为仍保持不变
3. stable system prompt 与 environment prompt 仍能正常渲染
4. 修改 `_FRAMEWORK_PROMPT` 或相关 system prompt 文本后，stable prompt cache 不会错误复用旧内容

### 集成测试目标

针对一个需要 `analysis-report` 的任务：

- 第一个相关工具调用应该是 `skill`
- 第一版 todo 创建必须发生在 injected skill message 可见之后

这是本阶段最重要的用户侧回归测试。

## 风险

### 1. 更多代码路径将依赖 richer tool protocol

这是无法回避的。当前仅字符串输出的协议无法表达所需行为。

### 2. Runtime batching 会变得没那么激进

这是 `skill` 参与的轮次中的预期结果。设计接受吞吐下降，以换取正确时序。

### 3. Reviewer 可能试图把设计拉回 delayed state activation

除非他们能证明“当前轮重新评估”依旧稳定成立，否则这种回退在 phase 1 中应被拒绝。

核心问题不在于 skill 能不能“被记住”，而在于它能不能真正控制下一步动作边界。

## 延后扩展点

### 未来的 `fork` skills

Phase 1 明确为未来 `fork` 执行路径预留空间：

- `skill` 后续可以根据类似 `command.context == "fork"` 的元数据分支
- `InvokedSkillRecord` 可以扩展 agent scope
- `ContextPatch` 可以从“作用于主 session”扩展为“作用于 forked agent”

### 未来的 compact 保留

Phase 1 故意把 `InvokedSkillRecord.content` 存下来。

当前主路径还用不到，但这会为 phase 3 提供干净起点，用于：

- compact attachments
- compact 后的 skill reminder
- 无需 replay 的 invoked skill 持久化

## 成功标准

如果以下条件全部成立，则 phase 1 视为成功：

1. inline local skill 执行不再依赖 `activate_skill`
2. `skill` 会把 skill 展开成可见的 conversation messages
3. barrier 会阻止后续工具执行并强制模型重新评估
4. 下一次模型调用能看到展开后的 skill context
5. 当 todo 发生时，它是在 skill expansion 之后，而不是之前
6. reviewer 能清楚区分 Claude Code 的直接证据与 harness 的设计选择
