# Session / Query / Runtime 跟进修复设计

> 日期: 2026-04-15
> 状态: 待审阅
> 关联文档:
> - [`docs/superpowers/specs/2026-04-15-session-query-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md)
> - [`docs/superpowers/plans/2026-04-15-session-query-runtime-implementation.md`](/Users/kino/works/kino/harness/docs/superpowers/plans/2026-04-15-session-query-runtime-implementation.md)

## 背景

`SessionEngine + QueryLoop + ToolRuntime` 重构已基本落地，但 code review 发现 3 个高优先级问题：

1. `QueryLoop` 与 `ToolRuntime` 的运行时契约不一致，导致工具调用路径不可用。
2. 稳定 system prompt、用户 identity、工具/技能说明没有真正进入模型输入，prompt 管线处于断裂状态。
3. `max_turns` 命中后只是追加一条收尾提示，没有真正进入“收尾态”，模型仍可能继续走工具循环。

这 3 个问题不是彼此独立的小 bug，而是同一条执行链上的边界失配：

- Loop 认为 Runtime 返回的是结构化批结果，Runtime 实际返回的是裸列表。
- Session/Prompt 层认为 Query 会使用完整模型输入视图，ModelGateway 实际忽略了 prompt。
- Policy 层认为 `max_turns` 是停止条件，Loop 实际仍把它当成“普通 follow-up”。

如果不一起修正，这个架构会在“看起来分层了”的情况下继续丢失关键行为。

## 修复目标

- 让 `ToolRuntime` 向 `QueryLoop` 暴露稳定、结构化的批结果契约。
- 让 `SessionEngine` 真正拥有稳定 prompt 构建责任，并保证模型调用消费这些上下文。
- 让 `max_turns` 从“建议”升级为 Loop 级别的收尾态。
- 保持已有目录边界不倒退，不把修复重新塞回单个总控文件。

## 非目标

- 不在本次修复中引入新的 compact / hook / streaming tool execution 机制。
- 不在本次修复中重做工具 schema 或工具注册系统。
- 不在本次修复中扩展新的 policy 类型，只修正 `max_turns` 和 prompt/context 主链路。

## 核心修复原则

1. `QueryLoop` 只消费结构化契约，不消费 Runtime 的内部细节。
2. 稳定 prompt 与动态 follow-up 必须进入真实模型请求，不能停留在死代码或未消费参数上。
3. `max_turns` 一旦命中，本轮只能收尾，不能再继续正常工具循环。
4. 稳定 prompt 构建一次，动态 follow-up 每轮生成，两者都必须进入实际模型请求。

## 问题一：`QueryLoop` 与 `ToolRuntime` 契约不一致

## 现状

当前 [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 做了如下假设：

- `tool_runtime.execute_batch(parsed_calls, context=tool_context)` 接受 `context` 关键字参数
- 返回对象具备：
  - `tool_results`
  - `files_modified`
  - `tool_names`

但当前 [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 的实际实现是：

- `execute_batch(self, tool_calls)` 不接受 `context`
- 返回值是 `list[ToolResult]`

这导致：

- 一旦出现真实 `tool_call`，Loop 会在调用 Runtime 时直接抛 `TypeError`
- 即使参数对齐，Loop 后续仍会因访问 `batch.tool_results` 等属性失败

## 目标契约

`QueryLoop` 不应理解 Runtime 的内部数据结构。Runtime 应统一返回结构化批结果：

```python
@dataclass(slots=True)
class ToolBatchResult:
    tool_results: list[dict[str, Any]]
    files_modified: list[str]
    tool_names: list[str]
```

### `tool_results` 的定义

- 每一项都已经是可直接 `store.extend(...)` 的 message-ready `tool_result` 字典
- Loop 不再负责把 `ToolResult` dataclass 转为消息

### `files_modified` 的定义

- Runtime 从 `ToolUseContext.files_modified` 抽取，并去重后返回
- Loop 只负责累积到 `RunState`

### `tool_names` 的定义

- 用于 `TodoTrackingPolicy` 等策略层观察
- Runtime 负责从 `ToolCall.name` 提取并保持与当前批次一致

## 设计变更

### `core/tools/runtime.py`

- 新增 `ToolBatchResult`
- `ToolExecutorRuntime.execute_batch()` 返回 `ToolBatchResult`
- 移除 `context=` 调用参数，继续使用构造注入的 `self._context`
- 新增内部方法负责把 `ToolResult` 映射成 `tool_result` message dict

### `core/query/loop.py`

- 调用签名改为 `batch = tool_runtime.execute_batch(parsed_calls)`
- `store.extend(batch.tool_results)`
- `state.files_modified.extend(batch.files_modified)`
- `policy_runner.after_tool_batch(..., batch)` 只消费结构化批结果

## 结果

修复后，Loop 与 Runtime 的边界将恢复为：

```text
QueryLoop
  -> parse tool_calls
  -> ToolRuntime.execute_batch(parsed_calls)
  -> get ToolBatchResult
  -> append tool_results
  -> continue
```

Loop 不再需要知道 `ToolResult`、线程执行细节或输出截断实现。

## 问题二：Prompt / Context 管线断裂

## 现状

当前实现存在 4 层断裂：

1. [`core/session/engine.py`](/Users/kino/works/kino/harness/core/session/engine.py) 创建了 `PromptAssembler`，但没有触发稳定 prompt 构建。
2. [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py) 的 `build_stable()` / `build_context()` 没有进入主执行路径。
3. [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 只拿到了 `dynamic prompt` 字符串。
4. [`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py) 完全忽略 `prompt` 参数。

