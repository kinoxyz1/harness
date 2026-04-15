# Skill 与 Task 运行时对齐设计

> 日期: 2026-04-17
> 状态: 待评审
> 相关文档:
> - [`docs/superpowers/specs/2026-04-15-skills-system-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-skills-system-design.md)
> - [`docs/superpowers/specs/2026-04-16-skill-reference-inline-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-16-skill-reference-inline-design.md)
> - [`docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)
> - [`docs/superpowers/specs/2026-04-17-todo-task-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-todo-task-parity-design.md)
> - [`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)

## 背景

`harness` 当前有两个彼此分离但实际上强耦合的问题：

1. **Skill 生效时机不对。**
   当前路径把 skill 当成一种“延迟生效的会话状态”：
   - 启动时发现本地 skills
   - 模型调用 `activate_skill`
   - 修改 `SessionState.active_skills`
   - 在后续模型轮次里再把 skill 内容注入 prompt

   这样做的结果是，skill 虽然“可见”，但并没有立刻接管当前推理链。

2. **Todo 约束过弱，无法稳定表达当前工作流。**
   当前系统提示词里只有一句“多步骤任务要用 todo”，这足够让模型产出“一个计划”，但不足以让它产出“符合当前 skill 工作流形状的计划”。

这两个问题不是独立问题，而是同一个控制流问题：

> 应该先由 skill 决定“如何理解任务”，再由 todo 决定“如何跟踪任务”。

要把这个顺序真正做成稳定运行时行为，而不是停留在 prompt 建议层，本项目还依赖一套更强的 Runtime control plane。平台级能力详见：
[`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)

一旦 skill 是延迟生效的，todo 往往会先拿到规划主动权，于是出现已经被反复验证的坏结果：

- 模型口头上说它会使用某个 skill
- skill 内容可能也会在后面某个时刻进入上下文
- 但第一版 todo 已经被压缩成 1 到 2 个大项
- skill 内部的工作流步骤，例如 `analysis-report` 里的 `2.5`，不会进入真实计划

## 本设计记录的用户约束

本设计严格遵循用户已经明确表达过的约束：

1. **不要要求改已有 skill。**
   现有 `analysis-report` skill 已经在 Claude Code 和 OpenClaw 中验证过。应该让运行时适配 skill，而不是反过来要求重写 skill。

2. **不要把 Hook 补偿当成主方案。**
   如果一个 skill 系统要靠一整套 Hook 才能工作，说明架构边界本身就错了。

3. **skill 与 todo 要作为一个整体问题看待。**
   不是“skill 弱一点”加上“todo 弱一点”，而是它们在争夺同一个规划时机。

4. **Phase 1 只覆盖 inline local skills。**
   `fork` skill 和 compact 后恢复必须在设计上预留，但本阶段明确延后。

5. **Phase 1 要替换旧 skill 路径，而不是和旧路径长期并存。**
   旧的 `activate_skill` 模型不是目标架构。新设计必须足够清晰，不能让 reviewer 被迫去脑内拼接两套不兼容模型。

## 来自 Claude Code 的直接证据

本项目级设计依赖的是源码证据，而不是“大家都这么感觉”。

1. Claude Code 有专门的 `SkillTool`，而不是单纯的“激活一个标志位”。
   相关文件：
   - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/SkillTool.ts`
   - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/processUserInput/processSlashCommand.tsx`

2. `SkillTool` 的 inline 执行会产出 `newMessages`。
   它不是改一个 session 字段，然后等后面的 prompt assembler 再来发现。

3. Claude Code 在源码中明确写出了控制流语义：

   > "Only one skill/command should run at a time, since the tool expands the command into a full prompt that Claude must process before continuing."

   来源：
   `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/SkillTool.ts`

   这里的直接证据是“skill 执行应形成单独处理的时序边界”，而不是 Claude Code 源码中已经存在一个名为 `ExecutionBarrier` 的抽象。`ExecutionBarrier` 是本设计为 `harness` 引入的实现化推断。

