# Harness 架构图 -- 当前实现 ASCII 版

> 只描述当前代码。
> 使用纯 ASCII 排版，避免终端里中英文混排导致对齐错位。

---

## 1. 顶层调用链

```text
用户输入
   |
   v
+------------------------+
| 01_agent_loop.py       |
| REPL + 依赖装配         |
+-----------+------------+
            |
            +------------------------------+
            |                              |
            v                              v
      /skills 命令                    普通文本输入
            |                              |
            v                              v
+------------------------+        +-------------------------------+
| SessionEngine          |        | SessionEngine                 |
| handle_command()       |        | submit_user_message()         |
+------------------------+        +---------------+---------------+
                                                   |
                                                   +-- bootstrap()
                                                   |    |
                                                   |    v
                                                   |  SkillRegistry.discover()
                                                   |
                                                   +-- SessionStore.append(user)
                                                   |
                                                   v
                                        +----------+-----------+
                                        | QueryLoop.run()      |
                                        | think -> act -> loop |
                                        +----------+-----------+
                                                   |
            +--------------------------------------+--------------------------------------+
            |                                      |                                      |
            v                                      v                                      v
 +----------+-----------+               +----------+-----------+               +----------+-----------+
 | PolicyRunner         |               | MessageViewBuilder   |               | ModelGateway         |
 | before/after/stop    |               | 构建当前输入视图        |               | call_once()          |
 +----------------------+               +----------+-----------+               +----------+-----------+
                                                   |                                      |
                                                   v                                      v
                                        +----------+-----------+               +----------+-----------+
                                        | PromptAssembler      |               | AnthropicClient      |
                                        | stable/runtime ctx   |               | normalize + API call |
                                        +----------------------+               +----------------------+
                                                   |
                                                   v
                                        +----------+-----------+
                                        | ToolExecutorRuntime  |
                                        | execute_batch()      |
                                        +----------+-----------+
                                                   |
                                                   v
                                        +----------+-----------+
                                        | reducers             |
                                        | apply_*_update()     |
                                        +----------------------+
```

这张图对应当前主路径：

- `/skills ...` 直接走 `SessionEngine.handle_command()`
- 普通用户输入走 `SessionEngine.submit_user_message()`
- `QueryLoop` 在每轮里调用 policy、view builder、model gateway 和 tool runtime
- 真正状态落地统一经过 reducer

---

## 2. SessionState 与 RunState

```text
+-----------------------------------------------------------------------------------+
| SessionState  (跨整个 REPL 会话持续存在)                                             |
|-----------------------------------------------------------------------------------|
| conversation_messages   append-only transcript                                    |
| prompt_cache            stable system prompt 缓存                                  |
| skill_catalog           已发现的 skill 元信息                                        |
| skill_events            skill 激活 / reload 事件日志                                |
| invoked_skills          已激活的 skill runtime body                                 |
| skills_revision         stable prompt 缓存修订号                                    |
| read_file_state         文件认知缓存 (内容 + mtime + 分页信息)                        |
| system_prompt_override  额外 stable prompt 后缀 (子代理路径会用到)                    |
| todo_state              当前计划 + 已完成快照                                        |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| RunState  (每次 QueryLoop.run() 都重新创建)                                         |
|-----------------------------------------------------------------------------------|
| turn_count                已完成的工具批次数                                         |
| empty_retry_count         recovery 重试计数                                        |
| stop_reason               例如 "max_turns"                                        |
| last_model_response       最近一次模型响应                                          |
| tool_calls_executed       累计工具调用数                                            |
| files_modified            本次 run 修改过的文件列表                                  |
| usage_delta               预留的用量累积字段                                         |
| transition                TransitionReason 枚举                                   |
| allowed_tools_override    运行时工具白名单                                          |
| model_override            预留模型覆盖                                              |
| effort_override           预留 effort 覆盖                                         |
| assistant_turns_since_todo todo 过期计数                                           |
| last_displayed_todo_items renderer 去重快照                                        |
+-----------------------------------------------------------------------------------+
```

