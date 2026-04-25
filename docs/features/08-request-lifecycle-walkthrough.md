# 08: 一次请求如何跑完整个运行时

> 前面的 feature 文档主要按“组件”讲。这篇换一种视角：不再拆零件，而是沿着一次真实用户请求，从入口一路走到最终回复。

---

## 你将理解什么

读完这篇，你应该能回答：

1. 用户输入一句话后，最先经过哪些代码
2. 一次 query 里，`SessionState` 和 `RunState` 分别承担什么职责
3. 模型每一轮真正收到的 `system`、`messages`、`tools` 是怎么来的
4. 模型要求调用工具后，工具结果如何回到下一轮
5. 一个 query 最终有哪些退出路径

---

## 先建立总图

把一次普通请求想成下面这条时间线：

```text
用户输入
  → 入口文件分流
  → SessionEngine bootstrap + 追加 user 消息
  → QueryLoop.run() 启动单次 query
  → 每轮：
      maintenance
      policy before_model_call
      MessageViewBuilder.build()
      ModelGateway.call_once()
      有 tool_calls → ToolExecutorRuntime.execute_batch()
      无 tool_calls 但有文本 → 返回结果
      空响应 → recovery
```

关键点只有一句：

**这个运行时不是“把完整历史直接喂给模型直到结束”，而是“每轮从显式状态重建一份当前视图，再决定下一步”。**

---

## 第 0 站：入口文件只做装配和分流

入口在 `01_agent_loop.py`。

它做两件事：

1. 在 `main()` 里把 runtime 需要的组件一次性装起来
2. 在 `handle_input()` 里把用户输入分流成“命令路径”和“普通请求路径”

分流规则很简单：

```text
/skills ...  → engine.handle_command()     # 不进入 QueryLoop
普通文本      → engine.submit_user_message() # 进入完整 think-act 循环
```

所以你要先记住第一条边界：

- `/skills list` 这类命令是会话级命令
- “阅读 README.md 并总结”这类普通请求才会进入 query runtime

---

## 第 1 站：SessionEngine 负责把请求送进运行时

普通请求进入 `SessionEngine.submit_user_message()` 后，会发生 3 步：

```text
1. bootstrap()
2. 追加一条 user message 到 conversation_messages
3. 调用 QueryLoop.run()
```

### 1. bootstrap 并不污染 transcript

`bootstrap()` 的职责是：

- 扫描 `.harness/skills/`
- 发现本地 skill
- 计算 `skills_revision`

但它**不会**往 `conversation_messages` 里写任何 system 消息。

这件事很重要，因为它说明：

- skill 目录的发现是会话状态初始化
- 不是“靠插一条历史消息告诉模型有哪些 skill”

### 2. user message 先进入会话状态

当用户发来一句普通文本，请求会先被追加到：

```text
SessionState.conversation_messages
```

这一点意味着：

- QueryLoop 不是直接拿“当前字符串输入”干活
- 它总是从完整的会话状态出发

### 3. 这里才真正进入单次 query

接下来 `SessionEngine` 把控制权交给 `QueryLoop.run()`。

从这一刻开始，我们进入“单次 query 的内部运行世界”。

---

## 第 2 站：单次 query 一开始，会创建一张新的 RunState

每次 `QueryLoop.run()` 开始时，都会创建一个新的：

```python
state = RunState()
```

这是理解整个系统的关键之一。

### SessionState 和 RunState 的分工

可以把它们想成两层：

```text
SessionState
  跨整个会话持久存在
  保存 conversation_messages / invoked_skills / todo / 文件缓存

RunState
  只活在这一次 QueryLoop.run() 里
  保存 turn_count / stop_reason / transition / files_modified / 本轮覆盖项
```

更直白一点：

- `SessionState` 回答“这次会话目前知道什么”
- `RunState` 回答“这次请求目前跑到了哪里”

如果你把这两个概念混在一起，后面几乎所有设计都会看不清。

---

## 第 3 站：每一轮开始前，运行时会先做静默维护

`while True` 循环的顶部，不是直接调模型，而是先做 maintenance：

```text
collect_runtime_maintenance_updates(session_state)
  → apply_session_update(...)
```

当前这一步主要检查：

- `read_file_state` 里缓存过的文件
- 它们在磁盘上的 `mtime` 是否已经变化

如果文件已经被外部修改，就会发出：

```text
INVALIDATE_FILE_STATE
```

所以这里不是“清缓存优化性能”，而是：

**保证运行时对文件的认知不会静默过期。**

---

## 第 4 站：策略层先插手，再让模型思考

接下来是：

```text
before_messages = policy_runner.before_model_call(session_state, state)
store.extend(before_messages)
```

当前默认策略主要有两个：

- `MaxTurnsPolicy`
- `TodoPlanningPolicy`

### 这一步会注入什么