4. Claude Code 会记录 invoked skills，以便后续 compact 时保留。
   相关文件：
   - `/Users/kino/works/opensource/Claude-Code-doc/src/bootstrap/state.ts`
   - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/compact.ts`

由这些源码事实可以抽出三个关键架构结论：

- skill 执行本质上是 prompt expansion，不是延迟状态激活
- skill 执行是一个运行时控制流事件，不只是 prompt 组装细节
- invoked skills 应该是可观察、可持久化的运行时记录，而不是隐藏副作用

## 证据边界

为了让后续评审更清楚，本设计区分两类内容。

### 直接源码证据

直接由 Claude Code 源码支持的结论：

- Claude Code 有专门的 `SkillTool`
- 调用该工具会返回 `newMessages`
- skill 执行被视为一个 sequencing boundary
- invoked skills 会被记录下来以支持 compact

### harness 的推断与设计选择

以下内容不是照抄 Claude Code，而是根据 `harness` 当前实现方式作出的合理设计选择：

- `harness` 中应当用一个普通 builtin tool `skill` 来承载对齐后的能力，遵循现有 snake_case 命名风格
- `harness` 需要更丰富的 `ToolResult` / `ToolBatchResult`，因为它的 runtime 是通过 `core/tools/runtime.py` 批量执行工具的
- `harness` 必须移除主路径上基于 `active_skills` 的延迟注入，因为这正是当前时序错误的源头
- 第一阶段的 `ContextPatch` 应当是 run-scoped，而不是一开始就把 session state 重新设计成复杂持久层

## 问题定义

当前 `harness` 的 skill 设计，其实解决的是错误的问题。

它确实改善了：

- 本地 skill 发现
- active skill 可见性
- reference inline 展开

但它没有解决真正的运行时顺序问题：

- 模型仍然可能在 skill 生效之前就决定第一版计划
- todo 仍然可能从 pre-skill 的推理状态里生成
- 一个定义了严格工作流的 skill，仍然会被当成“背景参考材料”

而当前 todo 系统又会放大这个问题：

- todo 工具本身很薄
- todo 的模型提示很弱
- 全局 prompt 只有一句约束
- 没有机制保证“第一版计划”一定发生在 skill expansion 之后

因此，这个联合改造必须从 skill 的运行时语义开始，而不是先做 todo prompt 微调。

## 项目目标

构建一个新的运行时模型，使其满足：

1. **inline local skills 以当前轮 prompt expansion 的方式执行。**
2. **skill expansion 可以打断当前工具批次，并强制模型重新评估下一步。**
3. **todo 规划发生在工作流已经进入上下文之后，而不是之前。**
4. **整体架构对未来的 `fork` skill 与 compact 恢复保持可扩展。**

## 非目标

本项目级设计明确 **不** 试图一次性完成所有对齐工作。

当前文档范围之外的内容：

- 远程 skill 搜索或远程 skill
- plugin / MCP skill 传输
- 改动社区 skill 文件格式
- 要求现有 skill 提供机器可读工作流元数据
- 在 phase 1 实现 task graph 持久化
- 在 phase 1 实现 `fork` skill 执行
- 在 phase 1 实现 compact 后的 `invoked_skills` 恢复
- 在 phase 1 完整解决 todo/task parity

## 架构原则

### 1. Skill 是运行时行为，不是延迟的 prompt 装饰

只要一个 skill 值得被调用，它就必须在工具调用完成的当下影响当前推理链。

### 2. Tool result 必须能表达的不只是字符串

当前 `ToolResult` 太薄，只能表达文本输出，无法表达：

- 注入消息
- 可用工具变化
- 模型覆盖
- 执行屏障

如果没有这些能力，`harness` 无法干净地表达接近 Claude Code 的 skill 运行时语义。

这组能力的更上层平台抽象，见：
[`docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-runtime-control-plane-design.md)

### 3. Session state 应该记录长期事实，而不是模拟当前轮 expansion

长期事实包括：

- discovered skills
- invoked skill records
- usage history

而当前轮 expansion 不应该被硬塞进持久性的 `active_skills` 字段里。

### 4. Skill 和 todo 必须串行排布，而不是混成一件事

正确顺序应该是：

1. 理解任务
2. 展开 skill
3. 重新评估下一步
4. 创建或更新 todo

todo 不应该承担“事后纠正 skill 遵循度”的职责。

## 共享运行时抽象

下面这些抽象是三个子项目共同依赖的基础。

### `ToolResult`

将工具返回值扩展为：

```python
@dataclass
class ToolResult:
    output: str
    success: bool
    error: str | None = None
    truncated: bool = False

    injected_messages: list[dict[str, Any]] = field(default_factory=list)
    context_patch: ContextPatch | None = None
    barrier: ExecutionBarrier | None = None
```

### `ContextPatch`

run-scoped 的运行时覆盖：

```python
@dataclass(slots=True)
class ContextPatch:
    allowed_tools: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
```

合并规则：

- `ContextPatch` 按工具实际执行顺序合并
- `model_override` 与 `effort_override` 使用“后写覆盖前写”
- `allowed_tools` 对所有非 `None` 白名单取交集，因为它本质上是限制性字段
- 如果交集结果为空，视为无效 runtime 配置，应在进入下一轮模型调用前抛出内部错误，而不是静默放行

虽然 phase 1 实际上预期只有 `skill` 会返回 `ContextPatch`，但总纲必须提前把合并规则定义清楚，以免 `ToolBatchResult.context_patches` 语义含糊。

### `ExecutionBarrier`

显式的控制流边界：

```python
@dataclass(slots=True)
class ExecutionBarrier:
    stop_after_tool: bool = True
    reason: str | None = None
```

说明：

- `ExecutionBarrier` 是 `harness` 的运行时协议抽象，不是 Claude Code 现成存在的源码类型
- phase 1 实现中，`reason` 应优先使用受约束的字面量值，例如 `Literal["skill_expanded"]`
- 当 barrier reason 在未来增长到多个值时，再考虑统一提升为枚举类型

### `InvokedSkillRecord`

对已经真正展开过的 skill 做长期记录：