核心原则：

```text
transcript 不是 runtime truth
runtime truth 放在显式状态里
模型输入每轮都从这些状态重新组装
```

---

## 3. 当前 QueryLoop

```text
+==================================================================================+
| QueryLoop.run()                                                                  |
|==================================================================================|
| state = RunState()                                                               |
|                                                                                  |
| while True:                                                                      |
|                                                                                  |
|   (1) maintenance                                                                |
|       collect_runtime_maintenance_updates(session_state)                         |
|       -> 失效过期的 read_file_state                                                |
|                                                                                  |
|   (2) policy before_model_call                                                   |
|       MaxTurnsPolicy        -> 无前置注入                                          |
|       TodoPlanningPolicy    -> 注入 <system-reminder type="todo_stale">           |
|                                                                                  |
|   (3) 构建模型输入                                                                 |
|       view = MessageViewBuilder.build(...)                                       |
|                                                                                  |
|   (4) 调用模型                                                                    |
|       active_tools = None if stop_reason == "max_turns" else view.tools          |
|       model_resp = ModelGateway.call_once(...)                                   |
|                                                                                  |
|   (5) 持久化 assistant 回合                                                        |
|       store.append(model_resp.to_message())                                      |
|                                                                                  |
|   (6) 如果 model_resp.tool_calls 存在:                                            |
|         解析规范化后的 tool calls                                                  |
|         渲染 assistant 文本或 fallback status                                     |
|         batch = ToolExecutorRuntime.execute_batch(...)                           |
|         store.extend(batch.messages)                                             |
|         state.turn_count += 1                                                    |
|         state.tool_calls_executed += len(tool_calls)                             |
|         apply_transition(NEXT_TURN)                                              |
|         必要时更新 todo UI                                                        |
|         after_tool_batch()                                                       |
|         should_stop()?                                                           |
|           如果 max_turns:                                                        |
|             state.stop_reason = "max_turns"                                      |
|             apply_transition(MAX_TURNS_RECOVERY)                                 |
|             store.append(user: "请给出最终答复")                                   |
|         continue                                                                 |
|                                                                                  |
|   (7) 如果 model_resp.has_final_text:                                            |
|         return QueryResult(COMPLETED or MAX_TURNS)                               |
|                                                                                  |
|   (8) 否则 recovery.handle(...)                                                  |
|         finish_reason == "length" -> 注入 "请继续输出"                             |
|         空响应                    -> 注入 "请直接给出最终答复"                        |
|         无法恢复                  -> return QueryResult(EMPTY_RESPONSE)           |
+==================================================================================+
```

---

## 4. 模型实际看到什么

```text
ModelInputView
  |
  +-- system
  |     |
  |     +-- stable context
  |     |     - framework prompt
  |     |     - .harness/context/*.md
  |     |     - <available-skills>
  |     |     - system_prompt_override
  |     |     - 按 skills_revision + prompt digest 缓存
  |     |
  |     +-- runtime context
  |     |     - <environment>
  |     |     - <active-skills>
  |     |     - <todo-state>
  |     |     - <file-runtime>
  |     |
  |     +-- query overlay
  |           - 当前为空的预留钩子
  |
  +-- messages
  |     |
  |     +-- 只是一段 transcript slice
  |     +-- 按字符预算从后向前贪心截取
  |     +-- 最新消息一定保留
  |     +-- 若保留 tool 消息，会回补匹配的 assistant tool_use
  |     +-- 对更早的 assistant 消息剥离旧 thinking
  |
  +-- tools
  |     |
  |     +-- 全部 builtin schema
  |     +-- 可被 allowed_tools_override 过滤
  |
  +-- internal_runtime_view
        |
        +-- 仅调试快照，不发给模型
```

这里最重要的点是：

