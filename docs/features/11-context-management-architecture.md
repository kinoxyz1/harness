# 11: Context Management Architecture

> 这篇讲的是 query-time 上下文管理，不是“怎么把历史消息截短一点”。
>
> 如果你只把它看成 view builder 的一个小优化，很容易把真正的运行时边界看丢。

---

## 先说结论

`ContextManager` 是这条链路里的上下文调度中心。

它做的不是单纯压缩 transcript，而是先把“这轮调用前真正该给模型看的东西”整理好，再交给 `MessageViewBuilder` 去组装视图。

最重要的顺序是：

```text
QueryLoop.run()
  → ContextManager.prepare_for_query()
  → MessageViewBuilder.build()
  → ModelGateway.call_once()
```

也就是说，`ContextManager.prepare_for_query()` 一定发生在 `MessageViewBuilder.build()` 之前。

正常 query 准备阶段的梯子也不是随便排的，而是：

```text
tool_result_budget -> microcompact -> maybe summary_compact
```

前两步是尽量把上下文变轻，第三步是当 token 压力真的上来了就做摘要压缩。

如果模型调用阶段已经因为上下文太长直接报错，`QueryLoop` 才会走 `reactive_recover()`。但它不是另一套独立压缩器，而是再走一次带 breaker 的摘要路径；如果 breaker 已经打开，或者摘要继续失败，这次 reactive recovery 也可能什么都不改。

---

## 为什么不能再把“截断历史”当成 view_builder 的局部问题

如果只盯着 `MessageViewBuilder`，很容易得到一个错误心智模型：

```text
历史太长 → 截掉前面几条 → 继续喂给模型
```

这个做法的问题是，它默认 transcript 就是 runtime truth。可是在这个仓库里，事实不是这样。

`conversation_messages` 只是对话轨迹，不是唯一事实来源。真正的运行时状态还活在：

- `invoked_skills`
- `todo_state`
- `read_file_state`
- `compact_state`

所以上下文管理不能只是“把消息切短”。它必须先决定：

- 哪些内容应该在 transcript 里继续保留
- 哪些内容应该被 rewrite 成更轻的形状
- 哪些事实根本不应该依赖 transcript 保存

这也是为什么 `ContextManager` 不能藏在 view builder 里面。它更像 query-time 的门卫和整理员。

---

## ContextManager 在主链路中的位置

主路径在 `QueryLoop.run()` 里非常清楚：

先补一句容易漏掉的前置步骤：`QueryLoop` 在进入策略注入和上下文管理之前，还会先跑一轮 runtime maintenance。当前这一步做的事很窄：只是在已缓存文件的 mtime 变了，或者文件已经不存在时，把对应的 `read_file_state` 条目失效掉。

1. 先做策略注入
2. 再调用 `context_manager.prepare_for_query(...)`
3. 然后把 `prepared.messages` 交给 `view_builder.build(...)`
4. 最后才是 `model_gateway.call_once(...)`

这意味着 `ContextManager` 处理的是“这一轮模型调用前的工作集”，而 `MessageViewBuilder` 处理的是“把已经整理好的输入变成最终模型视图”。

这里要特别防止另一个误解：`ContextManager` 产出的 working transcript 不是“模型必然完整看到的最终形状”。

`MessageViewBuilder` 还会再做两步独立处理：

- 按 24K 字符目标预算从尾部优先挑一个 transcript 子集，但它不是连续尾段：超预算消息可能被跳过，后面更便宜的旧消息仍可能被保留；最新那条消息一定保留，即使它自己就已经超预算
- 在这个选出来的消息子集里清理较早 assistant 消息的 reasoning；更准确地说，只会剥掉那些较早、而且没有 `tool_calls` 的 assistant 消息上的 reasoning，带 `tool_calls` 的 assistant 消息会原样保留

所以 `ContextManager` 刚写进去的 `compact boundary`、`summary`、`runtime restore`，并不保证会一起到达 API。它们先进入 working transcript，但真正送给模型的仍然要再过 view-builder 的切片和 thinking 清理。

