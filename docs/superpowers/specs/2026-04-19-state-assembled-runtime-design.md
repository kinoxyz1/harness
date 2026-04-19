# State-Assembled Runtime 设计

> 日期: 2026-04-19  
> 状态: 待评审  
> 目标: 将 `harness` 从“消息历史驱动的 prompt 组装”重构为“显式状态组装的 agent runtime”，让 `conversation_messages` 退化为 transcript，而不是模型输入的真相源。

## 相关文档

- [`docs/superpowers/specs/2026-04-15-session-query-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md)  
- [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)  
- Claude Code 参考文档：
  - `/Users/kino/works/opensource/Claude-Code-doc/docs/03-agent-loop.md`
  - `/Users/kino/works/opensource/Claude-Code-doc/docs/05-memory.md`
  - `/Users/kino/works/opensource/Claude-Code-doc/docs/24-services-compact.md`
  - `/Users/kino/works/opensource/Claude-Code-doc/docs/40-lifecycle.md`

## 摘要

当前 `harness` 已经完成 `Session / Query / ToolRuntime` 的初步分层，但模型输入仍然高度依赖 `SessionState.conversation_messages`。这导致会话 transcript 同时承担了三种职责：

- 审计日志：记录用户、assistant、tool 的交互轨迹
- 运行时真相：承载 skill 生效内容、环境注入结果、工具上下文痕迹
- 模型输入：被 `MessageViewBuilder` 直接复制给模型

这种结构在功能较少时可工作，但会阻碍后续能力演进，尤其是：

- compact
- resume / replay
- 可恢复的 runtime context
- 更稳定的 skill / todo / file-state 行为

本设计的主目标不是“为了 compact 做最小改造”，而是：

> 让模型输入不再直接依赖完整对话历史，而是由显式状态每轮重新组装。

compact 只是这次架构升级的附加收益，不是本设计的主导约束。

## 0. 与前序文档和当前实现的关系

这份文档不是推翻 2026-04-15 和 2026-04-17 的设计，而是在它们已经建立的分层之上，继续解决一个更核心的问题：

> 当前模型输入的真相源仍然是 transcript，而不是显式状态。

因此三份文档的分工应明确如下：

| 文档 | 解决的问题 | 当前实现状态 | 本文与其关系 |
|------|------------|--------------|--------------|
| 2026-04-15 Session / Query / Runtime | 主循环和目录结构分层 | 已基本落地 | 本文保留这三层，不回退 |
| 2026-04-17 Runtime Control Plane | 工具结果如何影响 run 内控制面 | 已部分落地 | 本文明确这些控制信号不等于 runtime truth |
| 本文 | 模型输入的真相源如何从 transcript 切到显式状态 | 未实现 | 为后续 compact / resume / replay 奠定前提 |

当前代码库里，04-17 的不少内容已经存在于实现中：

- [`core/tools/context.py`](/Users/kino/works/kino/harness/core/tools/context.py) 已有 `ContextPatch`、`ExecutionBarrier`、`ToolResult`
- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 已有 barrier-aware tool execution
- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 已有 `_apply_batch_control_plane()`

而本文的新增点不是再定义一套新的 ToolResult 协议，而是补上下面这层缺失语义：

- ToolResult / RunState 负责 run 内控制
- SessionState 负责长期 runtime truth
- PromptAssembler / MessageViewBuilder 负责把权威状态重新渲染成模型输入

换句话说：

> 04-17 解决的是“工具结果如何改变本次 run”，本文解决的是“下一轮模型输入到底应该由谁来定义”。

## 1. 背景

Claude Code 文档给出的关键启发不是某个具体 API 或消息类型，而是一条更本质的架构原则：

> 对话历史、稳定上下文、运行时上下文、单次 query 控制状态，应当是不同层次的对象，而不是一份消息数组的不同用途。

从参考文档可以归纳出以下原则：

1. transcript 不是唯一真相源
2. system prompt 与 runtime attachments 是不同性质的上下文
3. compact 的安全前提是“关键运行时状态可以重建”
4. query loop 每轮消耗的是“当前视图”，不是“原始历史的全量回放”

而当前 `harness` 的主要现状是：

- [`core/session/state.py`](/Users/kino/works/kino/harness/core/session/state.py) 中的 `conversation_messages` 仍是默认 prompt 源
- [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py) 只是复制消息历史
- [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py) 只显式渲染 stable prompt 的一小部分
- [`core/skills/runtime.py`](/Users/kino/works/kino/harness/core/skills/runtime.py) 仍通过追加 `<skill-runtime>` system message 让 skill 生效

也就是说，`harness` 已经有分层雏形，但还没有真正完成“上下文分层”。

## 2. 问题定义

当前系统的核心问题不是“prompt 太长”，而是：

> 模型输入的权威来源仍是消息历史，而不是显式状态。

这会带来四类结构性风险：

### 2.1 Transcript 语义过载

`conversation_messages` 同时承担：

- 给用户留痕
- 给模型续上下文
- 给 runtime 保留结构状态痕迹

当一个对象承担三种职责时，任何压缩、裁剪、重排都会有副作用。

### 2.2 Runtime 事实缺少权威存储

当前一些对后续回合必须持续生效的事实，仍然主要通过 transcript 间接保存，例如：

- skill 已激活且其正文已进入当前上下文
- 某些 file read 的内容已经被模型依赖
- 当前 todo / workflow 已经发生变化

这些信息一旦只存在于消息历史中，compact 或 resume 都会变得危险。

### 2.3 Query 恢复依赖历史回放

空响应恢复、tool barrier 后继续、未来的 compact 恢复，如果主要依赖“把旧消息继续带上”，那么系统缺少稳定的恢复边界。

### 2.4 Compact 会放大架构缺陷

在当前架构下，compact 不是一个独立功能，而是会直接挑战模型输入的真相源。一旦旧消息被摘要替换，就可能同时损失：

- transcript 细节
- runtime 真相
- prompt 组装约束

因此，compact 不是这次设计的起点，而是这次设计必须先解决的副作用放大器。

## 3. 设计目标

本设计的目标是把 `harness` 升级为 state-assembled runtime。

### 3.1 核心目标

1. `conversation_messages` 退化为 transcript / 审计日志
2. 模型输入每轮由显式状态重新组装
3. runtime truth 不再依赖旧 system messages 是否仍在历史中
4. QueryLoop 消费的是 `ModelInputView`，不是原始 transcript
5. 未来 compact 只需要处理 transcript channel，而不是补救丢失的 runtime state

### 3.2 设计原则

1. 权威状态与模型可见渲染必须分离
2. transcript 可以压缩，但 runtime truth 必须可重建
3. single-run 控制状态只活在 query scope，不写回长期 transcript
4. stable context、runtime context、query overlay、transcript slice 必须分通道组装
5. 本次改造采用直接切换，不保留 message-centric 的双轨兼容路径

## 4. 参考映射与边界

## 4.1 Claude Code 概念映射

这次设计借的是 Claude Code 的分层原则，不是其消息类型实现。为了避免抽象映射过散，实施时应优先按下表理解：

| Claude Code 概念 | harness 对应 | 当前状态 | 本设计目标 |
|------------------|-------------|----------|------------|
| `QueryEngine.mutableMessages` | `SessionState.conversation_messages` | 直接作为 prompt 源 | 降级为 transcript |
| compact 后 attachment 恢复 | runtime context 渲染 | 未实现 | 由 assembler 重渲染 |
| `getMessagesAfterCompactBoundary()` | `transcript_slice` 选择 | 未实现 | 由 MessageViewBuilder 选择 |
| `createPostCompactFileAttachments()` | `file_runtime` 渲染 | 未实现 | 由 `read_file_state` 驱动 |
| `createSkillAttachmentIfNeeded()` | `skill_runtime` 渲染 | 未实现 | 由 `invoked_skills` 驱动 |
| `planAttachment` | `planning_runtime` 渲染 | 部分存在 | 由 `todo_state` / 未来 plan state 驱动 |
| query loop 消费当前视图 | `MessageViewBuilder.build()` | 仅复制历史 | 组装 `ModelInputView` |

### 4.2 非目标

本设计明确不包含：

- 直接实现 compact
- 兼容旧链路的渐进式双轨模式
- 引入复杂事件溯源系统替换现有全部状态结构
- 复刻 Claude Code 的 attachment / hook / delta 类型体系
- 一次性实现 swarm、MCP、remote session 等 Claude Code 特有能力

本设计关注的是：在保留 `harness` 当前总体架构方向的前提下，完成“上下文真相源”的切换。

## 5. 总体分层

目标架构中的模型输入应由以下五层组成：

### 5.1 Stable Context

作用：定义 agent 的稳定身份与长期约束。

包含：

- system prompt
- available skills catalog
- 环境基线
- 长期 memory
- 稳定工具说明

特点：

- 在整个 session 内相对稳定
- 可缓存
- 不依赖 transcript 保持存在

### 5.2 Runtime Context

作用：定义当前 session 已建立、且后续回合必须保真的运行时事实。

包含：

- invoked skills
- todo / plan 快照
- read file state
- session metadata
- discovered / activated capabilities
- 未来 compact restore 所需状态

特点：

- 是 compact 之后仍必须保留的层
- 权威状态保存在 `SessionState`
- 模型看到的是渲染结果，不是原始 Python 对象

### 5.3 Query Overlay

作用：表达当前这一轮 query run 内部的临时控制面。

包含：

- allowed tools override
- barrier / transition reason
- todo replan flags
- model / effort override
- 临时 system reminder
- 预算或恢复相关短期提示

特点：

- 生命周期只覆盖单次 `QueryLoop.run()`
- run 结束即销毁
- 不承担长期状态职责

### 5.4 Transcript

作用：保存发生过什么。

包含：

- 用户消息
- assistant 文本
- tool result
- system 注入痕迹
- 未来的 compact boundary / summary event

特点：

- 面向审计、回放、调试
- 可以裁剪、预算、压缩
- 不再是模型输入的唯一真相源

### 5.5 Prompt Assembly

作用：将上述四类信息重新组装为当前模型调用视图。

建议顺序：

`Stable Context -> Runtime Context -> Query Overlay -> Transcript Slice`

## 6. Runtime Context 的权威状态模型

这次设计的关键不是再造一组 message types，而是把“当前藏在 transcript 里的运行时事实”搬回 `SessionState`。

### 6.0 Phase 1 的 `SessionState` 目标形状

Phase 1 不要求立即把 `SessionState` 改成嵌套的 `state.runtime.*` dataclass。

为了降低重写面，本设计明确采用：

- **语义切换立刻发生**
- **数据结构先保持扁平**

也就是说，Phase 1 的 target 不是“新增五组全新字段”，而是：

1. 保留当前已存在的权威字段
2. 改变这些字段的职责定义
3. 停止让 transcript 承担这些字段的替代真相源

建议的 Phase 1 target 形状如下：

```python
@dataclass(slots=True)
class SessionState:
    # Transcript only
    conversation_messages: list[dict[str, Any]]

    # Stable-context support
    prompt_cache: dict[str, str] = field(default_factory=dict)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    skills_revision: str | None = None

    # Runtime authority: skills
    invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
    active_skills: dict[str, ActiveSkillState] = field(default_factory=dict)  # deprecated compatibility field
    skill_events: list[SkillEvent] = field(default_factory=list)

    # Runtime authority: files
    read_file_state: dict[str, Any] = field(default_factory=dict)

    # Runtime authority: planning
    todo_state: TodoState = field(default_factory=TodoState)

    # Runtime authority: capabilities / metadata
    discovered_tools: set[str] = field(default_factory=set)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
```

这一定义强调：

- `invoked_skills`、`read_file_state`、`todo_state`、`session_metadata` 已经是 runtime authority 的一部分
- 本文并不要求先引入 `state.runtime.skill_runtime` 这类嵌套结构才能开始重构
- `SessionState.runtime.*` 在本文中是**概念层命名**，不是 Phase 1 的强制代码形状

只有当 Phase 1 完成后，再决定是否把扁平字段重组为嵌套 dataclass。

### 6.1 `skill_runtime`

记录哪些 skill 已激活，以及它们当前对模型生效的内容。

要求：

- `invoked_skills` 继续保留结构化记录
- skill 生效的权威来源不再是历史里的 `<skill-runtime>` system message
- PromptAssembler 根据当前激活状态重新渲染 active skill blocks

### 6.2 `file_runtime`

记录当前 session 的文件读取上下文。

要求：

- 现有 `read_file_state` 从“工具缓存”升级为正式 runtime context 组成部分
- compact 之后优先保留和重建文件状态，而不是依赖旧 tool_result 文本
- 渲染时可按预算只暴露模型当前真正需要的部分

### 6.3 `planning_runtime`

记录 todo、plan、workflow 对齐状态。

要求：

- todo state 是权威状态，不是聊天文本
- transcript 可记录计划更新事件
- 模型下一轮看到的是当前计划快照，而不是自己回看旧 todo 写入结果

### 6.4 `capability_runtime`

记录当前 session 已发现、已激活、或当前生效的能力事实。

包括：

- discovered tools
- skill catalog revision
- 未来的 plan mode / task handles / capability toggles

边界：

- “系统具备什么能力”更偏向 stable context
- “当前 session 已启用或限制了什么”属于 runtime context

### 6.5 `session_runtime_metadata`

记录会影响后续行为、但不适合仅依赖 transcript 的会话事实。

例如：

- session metadata
- compact restore metadata
- usage totals 的派生信息
- 未来 resume 需要的恢复锚点

### 6.6 权威状态与渲染分离

不建议把 runtime state 原样暴露给模型。

建议明确两层：

- `SessionState` 中现有 runtime authority 字段保存权威结构化状态
- `PromptAssembler` 渲染 `<runtime-context>`、`<active-skills>`、`<todo-state>` 等模型可见块

这条分离原则是 compact、resume、debug、replay 成立的前提。

## 7. 模块职责重组

### 7.1 `SessionEngine`

继续作为 session owner，但不再隐含“历史消息就是 prompt”。

职责：

- 接收用户输入
- 维护 `SessionState`
- 调用 context / view assembly 生成当前 `ModelInputView`
- 提交 transcript 事件与长期状态更新

不负责：

- 直接拼装最终模型输入文本
- 通过追加历史消息让 runtime 生效

### 7.2 `PromptAssembler`

从 stable prompt builder 升级为 context renderer。

当前实现现状：

- `build_stable()` 已存在
- `build_active_skill_messages()` 当前返回空列表
- `build_dynamic()` 当前返回空列表

Phase 1 目标接口：

```python
class PromptAssembler:
    def build_stable_context(
        self,
        state: SessionState,
        *,
        project_root: str | None = None,
    ) -> str: ...

    def build_runtime_context(
        self,
        state: SessionState,
        *,
        working_dir: str,
        char_budget: int | None = None,
    ) -> str: ...

    def build_query_overlay(
        self,
        state: SessionState,
        run_state: RunState,
    ) -> str: ...

    def build_internal_runtime_view(
        self,
        state: SessionState,
        run_state: RunState,
    ) -> dict[str, Any]: ...

    def build_active_skill_messages(
        self,
        state: SessionState,
    ) -> list[dict[str, str]]: ...
```

约束：

- `build_stable_context()` 是 `build_stable()` 的语义升级，可兼容复用现有缓存逻辑
- `build_runtime_context()` 必须开始消费 `invoked_skills`、`read_file_state`、`todo_state`
- `build_query_overlay()` 渲染 run-scoped 控制块，不写回 transcript
- `build_internal_runtime_view()` 只供 runtime 和 view assembly 使用，不进模型输入
- `build_active_skill_messages()` 不再允许返回空列表，它是把 skill 生效从 transcript 迁出的第一步

它的主要价值不再是缓存一段 system prompt，而是：

> 将权威状态翻译为模型当前应看到的上下文块。

### 7.3 `MessageViewBuilder`

从“复制消息列表”升级为“组装模型输入视图”。

建议引入明确的视图对象：

```python
@dataclass(slots=True)
class ModelInputView:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    internal_runtime_view: dict[str, Any] = field(default_factory=dict)
```

Phase 1 目标接口：

```python
class MessageViewBuilder:
    def build(
        self,
        state: SessionState,
        *,
        run_state: RunState,
        prompt_assembler: PromptAssembler,
        working_dir: str,
        project_root: str | None = None,
        transcript_char_budget: int | None = None,
    ) -> ModelInputView: ...
```

职责：

- 获取 stable / runtime / overlay 渲染结果
- 决定 transcript 要带哪些切片
- 组合最终 `messages`
- 组合最终 `tools`
- 挂出 `internal_runtime_view` 供 runtime / debug / future compact 使用

约束：

- `messages` 是最终传给模型 API 的 message payload
- `transcript_slice` 是 MessageViewBuilder 内部选出的 transcript 子集概念，不单独作为 `ModelInputView` 字段暴露
- 如果未来需要调试 transcript 选择结果，应通过 `internal_runtime_view["transcript_slice"]` 或专用 debug 结构承载，而不是与最终 `messages` 并列

它应成为模型调用前的最后装配层。

### 7.4 `QueryLoop`

职责应进一步收紧：

- 推进单次 run 的 `while True`
- 消费 `ModelInputView`
- 调模型
- 执行工具
- 更新 `RunState`
- 返回结构化 `QueryResult`

不再依赖：

- “只要 append 到 transcript，下一轮模型自然能看见正确运行时状态”

### 7.5 `ToolRuntime`

继续承担 runtime control plane 的工具执行角色，但输出语义要更结构化。

职责：

- 写入 `SessionState` 的权威 runtime 状态
- 产出 run-scoped overlay / barrier / injected messages / control effects
- 将 transcript 记录与 runtime truth 分离

### 7.6 `SessionStore`

语义上降级为 transcript store。

其职责是：

- 保存事件轨迹
- 提供审计/调试快照

而不是：

- 决定模型当前有效上下文

## 8. ModelInputView 设计

模型输入不再等于完整 transcript，而是一个每轮组装的 `ModelInputView`。

### 8.1 三个输入通道

建议将模型输入拆成三个通道：

1. `system channel`
2. `message channel`
3. `internal-only channel`

### 8.2 `system channel`

适合放高稳定、高约束、高优先级的上下文：

- stable system prompt
- skills catalog
- 环境基线
- runtime context 摘要
- 当前 todo / plan 快照
- active skill 生效内容

这些内容属于“当前运行环境定义”，不是普通对话。

### 8.3 `message channel`

适合放真正的对话与证据轨迹：

- 用户本轮输入
- 最近若干轮 user / assistant
- 必要的 tool result 证据
- compact boundary 后的 transcript slice
- 必要的 compact summary

这里负责“最近发生了什么”，不负责“当前运行时真相是什么”。

### 8.4 `internal-only channel`

只给 runtime 使用，不直接喂给模型：

- 全量 `read_file_state`
- usage / token 明细
- 细粒度 session metadata
- compact restore pointer
- 调试与 replay 辅助信息

当前设计中，它的消费者应明确为：

- `PromptAssembler.build_runtime_context()`：决定哪些 runtime authority 需要渲染到模型
- `MessageViewBuilder.select_transcript_slice()`：决定 transcript 裁剪时哪些历史证据仍值得保留
- `QueryLoop` 的调试与恢复路径：记录当前输入视图是如何组装出来的
- 未来 compact / resume 流程：作为恢复视图时的内部依据

如果一个内部状态既不被这些消费者读取，也不影响视图重建，那么它不应进入 `internal-only channel` 的设计范围。

### 8.5 最终组装方式

建议最终形成：

- `system = stable_system_text + runtime_system_text + query_overlay_text`
- `messages = transcript_slice`
- `tools = filtered_tools`

其中：

- `transcript_slice` 是 builder 内部选择出的 transcript 子集
- `messages` 是最终 API payload；在 Phase 1 中两者相等
- 如果未来 `messages` 需要包含非 transcript 注入内容，应在实现文档中单独引入新概念，而不是复用当前定义

这一定义明确了：

- system channel 尽量重渲染，不做 compact
- message channel 可以裁剪、预算、摘要
- internal-only channel 不参与 compact，只参与重建

### 8.6 `runtime_system_text` 的渲染顺序和预算

Phase 1 应采用固定渲染顺序，而不是把所有 runtime 信息随意拼接。

建议顺序：

1. active skills
2. todo / plan snapshot
3. file runtime summary
4. capability runtime summary
5. session runtime metadata summary

Phase 1 预算建议使用字符预算，而不是 token 预算，原因是当前 `harness` 还没有在 prompt assembly 层提供稳定 token 估算器。
这些数字是初始估计，参考 Claude Code compact attachment 的 token 预算，并按粗略的 `1 token ~= 4 chars` 换算后再做保守收缩；后续应根据真实使用情况校准。

推荐初始预算：

- active skills: 16_000 chars
- todo / plan: 4_000 chars
- file runtime: 12_000 chars
- capability runtime: 2_000 chars
- session metadata summary: 2_000 chars

裁剪原则：

- skills 不做整块删除，优先保留所有已激活 skill 的头部说明，再裁剪 reference bodies
- todo / plan 优先完整保留
- file runtime 按最近读取顺序保留，优先完整读取记录，超预算时丢弃最旧项
- capability / metadata 只保留摘要，不原样展开大型结构

这些数字是 Phase 1 的实现起点，不是长期冻结常量。

### 8.7 `transcript_slice` 的选择策略

Phase 1 需要一个明确、可测试的选择策略，而不是“带一点最近消息”。

建议按以下顺序选择：

1. 如果未来存在 compact boundary，则只从最后一个 boundary 之后开始选
2. 始终保留本轮用户输入
3. 按 API round 或 assistant/tool result 组从近到远回溯
4. 满足字符预算后停止
5. 对超大的 tool result 优先做 message-level budget，避免挤占全部 transcript 预算

Phase 1 的默认预算建议：

- transcript slice 总预算：24_000 chars
- 单条 tool result 预算：6_000 chars

这个策略与 Claude Code 的 `getMessagesAfterCompactBoundary()` 和 tool result budget 是同类思想，但在 `harness` 中先用更简单的字符预算落地。

## 9. 直接切换策略

本设计不采用平滑双轨迁移，而采用一次性切换。

### 9.1 切换原则

从某个提交开始，模型输入只允许来自：

- stable context
- runtime context
- query overlay
- transcript slice

不再允许任何模块默认把完整 `conversation_messages` 当作模型输入真相源。

### 9.2 明确废弃的旧假设

以下假设在新架构中应被直接删除：

- append 一条 system message 就等于 runtime 生效
- tool result 进入历史后，模型会自然领会后续状态
- `conversation_messages` 就是 prompt view

### 9.3 推荐切口

这次直接切换的切口建议放在：

- [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py)
- [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py)

原因：

- `PromptAssembler` 是上下文渲染边界
- `MessageViewBuilder` 是模型输入装配边界
- `QueryLoop` 只需改为消费新的视图对象，不必自己理解全部 session 细节

### 9.3.1 第一批必须落地的具体工作

推荐依赖顺序是 `1 -> 2 -> 3 -> 4`。

原因：

- 第 1 项先把 skill 生效从 transcript 迁出，建立第一个 runtime authority 渲染闭环
- 第 2、3 项再把 planning/file runtime 接入同一渲染面
- 第 4 项最后切断 `MessageViewBuilder` 对完整 transcript 的直接复制路径

如果要证明切换真正开始发生，第一批工作必须至少包含：

1. `PromptAssembler.build_active_skill_messages()` 从空实现变为基于 `invoked_skills` 的真实渲染
2. `PromptAssembler.build_runtime_context()` 开始渲染 `todo_state`
3. `PromptAssembler.build_runtime_context()` 开始渲染 `read_file_state` 的摘要
4. `MessageViewBuilder.build()` 不再直接返回完整 `conversation_messages`

只要这四点还没发生，系统就仍然停留在 message-centric 架构。

### 9.4 新的硬标准

切换完成后，系统应满足：

> 删除任意一段旧 transcript，只要当前 runtime context 仍完整，模型仍能正确延续当前工作状态。

如果做不到，说明 runtime truth 仍残留在消息历史里。

建议将这条标准落成一个明确的集成测试：

- 构造一个 session，包含：
  - skill 激活
  - todo 写入
  - 文件读取
- 删除除用户消息外的大部分 assistant / tool transcript
- 断言重新组装出的 `system` 仍包含：
  - skill 指令
  - todo 快照
  - 文件运行时摘要

## 10. 恢复与 Compact 语义

### 10.1 错误恢复

恢复不应依赖“继续带更多旧历史”，而应依赖“重新组装当前输入视图”。

这意味着：

- 空响应恢复影响 `Query Overlay`
- barrier 后继续影响 `RunState`
- 人工中断后只要 session state 在，就能重建当前输入视图

恢复模型应是：

> session state + run state -> re-assemble view -> continue

### 10.2 Compact

本设计不实现 compact，但规定其未来语义：

- compact 只作用于 transcript
- compact 不修改 runtime context
- compact summary 只是 transcript evidence 的替代，不是运行时真相的替代

未来 compact 更适合表现为：

- transcript 写入 compact boundary
- 将边界前历史折叠为 summary event
- 保留最近必要证据
- 下轮继续由 runtime context 重渲染 skill / todo / file state / plan

因此 compact 在新架构中只是：

> 对 transcript channel 做预算控制

而不是：

> 对整个 agent 当前状态做不可逆压缩

compact 的具体实现、boundary 结构和 summary event 形状，不在本文展开；需要在后续单独的 compact design 中定义。

## 11. 测试与验收标准

### 11.1 需要建立的测试类型

1. `assembly tests`
   - 给定 `SessionState + RunState`
   - 断言 `ModelInputView` 稳定、可预测

2. `runtime authority tests`
   - skill 激活、todo 更新、文件读取之后
   - 即使删掉部分旧 transcript，下一轮仍能得到正确 runtime context

3. `recovery tests`
   - 空响应、barrier、max turns、人工中断后
   - 系统仍能重新构建视图并继续运行

4. `future compact tests`
   - compact 后 skill 仍生效
   - todo 仍可见
   - file state 仍可参与后续推理

### 11.2 硬验收标准

本设计完成后，必须满足：

1. 模型输入不再直接等于 `conversation_messages`
2. skill 生效不再依赖历史中的 `<skill-runtime>` message 仍然存在
3. todo / file state / session metadata 能在无旧历史回放的情况下被重新渲染
4. compact 未来只需要处理 transcript，而不需要补救 runtime state 丢失

## 12. 结论

这次设计的真正目标不是“先把 compact 做出来”，而是：

> 把 `harness` 从 message-centric prompt assembly 升级为 state-assembled agent runtime。

一旦模型输入的真相源从 transcript 切换为显式状态：

- compact 会自然变安全
- resume / replay 会更可控
- skill / todo / file-state 的行为会更稳定
- QueryLoop、Runtime、PromptAssembler 的职责边界也会更清晰

compact 是这个改造的附加价值，而不是理由本身。
