# Todo / Task Parity 设计

> 日期: 2026-04-17
> 状态: 待评审
> 依赖:
> - [`docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-skill-and-task-runtime-parity-design.md)
> - [`docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)
> - [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)
> 当前相关实现:
> - [`core/tools/builtin/todo.py`](/Users/kino/works/kino/harness/core/tools/builtin/todo.py)
> - [`core/policy/todo_tracking.py`](/Users/kino/works/kino/harness/core/policy/todo_tracking.py)
> - [`core/prompt/system_context.py`](/Users/kino/works/kino/harness/core/prompt/system_context.py)

## 摘要

Phase 2 保留当前的 `todo` 工具名，但要把它从一个薄薄的 checklist writer，升级成一个可靠的任务跟踪控制点，并且它的行为必须建立在 phase 1 已经完成的 skill expansion 之上。

核心思想是：

- skill 决定工作流结构
- todo 用来镜像并跟踪这个工作流
- policy 与 tool guidance 用来保持计划新鲜
- stale plan reminder 用来纠偏
- 整体设计对未来更丰富的 task model 保持兼容，但现在不强行迁移

从平台视角看，todo parity 的真正前提不只是更强的 prompt，还包括 Runtime control plane：todo 的 replan flags、stale reminders、plan snapshot 与 verification nudge 都依赖同一套运行时协议。详见：
[`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)

这份设计明确不是要把 `harness` 一步改造成 Claude Code 的完整 task system。它是一个面向当前问题的对齐方案，专门解决用户已经验证过的失败模式：

> 模型会说它要使用某个 skill，但最终写出来的 todo 仍然只有 1 到 2 个大项，把 skill 内部工作流压扁了。

## 本设计记录的用户语境

以下约束和观察，都来自我们之前已经达成一致的讨论：

1. 问题不只是 skill 加载弱。用户已经明确指出，当前 todo 的提示也过于简陋。
2. 用户不接受通过重写 skill 文件来辅助 todo 拆解。
3. 用户不接受把大量 Hook 作为主解决方案。
4. 用户希望 skill 与 todo 作为一个整体的控制流问题来改，但可以分阶段落地。
5. 用户已经指出过一个非常关键的回归：
   - 在没有 skill 系统时，直接让模型去读 skill 目录，反而更容易生成结构化 todo
   - 在当前 skill 改造之后，模型虽然能“识别 skill”，却更容易只写出 1 到 2 个泛化 todo
6. 用户明确点名过 `2.5` 这种工作流细节，要求它不能在 todo 中被抹掉。

最后一点极其重要。只要有意义的 workflow labels 被 todo 压缩掉，本阶段就不能算成功。

## 为什么必须单独写 Todo Spec

总纲文档有意停在了 phase 边界：

- phase 1 先解决 skill timing
- phase 2 再解决 planning quality 和 task tracking

这种拆分是对的，但如果没有 todo 的单独 spec，reviewer 就会缺一块最关键的内容：

- `harness` 如何判断什么时候必须使用 `todo`
- skill barrier 之后，第一版计划如何被约束
- 一个 6 步 skill 如何稳定变成 6 步 todo，而不是 2 步 todo
- reminder 如何工作，且不会退化成 Hook 驱动补偿
- 当前简单的 `todo` 工具如何演进，而不会一上来就被迫切 full task graph

这份文档正是用来回答这些问题的。

## 来自 Claude Code 的直接证据

本设计依赖本地 Claude Code 源码镜像中的直接观察：
`/Users/kino/works/opensource/Claude-Code-doc`

### 证据 1: Todo 行为主要由 tool prompt 强驱动

Claude Code 的 `TodoWriteTool` 有一大段 prompt，里面明确规定了：

- 什么时候主动用
- 什么时候不要用
- 多个使用示例
- 状态管理规则
- 实时更新规则
- “恰好一个 in_progress” 约束

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TodoWriteTool/TodoWriteTool.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TodoWriteTool/prompt.ts`

### 证据 2: Todo 还会被全局 prompt 再强化一次

Claude Code 的系统 prompt 在 “Doing tasks” 和 “Using your tools” 段落中，都对 task tracking 有显式指导。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/constants/prompts.ts`

### 证据 3: Todo 还有 stale-usage reminders

Claude Code 会计算“距离上一次 `TodoWrite` 已经过了多少 assistant turns”，然后在合适的时机注入 reminder attachments。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/utils/attachments.ts`

### 证据 4: Todo 工具结果本身也带 nudge

Claude Code 的 `TodoWriteTool` 结果不会只说“写成功了”，还会提醒模型：

- todos 已更新
- 继续使用 todo 跟踪进度
- 按当前任务继续推进

在某些情况下，如果一个多项计划被关闭时没有 verification task，还会额外给 verification nudge。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TodoWriteTool/TodoWriteTool.ts`

### 证据 5: Claude Code 已经开始向完整 task system 演进

Claude Code 中已经存在 `TaskCreate`、`TaskUpdate`、`TaskList`、`TaskGet` 这类更丰富的 task tools。

相关文件：

- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TaskCreateTool/prompt.ts`
- `/Users/kino/works/opensource/Claude-Code-doc/src/tools/TaskUpdateTool/prompt.ts`

这说明 task tracking 在 Claude Code 里是一个真正的子系统，不是一句系统提示词能解决的事情。

但这并不等于 `harness` 现在就必须直接跳到完整 task graph。

## 证据边界

为便于 review，本设计区分两类内容。

### 直接源码证据

下面这些由 Claude Code 源码直接支持：

- todo/task 管理是多层强化，而不是单点强化
- todo tool prompt 非常强，不是简短 schema 描述
- stale task tracking 会收到 reminders
- tool result 本身也可以继续施加计划行为约束
- 整体方向确实是在从简单 todo 走向更丰富的 task tools

### harness 的推断与设计选择

下面这些不是照抄 Claude Code，而是根据 `harness` 当前实现作出的选择：

- phase 2 继续保留 `todo` 这个工具名
- 先提升计划质量，再考虑完整 file-backed task system
- 把 todo state 从模块全局迁移到 `SessionState`
- 通过 `workflow_ref` 保留 skill 的工作流标签，而不是立刻引入完整 task graph
- 用 reminder 与更强 guidance 提升效果，但不把硬 gating 当成主方案

## 当前 Harness 的 Todo 问题

当前 `harness` 的 todo 设计，在四个层面都偏弱。

### 1. 全局 prompt 约束太薄

当前系统提示词大意只有一句：

> 多步骤任务使用 `todo`，保持一个 `in_progress`，每步完成后更新

相关文件：

- [`core/prompt/system_context.py`](/Users/kino/works/kino/harness/core/prompt/system_context.py)

这句规则方向没错，但远不足以塑造真实规划行为。

### 2. `todo` 工具描述太薄，承载不了工作流纪律

当前 schema description 只有：

> Rewrite the current session plan for multi-step work.

相关文件：

- [`core/tools/builtin/todo.py`](/Users/kino/works/kino/harness/core/tools/builtin/todo.py)

它无法教会模型：

- 什么时候创建计划
- 如何控制粒度
- 如何保留 skill 结构
- 如何处理 blocker
- 如何在执行过程中持续更新

### 3. Todo state 存在模块级单例里

当前 planning state 保存在 `todo.py` 内部 `_state`。

这会带来三个架构问题：

- 它不是天然 session-scoped
- 它和其余 runtime state 不一致
- 它会让后续 compact/session 恢复更难做

### 4. Reminder policy 过于泛化

当前 reminder 逻辑是：

- 统计连续多少个 tool batches 没有调用 `todo`
- 到 3 之后，注入一条泛化的“重新评估计划”提醒

相关文件：

- [`core/policy/todo_tracking.py`](/Users/kino/works/kino/harness/core/policy/todo_tracking.py)

这条提醒并不知道：

- 前一步是不是刚展开了 skill
- 当前是否已经存在一份合理计划
- 计划是否真的 stale
- 当前任务是否真的值得 todo 跟踪

## 设计目标

Phase 2 结束后，应出现以下用户可见行为：

1. 在相关 skill expansion 之后，第一版真正有意义的计划会在 skill 上下文下写出。
2. 如果 skill 编码了多个工作流阶段，todo 会反映这些阶段，而不是压缩成模糊大项。
3. 计划会随着工作推进持续更新。
4. 当模型开始偏离计划时，会收到更有针对性的提醒。
5. 当前 `todo` 工具仍然可用，不要求本阶段直接切换到 `TaskCreate` / `TaskUpdate`。
6. 本阶段的设计不会阻断未来向 richer task model 迁移。

## 非目标

Phase 2 明确不包含：

- file-backed task storage
- 多 agent owner、依赖关系、任务分配
- 完整的 `TaskCreate` / `TaskUpdate` / `TaskList` parity
- compact 后 todo state 恢复
- 从 skill 文件中完全自动提取 task structure，而不经过模型判断
- 通过 hooks 或 gates 硬性阻塞所有非 todo 工具

## 候选方案比较

### 方案 A: 只加强当前 `todo` 的 prompt

改动内容：

- 扩写 `todo` description
- 微调 system prompt

优点：

- 成本低
- 代码改动少

缺点：

- 无法解决模块级单例 state
- 无法解决 stale reminder 质量问题
- 无法把 post-skill timing 和 first-plan 规则显式串起来
- 对后续 compact/session work 价值有限

结论：

- 不够

### 方案 B: 保留 rewrite 语义，但把 `todo` 提升为 session-scoped 的 planning subsystem

改动内容：

- 继续使用 `todo`
- 加强 prompt guidance
- 把 state 移到 `SessionState`
- 增加 plan quality rules
- 增加 post-skill planning rules
- 升级 reminders 与 tool results

优点：

- 直接命中用户反馈的问题
- 不会过早引入 full task system
- 同时为未来 richer task tools 留下迁移空间

缺点：

- 会波及 prompt、policy、session state、renderer、tool behavior 多个层面

结论：

- 推荐

### 方案 C: 直接用完整 task tools 取代当前 `todo`

改动内容：

- 废弃 rewrite-style `todo`
- 引入 create/update/list/get 任务工具
- 直接走向 task graph

优点：

- 长期结构最强
- 更接近 Claude Code 的新方向

缺点：

- 范围过大
- 会一次性引入 IDs、owner、persistence、migration、UI 重做等复杂面
- 对当前问题而言超出必要范围

结论：

- 延后，等 phase 2 先验证 planning rules

## 推荐方案

Phase 2 应采用 **方案 B**。

整体设计分成五部分：

1. 更强的 todo guidance
2. 更好的 todo 数据形状
3. post-skill 的 first-plan 规则
4. stale-plan reminders
5. session-scoped 的 todo state

## 第一部分：更强的 Todo Guidance

### 全局 prompt 的职责

在 [`core/prompt/system_context.py`](/Users/kino/works/kino/harness/core/prompt/system_context.py) 中保留短小的全局规则，但要把时序写清楚：

- 对非平凡、多步骤工作要主动使用 `todo`
- 如果一个 skill 刚展开，而任务明显是多步骤，则在继续深入执行之前先更新 `todo`
- 执行过程中保持恰好一个 `in_progress`
- scope 变化时立即更新计划

全局 prompt 仍然应该简短。它的职责是建立义务，不是承载完整教程。

### Tool description 的职责

主要行为约束应转移到 `todo` 工具自身的 description 中。

`harness` 当前没有像 Claude Code 那样独立的 `tool.prompt()` 通道。因此 phase 2 应把 `SCHEMA["description"]` 当成主要的模型行为载体，并显著扩写。

新的 `todo` description 应覆盖：

- 什么时候使用
- 什么时候不要使用
- 粒度规则
- 状态规则
- 实时更新规则
- blocker 处理
- 示例
- post-skill planning 指导
- workflow label 保留指导

这是一个明确的 `harness` 适配，不是逐字复制 Claude Code 的 `TodoWriteTool` prompt。

### 必须写入 description 的行为规则

新的 `todo` description 至少要包含以下规则：

1. 对非平凡、多步骤工作使用 `todo`
2. 收到新指令后，应尽快刷新计划
3. 开始工作时，必须已经有一个 task 是 `in_progress`
4. task 真正完成后应立即标记完成，不要批量积压
5. 部分完成、被阻塞、未验证完成时，不允许标记为完成
6. 如果当前 skill 隐含工作流，todo 必须镜像该工作流，而不是重新发明一套大项
7. 像 `2.5` 这样的有意义 workflow label 必须保留
8. 如果完成定义依赖验证，就必须有 verification task
9. 不再相关的任务应从计划中移除
10. 计划一旦与当前工作脱节，必须重写，而不是静默漂移

## 第二部分：更好的 Todo 数据形状

Phase 2 继续保留 rewrite 语义：

- 每次 `todo` 调用都用来替换当前 active plan

这在本阶段已经够用。真正要变化的是 item 结构。

### 新 item schema

把当前最小结构：

```json
{ "content": "...", "status": "pending" }
```

升级为：

```json
{
  "content": "Run report cross-checks",
  "active_form": "Running report cross-checks",
  "status": "in_progress",
  "workflow_ref": "2.5"
}
```

### 字段语义

- `content`: 祈使句形式的任务描述，给用户看
- `active_form`: 进行时形式，用于 renderer 与当前焦点显示
- `status`: `pending`、`in_progress`、`completed`
- `workflow_ref`: 可选的工作流标签，例如 `1`、`2`、`2.5`、`A`、`Appendix-B`

### 为什么 `workflow_ref` 重要

这是在不要求 skill 作者修改文件格式的前提下，保留工作流结构的最简单办法。

例如：

- skill 中的 `2.5` 不应该被压成 “Continue analysis”
- 应当允许 todo 明确保留 `workflow_ref="2.5"`

这样会带来两个好处：

- 用户可以直接看出 todo 是否真的跟随了 skill
- 后续 compact/session 恢复也能继续保留同样结构

但 phase 2 不应把计划正确性建立在 `workflow_ref` 一定存在之上。

更准确的约束是：

- `workflow_ref` 是一个可选的结构保真字段
- 当模型能够稳定识别真实 workflow label 时，应填写
- 当模型不确定时，应省略，而不是编造

因此：

- `workflow_ref` 用来提升可观察性
- 不是 phase 2 正确性的唯一支柱

### 状态模型

Phase 2 应将对模型暴露的状态模型收敛为：

- `pending`
- `in_progress`
- `completed`

当前的 `failed` 状态应从 model-facing schema 中移除。

原因：

- Claude Code parity 的主语义是 progress tracking，而不是 failure terminal state
- 被阻塞的任务应继续保持 `in_progress`
- 如果真的有 blocker，模型应新增一个描述 blocker 的 task，而不是把原任务直接标成失败

### Item 数量上限

将当前上限从 `12` 提高到 `20`。

原因：

- 当前限制本身就会迫使模型压缩计划
- 六阶段 skill 再加验证与收尾，很容易超过 12

## 第三部分：把 Todo State 移到 SessionState

### 新的 session-scoped state

在 [`core/session/state.py`](/Users/kino/works/kino/harness/core/session/state.py) 中增加：

```python
@dataclass(slots=True)
class TodoItem:
    content: str
    active_form: str
    status: str
    workflow_ref: str | None = None


@dataclass(slots=True)
class TodoState:
    items: list[TodoItem] = field(default_factory=list)
    last_completed_items: list[TodoItem] = field(default_factory=list)
    last_write_turn: int | None = None
    last_reminder_turn: int | None = None
```

然后在 `SessionState` 中增加：

```python
todo_state: TodoState = field(default_factory=TodoState)
```

### 为什么必须是 session state

todo state 和 invoked skill record 一样，都属于：

- 会话级事实
- 会影响后续模型行为
- 不应存在模块全局单例中

### 需要移除的模块级 helper

Phase 2 主路径应移除对以下对象的依赖：

- `todo._state`
- `save_snapshot()`
- `restore_snapshot()`
- `clear_state()`
- `increment_rounds()`
- `reset_rounds()`

这些职责应迁移到 `SessionState.todo_state` 以及新的 run policy 中。

### Renderer 行为

`core/tools/runtime.py` 在 `todo` 写入成功后，不应再通过 `todo.get_state()` 读取计划。

应改为从：

- `context.session_state.todo_state.items`

读取。

这样可以去掉跨模块隐藏耦合，也让 runtime state 更一致。

## 第四部分：Post-Skill 的 First-Plan 规则

这是 phase 2 最关键的控制流规则。

### 基本规则

只要 phase 1 报告了 `skill_expanded` barrier，下一次规划决策就必须把这个展开后的 skill 当成工作流权威来源。

这意味着：

- 如果 skill 明显隐含多步骤工作流，下一版 plan 必须在该工作流之下编写
- todo 不允许重新发明一套与 skill 无关的高层计划
- todo 也不允许在 skill 已展开后继续被拖延多个工具调用，导致执行脱离计划

### 实际 first-plan 规则

在 `skill_expanded` barrier 之后：

1. 允许做少量轻量级 scoping reads
2. 但在继续深入执行、修改文件、或进入长工具链之前，模型应先写或刷新 `todo`
3. 生成出来的 todo 必须尽量镜像 skill 提供的 workflow structure

这样设计是务实的：

- 它不要求模型在完全没有任何轻量观察前就盲写 todo
- 但它阻止模型“先做一半，再随手补一个大而粗的 todo”

### `RunState` 新字段

在 [`core/query/state.py`](/Users/kino/works/kino/harness/core/query/state.py) 中增加：

```python
todo_replan_required: bool = False
todo_replan_reason: str | None = None
```

当 query loop 处理到 batch barrier 时：

- 如果 barrier reason 是 `skill_expanded`
- 则设置 `todo_replan_required = True`
- 可选设置 `todo_replan_reason = "skill_expanded"`

当一次有效的 `todo` 写入发生后，这个标志应被清除。

这个标志的生命周期必须明确：

- `todo_replan_required` 是 per-query-run 的短期控制标志
- 它只负责承接“刚刚发生 skill barrier”的近场重规划需求
- 如果进入后续新的 query run，仍然存在“计划已偏离工作流”的问题，应由 stale-plan reminder 兜底，而不是依赖旧 run 的 flag 持续存在

### 为什么这不是 Hook 补偿

这里没有引入新的外部 Hook 系统。
它只是把 phase 1 已经同意引入的 barrier 语义，延续到下一步规划决策。

这不是补丁，而是 phase 1 控制流设计的自然补完。

## 第五部分：Reminder 与 Nudge 设计

### 替换当前的泛化 reminder policy

当前 reminder 行为过于笼统。

Phase 2 应用一个更丰富的 `TodoPlanningPolicy` 替换 [`core/policy/todo_tracking.py`](/Users/kino/works/kino/harness/core/policy/todo_tracking.py)。

### 新 policy 的职责

新的 policy 应该知道：

1. 当前是否存在 todo plan
2. 上一次 todo 写入发生在什么时候
3. 当前是否处于 post-skill replan 状态
4. 只有在相关时机才注入 targeted reminder

### Reminder 适用信号

为了避免 reminder 退化成拍脑袋的“复杂度分类器”，phase 2 应让 reminder 触发条件保持收敛。

自动 reminder 只在以下任一条件满足时触发：

1. 已经存在 todo plan，而且它可能 stale
2. `todo_replan_required` 为真，说明前一步刚发生 skill barrier

对于“没有 skill 的复杂任务第一次该不该建 todo”，phase 2 仍主要依赖：

- 更强的全局 prompt
- 更强的 `todo` tool description
- 模型自己的判断

policy 本身不应该在 phase 2 里演变成通用复杂度判别器。

### Reminder 类型

Phase 2 支持两类 reminder。

#### A. Post-skill planning reminder

触发条件：

- 在下一次模型调用前，`todo_replan_required` 为真

行为：

- 注入一条 synthetic reminder，告诉模型工作流上下文刚变化，应该在继续深入执行前重新规划

推荐形态：

```xml
<system-reminder type="todo_replan">
Skill context changed on the previous step. Re-evaluate the plan under the active skill guidance. If the work is multi-step, update the todo list before continuing deeper execution.
</system-reminder>
```

#### B. Stale-plan reminder

触发条件：

- 当前存在 todo plan
- 连续数个 assistant turns 没有发生 todo 写入

行为：

- 注入带当前 plan snapshot 的 reminder
- 明确告诉模型，如果计划已经和工作进度脱节，就应重写

推荐形态：

```xml
<system-reminder type="todo_stale">
The todo list has not been updated recently. If your work has progressed or changed scope, rewrite the todo list now.
Current plan:
- [in_progress] 2.5 Cross-check findings
- [pending] 3 Draft final report
</system-reminder>
```

### Reminder 的传输方式

继续使用 synthetic `user` messages，内容中包 `<system-reminder>`。

原因：

- 这和当前 harness 的 policy 注入方式一致
- 不会把重复 reminder 合并进顶层 system prompt
- reminder 内容会明确留在 conversation history 中，便于调试

### Reminder 阈值

初始推荐阈值：

- post-skill replan reminder: 下一次模型调用就触发
- stale-plan reminder: 当已有计划连续 4 个 assistant turns 未更新时触发

这里需要明确区分“现状”“Claude Code 实际值”和“设计选择”：

- 当前 `harness` 现状是 3 个 tool batches 后提醒
- Claude Code 的 `TodoWrite` stale reminder 使用更长的 assistant-turn 阈值
- phase 2 选择 `4` 不是在声称“与 Claude Code 完全一致”，而是基于 `harness` 当前 query 节奏做的折中设计

之所以不直接取 `2`：

- 当前 `harness` 在分析型任务里常常需要连续多次 `read_file` / `find`
- 如果 reminder 太激进，会在正常的信息收集阶段产生噪音

之所以不直接照搬更大的阈值：

- 当前 `harness` 还没有 Claude Code 那种更成熟的侧边任务面板与更强的 tool prompt
- 因此需要比 Claude Code 更早一点提醒，但不能早到打断正常收集节奏

## 计划质量规则

这是本次 redesign 的行为核心。

### 规则 1: 计划要围绕“可完成结果”，而不是模糊大项

坏例子：

- “Analyze codebase”
- “Write report”

好例子：

- “Read target inputs and confirm scope”
- “Extract evidence for section 2 findings”
- “Cross-check claims for section 2.5”
- “Draft final report”
- “Verify report completeness and references”

### 规则 2: 一个 item 应该对应一个明确 completion event

每个 todo item 都应当有一个清晰的“什么时候算完成”的时刻。

如果一个 item 覆盖了多个独立里程碑，那它就太大了。

### 规则 3: 保留有意义的 workflow labels

只要 skill 或用户输入中存在有意义的阶段标签，就要保留它们。

例如：

- `workflow_ref="2.5"`
- `content="Cross-check claims for section 2.5"`

重点不在于数字本身，而在于不要把数字背后的工作流边界擦掉。

### 规则 4: 只要存在 active skill structure，todo 就必须尽量跟随它

如果 skill 定义了工作流，todo 应尽量镜像它，除非存在非常明确的理由不这样做。

todo 不能把：

- 六个 skill stages

压缩成：

- “Do analysis”
- “Write summary”

这种压缩会直接毁掉 skill 的价值。

### 规则 5: 只要完成定义包含验证，就必须显式建 verification task

如果任务的 done condition 包含验证、测试、交叉核对、证据检查，就必须有独立 verification task。

适用范围包括：

- 代码修改
- 分析报告生成
- 依赖文件证据支撑的结论

### 规则 6: 只要有 active work，就必须恰好一个 `in_progress`

一旦计划存在且工作仍在进行：

- 恰好一个 item 为 `in_progress`
- 其他 item 只能是 `pending` 或 `completed`

如果所有工作都完成：

- 模型可以提交空列表
- 或提交全 `completed` 列表

handler 应把这两种情况都规范化为：

- active plan 为空
- 最后一版 completed snapshot 保存到 `last_completed_items`

对于当前 `harness` 的 renderer，这还需要一个补充行为：

- 当 active plan 为空，但 `last_completed_items` 在本轮刚刚生成时，不应让任务面板直接“消失”
- 应复用现有 [`core/ui/renderer.py`](/Users/kino/works/kino/harness/core/ui/renderer.py) 中的 `show_completion_summary(...)` 能力，显示一次明确的完成总结

否则从用户视角看，会像是 todo 突然被清空，而不是“已完成并归档”。

## Worked Example: `analysis-report`

假设 `analysis-report` skill 隐含如下工作流：

1. 收集目标范围
2. 执行主分析
2.5. 交叉核对发现
3. 起草报告
4. 验证输出质量

一个错误的 phase 2 plan 可能是：

1. Analyze repository
2. Write report

一个符合 phase 2 要求的 plan 应更接近：

```json
{
  "items": [
    {
      "content": "Collect target inputs and confirm report scope",
      "active_form": "Collecting target inputs and confirming report scope",
      "status": "completed",
      "workflow_ref": "1"
    },
    {
      "content": "Perform primary analysis for the report",
      "active_form": "Performing primary analysis for the report",
      "status": "completed",
      "workflow_ref": "2"
    },
    {
      "content": "Cross-check findings for section 2.5",
      "active_form": "Cross-checking findings for section 2.5",
      "status": "in_progress",
      "workflow_ref": "2.5"
    },
    {
      "content": "Draft the final analysis report",
      "active_form": "Drafting the final analysis report",
      "status": "pending",
      "workflow_ref": "3"
    },
    {
      "content": "Verify report completeness and evidence coverage",
      "active_form": "Verifying report completeness and evidence coverage",
      "status": "pending",
      "workflow_ref": "4"
    }
  ]
}
```

这就是 phase 2 应当让其“成为常态”的结构水平。

## Tool Result 行为

`todo` 工具的成功结果不应再只是一个非常短的进度字符串。

它仍然应该短，但必须带有继续保持计划同步的行为提醒。

推荐成功输出：

```text
Todo plan updated successfully. Continue using the todo list to track progress. Keep it aligned with the active workflow and update it as tasks complete or scope changes.
```

### Verification nudge

如果模型关闭了一个多项计划，但计划中没有 verification-oriented item，则追加一段短提醒：

```text
Before finalizing, make sure required verification is represented in the plan if the task is not yet fully validated.
```

这是对 Claude Code verification nudge 的 `harness` 适配版。

## 与未来 Task Tools 的关系

Phase 2 故意保留 rewrite-style `todo`，但它会为未来 richer task tools 做好准备。

### Phase 2 会为未来保留什么

- `active_form`
- `workflow_ref`
- session-scoped task state
- 基于真实 stale 状态的 reminder policy
- plan snapshots

这些都可以在未来自然映射到：

- task IDs
- task update tools
- task dependencies
- owner assignment
- file-backed persistence

而不需要推翻 phase 2 的成果。

### Phase 2 不做什么

Phase 2 不会假装 rewrite-style `todo` 就是最终形态。
它只是当前 `harness` 在正确范围内的中间架构。

## 文件级变更计划

### 修改文件

- `core/tools/builtin/todo.py`
  - 大幅扩展 tool description
  - 为 `active_form` 与可选 `workflow_ref` 更新输入校验
  - 从 model-facing schema 中移除 `failed`
  - 把 plan 写入 `SessionState.todo_state`
  - 将“全 completed 列表”规范化为空 active plan，并保留 completed snapshot
  - 输出更强的 success text

- `core/session/state.py`
  - 增加 `TodoItem`
  - 增加 `TodoState`
  - 在 `SessionState` 中增加 `todo_state`

- `core/query/state.py`
  - 增加 `todo_replan_required`
  - 增加 `todo_replan_reason`

- `core/query/loop.py`
  - 在 barrier reason 为 `skill_expanded` 时设置 replan flags
  - 在成功 todo 写入后清除 replan flags

- `core/policy/todo_tracking.py`
  - 替换为更丰富的 todo-planning behavior
  - 注入 targeted replan reminders
  - 注入带 plan snapshot 的 stale-plan reminders

- `core/prompt/system_context.py`
  - 加强简短的全局 todo guidance
  - 移除任何仍然假设 delayed `activate_skill` 的表述

- `core/prompt/assembler.py`
  - 修复 stable prompt cache key，避免 `_FRAMEWORK_PROMPT` 或其他 system prompt 文本变更后仍命中旧缓存

- `core/tools/runtime.py`
  - 从 `SessionState.todo_state` 渲染 todo progress
  - 不再读取模块全局 todo state

- `core/ui/renderer.py`
  - 对当前 `in_progress` item，可优先显示 `active_form`
  - 当 plan 规范化为空但存在 `last_completed_items` 时，显示完成总结而不是直接隐藏

### 可选新增文件

- `core/session/todo.py`
  - 如果 session-state todo 逻辑过大，可以把规范化与 reminder helper 抽到这里

这一步是可选的。设计追求的是清晰，不是为了抽文件而抽文件。

## 测试策略

### 单元测试

1. `todo` 会拒绝缺失 `active_form`
2. `todo` 接受可选 `workflow_ref`
3. `todo` 会拒绝多个 `in_progress`
4. `todo` 会拒绝非法状态值
5. `todo` 会把 plan 写入 `SessionState.todo_state`
6. 全 completed 列表会被规范化为空 active plan，并保留 `last_completed_items`

### Policy 测试

1. `skill_expanded` 会设置 `todo_replan_required`
2. 下一次模型调用会收到 post-skill todo reminder
3. 成功 `todo` 写入会清除 `todo_replan_required`
4. stale-plan reminder 只会在已有计划且确实 stale 时触发
5. stale-plan reminder 会包含当前 plan snapshot

### Prompt 测试

1. stable system prompt 包含更强的 todo guidance
2. `todo` 的 schema description 包含扩展后的行为约束
3. 修改 system prompt 文本后，stable prompt cache 不会错误复用旧内容

### 行为回归测试

1. 基于固定 transcript / 固定模型桩的场景回放，验证 stale reminder 不会在正常 1-3 步信息收集内过早触发
2. `workflow_ref` 缺失时，plan 仍可被正常接受、渲染、更新
3. plan 归零但有 completed snapshot 时，renderer 会显示完成总结而不是直接空白

### 集成测试

1. 在 `analysis-report` skill expansion 之后，第一版 todo plan 必须保留阶段结构，而不是压成 1 到 2 个泛化项
2. `2.5` 这样的步骤可以保留进 `workflow_ref`
3. 如果模型连续多轮工作却没有更新计划，会出现 stale-plan reminder
4. 如果用户在中途改变 scope，todo 可以被干净地重写

## 风险

### 1. 更强的 todo prompt 依然不保证 100% 被遵循

这是预期内的。本阶段并不承诺完美服从。

真正要提升的是三者组合后的总体行为：

- 更正确的时序
- 更强的任务 guidance
- 更有针对性的 reminder

### 2. `workflow_ref` 可能被模型误用

如果模型开始随意编造没有意义的标签，这个字段的价值就会下降。

缓解方式：

- 要求 label 来自用户输入或真实工作流结构
- 不允许为了“看起来结构化”而编造编号
- 在不确定时允许留空，不把 `workflow_ref` 设为必填
- 所有验证与 renderer 逻辑都必须在 `workflow_ref` 缺失时仍然正常工作

### 3. Session-state 迁移会波及多层

把 todo state 从模块全局迁出，会影响：

- tool logic
- renderer
- policy
- test setup

这是必要的架构债清理，不是可选优化。

## 延后问题

以下问题明确留给后续阶段：

1. phase 3 是否应从 transcript 或 compact attachments 中恢复 `todo_state`？
2. 后续是否应把 `todo` 拆成 `TaskCreate` / `TaskUpdate` 风格的工具？
3. 未来任务项是否需要 task IDs 与 dependencies？
4. `workflow_ref` 后续是否只用于显示，还是会进入更丰富的依赖语义？

## 成功标准

如果以下条件全部成立，则 phase 2 视为成功：

1. `harness` 不再依赖“一句 system prompt”去驱动 todo 行为
2. `todo` 工具具有足够强的 guidance，能稳定主动地产出多步骤计划
3. todo state 位于 `SessionState`，而不是模块级单例
4. 在 `skill_expanded` barrier 之后，下一版计划能稳定反映 active workflow
5. `2.5` 这类有意义的 workflow boundary 能保留进 plan
6. reminder 行为变成“post-skill planning”与“stale plan”两类精准提醒，而不是盲目的周期性 nag
7. 当前 todo 质量显著提升，同时又不要求立刻迁移到完整 task graph system