- `system` 由 `PromptAssembler` 每轮重建
- `messages` 只是最近一段 transcript，不是完整历史
- `tools` 可以被运行时白名单过滤

---

## 5. Thinking 持久化与裁剪

```text
API response
  -> AnthropicClient._parse_response()
  -> LLMResponse
  -> ModelGateway.call_once()
  -> ModelResponse
  -> ModelResponse.to_message()

如果 LLM_PERSIST_THINKING = true:
  assistant message 会保存:
    - reasoning
    - reasoning_signature

之后:
  normalize_messages()
    -> reasoning 会变回 Anthropic "thinking" block

但是:
  MessageViewBuilder._strip_old_thinking()
    -> 只保留最近 2 条 assistant message 的 reasoning
    -> 更老的 reasoning 字段会从 slice 副本里移除
```

所以当前实现既支持：

- thinking 在会话内持续存在
- 又通过 view builder 防止它无限膨胀

---

## 6. Anthropic 协议归一化

```text
internal messages
  |
  +-- system messages
  |     -> 提取为顶层 "system" 字符串
  |
  +-- assistant messages
  |     -> text block
  |     -> thinking block (如果有 reasoning)
  |     -> tool_use blocks (如果有 tool_calls)
  |
  +-- tool messages
        -> 合并成一条 user message，内部是 tool_result blocks

normalize_messages() 步骤:

  (1) 提取 system 消息
  (2) 转换 assistant/tool 内部格式
  (3) 配对缺失的 tool_result
        - 必要时插入 "(cancelled)" 占位
  (4) 把连续 tool 消息合并成一条 user 消息
  (5) 合并连续同角色消息
        - 最终满足 user/assistant 交替
```

这层的作用是把内部 transcript 变成 Anthropic messages API 可接受的结构。

---

## 7. Tool Runtime

```text
ToolExecutorRuntime.execute_batch(tool_calls)
  |
  +-- partition()
  |     |
  |     +-- readonly calls -> one parallel batch
  |     +-- write calls    -> one serial batch each
  |
  +-- execute
  |     |
  |     +-- each tool call runs in its own thread
  |     +-- debug trace shows internal runtime logs
  |     +-- compact mode shows only minimal status after 2s
  |
  +-- normalize outcomes
  |     |
  |     +-- flatten all tool messages in tool-call order
  |     +-- ensure role="tool" and tool_call_id exist
  |     +-- truncate first message if > MAX_OUTPUT_CHARS
  |
  +-- apply updates
        |
        +-- session_updates -> apply_session_update()
        +-- run_updates     -> apply_run_update()
```

当前统一返回协议：

```text
ToolInvocationOutcome
  - status
  - messages
  - session_updates
  - run_updates
  - error
```

当前 reducers：

```text
SessionUpdateKind
  - INVOKE_SKILL
  - SET_TODO_ITEMS
  - UPSERT_FILE_STATE
  - INVALIDATE_FILE_STATE
  - APPEND_SKILL_EVENT

RunUpdateKind
  - MARK_FILE_MODIFIED
  - NARROW_ALLOWED_TOOLS
  - SET_MODEL_OVERRIDE
  - SET_EFFORT_OVERRIDE
  - RESET_TODO_TURN_COUNTER
```

也就是说：

- 工具返回“更新声明”
- 真正写状态统一走 reducer

---

## 8. 当前 Query Control Plane

这一节说的是：在一次 `QueryLoop.run()` 内，哪些状态不属于“任务内容本身”，而是用来控制循环如何继续、何时收尾、下一轮允许什么工具。

当前代码里的 query control plane 仍然存在，但已经不是旧设计里那种“某个工具返回 barrier，然后 QueryLoop 针对它写特判”的形式，而是收敛成：

- `RunState` 上少量 query-scoped 控制字段
- reducer 风格的统一更新入口
- policy 对提醒与终止的控制

当前仍然存在的核心字段和入口如下：

