# Streaming Query Runtime 设计

> 日期：2026-05-13  
> 状态：Draft  
> 目标：将当前批响应式 LLM 交互改造为流式 runtime，同时保持 `QueryLoop.run()` 作为唯一主循环，不破坏近期刚稳定下来的 `task_plan / todo / subagent` 顺序约束。

---

## 1. 背景

当前 harness 的主链路是典型的“批响应”架构：

1. [`core/llm/anthropic_client.py`](/Users/kino/works/kino/harness/core/llm/anthropic_client.py) 同步等待 Anthropic 完整返回
2. [`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py) 通过 `ModelGateway.call_once()` 返回单个 `ModelResponse`
3. [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 在一次模型调用结束后，才统一展示 thinking、assistant 文本、tool 调用结果
4. [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py) 只有 batch 风格的 `show_thinking()` / `show_assistant()` / `show_tool_call()` / `show_tool_result()`

这导致四个直接问题：

1. 长请求期间用户只能看到 `AnthropicClient` 自己打印的 `正在思考... Ns`，看不到真实内容流。
2. thinking 和 reply 只有在本轮 API 调用结束后才出现，用户体感仍然是“卡住再一次性出来”。
3. tool 与 subagent 虽然局部有事件桥接，但主线程展示仍然以“事后回放”为主，不是实时进展。
4. 输出 ownership 分散在 LLM client、query loop、tool runtime、renderer 多层，后续继续做流式很容易把顺序和状态搞乱。

用户这次的目标不是“只把最终回复流出来”，而是同时支持：

- 主 assistant 文本实时流式展示
- thinking 实时流式展示，且颜色浅于正文
- tool 进度实时展示
- subagent 进度实时展示
- 主 agent 必须流式
- tool / subagent 允许分层降级，但要保持功能可用

---

## 2. 设计目标

## 2.1 目标

1. 保持 `QueryLoop.run()` 仍然是唯一主循环。
2. 为模型、工具、子代理建立统一的事件协议，而不是在 UI 层拼补丁。
3. 让 renderer 按事件增量渲染，而不是只接收完整块。
4. 保留“最终聚合为 `ModelResponse` / `QueryResult`”的兼容路径，避免一次性打碎现有状态管理。
5. 主 agent 输出必须是实时流。
6. tool / subagent 支持“实时流”与“批量回放”两种来源，并在事件中显式标记。
7. 让取消、超时、504、空响应恢复、max-turns、thinking 持久化等既有机制继续可用。

## 2.2 非目标

1. 本设计不把整个 session/runtime 改造成全局 async event bus。
2. 本设计不要求首版就做到所有 provider 的原生流式兼容。
3. 本设计不要求 tool 执行内部全部变成 token 级别流式，只要求 runtime 级事件实时可见。
4. 本设计不改写 `task_plan` / `TaskState` / policy gating 的权威关系。

---

## 3. 总体方案

采用“统一事件协议 + 流式 QueryLoop + 增量 Renderer + 最终结果聚合”的方案。

核心判断：

1. 不采用“只在 UI 外面补 callback”的最小方案，因为那样 tool/subagent 仍然只能伪实时。
2. 不采用“整套 runtime 全异步事件驱动”的激进方案，因为它会显著放大近期 runtime 顺序问题的回归风险。
3. 参考 Claude Code 的 `query(): AsyncGenerator[...]` 模式，将流式能力收敛到 query 主循环，同时保留最终 assistant message / result message 的完整落盘与状态聚合。

新的顶层链路：

```text
SessionEngine
  -> QueryLoop.run()
       -> ModelGateway.stream_once(...)
            -> LLM stream events
       -> ToolExecutorRuntime.emit(...)
       -> SubagentRuntime.emit(...)
       -> Renderer.consume(...)
       -> StreamAccumulator.finalize()
       -> QueryResult
```

原则：

1. 所有用户可见输出统一通过 renderer 事件接口发出。
2. `AnthropicClient` 不再直接写 stdout。
3. `QueryLoop` 既消费流式事件，也负责在轮次结束后组装最终 `ModelResponse`。
4. `ToolExecutorRuntime` 和 `SubagentRuntime` 不直接“决定怎么显示”，只发结构化运行事件。
5. “实时流”与“回放流”都走同一协议，只是 `source_mode` 不同。

---

## 4. 事件协议

新增共享协议模块，位置固定为：

- `core/shared/stream_events.py`

定义统一事件族 `StreamEvent`。

## 4.1 事件分类

### A. 模型事件

- `response_start`
- `thinking_delta`
- `content_delta`
- `tool_call_delta`
- `tool_call_ready`
- `response_metadata`
- `response_error`
- `response_completed`

### B. 工具事件

- `tool_batch_start`
- `tool_call_start`
- `tool_call_progress`
- `tool_call_result`
- `tool_batch_completed`

### C. 子代理事件

- `subagent_start`
- `subagent_thinking_delta`
- `subagent_content_delta`
- `subagent_tool_call_start`
- `subagent_tool_call_result`
- `subagent_status`
- `subagent_completed`

### D. 系统事件

- `status`
- `timing`
- `cancelled`
- `recovery_notice`
- `context_governance`

## 4.2 统一字段

所有事件至少包含：

- `type`
- `turn_id`
- `sequence`
- `origin`
- `source_mode`
- `timestamp`

其中：

- `origin`: `model | tool | subagent | system`
- `source_mode`: `live | replayed`

解释：

1. `live` 表示真实实时到达。
2. `replayed` 表示该链路暂不支持实时流，但 runtime 按统一事件格式补发。
3. renderer 不需要知道事件来自哪个 provider，只关心事件种类和 `source_mode`。
4. `sequence` 采用每个 turn 内单调递增的局部序号，每次进入新 turn 从 `1` 重新开始；不定义跨 turn 全局序号。

## 4.3 thinking 与 reply 的边界

thinking 与正文必须是两条独立流，而不是单字段里混写：

- `thinking_delta` 只累加到 thinking buffer
- `content_delta` 只累加到正文 buffer

这样做的原因：

1. UI 需要浅色渲染 thinking，正文保持正常色。
2. 历史持久化时，thinking 是否保留、保留多少、何时裁剪，应与正文分开处理。
3. 后续如果支持“隐藏历史 thinking，仅展示当前 thinking”，协议不用再改。

## 4.4 tool_call 事件边界

tool 调用必须拆成至少两层：

1. `tool_call_ready`
   含模型已经完整决定的工具名、参数、call_id。
2. `tool_call_start` / `tool_call_result`
   含 runtime 实际执行开始与结束。

这样才能区分：

- 模型正在构造工具调用
- runtime 已经开始执行工具

否则用户只会看到“工具突然跑完”，中间没有真实进展。

## 4.5 子代理事件边界

子代理事件不直接复用主线程 message 文本，而是带上：

- `task_id`
- `agent_type`
- `subagent_turn`

子代理内部若支持真实流式，则桥接为：

- `subagent_thinking_delta`
- `subagent_content_delta`

若暂不支持，则至少回放：

- `subagent_status`
- `subagent_tool_call_start`
- `subagent_tool_call_result`
- `subagent_completed`

---

## 5. LLM 层改造

## 5.1 `AnthropicClient`

[`core/llm/anthropic_client.py`](/Users/kino/works/kino/harness/core/llm/anthropic_client.py) 需要拆成两条能力：

1. `call(...) -> LLMResponse`
   保留，用于非 streaming 场景和兼容调用方。
2. `stream(...) -> Iterator[LLMStreamEvent]`
   新增，用于流式主链路。

关键要求：

1. 不再自己用 `sys.stdout.write()` 输出“正在思考... Ns”。
2. 取消检查、超时、504 重试仍保留在 client 层。
3. adaptive thinking fallback 逻辑仍保留在 client 层。
4. 原始 provider event 在 client 内部转成协议无关的内部事件，不把 Anthropic SDK 细节泄漏到 query 层。
5. Anthropic 的实现固定使用 `with client.messages.stream(...) as stream:` 路径，而不是原始 SSE 事件流；`AnthropicClient.stream()` 对外暴露为同步阻塞迭代器，但其内部基于 SDK 的 stream context manager 消费 `MessageStartEvent`、`ContentBlockDeltaEvent`、`MessageDeltaEvent`、`MessageStopEvent` 等事件。
6. 取消检查不在每个字符上执行，而是在“收到一个 SDK stream event 后”以及“心跳状态事件定时器触发时”执行，避免高频无意义轮询。

## 5.2 `ModelGateway`

[`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py) 从单一 `call_once()` 扩展为双通道：

