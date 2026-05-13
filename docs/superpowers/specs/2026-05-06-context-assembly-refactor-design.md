# Context Assembly Refactor 设计

> 日期：2026-05-06
> 状态：待评审
> 相关文档：
> - [2026-04-29-context-management-architecture-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-29-context-management-architecture-design.md)
> - [2026-04-19-state-assembled-runtime-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-19-state-assembled-runtime-design.md)
> 当前相关实现：
> - [core/session/view_builder.py](/Users/kino/works/kino/harness/core/session/view_builder.py)
> - [core/session/context_manager.py](/Users/kino/works/kino/harness/core/session/context_manager.py)
> - [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py)
> - [core/query/loop.py](/Users/kino/works/kino/harness/core/query/loop.py)
> Claude Code 参考：
> - `/Users/kino/works/opensource/Claude-Code-doc/src/QueryEngine.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/query.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/queryContext.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/compact.ts`

---

## 1. 摘要

当前 `harness` 已经有两套正确方向的基础能力：

1. `PromptAssembler` 已经能从 `SessionState` 重新渲染 skill、todo、file runtime，而不是完全依赖 transcript。
2. `ContextManager` 已经存在，并且在 `QueryLoop` 中先于 `MessageViewBuilder` 运行。

但最关键的问题仍然没有解决：

> `MessageViewBuilder.build()` 依然把“模型输入组装”建立在 transcript 字符预算切片之上，而不是建立在“变化中的上下文工作集”之上。

这会带来三个直接后果：

1. 长任务中，真正需要持续保真的用户目标，仍可能被最终的 transcript slice 切掉。
2. `ContextManager` 刚刚整理好的 working transcript，后面又被 `MessageViewBuilder` 二次贪心裁剪，导致 compact boundary、summary、runtime restore 不稳定。
3. 运行时上下文和历史对话仍然在最后一步相互竞争预算，而不是被明确分层治理。

本设计的目标是：

1. 让 `MessageViewBuilder` 退化为“最终模型输入装配器”。
2. 让 `ContextManager` 正式接管“变化部分”的预算、裁剪、重写。
3. 让模型输入围绕以下两类信息构造：
   - 永远不变的：system prompt、tool 定义、available skill 定义、未来 mcp 定义
   - 会变化的：历史对话、active skill、todo、读取文件状态、子 Agent 结果

其中本阶段只落地主线程当前已有能力：

1. system prompt
2. tool definitions
3. available skill definitions
4. active skills
5. 历史对话
6. todo 状态
7. 读取文件状态

以下内容本阶段只预留 TODO，不实现：

1. mcp 定义注入重构
2. 子 Agent 结果回流

---

## 2. 要解决什么，不解决什么

### 2.1 要解决的问题

本设计解决以下问题：

1. `MessageViewBuilder` 仍以 `char budget` 为核心，职责中心错误。
2. `ContextManager.prepare_for_query()` 的结果不是最终权威输入，后续还会被二次切片。
3. `PromptAssembler.build_runtime_context()` 现在把不同动态来源拼成一个大字符串，后续无法做分块预算与优先级治理。
4. “读取过哪些文件”虽然已经有 `read_file_state`，但最终是否进入模型仍被一刀式字符串截断控制。
5. 当前系统缺少一个一等的“prepared query context”对象，导致 pipeline 结果只能以零散参数向下游传递。

### 2.2 明确非目标

本设计不包含以下内容：

1. 复刻 Claude Code 的 `contextCollapse`。
2. 复刻 Claude Code 的 cached microcompact / cache edits。
3. 第一阶段引入 session memory、跨进程 compact 恢复、resume 增量恢复。
4. 第一阶段实现 mcp 定义注入重构。
5. 第一阶段实现子 Agent 结果回流。
6. 保留旧接口的兼容适配层。

---

## 3. 现状判断

### 3.1 当前实现的关键问题

当前代码路径是：

```text
QueryLoop.run()
  -> ContextManager.prepare_for_query()
  -> MessageViewBuilder.build()
  -> ModelGateway.call_once()
```

其中 `ContextManager` 已经会先做：

1. `tool_result_budget`
2. `microcompact`
3. `summary_compact`

见 [core/session/context_manager.py](/Users/kino/works/kino/harness/core/session/context_manager.py:102)。

但 `MessageViewBuilder.build()` 仍然会：

1. 从 `conversation_messages` 或 `transcript_messages` 中再次做 `_select_transcript_slice()`
2. 使用 `transcript_char_budget`
3. 再对历史消息做尾部贪心保留