这意味着：

- 稳定 system prompt 没进入模型输入
- 用户 identity / 项目 context 没进入模型输入
- tool / skill 稳定说明没进入模型输入
- 新架构下的 prompt 层在运行时几乎没有生效

## 职责分配

### `SessionEngine`

负责：

- 初始化 `SessionState`
- 构建或注入稳定 prompt
- 触发 user identity / project context / framework prompt 的加载
- 确保这些稳定上下文在第一次 query 前已经进入 `conversation_messages`

不负责：

- 每轮动态 follow-up 生成
- 底层模型调用参数拼装

### `PromptAssembler`

负责：

- `build_stable(state) -> str`
- `build_dynamic(state, run_state) -> list[dict[str, Any]] | str`

其中：

- `build_stable()` 返回稳定 system prompt
- `build_dynamic()` 返回本轮动态部分，例如：
  - max-turn 收尾提示
  - recovery follow-up
  - policy 追加的提示

### `MessageViewBuilder`

负责：

- 基于 `SessionState` 产生会话消息视图
- 返回当前轮真正要送给模型的 `messages` 与 `tools`
- 不新增独立的模型输入抽象层

### `ModelGateway`

负责：

- 消费 `messages + tools`
- 不再保留未使用的 `prompt` 参数
- 保证传入的 prompt/context 已经真正出现在 `messages` 中，而不是被静默忽略

## 推荐接法

### 方案

不引入新的 `ModelInput` dataclass，而是把链路修正为：

```python
stable_prompt = prompt_assembler.build_stable(session_state)
if not session_state.conversation_messages or session_state.conversation_messages[0]["role"] != "system":
    store.prepend({"role": "system", "content": stable_prompt})
```

然后在每轮 query 中：

```python
dynamic_messages = prompt_assembler.build_dynamic(session_state, state)
if dynamic_messages:
    store.extend(dynamic_messages)

view = view_builder.build(session_state)
model_resp = model_gateway.call_once(view.messages, tools=view.tools)
```

### `ModelGateway` 修订签名

```python
class ModelGateway:
    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse: ...
```

### `OpenAIClient` 修订要求

- 不再接收一个未使用的 `prompt` 参数
- 只消费已经完整组装好的 `messages`
- `normalize_messages()` 处理的应是“已包含 system prompt 的历史消息”

## 旧逻辑复用

当前原本用于 system / user / project context 加载的逻辑已经迁到 [`core/prompt/system_context.py`](/Users/kino/works/kino/harness/core/prompt/system_context.py)。

本次修复建议：

- 由 `PromptAssembler.build_stable()` 调用 `get_system_context()` / `get_user_context()`
- 将稳定上下文合并为缓存后的 `system_prompt`
- 由 `SessionEngine` 在会话初始化时通过 `append_message()` 或 `prepend` 方式将稳定 prompt 注入 `conversation_messages`
- 不回到旧的插件式 mutation pipeline，但要保留旧逻辑的内容来源

## 结果

修复后，系统上下文链路应为：

```text
SessionEngine
  -> PromptAssembler.build_stable()
  -> SessionState.prompt_cache
  -> inject stable system prompt into conversation_messages
QueryLoop
  -> PromptAssembler.build_dynamic()
  -> MessageViewBuilder.build()
ModelGateway
  -> call_once(messages, tools)
OpenAIClient
  -> actually send messages + tools
```

## 问题三：`max_turns` 没有形成真正的收尾态

## 现状

当前 [`core/policy/max_turns.py`](/Users/kino/works/kino/harness/core/policy/max_turns.py) 只负责返回 `"max_turns"`。