- `call_once(...) -> ModelResponse`
- `stream_once(...) -> Iterator[ModelStreamEvent]`

`stream_once()` 的职责不是只转发 provider event，而是保证：

1. 向上游暴露统一模型事件。
2. 轮次结束时可以被 `StreamAccumulator` 还原出等价的 `ModelResponse`。
3. 对暂不支持流式的 client，可以内部执行 `call_once()` 然后补发 `replayed` 事件。

## 5.3 `ModelResponse` 兼容

[`core/llm/response.py`](/Users/kino/works/kino/harness/core/llm/response.py) 保留，作为：

- conversation message 持久化格式
- recovery / policy / result finalization 的输入

新增 `StreamAccumulator`：

```text
ModelStreamEvent[] -> StreamAccumulator -> ModelResponse
```

累计内容包括：

- `content`
- `reasoning`
- `tool_calls`
- `finish_reason`
- `prompt_tokens`
- `completion_tokens`
- `reasoning_signature`

约束：

1. 只有 `response_completed` 后才能 finalize。
2. 若流中断但已有部分输出，accumulator 必须能给出“部分结果 + 中断原因”。
3. `reasoning_signature` 只在最终完成事件中落定，不要求在 thinking delta 阶段可用。

---

## 6. QueryLoop 改造