这里的分工很关键：

- `ContextManager` 负责让上下文尽量不爆
- `MessageViewBuilder` 负责把视图拼完整
- `ModelGateway` 负责把最终输入送进协议层

如果顺序反了，view_builder 看到的就不再是“已经压好”的消息，而是原始 transcript。那样 compaction 的价值会被削掉一半。

---

## 四层 compact / recovery 梯子

可以把 `ContextManager` 理解成四层梯子，越往下越重。

### 1. `tool_result_budget`

第一层先处理的是工具结果。

某些 tool result 会非常长，尤其是文件读取、搜索结果、grep 输出。`apply_tool_result_budget(...)` 会优先把这些内容压到一个可控的预算里。

这一步会直接把超预算的 tool result 内容替换成占位文本，写进 working transcript 里的就是被替换后的版本，不再保留原始长输出。

`SessionState.compact_state["tool_result_replacements"]` 会把这个预算阶段选中的占位文本按 `tool_call_id` 记下来，所以后续再次跑 `apply_tool_result_budget(...)` 时，同一个 `tool_call_id` 会继续拿到同样的预算占位文本。

但这个稳定性只保证在 `tool_result_budget` 这一步里。后面的 `microcompact` 仍然可能把同一个 tool result 改写成另一种占位文本。

如果后面又发生了成功的 summary compaction，并且新的 working transcript 被持久写回，那么旧 working transcript 里那份原始长 tool output 也不会再留在会话消息列表里。

### 2. `microcompact`

第二层是时间驱动的微压缩。

`apply_time_based_microcompact(...)` 会按时间和工具类型处理老旧 tool result。它会先找出 assistant 消息里属于 `read_file` / `find` / `grep` / `glob` 的 compactable `tool_call_id`，保留最后 2 个这样的 `tool_call_id`，再把更早、而且已经足够老的对应 tool result 改写成轻量占位文本。

你可以把它理解成：

```text
不是所有旧工具结果都要原样背着走
保留最后 2 个可微压缩的 tool call
更早的重内容可以轻描淡写
```

### 3. `summary_compact`

第三层才是摘要压缩。

当 transcript message 自身的估算压力在本地 pruning 之后仍然接近上下文窗口边界时，`ContextManager` 会调用 `compact_service.summarize_and_compact(...)` 生成新的 working transcript 形状。

这一步不是“给模型看一个摘要就完了”，而是会先把 working transcript 重写成：

```text
compact boundary
summary
kept tail messages
runtime restore messages
```

并且在成功后通过 `SessionStore.replace_working_transcript()` 把新的 working transcript 写回会话内存。

但这还不是 API 侧最终看到的消息形状。下一步 `MessageViewBuilder` 仍然会从这个 working transcript 里按尾部优先规则挑一个大约 24K 字符的消息子集，再清理更早的 reasoning，所以重写出来的这些结构消息只是在会话工作集里可用，不保证会全部进入当次模型调用。

还要注意，proactive summary compaction 看的只是这里这份 transcript message 集合的估算 token 压力，不包含后面才会由 `PromptAssembler` 拼出来的 system / runtime prompt 大小。于是会出现一种情况：本地 pruning 后的 transcript 看起来还没到 summary 阈值，但最终 model-visible input 因为 system/runtime 部分太大，仍然在真正调用 API 时触发 reactive overflow。

### 4. `reactive_recover`

最后一层是反应式恢复入口。

当模型调用真的因为上下文太长直接抛出 `ContextWindowExceededError` 时，`QueryLoop` 会走 `context_manager.reactive_recover(...)`。

这不是日常压缩，而是“已经爆了以后”的救火路径。

但要注意，它内部不是一条独立的 rescue 管线。`reactive_recover()` 仍然调用同一个 `_summarize_with_breaker(...)`，只是把 `keep_last_messages` 设成 2。也就是说：