[`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 在命中时：

1. 设置 `state.stop_reason = "max_turns"`
2. 追加一条“请基于当前信息给出最终回复”
3. `continue`

但这仍然允许下一轮：

- 继续带着 `tools` 调模型
- 模型再次发出 `tool_call`
- Loop 继续执行工具

因此 `max_turns` 目前只是提醒，不是停止边界。

## 目标语义

`max_turns` 一旦命中，Loop 进入“收尾调用”模式：

- 下一轮模型调用时 `tools=None`
- 模型若仍尝试返回 `tool_call`，Loop 直接结束，不再执行工具
- 收尾完成后返回 `StopReason.MAX_TURNS`

不引入新的 `finalizing` / `finalize_retries` 状态机字段。

## Loop 规则

### 规则 1：命中 `max_turns` 后禁用工具

在触发 stop policy 后：

```python
state.stop_reason = "max_turns"
store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
```

下一轮调用模型时：

```python
tools = None if state.stop_reason == "max_turns" else view.tools
```

### 规则 2：`max_turns` 后若仍返回 `tool_call`，直接终止

如果 `state.stop_reason == "max_turns"` 且模型仍返回 `tool_calls`：

- 不执行工具
- 不追加新的收尾重试状态
- 直接返回：

```python
QueryResult(
    final_output="",
    stop_reason=StopReason.MAX_TURNS,
    success=False,
    ...
)
```

### 规则 3：收尾文本优先返回

如果 `state.stop_reason == "max_turns"` 且模型给出最终文本，则：

```python
stop_reason = StopReason.MAX_TURNS
```

## Policy 与 Loop 的边界

`MaxTurnsPolicy` 仍只负责“报告已命中上限”。

真正的收尾控制由 `QueryLoop` 执行，原因是：

- 这属于 loop 安全边界
- policy 不应接管主控制流
- tool 禁用与最终终止判断必须在 loop 内完成

## 关键文件改动

### 必改

- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
- [`core/query/state.py`](/Users/kino/works/kino/harness/core/query/state.py)
- [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py)
- [`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py)
- [`core/session/engine.py`](/Users/kino/works/kino/harness/core/session/engine.py)

### 高概率同步修改

- [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py)
- [`core/llm/openai_client.py`](/Users/kino/works/kino/harness/core/llm/openai_client.py)
- [`core/policy/max_turns.py`](/Users/kino/works/kino/harness/core/policy/max_turns.py)
- [`core/policy/todo_tracking.py`](/Users/kino/works/kino/harness/core/policy/todo_tracking.py)
- [`core/session/subagent.py`](/Users/kino/works/kino/harness/core/session/subagent.py)
- [`01_agent_loop.py`](/Users/kino/works/kino/harness/01_agent_loop.py)

## 建议实施顺序

1. 先修 `ToolRuntime` 契约  
   目标：让工具路径可运行，避免 `TypeError` 和批结果错配。

2. 再修 prompt/context 管线  
   目标：让 system prompt、identity、tool/skill 说明重新进入真实模型输入。

3. 最后修 `max_turns` 收尾态  
   目标：让命中上限后只能收尾，不能继续工具循环。

这样做的原因是：

- 第 1 步修好后才能稳定验证工具路径
- 第 2 步修好后才能验证模型行为是否符合预期
- 第 3 步依赖前两步的 Loop 行为已经稳定

## 验收条件

至少满足以下 3 条：

### 1. 工具路径验收

- 模型首次返回 `tool_call`
- `ToolRuntime.execute_batch()` 返回 `ToolBatchResult`
- Loop 能正常回写 `tool_result`
- Loop 能继续到下一轮模型调用
- 不出现 `TypeError`、属性缺失或返回类型错配

### 2. Prompt 管线验收

- `SessionEngine` 初始化后，稳定 prompt 已缓存到 `SessionState`
- 模型真实请求中包含：
  - framework prompt
  - user identity / project context
  - 工具/技能稳定说明
- `PromptAssembler` 的输出不是死代码

### 3. Max Turns 验收

- 达到 `max_turns` 后，下一次模型调用不再带 `tools`
- 模型即使返回 `tool_call`，Loop 也不会执行工具
- 本轮最终返回 `StopReason.MAX_TURNS`

## 最小测试集

建议至少补 3 个测试：

1. `tool_call -> execute_batch -> tool_result 回写 -> 回到 loop 顶部`
2. `SessionEngine` 启动后，模型实际收到稳定 system prompt`
3. `max_turns` 命中后，下一轮模型调用不再携带 tools`

如果只能补最少量测试，优先这 3 个。

## 审阅重点

如果把这份文档交给其他模型审阅，重点让它们检查：

1. `ToolBatchResult` 是否是合适的 runtime 边界
2. `PromptAssembler` 与 `SessionEngine` 之间谁负责稳定 prompt 注入，边界是否清晰
3. `max_turns` 后“直接终止”与“接受最终文本”这两条路径是否完整且无歧义
4. `PromptAssembler` 与 `MessageViewBuilder` 的职责划分是否清晰
5. 子代理路径是否应复用同一套 prompt/build/finalize 语义

## 结论

这次跟进修复不是零散 patch，而是一次边界校正：

- Runtime 要向 Loop 暴露稳定契约
- Prompt 层要真正进入模型请求
- `max_turns` 要成为 Loop 级别的真实停止条件

只有这三件事一起修，`Session / Query / Runtime` 架构才算真正闭环，而且不需要再引入额外过渡抽象。
