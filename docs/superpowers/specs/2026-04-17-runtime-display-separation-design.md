# Runtime Display Separation 设计

> 日期: 2026-04-17
> 状态: 待评审
> 依赖:
> - [`docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)
> - [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)
> - [`docs/superpowers/specs/2026-04-17-todo-task-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-todo-task-parity-design.md)
> 当前相关实现:
> - [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
> - [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
> - [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py)
> - [`core/shared/interfaces.py`](/Users/kino/works/kino/harness/core/shared/interfaces.py)
> - [`core/shared/run_options.py`](/Users/kino/works/kino/harness/core/shared/run_options.py)
> - [`core/tools/builtin/todo.py`](/Users/kino/works/kino/harness/core/tools/builtin/todo.py)

## 摘要

当前 `harness` 的运行体验已经在 skill inline、todo parity、runtime control plane 三个方向上明显改善，但用户界面仍然存在一个关键问题：

> 一旦关闭思考过程，可见层几乎只剩下工具调用和 runtime 流水账，用户看不到“现在为什么要做这一步”。

这不是单纯的文案问题，而是显示模型的问题。现在的终端展示把两类原本应该分开的信息揉在了一起：

- 面向用户的工作播报
- 面向实现层的调度日志

本设计的目标不是简单重写 `[Runtime] ...` 日志，而是重建默认显示模型，明确拆分四类用户可见信息：

1. `Assistant Update`
2. `Todo State`
3. `Tool Event`
4. `Progress / Outcome`

同时保持当前已经成熟的架构边界，不引入新的平行控制平面，不把解释重新塞回 thinking，不破坏 `QueryLoop -> ToolExecutorRuntime -> Renderer` 的职责分离。

## 本设计记录的用户语境

以下约束来自本轮讨论，后续实现必须严格满足：

1. 用户明确指出，当前第一轮 `tool_call` 执行前只有“思考过程”，关闭思考后几乎无从判断系统正在做什么。
2. 用户不只想优化 runtime 文案，而是希望同时得到：
   - 工具调用前的人话播报
   - 更简洁的工具执行证据流
3. 用户特别强调 `todo` 不能像普通工具事件一样被降级隐藏，因为当前产品没有 Claude Code 那种底部固定 todo 区域。
4. 用户已经基于最近两份 2026-04-17 实现计划完成了一轮较大的架构重构，因此新设计必须复用现有架构，而不是引入另一条消息链路重新把系统搞乱。

第 3 点尤其关键。对当前 `harness` 而言，`todo` 不是一个普通的 tool log，它本身就是当前会话中的计划状态视图。

## 来自 Claude Code 的直接证据

本设计依赖对本地 Claude Code 文档镜像的直接阅读：

- `/Users/kino/works/opensource/Claude-Code-doc`

以下观察与当前问题直接相关。

### 证据 1：Claude Code 把 assistant 文本和 tool UI 明确分开

从 Claude Code 的 transcript 与可见消息统计逻辑可以看到：

- 纯 `tool_use` assistant message 不按普通 assistant 文本显示
- 纯 `tool_result` user message 也不按普通 user 文本显示
- 它们会进入 grouped / collapsed tool UI

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/utils/sessionStorage.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/components/messages/GroupedToolUseContent.tsx`

这说明 Claude Code 不是把“所有内容都塞进一条消息流里显示”，而是从消息模型开始就把：

- 对用户说的话
- 工具使用证据

分成不同展示层。

### 证据 2：Claude Code 对工具标签有单独的人类可读设计

Claude Code 的 Bash 工具支持从命令第一行的 `# comment` 中提取 label，明确用于非 verbose 的 tool-use label 和折叠提示。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/BashTool/commentLabel.ts`

这说明工具行本身也不是简单输出原始工具名，而是经过“人类可读标签”设计。

### 证据 3：Claude Code 有独立的 tool use summary 机制

Claude Code 还有单独的 `tool_use_summary` 生成器，要求输出像 git commit subject 一样短小的人类摘要，而不是底层流水账。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/services/toolUseSummary/toolUseSummaryGenerator.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/entrypoints/sdk/coreSchemas.ts`

虽然这个 summary 主要用于 SDK / 外部客户端，并不等同于本地 REPL 的唯一展示手段，但它再次证明 Claude Code 的方向是：

> 单独生成“人能看懂的摘要”，而不是让 runtime 调度日志承担解释职责。

### 证据 4：Claude Code 的 progress 是单独消息类型，不是调度 trace

Claude Code 的流式工具执行会产生独立的 progress message，并由专门组件渲染。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/services/tools/StreamingToolExecutor.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/components/shell/ShellProgressMessage.tsx`

它展示的是：

- 最近几行输出
- 耗时
- 行数或字节数

而不是：

- 收到几个 tool_call
- 分成几批
- 哪批串行哪批并行

这对 `harness` 的意义非常直接：当前 `[Runtime] 收到/分批/执行/完成` 这套日志更像内部 trace，不应该成为默认用户界面主内容。

## 当前 Harness 的显示问题

### 问题 1：对用户的解释被错误地压到了 thinking

当前 query loop 会显示 reasoning，但不会把“准备调用工具前的简短说明”当成一个独立且稳定的用户可见层。

更糟的是，现有实现里 assistant 在带 `tool_calls` 的回合中，即使有 `content`，也没有走 `renderer.show_assistant(...)` 的主路径展示。

相关文件：

- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
- [`core/llm/response.py`](/Users/kino/works/kino/harness/core/llm/response.py)

结果就是：

- 开启 thinking 时，用户勉强还能从思考里猜到下一步
- 关闭 thinking 时，界面立刻掉回“只剩工具调用”

### 问题 2：runtime 默认展示的是调度 trace，不是用户证据流

当前 runtime 默认向 stdout 直接打印：

- 收到多少个 tool_call
- 分成多少批
- 当前批次是并行还是串行
- 工具执行中 / 完成 / 异常

相关文件：

- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)

这些信息对调试调度很有价值，但对普通用户来说信息密度并不高，而且会挤压真正重要的内容：

- 为什么要做这一步
- 当前计划是什么
- 这一步完成后得到了什么

### 问题 3：`todo` 既是工具，又是计划视图，当前没有被明确区分

现有 runtime 在 `todo` 成功后会调用 renderer 渲染计划视图，这个方向是对的。

相关文件：

- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
- [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py)

但在整体展示模型里，`todo` 还没有被正式定义成“高优先级计划视图”，而更像是一个带特殊渲染副作用的普通工具。

这会带来两个风险：

- 后续优化工具事件流时容易误把 `todo` 一起压缩掉
- 计划展示和工具调用证据混在一起，缺少稳定规则

## 设计目标

本设计落地后，默认终端界面应满足以下用户可见行为：

1. 当模型即将发起工具调用时，用户优先看到一条短的人话播报，而不是只能去读 thinking。
2. 关闭 thinking 后，界面仍然具备基本可理解性。
3. `todo` 继续在对话流中承担计划状态视图职责，不因工具日志简化而消失。
4. 普通工具调用显示为极简事件流，不再默认暴露内部批处理调度细节。
5. 长耗时工具仍然能提供简洁、可信的进度反馈。
6. 原有 debug 价值不能丢失；内部 trace 可以保留，但必须退出默认显示。

## 非目标

本设计明确不处理以下内容：

- 重做底层消息存储结构
- 新建一套平行于 `conversation_messages` 的 assistant 解释消息历史
- 把 `todo` 改造成 Claude Code 风格的固定底栏
- 引入完整的 grouped/collapsed transcript 子系统
- 改变模型工具调用协议
- 重新设计整个终端 UI 组件体系

## 设计原则

本设计遵循 6 条原则。

### 原则 1：解释复用 assistant 文本通道，不走 thinking，不走 runtime 猜测

“接下来为什么这样做”的最佳来源仍然是模型本身，而不是 runtime。

因此：

- 主路径上的 `Assistant Update` 必须来自普通 assistant 内容
- 它应该像正常回复一样进入会话消息
- 不能再把这类信息压到 thinking 中

### 原则 2：`todo` 是计划状态视图，不是普通工具日志

对当前产品而言，`todo` 有双重身份：

- 模型工具
- 用户计划面板

展示层必须承认第二个身份。

### 原则 3：runtime 默认只显示“动作证据”，不显示“调度实现”

默认用户界面应该回答：

- 做了什么
- 结果怎样

而不是回答：

- runtime 内部分了几批
- 调度器为什么这么跑

### 原则 4：默认展示与 debug 展示必须分层

当前 `[Runtime] ...` 这套信息并非毫无价值，但它属于 debug / verbose 视图，不属于默认用户体验。

### 原则 5：UI 事件不能污染模型上下文

工具事件、进度提示、fallback 文案等若只是为了给用户看，就不应该全部回写到 `conversation_messages`。

要保持：

- 模型上下文
- 终端瞬时显示

这两者的边界稳定。

### 原则 6：复用现有扩展点，避免新增第二套控制平面

这次设计只允许在现有链路上做增量演进：

- `QueryLoop`
- `ToolExecutorRuntime`
- `Renderer`
- `RunDisplayOptions`
- `todo_state`

不新增一套新的 runtime bus、display store、message broker。

## 方案概览

默认显示模型拆分为四类信息。

### 1. Assistant Update

定义：

- 模型在即将执行某组工具前产出的简短人话说明
- 解释“接下来为什么做这一步”
- 是普通 assistant 文本，不是 thinking

例子：

- `先加载 analysis-report skill，再按它的工作流重估下一步。`
- `先建立执行计划，再开始读取 CSV。`
- `先读取 CSV 的表头和样例，确认数据结构。`

### 2. Todo State

定义：

- 由 `SessionState.todo_state` 驱动的计划视图
- 在对话流中高优先级展示
- 不等价于普通 tool event

展示规则：

- 首次创建或重大更新时显示完整计划
- 若计划未变化，只显示当前聚焦项
- 全部完成时显示总结

### 3. Tool Event

定义：

- 对普通工具调用的极简动作证据
- 不承担意图解释职责
- 不显示内部批处理调度细节

例子：

- `Skill(analysis-report)`
- `Read(TEST_DATA.csv)`
- `Bash(分析 CSV 结构)`

### 4. Progress / Outcome

定义：

- 长耗时工具的进行中反馈
- 完成后的人类可读摘要

例子：

- `已加载 skill，等待重新规划`
- `计划已更新，当前：读取并解析 CSV`
- `读取完成：10 列，预览 10 行`

## 架构归属

### Assistant Update：由模型生成，由 QueryLoop 展示和持久化

这是本设计最重要的架构约束。

`Assistant Update` 不新增消息协议，也不通过 tool result 反向注入。它直接复用已有 assistant content 通道：

1. 模型返回：
   - `content` 为短播报
   - `tool_calls` 为后续动作
2. `QueryLoop` 像现在一样把 assistant message 写入 store
3. 但在存在 `tool_calls` 的回合中，也必须调用 `renderer.show_assistant(model_resp.content)`，不能只在最终文本回复时才显示

这意味着：

- `Assistant Update` 是真实对话内容
- 它可以进入 transcript
- 它不会依赖 thinking 是否开启

### Todo State：由 todo tool 写 SessionState，由 renderer 专门渲染

`todo` 仍然是唯一权威状态源：

- `core/tools/builtin/todo.py` 负责写 `SessionState.todo_state`
- runtime 不得自己伪造计划内容
- renderer 根据 todo state 决定展示完整计划、当前聚焦项、完成总结

因此：

- 模型可以说“先建立计划”
- 但真正的计划内容必须来自 `todo_state`

### Tool Event：由 runtime 触发，由 renderer 生成紧凑标签

普通工具事件是 UI-only 证据流，不进入 conversation history。

推荐保持现有 `Renderer.show_tool_call(name, args)` / `show_tool_result(name, output)` 这两个扩展点，但调整它们的语义：

- 不再直译为原始工具名和大段 output preview
- 改成“基于工具类型的人类可读标签 + 紧凑结果摘要”

也就是说：

- runtime 仍负责调用 renderer
- renderer 负责把原始 `name + args + output` 变成简短展示

### Progress / Outcome：由 runtime 或具体 tool 提供，但只走 UI 层

进度类信息不应默认写入 conversation history。

推荐路径：

- 长耗时执行中的进行中状态，走 renderer 的 status/progress 接口
- tool 完成后的摘要，走 `show_tool_result` 或更细的 status 接口

这类信息主要服务终端即时反馈，而不是模型上下文。

## 默认显示协议

### A. 有 Assistant Update 的标准回合

理想展示顺序如下：

1. `Assistant Update`
2. 若有 `todo` 更新，则展示 `Todo State`
3. 展示普通 `Tool Event`
4. 展示 `Progress / Outcome`

例如：

```text
先加载 analysis-report skill，再按它的工作流重估下一步。

Skill(analysis-report)
已加载 skill，等待重新规划

正在按 analysis-report workflow 建立执行计划。

计划 0/5
⚡ 1. 读取并解析 CSV 数据
⬜ 2. 进行信息提取、维度识别和模式发现
⬜ 3. 进行多维分析和归因分析
⬜ 4. 生成 HTML 分析报告
⬜ 5. 保存报告到 ~/Downloads/a.html

Read(TEST_DATA.csv)
已读取 TEST_DATA.csv，预览 10 行
```

### B. 无 Assistant Update 的 fallback 回合

如果模型只返回 tool calls，没有任何用户可见文本，则系统必须补一条 fallback，但这条 fallback 仅用于显示，不写入 `conversation_messages`。

fallback 的生成逻辑在本设计中明确锁定为：

- 使用静态模板映射，而不是让模型再生成一句解释
- 模板只按工具名分类，不尝试推断更深层意图
- 若同一批有多个工具，则按工具顺序保守拼接为“先 A，再 B”
- 若首个工具已经足以代表本批主要动作，则允许只显示首个工具 fallback，避免句子过长

模板必须是保守的，只描述动作，不描述深层意图。例如：

- `先加载 skill，再重新评估下一步。`
- `先更新当前计划。`
- `先读取文件内容。`
- `先执行命令并查看结果。`

示例：

- `["skill"]` -> `先加载 skill，再重新评估下一步。`
- `["todo"]` -> `先更新当前计划。`
- `["read_file"]` -> `先读取文件内容。`
- `["skill", "todo"]` -> `先加载 skill，再重新评估下一步；然后更新当前计划。`

fallback 生成位置建议在 query loop 即将执行 batch 之前，基于解析后的第一组 tool calls 生成。

额外约束：

- fallback 在 `compact` 模式下必须可见
- fallback 不属于 runtime trace，不能因为隐藏 `[Runtime] ...` 而一起被隐藏

### C. Todo 的特殊展示规则

`todo` 不应只显示成 `Todo(updated)`。

它的展示优先级规则如下：

1. 若 `todo_state.items` 非空且与上次展示不同，显示完整计划视图
2. 若计划未变化但存在 `in_progress` 项，显示当前聚焦项
3. 若计划刚刚清空且存在 `last_completed_items`，显示完成总结
4. 只有在 debug/verbose 下，才额外显示机械的 `Todo(updated)` 事件

## debug / verbose 模式

本设计不删除现有 runtime trace，只调整默认可见性。

建议把 `RunDisplayOptions` 从单一 `quiet: bool` 扩展为最小分层显示配置，例如：

```python
from typing import Literal

@dataclass(frozen=True)
class RunDisplayOptions:
    quiet: bool = False
    runtime_trace: Literal["compact", "debug"] = "compact"
```

语义如下：

- `quiet`
  - 完全静默，不显示中间 UI
- `compact`
  - 默认模式
  - 显示 Assistant Update / Todo State / Tool Event / Progress
  - 不显示 `[Runtime] 收到/分批/串行/完成`
- `debug`
  - 在 compact 之上保留完整 runtime trace
  - 供开发和定位调度问题使用

换句话说，当前这些信息：

```text
[Runtime] 收到 1 个 tool_call，分为 1 批
[Runtime]   Batch 0: [串行] ['todo']
[Runtime] ▶ 串行执行写工具：todo
[Runtime]   ✓ todo 完成 (0.00s, success=True)
```

在未来仍可保留，但只应出现在 `debug` 模式。

## 对现有架构的保护约束

为了避免“做完显示优化，架构又乱掉”，本设计锁定以下约束。

### 约束 1：QueryLoop 仍然是 conversation messages 的唯一主写入点

以下内容可以进入 `conversation_messages`：

- user message
- assistant message
- tool result
- injected skill runtime message
- policy reminder

以下内容默认不得进入 `conversation_messages`：

- runtime 调度 trace
- 紧凑 tool event
- fallback tool preamble
- 纯 UI 进度提示

### 约束 2：ToolExecutorRuntime 仍然不负责 transcript 管理

`ToolExecutorRuntime` 可以：

- 执行工具
- 聚合结果
- 调用 renderer 展示 UI-only 反馈

但不能：

- 直接写 `conversation_messages`
- 伪造 assistant update
- 自行持久化额外的 tool transcript 结构

### 约束 3：todo 的真实状态继续只在 SessionState

不能为了做展示方便，再造一份独立的 todo display cache 作为权威状态。

可以存在：

- `RunState` 上的上次展示快照
- 当前轮暂存 display hint

但权威计划状态只能是：

- `SessionState.todo_state.items`
- `SessionState.todo_state.last_completed_items`

推荐新增的 run-scoped 字段形态：

```python
last_displayed_todo_items: list[TodoItem] | None = None
```

它只服务于“本次 run 内是否需要重复展示完整计划”的判断，不承担任何状态写回职责。

### 约束 4：thinking 与 assistant update 严格分层

thinking 仍然是：

- 可选
- 调试/理解模型过程的能力

assistant update 必须是：

- 默认用户可见
- 不依赖 thinking 开关

不能接受任何“用户想看解释就去开 thinking”的回退。

## 当前代码层面的直接调整方向

这份 spec 不展开逐步 implementation plan，但会锁定几个必须发生的高层实现方向。

### 方向 1：QueryLoop 必须显示带 tool_calls 的 assistant content

当前 [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 需要修正一个关键行为：

- 当 `model_resp.tool_calls` 非空且 `model_resp.content` 非空时，也必须向 renderer 输出 assistant text

这一步是本设计的主路径基础。

### 方向 2：runtime 的 stdout trace 需要进入可控 display mode

当前 [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 中大量 `sys.stdout.write(...)` 的调度 trace，需要迁移到：

- `debug` 模式才显示
- 默认 compact 模式不显示

否则默认体验无法真正从“内部流水账”转向“用户可理解事件流”。

### 方向 3：renderer 需要有工具级标签和摘要规则

当前 [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py) 中：

- `show_tool_call` 主要是原始参数摘要
- `show_tool_result` 主要是 output preview

后续应增加稳定的 label / summary 规则，例如：

- `skill` 优先显示 `Skill(<id>)`
- `read_file` 优先显示 basename
- `bash` 优先显示 `description`，其次才是命令摘要
- `todo` 默认不走普通工具事件主路径，而走计划视图

本设计在归属层上明确锁定为：

- 继续保持 `Renderer.show_tool_call(name, args)` / `show_tool_result(name, output)` 现有接口
- tool label 的生成规则归属 `renderer` 层
- 不新增 per-tool protocol，不要求 runtime 预先生成 label
- 当前 [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py) 中的 `_summarize_tool_args(...)` 是后续演进入口

### 方向 4：todo 的显示规则需要显式固定

当前 runtime 对 todo 的 `show_progress` / `show_completion_summary` 触发时机已经存在，但还不够系统化。

后续必须把以下规则固定下来：

- 什么时候显示完整计划
- 什么时候只显示当前项
- 什么时候显示完成总结
- 什么时候 suppress 普通 todo tool event

### 方向 5：Progress / Outcome 继续复用现有 renderer 接口

本设计不为 progress 引入新的 renderer protocol 方法。

锁定规则如下：

- 长耗时执行中的简短进度，复用 `Renderer.show_status(message)`
- runtime 负责把进行中状态格式化为用户可读字符串
- 完成后的紧凑结果摘要，继续走 `show_tool_result(name, output)` 或 todo 专用渲染

因此，当前 [`core/shared/interfaces.py`](/Users/kino/works/kino/harness/core/shared/interfaces.py) 的 `Renderer` 接口不需要为本设计新增 `show_tool_progress(...)` 之类的新方法

## 验收标准

完成本设计后，应满足以下验收标准：

1. 关闭 thinking 后，典型 skill + todo + read_file 场景下，用户仍能看懂系统先做什么、后做什么。
2. 默认界面中不再出现 `[Runtime] 收到/Batch/串行执行/完成` 这类调度 trace。
3. `todo` 计划视图仍然完整可见，不因工具事件压缩而退化。
4. assistant 带 tool_calls 的回合若有文本内容，文本会被用户看到。
5. 当模型未提供 assistant content 时，compact 模式下仍然会显示 fallback 级动作说明。
6. debug 模式仍能查看完整 runtime 调度信息。
7. 本次改动不引入新的 transcript 写入路径，不破坏 runtime control plane 与 todo session-state 的既有结构。

## 风险与边界

### 风险 1：模型仍然可能不给 Assistant Update

这无法靠 UI 完全解决，因此需要 fallback。

但 fallback 只能是保守动作说明，不能伪装成模型真实意图。

### 风险 2：如果把 too much UI-only 信息写回 transcript，会污染后续回合

这也是为什么本设计反复强调：

- 解释性 assistant text 可以入 transcript
- tool event / progress / fallback UI status 默认不能入 transcript

### 风险 3：若 `todo` 也被当普通工具压缩，计划可见性会立刻下降

因此 `todo` 必须继续保留特殊展示路径。

## 结论

这次不是“把 runtime 日志写得更好看”。

真正要做的是把默认终端体验从：

- thinking
- 工具调用
- runtime 调度 trace

改成：

- `Assistant Update`
- `Todo State`
- `Tool Event`
- `Progress / Outcome`

并且严格复用现有架构：

- assistant update 走现有 assistant 文本通道
- todo state 继续来自 `SessionState`
- tool event / progress 作为 UI-only 展示
- runtime trace 退到 debug 模式

一句话总结：

> 默认模式展示“人能理解的工作过程”，debug 模式才展示“系统如何调度实现它”。
