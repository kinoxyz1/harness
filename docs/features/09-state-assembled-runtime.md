# 09: 状态组装式运行时

> 这是整个 harness 最值得项目组统一心智模型的一篇。你如果只把它看成“又一个 agent loop”，后面做任何扩展都很容易走偏。

---

## 先说结论

这个运行时的核心不是“保存一段越来越长的聊天记录”，而是：

```text
把运行时真相保存到显式状态里
每一轮调用模型前，再从状态重建当前视图
transcript 只负责提供近期轨迹，不负责充当唯一事实来源
```

这就是所谓的：

**状态组装式运行时**

---

## 你为什么需要这个心智模型

很多 Agent 框架默认采用下面这种思路：

```text
完整历史 = 当前事实
只要历史够长，模型总能“记住”之前发生过什么
```

这个思路在简单 demo 里没问题，但很快会撞上三个现实限制：

1. 对话会越来越长，上下文窗口迟早不够
2. 旧消息必须被裁切，但一裁切就可能丢掉关键上下文
3. 系统状态和业务状态混在历史里，调试和验证都很困难

harness 选择了另一条路：

```text
历史消息只是“发生过什么”的轨迹
显式状态才是“现在真相是什么”的权威来源
```

这不是文风偏好，而是运行时组织方式的根本区别。

---

## 两层状态：SessionState 和 RunState

状态组装式运行时的第一块基础，是把状态明确拆成两层。

### SessionState：跨 query 持久化的真相

`SessionState` 里放的是整个会话期间需要持续存在的信息，例如：

```text
conversation_messages
invoked_skills
read_file_state
todo_state
skill_catalog
prompt_cache
```

这些字段的共同点是：

- 下一次 query 还需要它们
- transcript 被截断以后，它们仍然应该存在

举例：

- 某个 skill 一旦激活，不应该因为旧 assistant 消息被裁掉就失效
- 某个文件已经读取过，也不应该因为早期 tool result 被裁掉就丢失认知
- todo 当前做到哪一步，也不应该靠翻旧聊天记录去恢复

### RunState：单次 query 内部的控制状态

`RunState` 里放的是只对“这一次 QueryLoop.run()”有意义的控制信息，例如：

```text
turn_count
stop_reason
transition
tool_calls_executed
files_modified
allowed_tools_override
assistant_turns_since_todo
```

这些字段的共同点是：

- 它们描述的是“这次请求跑到哪了”
- query 结束后就没有继续保留的必要

所以不要把它理解成“小号 SessionState”。

它更像：

**QueryLoop 的运行笔记本。**

---

## transcript 在这个系统里到底是什么

`SessionState.conversation_messages` 当然很重要，但它不是运行时真相本身。

它更准确的角色是：

```text
近期交互轨迹
+ API 协议需要保留的消息结构
+ 给模型提供最近上下文的材料池
```

也就是说，transcript 有价值，但它承担的是“轨迹”和“协议配对”职责，不是“唯一权威事实来源”职责。

这一点特别关键，因为后面 `MessageViewBuilder` 会明确告诉你：

- transcript 会被裁切
- transcript 里的旧 thinking 会被清理
- 但 runtime 仍然需要保持完整

如果你把 transcript 当作唯一事实来源，这三件事都会变成灾难。

---

## MessageViewBuilder：把状态重新装配成模型输入

真正体现“状态组装”设计的地方，不在某个数据类，而在：

```text
MessageViewBuilder.build(...)
```

它每轮都会返回一份新的 `ModelInputView`：

```text
system
messages
tools
internal_runtime_view
```

### `system` 是从状态重建的

`system` 不是从历史里找几条 system message 拼出来的。

它来自 `PromptAssembler` 的三层组装：

```text
stable
runtime
overlay
```

其中真正体现“状态组装”精神的是 `runtime` 层。

它会从 `SessionState` 里重新取出：

- 当前激活的 skill
- 当前 todo
- 当前 file runtime
- 当前环境信息

然后重新渲染成：

```text
<runtime-context>
  <active-skills>...</active-skills>
  <todo-state>...</todo-state>
  <file-runtime>...</file-runtime>
</runtime-context>
```

所以 skill、todo、文件认知不是“遗留在旧消息里的碎片”，而是：

**每一轮都能重新注入模型输入的运行时事实。**

### `messages` 只是 transcript slice

与 `system` 相对，`messages` 明确是压缩过的。

`MessageViewBuilder` 会：

1. 从末尾往前贪心选择
2. 超预算时跳过旧消息
3. 清理老 thinking
4. 补齐 tool_result 对应的 assistant tool_use

这已经很直白地说明：

```text
messages 是优化后的近期窗口
不是完整历史
更不是唯一事实来源
```

---

## 为什么这套设计能容忍 transcript 被裁切

因为系统把“近期轨迹”和“持久事实”分开放了。

### 如果没有显式状态，会发生什么

假设没有 `invoked_skills`、`todo_state`、`read_file_state` 这些状态字段，而只依赖对话历史。

那么一旦 transcript 被裁掉早期消息，就会出现：

- skill 似乎“凭空失效”
- todo 当前进度不见了
- 模型忘记自己读过哪些文件

也就是说，压缩上下文会直接破坏 runtime。

