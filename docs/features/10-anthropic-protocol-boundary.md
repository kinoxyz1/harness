# 10: 协议边界：内部结构如何适配 Anthropic

> 到这一篇，我们要回答一个很实际的问题：既然运行时内部已经有自己的 `system / user / assistant / tool` 结构，为什么还需要单独一层协议适配？为什么不直接把整个运行时都写成 Anthropic 的 block 形状？

---

## 先说结论

当前 harness 明确把“运行时内部结构”和“外部模型协议”分成两层：

```text
运行时内部
  使用便于 QueryLoop / ToolRuntime / reducer 协作的内部消息结构

协议边界
  在真正发给模型之前，统一转换成 Anthropic messages API 需要的形状
```

这个边界由：

```text
core/llm/protocol.py
```

负责。

它的价值不是“多绕一层”，而是：

**把协议耦合限制在边界，而不是让整个运行时都被 API 格式牵着走。**

---

## 为什么内部结构不能直接等于 Anthropic 格式

乍看之下，直接把内部消息做成 Anthropic block 结构似乎更省事。

但真这样做，会立刻带来几个问题。

### 问题 1：QueryLoop 和 ToolRuntime 会被协议细节污染

QueryLoop 当前只需要理解这些内部概念：

- assistant 有没有 `tool_calls`
- tool 执行结果是什么
- tool result 如何写回 transcript

如果内部直接采用 Anthropic block 结构，那么上层逻辑也要开始理解：

- `tool_use` block 的具体形状
- `tool_result` block 是嵌在 user message 里的，不是独立 role
- 连续 user/assistant block 如何合并

这会把本来属于协议边界的复杂性，扩散到整个运行时。

### 问题 2：内部调试和测试会变得更难读

内部当前用的是直观结构：

```text
assistant: content + tool_calls
tool: tool_call_id + content
```

这对 QueryLoop、ToolExecutorRuntime、测试代码都很友好。

如果内部直接变成 Anthropic block 结构，你在调试时看到的将是：

- assistant.content 里混着 text / thinking / tool_use blocks
- tool 结果不再是独立消息，而是 user.content 里的 tool_result blocks

这会让“运行时到底发生了什么”变得更难观察。

### 问题 3：上游逻辑会被模型供应商反向塑形

今天这里是 Anthropic messages API。

如果未来需要兼容别的协议：

- OpenAI 风格工具调用
- 其他兼容层
- 甚至本地模型适配器

你会发现整个 runtime 的核心路径都已经被 Anthropic block 语义绑死了。

所以当前做法本质上是在保护一件事：

**运行时有自己的内部语义，模型协议只是出口格式。**

---

## 当前内部消息长什么样

在协议边界之前，系统内部主要使用这几种消息：

### 1. system

```python
{"role": "system", "content": "..."}
```

这类消息通常来自：

- stable system prompt
- runtime context
- 可选 override

不过注意，当前 `QueryLoop` 真正调用模型时，`system` 已经作为单独参数传出了，不是放在 `messages` 里混着发。

### 2. user

```python
{"role": "user", "content": "..."}
```

普通用户输入、recovery follow-up、policy reminder 都可能以这种内部形状存在。

### 3. assistant

```python
{
  "role": "assistant",
  "content": "...",
  "tool_calls": [...],
  "reasoning": "...",
}
```

这类消息保留了 runtime 最需要的语义：

- 文本回复
- 工具调用意图
- reasoning

### 4. tool

```python
{
  "role": "tool",
  "tool_call_id": "...",
  "content": "...",
}
```

这是当前运行时内部非常重要的一点：

**工具结果在内部是独立 `tool` role 消息。**

不是一开始就被塞成 Anthropic 的 `tool_result` block。

---

## Anthropic 协议真正要求什么

Anthropic messages API 有两个关键约束：

### 1. `system` 是顶层参数

不是普通消息数组里的一项，而是独立字段。

### 2. `messages` 里只有 `user` 和 `assistant`

工具调用相关信息不是这样：

```text
assistant
tool
assistant
tool
```

而是：

- assistant 里嵌 `tool_use` block
- user 里嵌 `tool_result` block

这意味着运行时内部的直观结构，不能原样直接发给 Anthropic。

所以必须有一层翻译器。

---

## `normalize_messages()` 做了哪 5 件事

`core/llm/protocol.py` 里的 `normalize_messages()` 是协议边界的主入口。

它按顺序做 5 步。

### 第 1 步：把所有 system 消息抽成顶层字符串

它先扫描内部消息列表：

```text
role == "system" → 收集到 system_parts
其他消息         → 放进 non_system
```

最后把 system 拼成：

```text
system = "\n\n".join(system_parts)
```

这一步很重要，因为它把“system 不属于普通 messages 列表”这个协议要求收口在边界层实现了。

上层运行时不需要为了这个约束重写自己的内部流程。

### 第 2 步：把内部 assistant 转成 Anthropic assistant blocks

`_convert_assistant()` 会把内部 assistant 消息变成 block 列表：

- reasoning → `thinking` block
- content → `text` block
- tool_calls → `tool_use` blocks

举个内部到外部的映射：

```text
内部:
assistant(content="Let me check", tool_calls=[bash(...)])

Anthropic:
assistant.content = [
  {"type": "text", "text": "Let me check"},
  {"type": "tool_use", ...}
]
```

所以“assistant 想调什么工具”在内部和外部都保留了同一层语义，只是表达形状不同。