- breaker 已开时，它会直接记录 `summary_compact_skipped_breaker`
- 摘要再次失败时，它会记录 `summary_compact_failed`
- 只有真的 summary 成功时，才会 rewrite transcript 并写回 store

---

## working transcript rewrite 长什么样

摘要压缩真正做的事，不是删除历史，而是重写 working transcript。

看一个简化例子。

### 压缩前

```text
user: 帮我检查这个项目的状态
assistant: 我先读 README 和几个核心文件
assistant -> tool_calls: read_file, grep, find
tool: README 很长，输出了很多行
assistant: 我发现问题主要在 context 组装
user: 继续看 session 里的上下文管理
assistant -> tool_calls: read_file
tool: core/session/context_manager.py 的完整输出
```

### 压缩后

```text
meta_compact_boundary: reason=summary_compact;summarized_messages=12
meta_compact_summary: 前 12 条消息已经总结为：用户在追查上下文管理...
assistant: 我发现问题主要在 context 组装
tool: [Old tool result content cleared]
user: 继续看 session 里的上下文管理
assistant -> tool_calls: read_file
tool: core/session/context_manager.py 的完整输出
meta_runtime_restore: todo_restore ...
meta_runtime_restore: skills_restore ...
meta_runtime_restore: file_runtime ...
```

这件事的关键不在“少了多少字”，而在“summary 把更早的历史折叠掉以后，最近保留的 assistant/tool batch 仍然是完整的”。

不过这里说的仍然是 working transcript 形状，不是最终必然送进 API 的 message 列表。后面 view-builder 还会再做一次尾部优先挑选，这个结果可能跳过中间某些超预算消息；并且为了 tool_use/tool_result 配对，必要时还会把匹配的 assistant tool_use 回补进结果里，即使这会让总量超过目标预算。

摘要压缩保留了：

- 最新的对话尾巴
- 一段可读的摘要
- 运行时恢复消息

它丢掉的是大块重复性历史，而不是 runtime 真相。

---

## 为什么 compact 后 skill/todo/file truth 不会丢

答案很简单：这些真相本来就不靠 transcript 活着。

但要把“truth survives”和“模型每次都完整看见 truth”分开。前者说的是状态还在 `SessionState`，后者则还要受 final model-visible input 的预算约束。

### skill truth

已激活的 skill 在 `SessionState.invoked_skills` 里，不是藏在旧 assistant 消息里。

所以 transcript 就算被 rewrite，下一轮 `PromptAssembler` 还是会尝试把当前 skill 状态重新渲染进 system prompt。

### todo truth

todo 的当前状态在 `SessionState.todo_state` 里。

摘要压缩之后，系统不是去翻旧消息找“刚才做到哪一步”，而是直接读 state，再生成恢复消息和 runtime context。

### file truth

文件认知在 `SessionState.read_file_state` 里。

这意味着即便旧的 file tool result 被压掉，系统仍然知道：

- 哪些文件读过
- 读到了哪一段
- 是否是 full read

但这里也不能把“模型总能重新看到全部文件状态”说得太满。

- summary compaction 生成的 runtime restore，只会带上最近 3 个 `read_file_state` 条目
- `PromptAssembler` 另外渲染出来的 `<file-runtime>` 也有单独预算，按新近顺序尽量塞，超出预算就截断

再加一层限制：`PromptAssembler.build_runtime_context(...)` 最后还会把整个 runtime context 按总预算截断。

所以所谓“runtime truth survives”，准确说是：skill / todo / file 真相仍然保存在 `SessionState` 里，并且系统会尽量把它们重新渲染进 final model-visible input；但它并不保证每次模型调用都能完整看到全部 skill、todo、file 状态。

---

## breaker / overflow / observability

这里最容易误解的有四件事。

### 1. summary breaker 不是永久锁死的

`ContextManager` 里的 summary breaker 只有在连续失败达到阈值后才会打开，而且它受 `summary_compact_cooldown_until` 控制。