见 [core/session/view_builder.py](/Users/kino/works/kino/harness/core/session/view_builder.py:137) 和 [build()](/Users/kino/works/kino/harness/core/session/view_builder.py:185)。

这意味着当前系统虽然引入了 context pipeline，但真正决定模型最终看到什么的，仍然是 `MessageViewBuilder` 的切片策略。

### 3.2 Claude Code 值得借鉴的点

Claude Code 的骨架不是“最后切一刀”，而是：

1. 先构造稳定前缀：`systemPrompt + userContext + systemContext`
2. 再拿当前 working transcript：`getMessagesAfterCompactBoundary(messages)`
3. 再在 working transcript 上做 query-time pipeline：
   - `applyToolResultBudget`
   - `snipCompactIfNeeded`
   - `microcompactMessages`
   - `contextCollapse`
   - `autoCompactIfNeeded`
4. 最终调用模型时，`systemPrompt`、`messages`、`tools`、`mcpTools` 分轨传输

见：

1. [src/query.ts:365](/Users/kino/works/opensource/Claude-Code-doc/src/query.ts:365)
2. [src/query.ts:379](/Users/kino/works/opensource/Claude-Code-doc/src/query.ts:379)
3. [src/query.ts:449](/Users/kino/works/opensource/Claude-Code-doc/src/query.ts:449)
4. [src/query.ts:660](/Users/kino/works/opensource/Claude-Code-doc/src/query.ts:660)
5. [src/utils/queryContext.ts:31](/Users/kino/works/opensource/Claude-Code-doc/src/utils/queryContext.ts:31)
6. [src/QueryEngine.ts:288](/Users/kino/works/opensource/Claude-Code-doc/src/QueryEngine.ts:288)

它的核心原则是：

1. 稳定信息不是从历史消息里“回忆”出来的。
2. 变化信息不是在最后一步靠 budget slice 决定生死。
3. compact 成功后，后续轮次消费的是新的 working transcript。

---

## 4. 目标架构

### 4.1 总体分层

重构后模型输入分为两大层：

1. `Stable Context`
2. `Mutable Context`

其中：

#### Stable Context

本轮不随 query 内状态变化而变化，或者变化频率极低。

本阶段包含：

1. system prompt
2. tool definitions
3. available skill catalog

未来 TODO：

1. mcp tool definitions

#### Mutable Context

每轮调用前需要重新评估和组装的工作集。

本阶段包含：

1. 历史对话 working transcript
2. 文件读取状态 `read_file_state`
3. todo 状态
4. active skills

未来 TODO：

1. 子 Agent 结果回流

### 4.2 新的数据流

目标数据流改为：

```text
QueryLoop.run()
  -> PromptAssembler.build_stable_context()
  -> PromptAssembler.build_stable_tools()
  -> PromptAssembler.build_runtime_blocks()
  -> ContextManager.prepare_for_query()
       - 处理 working transcript
       - 处理 runtime blocks 预算
       - 生成 prepared query context
  -> MessageViewBuilder.build(prepared_context)
  -> ModelGateway.call_once(view)
```

关键变化：

1. `MessageViewBuilder` 不再读取 `SessionState.conversation_messages`。
2. `MessageViewBuilder` 不再拥有 transcript slice 选择权。
3. `ContextManager` 输出一个一等的 `PreparedQueryContext`。
4. 最终预算是围绕 mutable context 做，而不是围绕单个 transcript char budget 做。

---

## 5. 新对象模型

新增文件建议：

- [core/session/query_context.py](/Users/kino/works/kino/harness/core/session/query_context.py)

### 5.1 `ContextBlock`

```python
from dataclasses import dataclass


@dataclass(slots=True)
class ContextBlock:
    kind: str
    content: str
    required: bool
    token_estimate: int
```

语义：

1. `kind`：`environment` / `active_skills` / `todo_state` / `file_runtime`
2. `required`：是否属于必须保留的动态块
3. `token_estimate`：供 `ContextManager` 做预算决策

设计原因：

1. 不能再把所有动态上下文预先拼成一个大字符串。
2. 需要能对不同 runtime 来源单独排序、保留、裁剪。

### 5.2 `PreparedQueryContext`

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PreparedQueryContext:
    stable_system: str
    stable_tools: list[dict[str, Any]] | None
    runtime_blocks: list[ContextBlock]
    working_transcript: list[dict[str, Any]]
    observability: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, int] = field(default_factory=dict)