### 第 3 步：补齐未闭合 tool_use

`_pair_tool_results()` 做的是一个非常实用的修复：

如果出现：

```text
assistant 发起了 tool_use
但后面没有对应 tool 消息
```

它会补一条占位：

```text
(cancelled)
```

这不是业务逻辑，而是协议完整性修复。

它保证最终送给 Anthropic 的结构里，每个 `tool_use` 都能找到对应的 `tool_result`。

### 第 4 步：把内部 `tool` 消息聚合成 user 里的 `tool_result` blocks

这是最值得记住的一步。

内部明明是：

```text
assistant(tool_calls)
tool
tool
```

转换后会变成：

```text
assistant(content=[tool_use...])
user(content=[tool_result..., tool_result...])
```

也就是说：

**内部 `tool` role 只是 runtime 里的工作形态，真正发给 Anthropic 时会被折叠进 user 消息。**

### 第 5 步：合并连续同角色消息

Anthropic 需要 `user / assistant` 交替。

所以如果转换后出现连续两个 user 或连续两个 assistant，就要再合并一次。

这一步也说明 protocol 层不只是“格式替换”，它还在负责：

**修正最终消息序列的协议合法性。**

---

## 为什么这层边界会反向约束上游运行时

虽然 protocol 层在调用模型之前才执行，但它会倒逼上游一些设计不能乱来。

### 1. transcript slice 不能把 tool 配对切坏

`MessageViewBuilder` 在截取 transcript 时，专门会回溯补齐：

```text
tool_result 对应的 assistant tool_use
```

原因就在这里。

如果上游随便裁掉了 assistant 的 tool_call 发起消息，那么 protocol 层后面就很难合法地还原成 Anthropic 需要的 `tool_use / tool_result` 对。

所以别把这条规则看成“小心翼翼保历史”。

它其实是在维护协议合法性。

### 2. 不能随便删除旧 assistant 消息

`MessageViewBuilder` 对旧 thinking 的清理策略是：

- 清 reasoning
- 但尽量不删会影响 tool 配对的 assistant 消息

这同样不是因为“舍不得删历史”，而是因为协议边界后面还需要这些结构。

### 3. QueryLoop 可以继续使用内部 `tool` role

反过来说，正因为 protocol 层兜底做了 Anthropic 转换，QueryLoop 和 ToolRuntime 才能继续保留更容易实现和调试的内部结构。

这就是边界隔离的收益：

```text
上游保持 runtime 可读性
下游单点承担协议合法性
```

---

## 测试在这里证明了什么

`tests/test_protocol.py` 已经把这层边界最关键的行为钉住了。

### 证明 1：system 会被单独抽取

测试验证了：

- 多条 system 消息会被合并
- 非 system 消息不会混进去

这说明“system 顶层化”不是调用方偶然这么传，而是 protocol 层明确负责的职责。

### 证明 2：assistant tool_calls 会变成 `tool_use`

测试直接断言：

- 内部 `tool_calls`
- 会变成 assistant.content 里的 `tool_use` block

所以不要在上游自己手写 `tool_use` block；当前架构就是要在边界层统一做这件事。

### 证明 3：内部 `tool` 消息会变成 user 里的 `tool_result`

测试明确断言了：

- 最后一条消息是 user
- 其中 content 是 `tool_result` block 列表

这是当前协议边界最核心的结构转换。

### 证明 4：未闭合工具调用会自动补 `(cancelled)`

这说明 protocol 层不是被动搬运，而是主动修补不完整结构。

### 证明 5：不会修改原始消息列表

测试还验证了 `normalize_messages(original)` 后：

```text
original == snapshot
```

也就是说协议层是纯转换，不偷偷反写上游状态。

这对调试和推理都很重要。

---

## 最容易踩的 5 个坑

### 坑 1：想在 QueryLoop 里直接拼 Anthropic block

不要这么做。

那会把协议细节扩散进 runtime 主路径，破坏当前边界。

### 坑 2：以为内部没有必要保留 `tool` role

有必要。

它让 ToolRuntime、store、测试、调试都更直观；`tool_result` block 只是对外发送格式。

### 坑 3：以为 transcript slice 只是上下文优化

不完全是。

它还必须保证后续协议转换仍然合法。

### 坑 4：想把 system 混回普通消息列表

当前架构明确把它抽成顶层参数。

这不是风格问题，是 Anthropic 协议要求。

### 坑 5：看到 thinking block 就想在上游到处处理

thinking 如何进入 Anthropic 格式，当前也是 protocol 层统一处理的一部分。

上游只需要维持内部 `reasoning` 字段的语义。

---

## 这一层边界保护了什么

如果只用一句话总结这篇：

**runtime 内部维护自己的语义完整性，protocol 边界负责把这些语义安全、合法地翻译成 Anthropic API 需要的形状。**

这层边界保护了三件事：

1. QueryLoop 不被协议细节污染
2. ToolRuntime 可以继续使用直观的内部 `tool` 消息
3. 将来如果协议变了，主要修改面仍然集中在边界层

---

## 这篇之后该读什么

如果你现在最关心的是：

- “那我自己想加一个 tool、skill 或 policy，应该从哪里动手？”

下一篇请读：

`11-extension-playbook.md`

因为到了这里，你已经知道：

- 运行时真相放在哪
- 最终协议怎么适配

接下来就是把这两套约束变成实际扩展流程。