现在最常见的是 todo stale 提醒：

```text
<system-reminder type="todo_stale">
当前计划可能已过时，请先刷新 todo。
</system-reminder>
```

注意它的形态：

- 它不是改 system prompt
- 它是作为一条普通消息写进 transcript

这说明当前运行时有两种“影响模型下一轮”的渠道：

1. 改显式状态，下一轮重建 `system`
2. 直接注入 follow-up / reminder 消息，进入 `messages`

---

## 第 5 站：真正发给模型的输入，是一份临时重建的视图

接下来 `QueryLoop` 调用：

```text
view = view_builder.build(...)
```

返回的是一个 `ModelInputView`：

```text
system
messages
tools
internal_runtime_view
```

这是本框架最该被记住的对象，因为它就是“模型这一轮真正看到的世界”。

### 1. `system` 从状态重建，不依赖 transcript

`system` 由三层拼出来：

```text
stable
runtime
overlay
```

其中：

- `stable`：框架指令 + `<available-skills>` + 可选 override
- `runtime`：环境信息 + `<active-skills>` + `<todo-state>` + `<file-runtime>`
- `overlay`：当前为空的单轮信号层

这意味着：

- 哪怕早期 assistant/tool 消息被 transcript 截断了
- 只要状态还在，skill/todo/文件认知仍然能重新进入模型视野

### 2. `messages` 不是完整历史，而是 transcript slice

`MessageViewBuilder` 会从 `conversation_messages` 里按预算倒序取最近消息。

规则有 3 条：

1. 最新消息一定要保住
2. 旧 thinking 会清掉大部分内容
3. `tool_result` 需要的配对 `assistant tool_use` 必须补齐

也就是说，`messages` 是压缩后的“近期轨迹”，不是运行时真相。

### 3. `tools` 是这一轮允许模型看到的工具集合

默认情况下，`tools` 就是注册表里所有工具 schema。

如果 `RunState.allowed_tools_override` 不为空，`MessageViewBuilder` 会把工具列表过滤成白名单子集。

所以“模型能不能调某个工具”有两层约束：

1. 这一轮 API 请求里有没有把这个 schema 发给模型
2. 工具运行时收到调用后，会不会再次拒绝

### 4. `internal_runtime_view` 不发给模型

这个字段只给调试和测试用。

里面通常会放：

- 当前激活的 skills
- 当前 todo 项
- 当前 read_file_state 快照
- 最近一次 transition
- 当前 transcript_slice

它的作用是让你观察“这一轮 view 是怎么被组装出来的”，而不是参与推理。

---

## 第 6 站：模型调用后，assistant 消息会先写回 transcript

当 `model_gateway.call_once(...)` 返回后，`QueryLoop` 先做的是：

```text
store.append(model_resp.to_message())
```

这意味着无论模型这轮是：

- 直接给最终文本
- 文字 + tool_calls
- 纯 tool_calls

assistant 消息都会先进入 `conversation_messages`。

这个顺序很关键，因为后面的工具结果需要和它形成完整的“assistant 发起 tool_use，tool 再回结果”的轨迹。

---

## 第 7 站：如果模型要调用工具，会进入 ToolExecutorRuntime

只要 `model_resp.tool_calls` 非空，就会走工具分支。

这个分支里发生 4 件事：

```text
1. 解析 raw tool_calls → ToolCall
2. 执行工具批次
3. 回写 messages / session_updates / run_updates
4. 更新 turn_count、tool_calls_executed、transition
```

### 1. 先把模型返回的调用统一成内部结构

`_parse_tool_calls()` 会把 API 返回的 dict 统一成：

```text
ToolCall(idx, name, call_id, args)
```

这一步的意义是：

- QueryLoop 后面只和内部统一结构打交道
- 不把 API 的原始返回格式散落到 runtime 各处

### 2. ToolExecutorRuntime 先分批，再执行

工具运行时不是简单地 `for call in tool_calls: execute()`。

它会先按工具属性分批：

```text
只读工具   → 可以并行放在同一批
写入工具   → 必须单独串行执行
```

比如：

```text
[read_file(A), read_file(B), todo(...), write_file(...)]
```

会变成：

```text
Batch 1: 并行 [read_file(A), read_file(B)]
Batch 2: 串行 [todo(...)]
Batch 3: 串行 [write_file(...)]
```

### 3. 工具不会直接改状态，只会返回结构化 outcome

每个工具 handler 返回的是：

```text
ToolInvocationOutcome
  - messages
  - session_updates
  - run_updates
  - status
  - error
```

然后由 runtime 统一应用：

```text
run_updates     → apply_run_update(run_state, ...)
session_updates → apply_session_update(session_state, ...)
```

所以状态写入的真实入口仍然只有 reducer。

### 4. 工具结果会重新写回 transcript

