# Session / Query / Runtime 重构设计

> 日期: 2026-04-15
> 状态: 已批准，待实现

## 背景

当前实现把会话管理、单次用户输入触发的 agent run、工具执行、todo 提醒、空响应恢复等职责混在 [`core/agent.py`](/Users/kino/works/kino/harness/core/agent.py) 中。`run()` 在首次 LLM 调用后，一旦遇到工具调用就进入 `_run_tool_loop()`，后续控制流被嵌套循环接管。

这个结构有四个核心问题：

1. 工具调用不是原子操作，而是嵌套 loop 的入口。
2. 会话层与单次 query 层混在一起，用户输入和历史状态协同困难。
3. todo / reminder / 恢复逻辑侵入主控制流，边界不清晰。
4. 目录结构继续把新代码堆到 `core/` 根目录下，模块边界在文件系统层面无法体现。

Claude Code 文档给出的启发是：

- 会话生命周期和单次 query loop 是两层。
- 主循环是扁平的 `while true`。
- 工具执行只负责产生 `tool_result`，然后返回主循环。
- 长期上下文状态可以贯穿一次 query，但不应拥有主控制流。

## 设计目标

- 将系统重组为 `SessionEngine + QueryLoop + ToolRuntime` 三层。
- `QueryLoop.run()` 成为唯一主循环。
- 工具执行恢复为原子行为：执行、回写结果、返回主循环顶部。
- 会话状态与单次 run 状态明确分离。
- 稳定 prompt 组装上移到 session 层，避免每次 query 重新拼装完整系统提示。
- todo / reminder / max-turn 之类策略以 policy 形式挂接，不再侵入 loop 本体。
- 通过目录结构表达架构边界，而不是继续在 `core/` 根目录平铺模块。

## 非目标

- 本设计不要求完整复刻 Claude Code 的 compact、streaming tool execution、hook、swarm 等复杂能力。
- 本设计不要求保留现有 [`core/agent.py`](/Users/kino/works/kino/harness/core/agent.py) 的结构和方法名。
- 本设计不包含具体代码迁移顺序，迁移顺序应在后续 implementation plan 中单独展开。

## 总体架构

### 顶层关系

```text
CLI / REPL
  -> SessionEngine
       -> submit_user_message(...)
       -> QueryLoop.run(...)
            -> MessageViewBuilder
            -> PromptAssembler
            -> ModelGateway.call_once(...)
            -> ToolRuntime.execute_batch(...)
            -> RunPolicies.observe(...)
            -> ResultFinalizer
```

### 核心原则

1. 只有 `QueryLoop.run()` 拥有主 `while True`。
2. 只有 `ModelGateway.call_once()` 执行单次模型调用。
3. 只有 `ToolRuntime.execute_batch()` 执行工具调用。
4. `SessionEngine` 持有完整会话状态，但不直接调模型或执行工具。
5. `RunPolicy` 只能建议追加消息或停止，不能自行调模型或执行工具。

## 状态分层

### `SessionState`

作用：保存整个会话的长期状态。

包含：

- `conversation_messages`
- `prompt_cache`
- `discovered_tools`
- `discovered_skills`
- `read_file_state`
- `session_metadata`
- `usage_totals`
- 未来的 compact boundary / attachment 恢复信息

约束：

- 生命周期覆盖整个会话。
- 不记录本次 run 的临时控制流细节。

### `RunState`

作用：保存一次用户输入触发的一次 query run 的状态。

包含：

- `turn_count`
- `empty_retry_count`
- `stop_reason`
- `last_model_response`
- `tool_calls_executed`
- `files_modified`
- `usage_delta`

约束：

- 只在 `QueryLoop.run()` 内存活。
- run 结束后销毁，不回写为长期状态，除非通过结构化结果提交给 `SessionEngine`。

### `ToolRuntimeContext`

作用：提供工具执行期的共享上下文。

包含：

- `working_dir`
- `messages_view`
- `read_file_state`
- `tool identity`
- `cancellation`
- `files_modified`
- 权限与环境信息

约束：

- 可以跨一次 query 持续存在。
- 不能拥有主控制流，不决定是否继续调模型。

## 目录结构

本次重构后，推荐目录如下：

```text
core/
  session/
    engine.py
    state.py
    store.py
    view_builder.py

  query/
    loop.py
    state.py
    result.py
    recovery.py

  prompt/
    assembler.py
    cache.py
    context.py

  llm/
    client.py
    response.py

  tools/
    registry.py
    runtime.py
    context.py
    builtin/
      bash.py
      read_file.py
      write_file.py
      edit_file.py
      todo.py
      subagent.py

  policy/
    base.py
    max_turns.py
    todo_tracking.py

  ui/
    renderer.py

  shared/
    types.py
    protocol.py
```