这说明它不是一个永远 latched 的熔断器，而是一个“带冷却时间的保护门”。

只要冷却时间过去，breaker 就会自然恢复，不需要重启会话。

### 2. overflow recovery 是单次受保护的

`QueryLoop` 遇到上下文窗口超限时，只允许尝试一次 reactive recovery。

这件事靠的是 `RunState.reactive_recovery_attempted` 这层单 guard：

```text
第一次 ContextWindowExceededError → reactive_recover()
第二次还超 → 直接抛出
```

所以它不是无限重试，也不是“遇到超限就一直压到成功为止”。

而且这一次 recovery 也不保证真的会改写 transcript，因为它还要经过前面同一个 summary breaker 和同一个摘要调用。

### 3. overflow 分类是 typed 的，不是字符串碰运气

`QueryLoop` 最终拿到的是一个类型化的 `ContextWindowExceededError`，所以它可以把“上下文爆了”这件事和普通模型失败分开处理。

但这个类型边界的前端，当前仍然是 provider-specific 的错误文本匹配：`AnthropicClient` 先识别底层 API 报错里那些 context-window 语义，再把它提升成 `ContextWindowExceededError`。

换句话说，reactive recovery 依赖的是明确的上层类型边界，但这个类型边界的来源，仍然受底层 provider 检测逻辑约束。

### 4. observability 要看两层

`ContextManager` 会把这轮处理过程写进：

- `session_state.compact_state["last_compact_observability"]`
- `run_state.context_observability`

同时，模型返回的 `prompt_tokens` 会回写到 `session_state.compact_state["last_prompt_tokens"]`。下一轮开始时，`calibrated_input_tokens(...)` 只是在 pruning 之前把它和本轮估算值取 `max(...)`，作为 `before_tokens` 这类观测字段里的保守 carry-forward。

真正决定要不要触发 `summary_compact` 的，不是这个 carry-forward 值，而是 `tool_result_budget` 和 `microcompact` 跑完之后重新计算出来的 `post_pruning_tokens`。

典型内容包括：

- `steps`
- `before_tokens`
- `after_tokens`

这让你不只是知道“上下文变短了”，还知道到底是哪一层梯子生效了。

---

## 最容易踩的坑

### 把 summary compaction 当成“丢历史”

错。它是 rewrite，不是裸删。

### 把 transcript 当成唯一真相

错。skill、todo、file 状态都在 transcript 之外。

### 以为 `MessageViewBuilder.build()` 先于 `prepare_for_query()`

错。顺序正好相反，`ContextManager.prepare_for_query()` 在前，`MessageViewBuilder.build()` 在后。

### 以为 breaker 会一直卡死 summary compaction

错。它有冷却时间，过了会自己恢复。

### 以为 reactive recovery 可以无限试

错。它是单 guard 的，只给一次反应式修复机会。

### 以为 reactive recovery 一定能绕过 summary breaker

错。它复用的还是同一个 `_summarize_with_breaker(...)`。

---

## 关键文件索引

- `core/session/context_manager.py`：query-time 上下文梯子，负责预算、压缩、恢复、观测
- `core/session/compact_service.py`：真正执行工具结果预算、微压缩、摘要重写
- `core/session/store.py`：`replace_working_transcript()` 的正式写入口
- `core/session/transcript_rewriter.py`：构造 compact boundary、summary、runtime restore 结构
- `core/session/view_builder.py`：从 working transcript 再挑一个 24K 目标预算的尾部优先消息子集，并清理较早 reasoning
- `core/prompt/assembler.py`：运行时 system prompt 的 skill/todo/file 渲染与文件预算控制
- `core/session/state.py`：`compact_state`、todo、skill、file 等 runtime truth 的存放地

---

## 一句话记住

**ContextManager 不是“截断历史”的辅助工具，而是把 transcript、runtime truth、token 压力和 overflow recovery 串成一条可观察、可恢复、可重写的 query-time 管线。**