工具批次返回后，`batch.messages` 会被：

```text
store.extend(batch.messages)
```

追加进 `conversation_messages`。

于是下一轮模型看到的近期轨迹就变成：

```text
user
assistant(tool_calls)
tool(result)
```

这正是 think-act-observe 再回到 think 的闭环。

---

## 第 8 站：工具批次结束后，QueryLoop 会决定“继续还是收尾”

工具跑完后，`QueryLoop` 还会做 3 件事：

### 1. 递增计数

```text
state.turn_count += 1
state.tool_calls_executed += len(parsed_calls)
```

注意：

- `turn_count` 统计的是“工具批次轮次”
- 不是“模型一共调用了多少次 API”

### 2. 记录 transition

正常工具轮结束后，会写：

```text
TransitionReason.NEXT_TURN
```

这不是装饰字段，而是控制平面的观察点：

- 测试可以验证某条恢复路径是否真的发生
- 日志和调试可以知道“为什么继续到了下一轮”

### 3. 检查 stop policy

当前最重要的是 `max_turns`。

如果刚好达到上限，不会立刻报错退出，而是进入“两阶段收尾”：

```text
1. 把 stop_reason 设为 "max_turns"
2. 注入一条 user 消息，要求模型基于现有信息收尾
3. 下一轮调用模型时，不再传 tools
```

这样做的目标不是“硬切断”，而是：

**先剥夺继续行动的能力，再给模型一次组织最终答案的机会。**

---

## 第 9 站：如果模型不给工具，而是直接给文本，query 就完成了

当 `model_resp.has_final_text` 为真时，`QueryLoop` 直接返回 `QueryResult`。

返回内容通常包括：

- `final_output`
- `stop_reason`
- `turns_used`
- `tool_calls_executed`
- `files_modified`

所以最终返回给用户的并不只是“一段文字”，而是一份带元数据的 query 结果。

---

## 第 10 站：如果模型空响应，会进入 recovery

如果模型既没有有效文本，也没有 tool_calls，就会走 recovery。

当前 recovery 很窄，只处理两种事：

1. 输出被长度截断
2. 模型空响应

它会返回一个 `RecoveryDecision`，告诉 QueryLoop：

```text
要不要继续
要注入哪些 follow-up 消息
这次继续的 transition_reason 是什么
```

然后 query 会再进入下一轮。

也就是说，recovery 不是一个通用错误系统，而是：

**给模型一次受控的、可观察的补答机会。**

---

## 一次完整例子：把所有步骤串起来

假设用户输入：

```text
“阅读 README.md 并总结”
```

一次典型路径可能像这样：

```text
1. REPL 读取输入
2. SessionEngine.bootstrap() 发现 skills
3. SessionEngine 追加 user message
4. QueryLoop 创建新的 RunState
5. maintenance 检查文件缓存是否过期
6. policy before_model_call 注入提醒（通常没有）
7. MessageViewBuilder 组装 system / messages / tools
8. 模型返回 tool_calls=[read_file(README.md)]
9. assistant tool_call 消息写回 transcript
10. ToolExecutorRuntime 执行 read_file
11. tool result 消息写回 transcript
12. turn_count += 1，transition=NEXT_TURN
13. 下一轮重新 build view
14. 模型基于刚读到的结果输出最终总结
15. QueryLoop 返回 QueryResult
16. REPL 打印 final_output
```

注意第 13 步：

不是“模型自动记得上轮发生了什么”，而是 runtime 又重新给它组了一份新的输入视图。

---

## 最容易误解的 6 件事

### 误解 1：QueryLoop 拿的是“完整历史”

不是。它拿的是：

- 从状态重建出来的 `system`
- 从 transcript 切出来的近期 `messages`

### 误解 2：工具执行完会直接改全局状态

不是。工具只返回结构化 outcome，真正改状态的是 reducer。

### 误解 3：skill、todo、文件上下文主要靠历史消息保留

不是。它们主要靠 `SessionState` 保留，然后每轮重新进入运行时上下文。

### 误解 4：`turn_count` 是模型调用次数

不是。它统计的是执行过多少轮工具批次。

### 误解 5：达到 `max_turns` 会立刻报错退出

不是。当前策略会先进入“禁止继续用工具、要求模型收尾”的恢复阶段。

### 误解 6：工具白名单只在调用前过滤一次

不是。当前实现有双保险：

1. `MessageViewBuilder` 过滤本轮发给模型的工具 schema
2. `ToolExecutorRuntime` 在执行时再次检查是否允许

---

## 这一篇看完，下一篇应该读什么

如果你此时最想继续回答的问题是：

- “为什么这套系统能不依赖完整 transcript 还保持运行时真相？”

下一篇请直接读：

`09-state-assembled-runtime.md`

那一篇会把这篇里反复出现的核心思想单独拆出来讲透。