### 目录设计理由

- `session/` 表达跨用户多轮对话的生命周期。
- `query/` 表达单次输入触发的 agent run。
- `tools/` 内部同时容纳工具定义和工具执行 runtime，避免“工具 schema 在一处、执行器在另一处”的认知割裂。
- `policy/` 作为横切策略层单独存在，避免 reminder/todo 再次侵入主循环。
- `shared/` 只保留轻量协议和共享类型，严格限制膨胀。

### 明确废弃

- 不再保留总控式 [`core/agent.py`](/Users/kino/works/kino/harness/core/agent.py) 作为架构中心文件。

## 依赖规则

允许依赖：

- `session/` -> `query/`, `prompt/`, `shared/`
- `query/` -> `llm/`, `tools/`, `policy/`, `shared/`
- `prompt/` -> `shared/`
- `llm/` -> `shared/`
- `tools/` -> `shared/`
- `policy/` -> `shared/`, 必要时读取 `query.state`
- `ui/` -> `shared/`

禁止依赖：

- `tools/` -> `query/`
- `tools/runtime.py` -> `llm/`
- `policy/` -> `llm/` 或 `tools/runtime.py`
- `llm/` -> `session/`
- `ui/` -> `query/` 控制逻辑
- `shared/` -> 任何业务目录

## 核心组件职责

### `SessionEngine`

职责：

- 接收用户输入并写入会话消息。
- 持有 `SessionState`。
- 构建本轮 `QueryContext`。
- 管理稳定 prompt 缓存。
- 生成“本轮可供模型消费的消息视图”。
- 在 run 结束后提交 `QueryResult` 回会话状态。

不负责：

- 主循环控制。
- 单次模型调用。
- 工具执行。

### `QueryLoop`

职责：

- 管理一次 run 的唯一主循环。
- 在循环顶部统一执行模型调用。
- 根据模型响应决定继续、执行工具、恢复或结束。
- 维护 `RunState`。
- 产出结构化 `QueryResult`。

不负责：

- 长期会话状态所有权。
- prompt 缓存管理。
- 工具内部执行细节。

### `ModelGateway`

职责：

- 执行单次模型调用。
- 将底层 API 响应标准化为 `ModelResponse`。
- 屏蔽流式/非流式差异。

不负责：

- 控制循环。
- 拼接会话消息历史。
- 工具执行。

### `ToolRuntime`

职责：

- 解析并执行一批 `tool_calls`。
- 负责并发/串行调度。
- 将异常转为结构化 `tool_result`。
- 更新工具上下文中的认知状态，例如 `read_file_state` 和 `files_modified`。

不负责：

- 决定是否继续调模型。
- 决定是否停止本轮 run。

### `RunPolicy`

职责：

- 观察 run 过程中的事件。
- 产生 reminder / follow-up / stop 建议。
- 实现 max-turn 和 todo-tracking 等策略。

不负责：

- 直接调用模型。
- 直接执行工具。
- 修改会话所有权边界。

## Prompt 组织

### Session 层负责稳定部分

由 `SessionEngine` 和 `PromptAssembler` 负责：

- framework prompt
- 用户定制上下文
- 项目上下文
- 工具/技能的稳定说明
- 已发现工具与技能的缓存

这些内容按 session 生命周期缓存，不在每次 query 从头拼装。

### Query 层负责动态部分

由 `QueryLoop` 在每次循环中追加：

- 当前用户输入
- max turns 收尾提示
- continuation / retry follow-up
- policy 产出的 reminder

### 关键约束

- `messages` 是会话事实。
- prompt parts 是模型输入视图。
- prompt 片段不直接回写为历史消息，除非被明确建模为用户/系统 follow-up。

## Query Loop 语义

`QueryLoop.run()` 的定义如下：

- 输入：`QueryContext`
- 输出：`QueryResult`
- 主循环：唯一 `while True`

伪代码：

```python
def run(context: QueryContext) -> QueryResult:
    state = RunState(...)

    while True:
        view = context.message_view_builder.build(context.session_state, state)
        prompt = context.prompt_assembler.build_dynamic(context.session_state, state)

        model_resp = context.model_gateway.call_once(
            view.messages,
            tools=view.tools,
            prompt=prompt,
        )
        state.last_model_response = model_resp
        context.session_state.conversation_messages.append(model_resp.to_message())

        if model_resp.tool_calls:
            batch = context.tool_runtime.execute_batch(
                model_resp.tool_calls,
                context=context.tool_context,
            )
            context.session_state.conversation_messages.extend(batch.tool_results)
            state.turn_count += 1
            state.tool_calls_executed += len(model_resp.tool_calls)
            state.files_modified.extend(batch.files_modified)
            context.policy_runner.after_tool_batch(context, state, batch)
            continue

        if model_resp.has_final_text:
            return finalize(context, state, model_resp)

        decision = context.recovery.handle(model_resp, context, state)
        if decision.should_continue:
            context.session_state.conversation_messages.extend(decision.follow_up_messages)
            continue

        return finalize(context, state, model_resp)
```

