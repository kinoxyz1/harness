# Context Management Architecture 设计

> 日期：2026-04-29
> 状态：待评审
> 相关文档：
> - [`docs/compact-research.md`](/Users/kino/works/kino/harness/docs/compact-research.md)
> - [`docs/superpowers/specs/2026-04-19-state-assembled-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-19-state-assembled-runtime-design.md)
> - [`docs/superpowers/specs/2026-04-22-runtime-update-protocol-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-22-runtime-update-protocol-design.md)
> 当前相关实现：
> - [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py)
> - [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
> - [`core/session/state.py`](/Users/kino/works/kino/harness/core/session/state.py)
> - [`core/session/store.py`](/Users/kino/works/kino/harness/core/session/store.py)
> - [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py)
> - [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py)
> - [`core/llm/anthropic_client.py`](/Users/kino/works/kino/harness/core/llm/anthropic_client.py)
> Claude Code 参考：
> - `/Users/kino/works/opensource/Claude-Code-doc/src/query.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/autoCompact.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/microCompact.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/compact.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/sessionMemoryCompact.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/prompt.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/messages.ts`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/toolResultStorage.ts`

---

## 1. 摘要

当前 `harness` 已经完成了两件重要的前置工作：

1. 运行时状态开始从 transcript 中分离，`PromptAssembler` 可以直接从 `SessionState` 渲染 skill、todo、file runtime 等信息。
2. `QueryLoop -> ToolRuntime -> PromptAssembler -> ModelGateway` 的主干边界已经基本稳定。

但在最关键的一层，系统仍然缺少正式的 context management architecture。当前模型输入仍然主要依赖 [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py) 中的尾部贪心截断：

- 从 `conversation_messages` 末尾向前截取固定字符窗口
- 为满足 API 要求，回补 `tool_use -> tool_result` 配对
- 在视图副本中清理旧 thinking

这会把“上下文管理”错误地收缩成“最后一段 transcript slice 的选取问题”。

Claude Code 的真实做法不是只在发送前做一次 smarter slice，而是在每轮 API 调用前运行一条分层的 query-time context pipeline。这个 pipeline 不只包含 compact，还包含：

1. active context 的切片规则
2. 上下文压力估算
3. 工具结果减压
4. 局部 transcript slimming
5. summary-based compact
6. overflow recovery
7. API invariant repair

因此，本设计的目标不是“把 `harness` 改得像 Claude Code 的 compact 模块”，而是：

> 为 `harness` 定义一套完整的 context management architecture，并在其中把 compact 定位成一个关键子系统，而不是唯一主题。

本设计的结论是：

1. `message tier assembly` 不再作为总体方案名称。
2. `conversation_messages` 从“默认 prompt 源”退化为“工作 transcript 的一个载体”。
3. query 前处理必须拥有正式的 context pipeline。
4. compact 是该 pipeline 的中后段能力，不是全部内容。
5. 第一阶段以 token budget、tool result budget、time-based microcompact、summary compact、reactive recovery 为最小闭环。

---

## 2. 要解决什么，不解决什么

### 2.1 要解决的问题

本设计解决以下问题：

1. 长会话中，早期用户意图、关键决策和工作轨迹会被尾部贪心截断直接丢失。
2. 旧工具结果占据大量上下文，但当前系统没有独立的减压层优先处理它们。
3. `QueryLoop` 缺少正式的 context pressure 评估、compact trigger、overflow recovery 和 failure breaker。
4. `conversation_messages` 目前既像审计日志，又被迫承担工作历史，但又没有正式 rewrite 语义。
5. 当前系统用字符预算控制 transcript，而 Claude Code 的上下文治理、阈值和恢复逻辑都围绕 token 预算展开。
6. 当前系统没有统一定义 active context、working transcript、runtime truth 与最终 API payload 之间的关系。

### 2.2 明确非目标

本设计不包含以下内容：

1. 照搬 Claude Code 的 `cache_edits` / prompt cache 细节。
2. 照搬完整的 `session memory` 子系统。
3. 引入多 agent / async agent 的 attachment delta 恢复机制。
4. 引入 KAIROS、远程 resume、旁路 transcript 存储。
5. 重写 `QueryLoop` 的 think-act-observe 主循环。
6. 第一阶段实现真正“语义理解级”的 kept-segment 选择器。
7. 第一阶段定义跨进程持久化的 compacted transcript migration 协议。

设计说明：
这是一篇 architecture spec，不是 Claude Code 功能移植清单。对 `harness` 来说，第一阶段最重要的是先建立 context pipeline 骨架，而不是追逐所有外围优化。

---

## 3. 系统模型

为了避免后续 spec 再把 transcript、runtime、prompt view 混成一层，本设计先定义四个概念。

### 3.1 Full Transcript

`Full Transcript` 指用户、assistant、tool 之间发生过的完整会话历史。

对当前 `harness` 而言，`SessionState.conversation_messages` 在设计初衷上接近这个概念，但实现上并不纯粹：

1. 它已经混入了部分 system/user 注入消息。
2. 它当前仍被 `view_builder` 直接当作 prompt 源。
3. 它没有正式的 compact boundary 和 rewrite 语义。

第一阶段结束后，`harness` 不再要求内存中的 `conversation_messages` 永远等价于 full transcript。

### 3.2 Working Transcript

`Working Transcript` 指当前后续推理真正依赖的工作历史。

它具有以下特征：

1. 可以比 full transcript 更短。
2. 可以包含 compact boundary、compact summary、messagesToKeep 等结构性消息。
3. 会被后续 query 继续消费。
4. 必须满足 API invariant。

本设计中的 transcript rewrite，实质上是在维护 working transcript。

### 3.3 Runtime Truth

`Runtime Truth` 指不能依赖 transcript 才能稳定存在的会话事实，例如：

1. 已激活 skill
2. todo 状态
3. read file 状态
4. 环境上下文
5. 未来 compact 相关状态

这些事实必须以显式状态存在于 `SessionState` / `RunState`，并由 `PromptAssembler` 在需要时重新注入。

### 3.4 Active Context

`Active Context` 指某一轮模型调用实际使用的上下文集合。

它由三部分组成：

1. `system prompt`
2. `runtime context`
3. `working transcript` 的模型可见片段

context management architecture 的职责，就是在每轮 query 前构造一个合法、可继续工作、且不超预算的 active context。

---

## 4. 当前 Harness 与 Claude Code 的关键差异

### 4.1 当前 Harness 的现实边界

当前 `harness` 的相关事实如下：

1. [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py) 负责尾部贪心截取 transcript，并在 slice 副本上做 old thinking 清理。
2. [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 还没有独立 context pipeline；它每轮只做 policy 注入、视图构建、模型调用、工具执行和 recovery。
3. [`core/session/store.py`](/Users/kino/works/kino/harness/core/session/store.py) 把 `conversation_messages` 定义为 append-only 写入目标。
4. [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 会把 transcript 中的 `system` 消息抽出并折叠到顶层 `system` prompt。
5. [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py) 已经把 skill、todo、file runtime 等状态与 transcript 分离；它是 compact 后 runtime restore 的天然基础设施。

### 4.2 Claude Code 值得借鉴的骨架

Claude Code 每轮调用模型前，会按如下顺序处理上下文：

1. `getMessagesAfterCompactBoundary()`
2. `applyToolResultBudget()`
3. `snipCompactIfNeeded()`
4. `microcompactMessages()`
5. `contextCollapse.applyCollapsesIfNeeded()`
6. `autoCompactIfNeeded()`
7. 当主动路径失手且 API 已报错时，进入 `reactive compact`

这个顺序的价值不在于照抄名字，而在于它明确了：

1. 便宜且局部的减压优先。
2. summary-based compact 是更重的兜底，不应该一开始就接管。
3. compact 一旦成功，后续轮次消费的是 compact 后的 working transcript。

### 4.3 Harness 不能机械照搬的点

`harness` 当前至少有四处不能直接照搬：

1. Claude Code 的 `compact boundary` 是 transcript 中的 `system compact_boundary` 消息；`harness` 如果直接这样做，会被 [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 误并入顶层 `system` prompt。
2. Claude Code 的 cached microcompact 依赖 `cache_edits`；`harness` 第一阶段没有对应 API 能力。
3. Claude Code 的 post-compact attachment 体系非常完整；`harness` 第一阶段只需要恢复真正会影响继续工作的最小 runtime 信息。
4. Claude Code 已经有 resume、session file、replacement record 等持久化基础设施；`harness` 目前没有。

---

## 5. 核心设计原则

### 5.1 Context Pipeline First

上下文管理的主战场不再是 `view_builder` 的 slice 逻辑，而是 query 前处理 pipeline。

`view_builder` 的职责应收缩为：

1. 读取当前有效 working transcript
2. 做最终 API 视图装配
3. 应用少量发送前规范化

### 5.2 Runtime Truth Outlives Transcript

compact 可以改写 working transcript，但不能让系统丢失 runtime truth。

因此：

1. compact 不能依赖“那条旧 transcript 还在”来维持 skill、todo、file state。
2. compact 后必须利用 runtime state 恢复最小可继续工作的上下文。

### 5.3 Repair First

任何 compact、truncate、summary、rewrite 都必须优先保证 API invariant。

repair 不是异常补丁，而是 context management architecture 的一部分。

### 5.4 Token Budget Is Primary

上下文压力、compact trigger、overflow recovery 的总预算统一以 token 为单位。

字符数仍可在局部用作估算或短路，但不能再担任总体预算标准。

### 5.5 Restricted Summarization

summary 调用本身必须是受限操作：

1. 独立 `query_source`
2. recursion guard
3. no-tools 或极小白名单
4. thinking off 或强约束
5. bounded output

### 5.6 Observability By Default

compact 是隐式操作，但不能是黑箱操作。

至少必须能观测：

1. 为什么触发
2. 触发了哪一层
3. 触发前后预算变化
4. 失败在哪里
5. failure breaker 是否接管

---

## 6. 总体架构

### 6.1 总体分层

context management architecture 由四层构成：

1. `State Layer`
   - `SessionState`
   - `RunState`
   - runtime truth
2. `Context Pipeline Layer`
   - query 前处理
   - compact trigger
   - repair
   - recovery
3. `View Assembly Layer`
   - `PromptAssembler`
   - `MessageViewBuilder`
   - active context 组装
4. `API Normalization Layer`
   - role alternation
   - tool pairing repair
   - provider payload 规约

### 6.2 Compact 的位置

compact 不是整个 architecture 的同义词。

compact 只是 `Context Pipeline Layer` 中的一组能力：

1. `tool result budget`
2. `microcompact`
3. `summary compact`
4. `reactive recovery`

其他同等重要的能力还包括：

1. active context 定义
2. token pressure estimation
3. runtime restore
4. API invariant repair

---

## 7. Query-Time Context Pipeline

`harness` 第一阶段的 query-time context pipeline 定义如下：

1. 从 `SessionState` 读取当前 working transcript
2. 估算 token 使用与上下文压力
3. 执行 `tool result budget`
4. 执行 `microcompact`
5. 判断是否需要 `summary compact`
6. 如需要，执行 `summary compact` 并重写 working transcript
7. 用 `PromptAssembler + MessageViewBuilder` 组装最终 active context
8. 执行 API normalization 与 pairing repair
9. 调用模型
10. 若 API 返回 prompt-too-long 或相关 overflow，执行单次 guarded `reactive recovery`

### 7.1 各层接管关系

1. `tool result budget` 与 `microcompact` 每轮都可以前处理运行。
2. 每轮最多进入一次 `summary-producing compact`。
3. `reactive recovery` 只在主动路径失手、API 已超窗时接管。
4. `summary compact` 失败时，不得在同一轮无界重试。

### 7.2 为什么不是“直接升级 view_builder”

因为 `view_builder` 只能控制“这次发什么”，却不能正式管理：

1. 前几轮已经做过的 compact 决策
2. compact failure breaker
3. overflow recovery
4. working transcript rewrite
5. runtime restore

所以“更聪明的 slice 算法”不是目标架构，只能是其中一项局部策略。

---

## 8. 预算与压力估算

这是第一阶段必须明确写死的实现约束。

### 8.1 预算单位

第一阶段所有 compact trigger 使用 token 预算，而不是字符预算。

### 8.2 估算策略

第一阶段采用 `rough token estimation + API usage calibration` 的组合策略：

1. 平时触发判断使用本地 rough token estimation。
2. 若上一轮 API 返回了 usage，则把 usage 作为最近一次真实观测值。
3. 在本地估算与真实 usage 出现明显偏差时，以真实 usage 优先调整阈值判断。

这意味着第一阶段不需要引入 provider 精确 tokenizer，也不继续依赖字符数。

### 8.3 Headroom 规则

第一阶段 context pressure 判断必须显式预留输出 headroom。

预算判断至少包含：

1. 当前 active context 的估算输入 token
2. summary 调用的预留输出 headroom
3. normal query 的预留输出 headroom

### 8.4 触发阈值

第一阶段不在 spec 中绑定具体常数，但要求：

1. `tool result budget` 有 per-message 预算阈值
2. `summary compact` 有整轮 active context 压力阈值
3. `reactive recovery` 以 API overflow 为最终事实触发

常数由 implementation plan 定义，但预算模型必须先在 spec 中固定。

---

## 9. Compact 子系统

### 9.1 Level 1: Tool Result Budget

这一层处理“单个 API-level message 中 tool result 体积过大”的问题。

第一阶段定义如下：

1. 作用对象仅限 `tool_result` 内容。
2. 不处理用户文本与 assistant 文本。
3. 替换决策必须对同一段历史稳定。
4. 替换后的内容必须可识别、可诊断、可恢复到同一替换形态。

该层是预算治理层，不是语义总结层。

对 `harness` 的要求：

1. 需要引入独立的 replacement state。
2. 需要让 next turn 能复用同一批替换决策。
3. 第一阶段允许采用比 Claude Code 更简单的记录格式，但不允许只做一次性内存替换。
4. 第一阶段不要求“语义价值评分”，可以用预算与体积规则驱动替换。

### 9.2 Level 2: Microcompact

这一层处理“旧工具轨迹整体过多”的问题。

第一阶段只实现 `time-based local rewrite` 路径，不实现 `cache_edits` 路径。

第一阶段定义如下：

1. 只处理 compactable tools 的旧结果。
2. 只对足够旧、且不在最近工作段内的结果生效。
3. 替换为标准 placeholder，而不是生成 summary。
4. 保留原始消息骨架与工具配对锚点。

第一阶段 compactable tools 的建议集合：

1. `read_file`
2. `find`
3. 只读 shell / bash 结果
4. `glob` / `grep` 等只读搜索型工具

`edit_file` / `write_file` 不纳入第一阶段默认集合。

### 9.3 Level 3: Summary Compact

这一层处理“局部瘦身已不足以维持工作连续性”的问题。

第一阶段的 summary compact 定义如下：

1. 以“旧历史摘要 + 保留近期工作段”为主。
2. 只实现一种主路径，不同时实现 `full compact` 与双向 `partial compact`。
3. compact 成功后，working transcript 被正式重写。

第一阶段不把 Claude Code 的 `partialCompactConversation()` 原样作为自动主路径。

原因是：

1. Claude Code 当前自动路径的主 compact 不是 partial compact，而是 `sessionMemoryCompact -> compactConversation`。
2. `partialCompactConversation()` 仍带有很强的手动消息选择语义，不适合作为 `harness` 第一阶段的自动模型。

### 9.4 Level 4: Reactive Recovery

这一层只在主动路径失手后接管。

第一阶段范围如下：

1. 接住 prompt-too-long 一类 overflow
2. 只允许单次 guarded retry
3. 若恢复失败，显式报错，不再继续注入更多控制消息

---

## 10. Summary 调用模型

### 10.1 模型选择

第一阶段 summary 调用使用与主循环相同的模型。

原因是：

1. 这最接近 Claude Code 当前策略。
2. 不会额外引入“副模型摘要质量漂移”问题。
3. 第一阶段需要先把调用约束做对，而不是先做模型降本优化。

### 10.2 调用约束

第一阶段 summary 调用必须满足：

1. 使用独立 `query_source=compact` 或等价标记。
2. 自动 compact recursion guard 在该 source 下生效。
3. 默认不给工具。
4. thinking 默认关闭。
5. 设置独立的 `max_output_tokens` 上限。
6. prompt 明确声明 `TEXT ONLY / DO NOT CALL TOOLS`。

### 10.3 第一阶段的工具策略

第一阶段默认 summary 模型无工具。

若后续证明必须保留工具，也仅允许：

1. `read_file`
2. 其他显式批准的只读工具

第一阶段不复制 Claude Code 的 `FileReadTool + ToolSearch + MCP tools` 组合。

### 10.4 Summary 内容约束

summary prompt 至少要覆盖以下内容：

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages
7. Pending Tasks
8. Current Work
9. Optional Next Step

---

## 11. Transcript Rewrite 模型

### 11.1 `conversation_messages` 的新定位

第一阶段开始后，`conversation_messages` 不再被视为“永不变化的事实日志”。

它的新定位是：

1. 当前会话的 working transcript 容器
2. 模型后续轮次使用的工作历史
3. 调试与审计仍可读取，但不保证保留全部原始细节

### 11.2 Rewrite 入口

第一阶段必须新增正式 rewrite 入口。

推荐约束：

1. `SessionStore` 继续担任普通 append/extend/prepend 写入门面。
2. transcript rewrite 通过单独的 `replace_working_transcript(...)` 或等价接口完成。
3. 任何 compact 成功后的 transcript 替换都必须走这一入口。

### 11.3 Boundary 语义

第一阶段需要 boundary 概念，但不要求与 Claude Code 的消息类型完全一致。

boundary 必须承担以下职责：

1. 标记 working transcript 的新起点
2. 记录 compact 元数据
3. 为 kept segment 与恢复逻辑提供锚点

但 boundary 的内部表示必须兼容 `harness` 当前 protocol，不得污染顶层 system prompt。

### 11.4 Concrete 示例

compact 前的最小工作片段可以是：

```python
[
    {"role": "user", "content": "分析 query loop 的 compact 设计"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "core/query/loop.py"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "... very long file content ..."},
    {"role": "assistant", "content": "我已经读完 loop.py，接下来会检查 view_builder。"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2", "name": "find", "args": {"pattern": "compact"}}]},
    {"role": "tool", "tool_call_id": "call_2", "content": "... long search output ..."},
]
```

compact 后的 working transcript 应近似变为：

```python
[
    {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "...boundary metadata..."},
    {"role": "meta_compact_summary", "kind": "compact_summary", "content": "... structured summary ..."},
    {"role": "assistant", "content": "我已经读完 loop.py，接下来会检查 view_builder。"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2", "name": "find", "args": {"pattern": "compact"}}]},
    {"role": "tool", "tool_call_id": "call_2", "content": "... maybe kept or slimmed ..."},
    {"role": "meta_runtime_restore", "kind": "file_runtime", "content": "... restored file context ..."},
]
```

这个示例只说明 rewrite shape，不约束最终内部消息字段名必须与示例完全一致。

---

## 12. Runtime Restore

compact 后，不是所有上下文都应该靠 summary 承载。

第一阶段必须恢复的内容包括：

1. 当前 todo 状态
2. 已激活 skill
3. 近期 read file runtime

其中第 3 点应纳入第一阶段。

原因是：

1. `harness` 已有 [`SessionState.read_file_state`](/Users/kino/works/kino/harness/core/session/state.py) 作为现成快照源。
2. compact 后模型最容易丢失的是“最近读过哪些文件、看的是什么段落”。

第一阶段不恢复：

1. 工具 schema delta
2. agent listing delta
3. MCP instruction delta
4. async agent attachments

---

## 13. Invariants 与 Repair

### 13.1 配对 invariant

任何时刻发送给 API 的消息必须满足：

1. 每个 `tool_result` 都能找到合法的 `tool_use`
2. 不允许 orphaned `tool_result`
3. 不允许重复 `tool_use id`
4. 不允许重复 `tool_result` 对应 id

### 13.2 轨迹 invariant

任何时刻都不能把一条仍在工作中的 assistant 轨迹切断成 API 无法解释的片段。

特别是：

1. 包含 `tool_use` 的 assistant 轨迹不能只保留一半。
2. 与 kept 段共享同一 logical assistant 消息标识的 thinking 片段，必要时要一起保留。
3. 最近完成的工具轨迹与其后 assistant 结论应视为更强绑定单元。

### 13.3 角色 invariant

compact 后仍然必须能规约成合法的 role alternation。

### 13.4 运行 invariant

以下规则必须始终成立：

1. compact trigger 使用 token 预算。
2. summary 调用不能自由使用工具。
3. `query_source == compact` 或等价标记时，自动 compact 必须被 recursion guard 禁止。
4. compact 连续失败必须触发 circuit breaker。
5. compact 成功后，下一轮只能消费最新 working transcript。

### 13.5 Pairing Repair 的归属

`pairing repair` 是正式上下文管理的一部分，但不属于某一个 compact 层。

它必须同时满足：

1. compact path 可以调用它，用于 rewrite 后自修复。
2. 通用 API 发送前路径也必须可调用它，而不是只在 compact 时运行。

---

## 14. 可观测性

第一阶段必须提供最低限度的 compact observability。

至少记录：

1. 触发原因
2. 触发层级
3. 触发前 token estimate
4. 触发后 token estimate
5. summary compact 是否成功
6. 失败原因
7. circuit breaker 是否触发

如果有内部调试视图，还应能看到：

1. 当前 working transcript 大小
2. replacement state 概况
3. 最近一次 compact 时间与层级

---

## 15. 测试策略

这部分不是 implementation plan，但 architecture 必须先定义最少测试面。

第一阶段至少需要以下测试类型：

1. `Invariant tests`
   - tool pairing
   - role alternation
   - no orphaned tool_result
2. `Rewrite shape tests`
   - boundary
   - summary
   - kept segment
   - runtime restore
3. `Pressure / trigger tests`
   - 预算未超时不 compact
   - 预算超时进入正确层级
4. `Reactive recovery tests`
   - prompt-too-long 后单次恢复
   - 恢复失败时显式报错
5. `Continuation tests`
   - compact 后模型仍能基于当前任务继续工作

---

## 16. 现有会话兼容性

第一阶段不引入跨进程会话迁移协议，也不为旧 transcript shape 定义 `schema_version`。

明确约束如下：

1. 第一阶段只保证当前进程内 working transcript rewrite 正确。
2. 第一阶段不承诺 compacted transcript 的持久化恢复兼容性。
3. 若未来引入正式 session persistence / resume，再为 boundary、summary、replacement record 增加 versioned schema。

这样做的原因很简单：

1. 当前 `harness` 还没有 Claude Code 那样的成熟 session restore 基础设施。
2. 现在过早引入 migration/version 协议，只会把第一阶段目标拖散。

---

## 17. 第一阶段范围

### 17.1 第一阶段必须实现

1. token-based context pressure estimation
2. compact trigger 与 recursion guard
3. tool result budget
4. time-based microcompact
5. summary compact 主路径
6. working transcript rewrite
7. pairing repair 能力
8. circuit breaker
9. prompt-too-long guarded recovery
10. compact 后的 todo / skill / file runtime restore
11. compact observability

### 17.2 第一阶段明确不做

1. cached microcompact
2. session memory compact
3. full compact / 双向 partial compact 体系
4. context collapse
5. post-compact attachment delta 全量恢复
6. 多 agent 相关 compact 联动
7. compacted transcript 持久化迁移

### 17.3 第一阶段目标效果

1. 长会话不再主要依赖尾部字符截断。
2. 旧工具结果优先被减压。
3. compact 后模型仍能持续完成当前工作，而不是只剩抽象摘要。
4. compact 自身不会轻易递归、自爆或进入失败重试泥潭。
5. compact 触发与失败行为可观测、可测试。

---

## 18. 推荐组件边界

第一阶段推荐的最小新增结构如下：

1. `core/session/context_manager.py`
   - 负责 token pressure、trigger、pipeline orchestration、circuit breaker
2. `core/session/compact_service.py`
   - 负责 tool result budget、microcompact、summary compact、reactive recovery
3. `core/session/pairing_repair.py`
   - 负责独立的 API invariant repair
4. `core/session/transcript_rewriter.py`
   - 负责 working transcript rewrite、boundary、summary、kept 组装

以下内容第一阶段不必独立文件：

1. `message_tier.py`
2. `budget_allocator.py`

它们可以先作为 `context_manager.py` 或 `compact_service.py` 的内部策略函数存在。

---

## 19. 设计结论

这次改造不是把 `_select_transcript_slice()` 换成一个更聪明的切片函数，而是把 `harness` 的上下文管理模型从：

- 发送前截一段尾部消息

升级为：

- query 前运行正式 context pipeline
- 用 token budget 评估压力
- 在局部减压不足时改写 working transcript
- 用 runtime truth 恢复可重建状态
- 用 pairing repair 与 guarded recovery 保证问答主流程不被 compact 破坏

对 `harness` 来说，第一阶段的正确目标不是“尽可能复制 Claude Code 的所有细节”，而是：

1. 学到 Claude Code 的 context management 骨架
2. 避免误学其依赖特定基础设施的外围实现
3. 在当前协议和 runtime 边界下，先建立一个稳定、可验证、可继续演化的 architecture