```text
RunState
  - transition
  - stop_reason
  - allowed_tools_override
  - model_override
  - effort_override

reducers
  - apply_transition()
  - apply_run_update()

policies
  - before_model_call()
  - after_tool_batch()
  - should_stop()
```

可以把当前 control plane 拆成三条线看：

1. 工具通过 `run_updates` 改变下一轮运行条件
2. QueryLoop 自己通过 `transition` 记录本轮为何继续
3. policy 在工具批次后决定是否进入收尾模式

### 8.1 工具如何写入 control plane

当前最直接的入口是工具返回的 `run_updates`：

```text
tool outcomes
  -> run_updates
       |
       +-- NARROW_ALLOWED_TOOLS
       |     -> run_state.allowed_tools_override
       |     -> MessageViewBuilder filters tool schemas next turn
       |
       +-- SET_MODEL_OVERRIDE
       |     -> run_state.model_override
       |
       +-- SET_EFFORT_OVERRIDE
             -> run_state.effort_override
       |
       +-- RESET_TODO_TURN_COUNTER
             -> run_state.assistant_turns_since_todo = 0
```

这里面当前最“真的会影响行为”的是 `NARROW_ALLOWED_TOOLS`：

- 写入位置：`apply_run_update()`
- 读取位置 1：`MessageViewBuilder.build()`
- 读取位置 2：`ToolExecutorRuntime.execute_batch()`

也就是说，它会同时影响两层：

1. 下一轮发给模型的 tools schema 列表
2. 本轮 / 下一轮 runtime 对非法工具调用的拒绝逻辑

所以当前的 tool whitelist 不是只做 prompt 提示，而是真正进入执行层。

### 8.2 QueryLoop 自己推进的 transition

`transition` 是 query 内部状态机标签，不是直接发给模型的 prompt 内容。当前真实流转如下：

```text
normal tool batch success
  -> apply_transition(NEXT_TURN)
  -> empty_retry_count reset

max turns reached after tool batch
  -> apply_transition(MAX_TURNS_RECOVERY)
  -> state.stop_reason = "max_turns"
  -> inject finalization user message
  -> next model call uses tools = None

model returned finish_reason == "length"
  -> recovery.handle(...)
  -> apply_transition(MAX_TOKENS_RECOVERY)
  -> inject "please continue"

model returned empty response
  -> recovery.handle(...)
  -> apply_transition(EMPTY_RESPONSE_RETRY)
  -> empty_retry_count += 1
  -> inject "please give final answer"
```

所以 `transition` 现在主要承担三件事：

- 标记这轮为什么继续
- 驱动 `empty_retry_count` 的维护
- 进入 `internal_runtime_view`，方便调试和测试观察

### 8.3 policy 如何接管终止和提醒

`PolicyRunner` 虽然暴露了三个钩子：

```text
before_model_call()
after_tool_batch()
should_stop()
```

但在当前代码里，真正有行为的是两处：

```text
TodoPlanningPolicy.before_model_call()
  -> if todo exists and plan looks stale
  -> inject <system-reminder type="todo_stale">

MaxTurnsPolicy.should_stop()
  -> if turn_count >= max_turns
  -> return "max_turns"
```

`QueryLoop` 收到 `"max_turns"` 后，不会马上报错退出，而是进入一个明确的 control-plane 分支：

```text
MaxTurnsPolicy.should_stop()
  -> state.stop_reason = "max_turns"
  -> inject finalization user message
  -> next model call uses tools = None
  -> force model to summarize with current information
```

这就是当前“收尾模式”的真正实现位置。

### 8.4 哪些字段现在是“已定义，但还没被完整消费”

这点需要单独说明，否则容易把“字段存在”误读成“已经生效”。

当前状态大致是：

- `allowed_tools_override`
  - 已被真实消费
  - `MessageViewBuilder` 和 `ToolExecutorRuntime` 都会读取