### 关键约束

1. 模型调用只发生在循环顶部。
2. 工具执行完成后必须 `continue` 回顶部。
3. `finalize()` 只能收尾，不能隐藏地再跑一层 loop。
4. 恢复策略必须显式返回“继续”或“停止”，不能绕开主循环。

## 错误恢复与横切策略

### 恢复逻辑归属

- 工具执行错误：由 `ToolRuntime` 转为 `tool_result`。
- 模型空响应、截断、prompt-too-long、API 恢复：由 `QueryLoop` / `recovery.py` 处理。
- max turns：由 `RunPolicy` 或 `QueryLoop` 停止条件处理。

原则：

- `ToolRuntime` 产出事实。
- `QueryLoop` 决定控制流。

### Max Turns

`max_turns` 属于 `RunState`，不是工具层或会话层概念。

达到上限时：

1. 由 policy 或 loop 追加一条“基于当前信息给出最终答复”的 follow-up。
2. 再给模型一次收尾机会。
3. 本轮结束，`stop_reason = "max_turns"`。

### Todo / Reminder

todo 跟踪不再硬编码进主循环，而是通过 `RunPolicy` 实现：

- 检查是否调用了 todo 工具。
- 记录连续多少轮未更新计划。
- 必要时生成 follow-up reminder。

该策略只能追加消息建议，不能接管控制流。

### Compact / History Budget

compact 和 history budgeting 归属 `SessionEngine` / `MessageViewBuilder`：

- `SessionEngine` 持有完整历史。
- `MessageViewBuilder` 生成当前 query 的消息视图。
- `QueryLoop` 不直接操作整段历史压缩逻辑。

## 关键接口

```python
class SessionEngine:
    def submit_user_message(self, text: str) -> QueryResult: ...
    def append_message(self, message: dict[str, Any]) -> None: ...
    def build_query_context(self) -> QueryContext: ...
```

```python
class QueryLoop:
    def run(self, context: QueryContext) -> QueryResult: ...
```

```python
class ModelGateway:
    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        prompt: str | None = None,
    ) -> ModelResponse: ...
```

```python
class ToolRuntime:
    def execute_batch(
        self,
        tool_calls: list[ToolCall],
        *,
        context: ToolRuntimeContext,
    ) -> ToolBatchResult: ...
```

```python
class RunPolicy(Protocol):
    def before_model_call(self, context, state) -> list[dict[str, Any]]: ...
    def after_tool_batch(self, context, state, batch) -> list[dict[str, Any]]: ...
    def should_stop(self, context, state) -> str | None: ...
```

## 结构化结果

`QueryResult` 至少包含：

- `final_output`
- `stop_reason`
- `success`
- `turns_used`
- `assistant_messages_added`
- `tool_calls_executed`
- `files_modified`
- `usage_delta`

这样 `SessionEngine` 能把一次 run 视为结构化事件，而不是只拿到一段最终字符串。

## 测试策略

### Session 层

- 多轮用户输入后，消息是否正确持久化。
- prompt cache 是否正确命中。
- message view 是否按预期生成。

### Query 层

- 无工具调用时是否直接完成。
- 有工具调用时是否“执行 -> 回写 -> 返回循环顶部”。
- 空响应恢复是否只通过主循环继续。
- `max_turns` 是否只在 run 级别生效。

### Tool Runtime 层

- 并发/串行批次划分是否正确。
- 工具异常是否稳定转为 `tool_result`。
- 结果顺序是否与原始 `tool_call` 顺序一致。

### Policy 层

- todo reminder 是否只生成 follow-up，不接管控制流。
- max-turn policy 是否只返回停止建议或收尾提示。

### 端到端

- `read -> edit -> summarize`
- `todo -> tool -> final response`
- `subagent tool` 基本回路

## 迁移结果要求

实现完成后，应达到以下结果：

1. 不再存在 `_run_tool_loop()` 之类接管主控制流的嵌套循环。
2. 会话状态和单次 run 状态在代码结构与目录结构上都已分离。
3. prompt 稳定部分在 session 层缓存，动态部分在 query 层生成。
4. todo / reminder / max-turn 以 policy 方式存在，而不是嵌入 loop 本体。
5. 目录结构能让读者直接看出 `session / query / tools / prompt / policy` 的边界。