### 在当前设计里，会发生什么

当前设计下，transcript 就算被裁掉一部分：

- `SessionState.invoked_skills` 还在
- `SessionState.todo_state` 还在
- `SessionState.read_file_state` 还在

于是下一轮 `PromptAssembler.build_runtime_context()` 仍然可以把它们重新渲染进 `system`。

所以裁切 transcript 的后果变成：

```text
模型看不到那么多旧轨迹细节
但 runtime 的核心事实并没有丢
```

这就是状态组装式运行时最大的收益。

---

## 代码和测试已经证明这不是一句口号

这个设计不是“理论上可行”，仓库里有直接证明。

最重要的是：

```text
tests/session/test_state_assembled_runtime.py
```

### 证明 1：就算 assistant/tool transcript 不在了，system 里仍然有运行时上下文

第一个测试做的事情很直接：

1. 构造一个只有 user message 的 transcript
2. 把 active skill、todo、file runtime 直接放进 `SessionState`
3. 调用 `MessageViewBuilder.build()`
4. 断言这些 runtime 信息都仍然出现在 `view.system` 里

这证明：

```text
运行时上下文来源于状态重组
不是来源于 assistant/tool 历史消息残留
```

### 证明 2：运行时上下文不会伪装成 transcript 的 system message

第二个测试验证的是：

- `view.messages` 里不应该混进 runtime 注入的 system-role 消息
- 这些 runtime 信息应该只存在于组装后的 `view.system`

这一步很重要，因为它说明当前实现不是在偷偷把状态重新塞回 transcript，而是真的做了：

```text
system 和 messages 分离
```

---

## PromptAssembler 在这套设计里扮演什么角色

你可以把 `PromptAssembler` 理解成：

**把运行时权威状态翻译成模型可消费上下文的渲染器。**

它做了 4 件非常重要的事：

### 1. 稳定上下文带缓存

`build_stable()` 负责组装：

- 框架系统指令
- 可用 skill 目录
- 可选 override

这部分通常变化少，所以它通过 `skills_revision + system_context hash` 做缓存。

含义是：

- 系统不需要每轮都重渲染稳定部分
- 但稳定部分仍然是从状态和上下文函数现算出来的

### 2. 运行时上下文每轮重建

`build_runtime_context()` 每轮都会读取：

- `state.invoked_skills`
- `state.todo_state`
- `state.read_file_state`

这一步是状态组装式运行时的核心动作。

### 3. 覆盖层预留单轮信号扩展点

当前 `build_query_overlay()` 还是空的。

这反而说明边界很清楚：

- 运行时真相目前已经能被 stable/runtime 两层承载
- overlay 只是未来为 compact、memory 之类单轮信号预留

### 4. 内部运行时视图只服务调试

`build_internal_runtime_view()` 会暴露：

- 当前 invoked skills
- 当前 todo items
- 当前 read_file_state
- 当前 transition

但不会发给模型。

这体现的是另一层设计纪律：

**让系统可观察，不等于把所有内部状态都塞给模型。**

---

## 为什么这套设计比“历史即真相”更适合扩展

项目里后续如果继续扩展 skill、todo、文件运行时、subagent、memory，最怕的不是功能多，而是：

```text
新增能力只能靠在历史消息里偷偷塞一段文本维持
```

那样会导致：

- 行为不可验证
- 上下文一裁就坏
- 多个能力互相抢 prompt 空间
- 调试只能靠猜

而状态组装式运行时的扩展方式更稳定：

1. 先把新能力的事实状态放进 `SessionState` 或 `RunState`
2. 再明确决定它应该进入 `stable`、`runtime` 还是 `overlay`
3. 最后通过 `MessageViewBuilder` 控制它和 transcript 的关系

也就是：

**先定义状态权威来源，再定义它如何进入模型输入。**

这比“先想办法给模型塞一句话”高一个层级。

---

## 什么时候应该修改状态，什么时候应该修改 transcript

这是最常踩错的点。

一个简单判断方式：

### 如果某信息需要跨轮稳定存在

放状态里，例如：

- skill 已激活
- todo 当前进度
- 文件读取认知
- query 内部工具限制

### 如果某信息只是让模型响应当前一轮

可以作为消息注入 transcript，例如：

- recovery follow-up
- todo stale reminder
- 达到 max_turns 后要求收尾的那条 user 消息

一句话记忆：

```text
长期事实进 state
单轮追问进 messages
```

---

## 应该先记住的 5 条纪律

1. 不要把 transcript 当成运行时真相。
2. 想新增上下文前，先问它属于 `SessionState` 还是 `RunState`。
3. 想让模型稳定看到某个事实，优先考虑进入 `PromptAssembler` 的 runtime 层。
4. transcript 可以压缩，但压缩不能成为丢失运行时事实的理由。
5. 如果一个能力只能靠历史消息残留生效，它大概率还没有真正接入这个 runtime。

---

## 这篇之后该读什么

如果你现在最想继续回答的问题是：

- “那这些内部消息最后为什么还能适配 Anthropic 的协议要求？”

下一篇请读：

`10-anthropic-protocol-boundary.md`

因为状态组装式运行时解决的是“运行时真相放哪里”，而协议边界解决的是“这些内部结构最终怎么安全地发给模型”。