[`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 继续保留唯一主循环，但“调用模型”部分从同步取回单结果改成“消费事件流并在本轮末尾 finalize”。

## 6.1 新的模型调用阶段

旧流程：

```text
call_once() -> ModelResponse -> show_thinking/show_assistant -> 分支判断
```

新流程：

```text
stream_once()
  -> for event in stream:
       renderer.consume(event)
       accumulator.consume(event)
  -> model_resp = accumulator.finalize()
  -> store.append(model_resp.to_message())
  -> 分支判断
```

## 6.2 轮次状态

`RunState` 需要新增或派生以下字段：

当前 [`core/query/state.py`](/Users/kino/works/kino/harness/core/query/state.py) 中的 `RunState` 已持有：

- `turn_count`
- `empty_retry_count`
- `stop_reason`
- `last_model_response`
- `tool_calls_executed`
- `files_modified`
- `usage_delta`
- `reactive_recovery_attempted`
- `allowed_tools_override`
- `assistant_turns_since_todo`
- `task_planning_required`

在此基础上新增或派生以下流式字段：

- `current_turn_id`
- `current_response_streaming`
- `current_thinking_visible`
- `current_content_visible`
- `last_stream_sequence`
- `stream_source_mode`

注意：

1. 这些字段只服务于当前轮的流式展示与恢复。
2. 不提升为 session 级权威状态。

## 6.3 工具调用分支

在 `tool_call_ready` 前，`QueryLoop` 不执行工具。

当本轮 finalize 得到 `model_resp.tool_calls` 后：

1. 先根据已累计的正文决定是否展示 assistant 文本。
2. 再把工具批次交给 `ToolExecutorRuntime`。
3. `ToolExecutorRuntime` 在执行过程中实时 emit 事件。
4. 执行完成后，仍生成标准 `tool_result` message 回写 transcript。

换言之：

- transcript 依旧以 message 为权威
- 展示层以 event 为权威

## 6.4 空响应、截断与恢复

原有逻辑必须保留，但触发点后移到 finalize 后：

1. `max_tokens` 截断：仍注入“请继续完成” user message。
2. `ContextWindowExceededError`：仍走 reactive recovery。
3. `RequestCancelledError`：转为 `cancelled` 事件后退出。
4. 空响应恢复：依据 finalize 后的 `ModelResponse` 决策。

关键点：

1. 流式并不改变 recovery 的策略判断。
2. 流式只改变“结果如何到达”和“中间如何展示”。

## 6.5 governor、policy_runner 与 offloader 时序

以下三者的时序在流式化后保持明确不变：

1. `governor.assess()` 仍然只在模型调用前执行，用于输入侧上下文治理。
2. `policy_runner.before_model_call()` 仍然发生在本轮流开始前。
3. `policy_runner.after_tool_batch()` 与 `policy_runner.should_stop()` 仍然发生在工具批次结束后。

补充约束：

1. policy 若依赖 `state.last_model_response`，读取到的仍应是 `StreamAccumulator.finalize()` 后的完整 `ModelResponse`，而不是半成品 delta。
2. v1 不做“流式输出中途由 governor 二次截断”的能力；若 thinking 很长或输出超预期，仍由 provider 的 `max_tokens`、现有 continuation 注入和 recovery 机制处理。
3. `offloader` 仍不介入模型文本 delta；它只作用于工具结果 transcript 回写路径。

---

## 7. Renderer 改造

[`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py) 从 batch API 扩展为事件消费式接口。