- `stop_reason`
  - 已被真实消费
  - `QueryLoop` 会根据它决定下一轮是否传 `tools`

- `transition`
  - 已被真实消费
  - 用于 recovery / retry 状态推进与内部可观测性

- `model_override`
  - 当前可被写入
  - 但 `ModelGateway.call_once()` 主路径还没有读取它

- `effort_override`
  - 当前可被写入
  - 但当前主路径也还没有把它传给模型层

所以，如果严格只看“当前真的影响行为的 control plane”，最核心的其实是：

- `allowed_tools_override`
- `stop_reason`
- `transition`
- policy 的 stale-todo 提醒与 max-turns 收尾

### 8.5 一句话理解这一节

当前 query control plane 不是消失了，而是从“工具私有信号 + QueryLoop 特判”收敛成了：

- `RunState` 上少量明确字段
- reducer 风格的统一写入口
- policy 驱动的提醒与终止

这也是为什么现在的 `QueryLoop` 比旧式 agent loop 更容易继续扩展，而不会越来越依赖临时 if/else 补丁。

---

## 9. 文件认知流

```text
read_file
  -> reads file text with line numbers
  -> self-pages large files before runtime truncation
  -> writes UPSERT_FILE_STATE

edit_file
  -> requires full prior read_file
  -> rejects partial-read state
  -> rejects stale mtime
  -> writes UPSERT_FILE_STATE + MARK_FILE_MODIFIED

write_file
  -> write or append
  -> writes UPSERT_FILE_STATE + MARK_FILE_MODIFIED

each query turn start
  -> maintenance checks cached file mtimes
  -> mismatched file state becomes INVALIDATE_FILE_STATE
```

关键规则：

```text
edit_file is read-before-write by design
partial read_file is NOT enough
```

---

## 10. Skill 流

```text
bootstrap
  -> SkillRegistry.discover()
  -> skill_catalog + skills_revision
  -> stable context renders <available-skills>

/skills use <id>
  -> SessionEngine.handle_command()
  -> registry.load()
  -> build_invoked_skill_record()
  -> state.invoked_skills[skill_id] = record
  -> no transcript injection

skill tool call
  -> builtin skill.handle()
  -> registry.load()
  -> build_invoked_skill_record()
  -> SessionUpdate(INVOKE_SKILL)
  -> next turn renders <active-skills>
```

skill runtime body 结构：

```text
<skill-runtime>
  <skill id="..." source="local-inline">
    Base directory for this skill: ...
    <instruction>...</instruction>
    <reference-files>...</reference-files>
  </skill>
</skill-runtime>
```

引用文件规则：

```text
if frontmatter declares references:
  load only declared files
  reject paths escaping skill dir

else:
  auto-discover *.md files under the skill directory
  excluding SKILL.md
```

---

## 11. Todo 流

```text
todo tool
  -> validates full replacement payload
  -> requires at most one in_progress
  -> SessionUpdate(SET_TODO_ITEMS)
  -> RunUpdate(RESET_TODO_TURN_COUNTER)

PromptAssembler.build_runtime_context()
  -> renders <todo-state>

TodoPlanningPolicy.before_model_call()
  -> if todo exists and 4 assistant turns passed without todo write
  -> inject <system-reminder type="todo_stale">

renderer
  -> show_progress()
  -> show_current_todo()
  -> show_completion_summary()
```

这说明 Todo 在当前实现里是 runtime state，而不是单纯的 UI 展示层。

---

## 12. 当前默认能力边界

```text
registered builtin tools:

  bash
  edit_file
  find
  read_file
  skill
  todo
  write_file
```

需要特别说明：

```text
core/session/subagent.py exists
but "subagent" is NOT currently a registered builtin tool
for the main REPL path
```

---

## 13. 一句话总结

```text
Harness currently works by storing runtime truth in explicit state,
rebuilding a fresh model view on every turn,
and applying tool-driven state changes through reducer-style update entry points.
```