```python
@dataclass(slots=True)
class InvokedSkillRecord:
    skill_id: str
    skill_path: str
    content_digest: str
    content: str
    invoked_at_turn: int
```

## 项目结构

本轮工作刻意拆成一个总纲加三个子项目。

### 总纲文档

本文件负责定义：

- 共享问题定义
- 关键架构原则
- 公共运行时抽象
- skill 与 todo 的时序关系
- `fork` 与 compact 的延后扩展点

### 子项目 1: Skill Execution Parity

交付内容：

- `skill` 工具
- 当前轮 skill expansion
- 更丰富的 tool result 协议
- 基于 barrier 的重新评估
- invoked skill runtime records

文档：
- [`docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-inline-local-skilltool-design.md)

### 子项目 2: Todo / Task Parity

交付内容：

- 更强的 todo/task 工具提示
- skill barrier 之后的 first-plan 规则
- 更细粒度的任务拆解
- 必要时向更丰富 task model 演进的路径

文档：
- [`docs/superpowers/specs/2026-04-17-todo-task-parity-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-17-todo-task-parity-design.md)

本项目与子项目 1 的依赖关系是：

> 只有在 skill 时序被修正之后，todo parity 的评估才有意义。

### 子项目 3: Compact / Session Parity

未来范围：

- compact 后恢复 invoked skill 内容
- compact 后恢复任务/计划上下文
- 定义 run-scoped patch 与 compact 后 session state 的关系

本总纲只要求提前准备两件事：

- 保留 `InvokedSkillRecord`
- 把 conversation history 与 runtime patch state 分离

## Phase 顺序

### Phase 1: Inline local skill runtime parity

主要目标：

- 用 `skill` 替换 `activate_skill`
- 让 skill expansion 成为运行时一等事件

预期用户可见效果：

- 相关 skill 会先于后续任务规划生效
- 第一版 todo 会在扩展后的 skill 上下文中创建

### Phase 2: Todo / task parity

主要目标：

- 在 skill 时序正确之后，把规划行为变得稳定、可预期、可观察

预期用户可见效果：

- todo 列表更接近 skill 的工作流结构
- 过程中的任务跟踪更稳定

### Phase 3: Compact / session parity

主要目标：

- skill 与 task 上下文能够在长会话与 compact 之后保持语义连续

预期用户可见效果：

- compact 不再抹掉先前 skill 调用的操作意义

## 为什么必须先做 Skill，再做 Todo

第一反应往往是先增强 todo prompt，但这不是推荐顺序。

原因很简单：

- 更强的 todo prompt 也许能提升拆解质量
- 但它修不好“todo 发生在 skill 接管之前”这个核心问题

所以：

- **Phase 1 解决时序**
- **Phase 2 解决拆解质量**

这个拆分是刻意的。这样做的好处是，如果 phase 1 之后 todo 依然过粗，团队就可以非常明确地把剩余问题归因到 todo/task 设计，而不是继续纠缠 skill 时序。

## Phase 1 结束后应达到的最小正确行为

在 phase 1 完成、phase 2 尚未开始前，系统至少应该表现为：

1. 模型为相关 inline local skill 调用 `skill`
2. `skill` 把 skill 展开为 injected conversation messages
3. 工具执行在 skill barrier 处停止
4. 下一次模型调用能看到展开后的 skill 内容
5. 只有在这之后，模型才决定是否创建 todo、读取文件或执行其他操作

这是对 todo/task redesign 做诚实评估之前必须具备的最低运行时对齐。

## 风险

### 1. 协议复杂度会上升

扩展 `ToolResult` 和 `ToolBatchResult` 会波及多个层次：

- tool modules
- runtime
- query loop
- message view construction

这是设计成本，但也是必要成本。

### 2. 并行工具执行会受到一定约束

`skill` 需要 barrier 语义，这会让包含 skill 调用的轮次暂时牺牲一部分并行吞吐。

在 phase 1 中这是可接受的，因为正确性比最大吞吐更重要。

### 3. Phase 1 之后 todo 仍可能不完美

这并不否定 phase 1 的价值。它只说明剩余问题属于 phase 2。

## 延后问题

以下问题是明确延后，而不是遗忘：

1. 某些 `ContextPatch` 是否需要变成 session-scoped？
2. `fork` skill 应该如何携带自己的 `allowed_tools`、model override、invoked-skill 生命周期？
3. compact 后如何重新物化 invoked skill context？
4. todo parity 最终应继续沿用当前 `todo` 工具，还是向更接近 Claude Code 的 task system 演进？

## 成功标准

如果本项目级设计能支持后续 phase-specific specs，并满足以下条件，则视为成功：

1. reviewer 能清楚区分 Claude Code 的直接证据与 harness 的设计推断
2. reviewer 能清楚看出 phase 1 到哪结束，phase 2 从哪开始
3. reviewer 能解释为什么 skill timing 必须先于 todo quality 修复
4. reviewer 能看出从 inline local skills 到未来 `fork` 和 compact 的平滑演进路径，而不需要整体重做架构