## 7.1 接口调整

现有接口保留用于兼容：

- `show_thinking()`
- `show_assistant()`
- `show_tool_call()`
- `show_tool_result()`

新增主接口：

- `begin_stream(turn_id, meta)`
- `consume_event(event)`
- `end_stream(turn_id, result_meta)`

短期内，`consume_event()` 内部可复用旧方法。

## 7.2 终端展示约定

用户确认的形态是：

```text
思考 xxxxxxx
回复 xxxxxxx
```

设计要求：

1. thinking 与 reply 分两行或两段显示，不再单独用大 panel。
2. thinking 用浅色 / dim 样式。
3. reply 用正常 markdown 渲染。
4. tool 与 subagent 事件仍保留现有“`$ Tool(...)` + 结果摘要”风格，但要插入到流时间线中。

## 7.3 渲染缓冲策略

参考 Claude Code，需要引入轻量缓冲，而不是 token 来一条刷一次：

- 文本 delta 固定缓冲窗口：`80ms`
- 非流式结构事件：立即 flush 之前缓存后再输出

原因：

1. 减少终端闪烁与滚动抖动。
2. 保证 `content_delta` 不会把 `tool_call_start` 顺序淹没。
3. 保持后续 remote transport 兼容性。

v1 不做动态调节；`80ms` 同时作为默认配置值。

## 7.4 历史记录与最终渲染

实时流展示不等于不保留最终完整输出。

规则：

1. renderer 在流中只负责实时增量画面。
2. transcript 仍由 `ModelResponse.to_message()` 和 tool_result messages 持久化。
3. 若用户稍后恢复会话，读取的是最终 message，不是原始 delta 日志。

---

## 8. Tool Runtime 改造