```

语义：

1. `stable_system`：稳定 system prompt
2. `stable_tools`：当前会话稳定可见的工具定义
3. `runtime_blocks`：本轮经过预算整理后的动态块
4. `working_transcript`：本轮最终使用的 working transcript
5. `observability`：预算和 compact 观测
6. `budget`：记录本轮各层预算消耗

这是本次重构最核心的新边界。

---

## 6. 组件职责重排

### 6.1 `PromptAssembler`

#### 现状问题

当前 `build_runtime_context()` 直接返回一个字符串，[core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py:172)。

这会让后续无法做到：

1. 文件状态和 todo 分开裁剪
2. 给不同动态块设置优先级
3. 单独观测某类 runtime 信息被裁掉多少

#### 目标职责

`PromptAssembler` 负责“从状态渲染 stable / runtime 两类块”，不负责总体预算治理。

建议接口改为：

```python
class PromptAssembler:
    def build_stable_context(self, state: SessionState, *, project_root: str | None = None) -> str: ...

    def build_stable_tools(
        self,
        state: SessionState,
        *,
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None: ...

    def build_runtime_blocks(
        self,
        state: SessionState,
        *,
        working_dir: str,
    ) -> list[ContextBlock]: ...

    def build_query_overlay_blocks(
        self,
        state: SessionState,
        run_state: RunState,
    ) -> list[ContextBlock]: ...

    def build_internal_runtime_view(
        self,
        state: SessionState,
        run_state: RunState,
    ) -> dict[str, object]: ...
```

#### `build_stable_tools()`

本阶段正式纳入重构范围，但保持语义简单：

1. 输入外部注册的原始 `tools`
2. 基于 `SessionState` 返回当前稳定可用工具列表
3. 允许在这里统一处理：
   - tool schema 排序
   - 未来 deferred tool discoverability
   - 未来 mcp tools 合流

本阶段不在这里做 budget 剪裁，只做“稳定工具通道”的显式建模。

#### `build_runtime_blocks()` 输出规则

本阶段输出以下 block：

1. `environment`
   - `required=True`
2. `active_skills`
   - `required=True`
3. `todo_state`
   - `required=True`
4. `file_runtime`
   - `required=False`

原因：

1. 环境信息、active skill、todo 都是“行为约束”或“当前目标”，应尽量稳定保留。
2. `file_runtime` 很大，应该首先成为可裁剪对象。

#### `build_query_overlay_blocks()`

本阶段可以返回空列表，但保留接口，不再继续返回单个空字符串。

未来 TODO：

1. compact 提示
2. model/effort override
3. 子 Agent 结果摘要提示

### 6.2 `ContextManager`

#### 目标职责

`ContextManager` 正式成为 mutable context 的整理中心。

它的输入：

1. `stable_system`
2. `stable_tools`
3. `runtime_blocks`
4. `overlay_blocks`
5. `working transcript`

它的输出：

1. `PreparedQueryContext`

#### 新接口

```python
class ContextManager:
    def prepare_for_query(
        self,
        *,
        session_state: SessionState,
        run_state: RunState,
        store: SessionStore,
        query_source: str,
        stable_system: str,
        stable_tools: list[dict[str, Any]] | None,
        runtime_blocks: list[ContextBlock],
        overlay_blocks: list[ContextBlock],
    ) -> PreparedQueryContext: ...

    def reactive_recover(
        self,
        *,
        session_state: SessionState,
        run_state: RunState,
        store: SessionStore,
        stable_system: str,
        stable_tools: list[dict[str, Any]] | None,
        runtime_blocks: list[ContextBlock],
        overlay_blocks: list[ContextBlock],
    ) -> PreparedQueryContext: ...
```

#### `prepare_for_query()` 的处理顺序

```text
1. 读取当前 working transcript
2. 估算 stable_system tokens
3. 估算 stable_tools token equivalent
4. 估算 runtime_blocks tokens
5. 计算 mutable headroom
6. 先治理 runtime_blocks 中可裁剪部分
7. 再治理 transcript：
   - tool_result_budget
   - microcompact
   - summary_compact
8. 必要时再次压缩 file_runtime block
9. 产出 PreparedQueryContext
```

#### 预算原则

本设计采用“先保动态块，再保 transcript 尾部”的原则，而不是“所有消息统一切片”。

建议默认预算：

1. `context_window_tokens = 100_000`
2. `reserved_output_tokens = 10_000`
3. `stable_context_reserve = exact estimate`
4. `stable_tools_reserve = estimated schema cost`
5. `required_runtime_reserve = exact estimate`
6. `optional_runtime_soft_cap = 12_000`
7. `transcript_target = 剩余 headroom`

具体计算：

```text
available_input_budget
  = context_window_tokens - reserved_output_tokens

required_fixed_budget
  = stable_system_tokens
  + stable_tools_tokens
  + required_runtime_block_tokens

optional_runtime_budget
  = min(optional_runtime_soft_cap, remaining_headroom_before_transcript)

transcript_budget
  = available_input_budget
  - required_fixed_budget
  - optional_runtime_budget
```

如果 `transcript_budget <= 0`：

1. 优先裁掉 optional runtime blocks
2. 再进入 summary compact
3. 不再让 `MessageViewBuilder` 兜底切片

#### runtime block 裁剪规则

本阶段仅 `file_runtime` 可裁剪：

1. 先减少文件数量
2. 再缩短每个文件 excerpt
3. 如仍超预算，整个 `file_runtime` block 可以被移除

`todo_state`、`active_skills`、`environment` 默认不裁。

### 6.3 `MessageViewBuilder`

#### 目标职责

`MessageViewBuilder` 从“上下文选择器”退回为“最终视图装配器”。

它不再负责：

1. transcript slice 选择
2. char budget 控制
3. working transcript 决策

它只负责：

1. 接收 `PreparedQueryContext`
2. 拼出最终 `system`
3. 对 `working_transcript` 做最少量发送前规范化
4. 过滤 tools
5. 输出 `ModelInputView`

#### 新接口

```python
class MessageViewBuilder:
    def build(
        self,
        prepared: PreparedQueryContext,
        *,
        run_state: RunState,
    ) -> ModelInputView: ...
```

#### 具体变化

删除以下能力：

1. `_content_char_cost()`
2. `_message_char_cost()`
3. `_select_transcript_slice()`
4. `transcript_char_budget` 参数
5. `transcript_messages` 参数

保留并收缩以下能力：

1. `_strip_old_thinking()`
2. 未来 provider-facing pairing repair

最终拼装规则：

```python
system = "\n\n".join(
    [prepared.stable_system, *[block.content for block in prepared.runtime_blocks]]
)

messages = normalize(prepared.working_transcript)
tools = prepared.stable_tools filtered by allowed_tools_override
```

注意：

1. `MessageViewBuilder` 不得再根据预算丢消息。
2. 如果此时模型输入超窗，这是 `ContextManager` 的责任，而不是 `MessageViewBuilder` 再兜底切片。

### 6.4 `QueryLoop`

#### 目标职责

`QueryLoop` 改成显式编排 stable/mutable 两层。

建议主路径变为：

```python
stable_system = prompt_assembler.build_stable_context(...)
stable_tools = prompt_assembler.build_stable_tools(...)
runtime_blocks = prompt_assembler.build_runtime_blocks(...)
overlay_blocks = prompt_assembler.build_query_overlay_blocks(...)

prepared = context_manager.prepare_for_query(
    ...,
    stable_system=stable_system,
    stable_tools=stable_tools,
    runtime_blocks=runtime_blocks,
    overlay_blocks=overlay_blocks,
)

view = view_builder.build(prepared, run_state=state)
```

`reactive_recover()` 路径也要改成同样的输入结构，不能只重新拿 transcript。

---

## 7. compact 与 transcript rewrite 规则

### 7.1 working transcript 仍由 `SessionStore.replace_working_transcript()` 落盘

这一点保持不变：

1. proactive summary compact 成功后替换 working transcript
2. reactive recover 成功后替换 working transcript

见 [core/session/store.py](/Users/kino/works/kino/harness/core/session/store.py:48)。

### 7.2 summary compact 的位置不变，但其触发依据要改

当前 `ContextManager` 的 summary compact 主要看 transcript token 估算。

重构后应该改为看：

```text
stable_system
+ stable_tools
+ runtime_blocks
+ working_transcript
```

也就是：

1. compact trigger 必须基于真正的 active context 估算
2. 不能只看 transcript，自认为还没超预算

### 7.3 reactive recover 仍只做一次

当前单次 guarded recovery 的策略保持不变：

1. proactive 路径失手
2. API 抛 `ContextWindowExceededError`
3. 进入 `reactive_recover()`
4. 最多一次

但 `reactive_recover()` 也必须消费完整的 stable/mutable 结构，而不是只接 `session_state.conversation_messages`

---

## 8. 需要修改的文件

### 新增

1. [core/session/query_context.py](/Users/kino/works/kino/harness/core/session/query_context.py)
   - `ContextBlock`
   - `PreparedQueryContext`

### 修改

1. [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py)
   - 新增 `build_stable_tools()`
   - 新增 `build_runtime_blocks()`
   - 新增 `build_query_overlay_blocks()`
   - 删除 `build_runtime_context()` 的中心地位

2. [core/session/context_manager.py](/Users/kino/works/kino/harness/core/session/context_manager.py)
   - 改为产出 `PreparedQueryContext`
   - 引入 stable/runtime/transcript 联合预算

3. [core/session/view_builder.py](/Users/kino/works/kino/harness/core/session/view_builder.py)
   - 删除 transcript budget slice 逻辑
   - 改为只消费 `PreparedQueryContext`

4. [core/query/loop.py](/Users/kino/works/kino/harness/core/query/loop.py)
   - 显式先构建 stable system / stable tools / runtime blocks
   - 再调用 `ContextManager`
   - 最后调用 `MessageViewBuilder`

5. [core/session/compact_service.py](/Users/kino/works/kino/harness/core/session/compact_service.py)
   - 可能需要新增辅助函数，用于 `file_runtime` block 缩减策略

6. 测试文件
   - `tests/session/`
   - `tests/query/`
   - `tests/prompt/`

---

## 9. 测试策略

### 9.1 `PromptAssembler`

新增测试：

1. `build_runtime_blocks()` 返回 block 列表而不是大字符串
2. `build_stable_tools()` 返回稳定工具列表
3. available skill catalog 继续进入 stable context
4. active skill 进入 runtime blocks
5. `file_runtime` block 可独立出现
6. active skill / todo / environment 的 `required=True`

### 9.2 `ContextManager`

新增测试：

1. required runtime blocks 不会被裁掉
2. stable tools 成本会进入总预算估算
3. `file_runtime` 会先于 transcript summary 被裁减
4. 当总预算不足时，先裁 optional runtime，再做 summary compact
5. summary compact 的触发基于 stable + tools + runtime + transcript 总和
6. reactive recovery 仍然只尝试一次

### 9.3 `MessageViewBuilder`

新增测试：

1. `build()` 不再做 transcript slice
2. 传入多少 `working_transcript`，输出就消费多少
3. stable tools override 仍然正确生效
4. `reasoning_signature` 仍会被移除

### 9.4 集成测试

至少补两条：

1. 长任务场景
   - transcript 很长
   - tool definitions / active skills / todo / file runtime 仍按分层可见
   - 用户最近目标不会因为最后切片而丢失

2. compact 后继续工作场景
   - summary compact 成功
   - 下一轮继续消费新的 working transcript
   - `MessageViewBuilder` 不再二次把 summary/boundary 切掉

---

## 10. 迁移步骤

推荐按以下顺序实施：

1. 新增 `query_context.py`
2. 先改 `PromptAssembler`，让它输出 stable tools 和 runtime blocks
3. 再改 `ContextManager`，让它产出 `PreparedQueryContext`
4. 再改 `MessageViewBuilder`，移除 slice 逻辑
5. 最后改 `QueryLoop` 主路径和 reactive recover 路径
6. 补测试

原因：

1. 先建立新对象边界
2. 再让上下游切换到新边界
3. 避免中间态继续依赖旧 `transcript_char_budget`

---

## 11. TODO

本设计明确留下以下 TODO：

1. stable context 中正式加入 mcp definitions
2. mutable context 中加入 sub-agent result blocks
3. provider-facing API normalization 的结构化抽象
4. 更精确的 token estimator

---

## 12. 结论

这次重构不是“把 `char budget` 换成 `token budget`”。

真正要改的是：

1. `MessageViewBuilder` 不再决定上下文取舍
2. `ContextManager` 正式接管变化部分
3. `PromptAssembler` 不再只输出大字符串，而是输出可治理的 runtime blocks
4. 最终模型输入围绕 stable context + mutable context 构造

这样才能真正解决：

1. 长任务中忘掉用户原始目标
2. compact 后信息重新被 view slice 丢掉
3. 文件状态和历史对话互相挤占预算

这也是当前 `harness` 在现有架构边界下，最接近 Claude Code 正确骨架、同时又足够可落地的改造路径。