[`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 当前最大问题是：

1. 并行只读工具虽然并发执行，但用户侧接近“完成后统一看到结果”。
2. runtime 与 renderer 是直接耦合的 `show_tool_call()` / `show_tool_result()`。

## 8.1 新的事件发射接口

`ToolExecutorRuntime` 新增 `emit` 回调或 `event_sink`，用于发出：

- `tool_batch_start`
- `tool_call_start`
- `tool_call_progress`
- `tool_call_result`
- `tool_batch_completed`

## 8.2 并行工具的顺序

要求区分：

1. 展示顺序
2. transcript 回写顺序

约束：

1. `tool_call_start` 按调度顺序发出。
2. `tool_call_result` 按真实完成顺序实时发出。
3. transcript 中的 `tool_result` message 仍按原始 tool call 顺序回写，避免影响模型后续消费的一致性。

这意味着：

- UI 可以实时看到谁先返回
- 模型输入仍保持稳定的原顺序

## 8.3 工具结果截断与 offloader

并行工具的展示顺序与 transcript 写回顺序分离，但工具结果大小控制逻辑不变。

约束：

1. [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 中现有 `_truncate_first_message()` 仍按当前逻辑作用于工具结果消息。
2. [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 中现有 `offloader.maybe_persist(...)` 仍在 transcript 写回阶段执行。
3. `tool_call_result` 事件面向展示层，应携带 preview-safe 内容，而不是绕过截断直接把完整超大结果灌给 renderer。
4. 工具结果是否被截断、是否被 offload，不应受“哪个工具先完成”的展示顺序影响。

## 8.4 写工具与只读工具

写工具仍保持串行独占，不因为流式改造而放宽。

流式只影响：

- 何时展示开始
- 何时展示结果
- 是否能在执行中发 progress

不影响：

- 并发安全规则
- `allowed_tools_override`
- session/run update 的应用顺序

---

## 9. Subagent Runtime 改造

[`core/session/subagent.py`](/Users/kino/works/kino/harness/core/session/subagent.py) 已经有 `SubagentBridgeRenderer` 和 `emit` 回调雏形，但现在桥接的仍是 batch 风格事件。

## 9.1 子代理流式原则

首版采用分层策略：

1. 主 agent：必须真实流式。
2. 子代理：优先真实流式；若子代理内部仍是 batch，则桥接为 `replayed` 事件。

这样做的原因：

1. 用户已经明确接受分层降级。
2. 主线程体验收益最大。
3. 可避免首版就把所有嵌套 session engine 一并重构。

## 9.2 桥接协议

`SubagentBridgeRenderer` 废弃“把完整文本一次性 emit”的方式，改为桥接统一事件：

- `subagent_start`
- `subagent_status`
- `subagent_thinking_delta`
- `subagent_content_delta`
- `subagent_tool_call_start`
- `subagent_tool_call_result`
- `subagent_completed`

若子代理内部尚未流式化，则允许：

- 把最终 thinking 当一条 `replayed subagent_thinking_delta`
- 把最终正文当一条 `replayed subagent_content_delta`

## 9.3 主线程展示策略

子代理事件在主线程中不与主 assistant 正文混写，而是作为辅助事件块展示，例如：

```text
[子代理 explore / task-2] 思考 ...
[子代理 explore / task-2] Read(...)
[子代理 explore / task-2] 已完成
```

原因：

1. 保留任务边界
2. 不污染主 assistant 正文
3. 为后续并行 subagent 展示留出扩展空间

---

## 10. 兼容与迁移策略

## 10.1 API 兼容

保留旧接口一段时间：

- `ModelGateway.call_once()`
- `Renderer.show_*()`

新增流式接口：

- `ModelGateway.stream_once()`
- `Renderer.consume_event()`

迁移策略：

1. 先让 `QueryLoop` 走新接口。
2. 其他调用方暂时继续走 `call_once()`。
3. compact service、skill relevance policy 等同步调用场景先不改。

## 10.2 Provider 兼容

`AnthropicClient` 支持真实流式时：

- 发 `source_mode=live`

若未来其他 client 暂不支持：

- `stream_once()` 内部回退到 `call_once()`
- 补发 `source_mode=replayed` 事件

## 10.3 配置开关

新增配置：

- `STREAMING_ENABLED=true|false`
- `STREAMING_THINKING_ENABLED=true|false`
- `STREAMING_RENDER_FLUSH_MS=80`
- `SUBAGENT_STREAMING_MODE=live|replayed|off`
- `PERSIST_PARTIAL_STREAM_OUTPUT=false|true`

规则：

1. 首版默认对主 REPL 打开流式。
2. 测试环境与问题排查时允许关回 batch 模式。
3. 当 `STREAMING_ENABLED=false` 时，由 `QueryLoop.run()` 在主循环内显式走 `call_once()` 分支，而不是让 `ModelGateway.stream_once()` 内部兜底接管控制流。

## 10.4 `display` 参数语义调整

[`core/shared/run_options.py`](/Users/kino/works/kino/harness/core/shared/run_options.py) 中现有 `RunDisplayOptions` 继续保留，但语义收敛为“展示策略提示”，不再允许 LLM client 直接打印。

新语义：

1. `quiet=True` 表示 renderer 不输出用户可见事件，且不发周期性状态提示。
2. `runtime_trace=debug` 仍控制 runtime 级调试事件是否发出。
3. LLM client 只读取 `display` 来决定是否生成低频 `status` 事件，不再直接写 stdout。

---

## 11. 错误处理

## 11.1 网络错误与 504

504 不应再表现为“终端长时间无反馈然后超时”。

新行为：

1. 在模型请求开始时发 `response_start`。
2. 若长时间无 token，但请求仍在进行，可周期性发 `status` 事件，如“模型仍在思考，已等待 12s”。
3. 若最终触发 504 / 502 / 503，发 `response_error`，再按现有重试策略执行。
4. 若重试成功，UI 连续显示，不重置整个轮次。
5. 若重试失败，`QueryResult` 仍以 `API_ERROR` 结束。

## 11.2 取消

取消时：

1. client 层检测 `cancel_check`
2. 发 `cancelled` 事件
3. query loop 返回 `StopReason.ABORTED`

要求：

1. 流式状态必须能被 renderer 清理收尾
2. 不留下“未闭合”的 thinking / reply 行

## 11.3 部分输出

如果中途中断但已收到部分文本：

1. 实时内容可保留在终端上
2. transcript 默认不持久化不完整 assistant message
3. 由 `QueryResult.final_output` 明确返回错误文案

这样可以避免恢复会话时把“半截 thinking / 半截 reply”当成已完成回复。

这是一个明确 tradeoff：

1. 默认值优先保证 transcript 语义稳定，而不是保证“用户看过的每个字符都可恢复”。
2. 若用户更重视中断后保留可见文本，可通过 `PERSIST_PARTIAL_STREAM_OUTPUT=true` 开启可选持久化；开启后需将该 assistant message 标记为 `incomplete`，避免后续恢复时被误认为已完成回复。

---

## 12. 测试策略

## 12.1 单元测试

新增测试覆盖：

1. `AnthropicClient.stream()` 将 provider event 正确映射到内部模型事件
2. `StreamAccumulator` 能从 delta 正确还原 `ModelResponse`
3. `QueryLoop` 在流式路径下仍能正确处理：
   - content only
   - thinking + content
   - tool calls
   - max_tokens continuation
   - cancelled
   - empty response recovery
4. `ToolExecutorRuntime` 的实时事件顺序与 transcript 回写顺序分离
5. `SubagentBridgeRenderer` / subagent event bridge 的 `live` 与 `replayed` 标记

## 12.2 Renderer 测试

为 [`tests/test_runtime_logging.py`](/Users/kino/works/kino/harness/tests/test_runtime_logging.py) 与相关展示测试新增：

1. thinking 浅色显示
2. reply markdown 渲染保持不变
3. delta flush 合并策略
4. tool 事件插入流中时的顺序稳定性
5. 子代理事件前缀与主正文不混写

## 12.3 集成测试

增加 fake streaming gateway：

1. 逐条产出 `thinking_delta`
2. 再产出 `content_delta`
3. 再产出 `tool_call_ready`
4. 验证 QueryLoop 的最终 `QueryResult` 与 transcript 完整性

并发测试必须单列覆盖：

1. 两个以上只读工具并行执行时，`tool_call_start` 按调度顺序发出
2. `tool_call_result` 按真实完成顺序到达
3. transcript 中的 `tool_result` 仍按原始 tool call 顺序写回
4. renderer 在交错事件下不乱序、不丢事件

## 12.4 回归重点

必须重点回归：

1. `task_plan` gate 是否仍然优先生效
2. `todo` 展示是否仍与 `TaskState` 一致
3. subagent 生命周期事件是否比旧版更清晰
4. 取消和长任务是否不再卡死
5. 并行工具事件交错时，展示顺序与 transcript 顺序是否各自稳定

---

## 13. 实施边界

实施拆成三个阶段：

### 阶段 1：主链路流式化

- 引入事件协议
- 实现 `AnthropicClient.stream()`
- 实现 `ModelGateway.stream_once()`
- QueryLoop 消费流式事件
- Renderer 增量展示 thinking / reply

完成标准：

- 主 agent 文本与 thinking 实时流式显示

### 阶段 2：tool runtime 事件化

- ToolExecutorRuntime 发结构化事件
- 并行工具结果实时展示
- transcript 顺序与展示顺序解耦

完成标准：

- 用户可实时感知工具开始、结束和部分进展

### 阶段 3：subagent 分层流式

- 子代理桥接统一协议
- 支持 `live` / `replayed` 两种来源
- 主线程展示子代理过程

完成标准：

- 子代理不再是黑盒

---

## 14. 风险与取舍

### 风险 1：Renderer 抖动和终端污染

若不做缓冲和保序，终端会因高频 delta 刷屏而变得更难读。

取舍：

- 增加小窗口缓冲
- 结构事件强制 flush

### 风险 2：状态与展示耦合再次失控

如果 event 直接取代 transcript 作为权威状态，容易破坏当前 runtime 的稳定性。

取舍：

- event 只负责实时展示
- message / `ModelResponse` 仍负责持久化与后续模型消费

### 风险 3：子代理首版无法完全真实流式

这是接受的，因为用户已经明确允许分层降级。

取舍：

- 主 agent 先做到真实流式
- 子代理先桥接为 `replayed` 也可上线

### 风险 4：测试面扩大

`call_once()` 假实现和 renderer 假对象大量依赖旧接口。

取舍：

- 新增流式 fake，不立即删除旧 fake
- 允许双接口并存一个迁移周期

---

## 15. 结论

本次 streaming 改造应当被视为一次 runtime 边界重构，而不是简单的“把 token 流出来”。

最终决策是：

1. 保留 `QueryLoop.run()` 作为唯一主循环。
2. 在 LLM、tool、subagent 之上建立统一 `StreamEvent` 协议。
3. 让 renderer 以增量事件消费为中心，thinking 与 reply 分轨显示。
4. 主 transcript 仍以最终 message 为权威，不让 event 日志接管状态。
5. 主 agent 必须真实流式，tool / subagent 允许分层降级并显式标注 `source_mode`。

这个方案能同时满足：

- 更好的长任务体验
- 更明确的 tool / subagent 过程可见性
- 更低的 runtime 回归风险
