# Skills System 设计

> 日期: 2026-04-15
> 状态: 待审阅
> 关联文档:
> - [`docs/superpowers/specs/2026-04-15-session-query-runtime-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-session-query-runtime-design.md)
> - [`docs/superpowers/specs/2026-04-15-runtime-followup-fixes-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-runtime-followup-fixes-design.md)

## 背景

当前 harness 已经重构为 `SessionEngine + QueryLoop + ToolRuntime` 三层，但 skill 能力仍处于缺位状态：

1. `SessionState` 里有 `discovered_skills` 字段，但运行时没有真正消费它。
2. `PromptAssembler` 没有组装 skill catalog 或 active skills。
3. `QueryLoop` 没有 skill 激活、失活、按需加载或 fork skill 调度逻辑。
4. 当前系统只能依靠基础 prompt 和 tools 自行完成任务，无法稳定复用社区 skill。

之前 stash 中已经尝试过一版 skill 方案，但有两个根本问题：

1. **注册格式脆弱**：实现要求自定义 frontmatter 字段，和标准 Agent Skills 生态不兼容，导致本地 skill 很容易“看起来存在，实际上未注册”。
2. **激活语义不稳定**：旧方案依赖模型先输出 `<use-skill .../>`，框架再把完整 skill 内容后插到 `system` 消息中。这个链路成功与否不可观测，失败时用户只会看到“模型好像答应使用了 skill，但后续动作并没按 skill 执行”。

用户已明确本次设计的边界：

- 只支持**本地 skill**
- 只支持**标准 Agent Skills 格式**
- **不兼容旧本地变体**
- 激活方式采用**显式激活优先**
- 社区 skill 由用户手动下载到本地目录，不在 v1 中实现安装器

## 设计目标

- 让 harness 原生支持标准 Agent Skills，本地落盘即可发现和使用。
- 让 skill 激活成为**显式、可观测、可调试**的运行时行为。
- 复用当前 `SessionEngine + QueryLoop + PromptAssembler + ToolRuntime + subagent_runtime` 架构，不把 skill 重新做成隐藏在 prompt 里的副作用机制。
- 在 v1 中只支持**会话级 inline skills**，先把“发现、激活、持续生效、关闭”闭环做完整。
- 为后续 compact、session 恢复、conditional activation 留出明确扩展点。

## 非目标

- 不实现远程 skill 搜索、下载或安装。
- 不兼容旧 stash 里的 `<use-skill slug="..."/>` 协议。
- 不在 v1 中支持 skill hooks、远程 skill、plugin skill、MCP skill。
- 不在 v1 中做自动激活优先；模型可建议 skill，但最终激活必须经过显式运行时动作。
- 不在 v1 中支持 fork skills、skill references 按需加载、skill 专属工具权限或模型覆盖。
- 不在 v1 中引入新的 DSL 或自定义 skill manifest。

## 外部参考

本设计主要参考两类外部模型：

### 1. Anthropic / Agent Skills 标准

- skill 是一个可复用的本地能力包，核心入口是 `SKILL.md`
- 支持 progressive disclosure：先暴露 catalog，再按需读取完整内容和附属文件
- skill 目录可以在不同 agent 之间共享

### 2. Claude Code 的 skill 运行时

Claude Code 提供了两类关键启发：

1. **skill 有上下文模式**
   - `inline`: 注入当前对话，并在 compact 后继续保留 invoked skill
   - `fork`: 在独立子代理中执行
2. **skill 不应该偷偷生效**
   - 系统会记录 invoked skills
   - compact 时会保留已调用技能的内容
   - 说明 skill 是一等运行时概念，不只是“把提示词拼进去”

这些启发与 harness 当前架构是兼容的，但不能照抄实现，因为 harness 目前没有 Claude Code 那套命令系统、hooks 总线和 prompt command registry。

因此本设计采用一个更窄的落地策略：

- v1 只实现 `inline` 语义
- `fork` 只作为 v2 参考方向保留在方案比较里，不进入本次实现范围

## 问题定义

新版 skill 设计必须同时解决 5 个问题：

1. **发现问题**：本地有哪些 skill，如何被稳定发现。
2. **激活问题**：skill 何时生效，激活结果如何被系统记录。
3. **注入问题**：`inline` skill 如何真正进入模型输入，而不是只停留在状态字段里。
4. **隔离问题**：`fork` skill 如何复用现有 subagent 机制，而不污染主会话。
5. **恢复问题**：会话继续、compact、后续多轮时，系统如何知道哪些 skill 仍在生效。

## 三套方案

下面给出 3 套方案。三套都基于本地标准 Agent Skills，但运行时边界不同。

### 方案 A：Prompt-Only Skills

核心思路：

- skill catalog 作为稳定 prompt 的一部分提供给模型
- 模型在回答中“声明”想用哪个 skill
- 框架把 skill 内容直接拼接进 prompt
- skill 不作为显式运行时状态，只是 prompt 组装结果

#### 数据流

```text
SessionEngine
  -> PromptAssembler.build_stable()
       -> available skills catalog
  -> QueryLoop.run()
       -> 模型回复提到 skill
       -> 框架拼接 skill 正文
       -> 再次调模型
```

#### 优点

- 实现最轻
- 代码改动最少
- 直觉上接近 stash 原始方案

#### 缺点

- 本质上仍然是“提示词副作用”
- skill 是否真的生效不可观测
- 调试困难，用户只能从模型行为反推
- `fork` skill 很难自然融入
- compact / session 恢复时无法稳定知道哪些 skill 正在生效

#### 结论

不推荐。它会复现旧 stash 方案最核心的问题，只是把协议名从 `<use-skill>` 换了一层皮。

### 方案 B：Session-Managed Skills

核心思路：

- skill 是 session 层的一等对象
- 激活由显式动作触发，例如用户说“使用 xxx skill”或模型建议后再走统一激活路径
- `inline` skill 进入 `SessionState.active_skills`
- `PromptAssembler` 根据 `active_skills` 组装稳定或半稳定上下文
- `fork` skill 仍视为一种 skill，但调度到子代理，不进入 `active_skills`

#### 数据流

```text
用户/模型建议
  -> SkillResolver.resolve(...)
  -> SessionEngine.activate_skill(...)
       -> inline: state.active_skills += slug
       -> fork: spawn subagent run
  -> PromptAssembler.build_skill_messages(...)
  -> QueryLoop.run()
       -> ModelGateway.call_once(...)
```

#### 优点

- skill 激活是显式状态变化，可观测、可测试
- `inline` 和 `fork` 的语义边界清楚
- 和当前 `SessionEngine + PromptAssembler` 架构最匹配
- compact / session 恢复时有明确依托
- 容易支持“列出已激活 skill”“关闭 skill”“查看 skill 详情”

#### 缺点

- 需要新增 skill catalog、activation、session 状态和 prompt 组装模块
- 比 prompt-only 方案多一层状态同步
- 需要定义 skill content 进入 prompt 的预算和裁剪策略

#### 结论

推荐。它是最平衡的方案，既保留 skill 的 prompt 本质，又把激活和生命周期提升为可管理的运行时概念。

### 方案 C：Command-Oriented Skills

核心思路：

- skill 被做成类似 Claude Code prompt command 的命令系统
- 每个 skill 都有 `run_skill(slug, args)` 风格的统一入口
- `inline` 和 `fork` 都通过“命令执行结果”间接反映到会话
- skill runtime 独立于 `SessionEngine`

#### 数据流

```text
QueryLoop
  -> tool or command: run_skill(slug)
       -> SkillRuntime
            -> inline expand
            -> or fork execute
  -> 回写 skill result / prompt delta
  -> 再进入模型
```

#### 优点

- 接近 Claude Code 的 command abstraction
- skill 调用路径统一
- 后续接 hooks / plugin / MCP 更顺手

#### 缺点

- 对 harness 当前架构来说过重
- 会重新引入“skill 是不是 tool / command”的边界争议
- 需要额外的 command registry、permission、result protocol
- 容易把 skill 系统做成第二套主运行时

#### 结论

不推荐作为当前阶段方案。它适合更成熟的命令式 agent 平台，不适合当前 harness 的阶段性目标。

## 推荐方案

推荐采用 **方案 B：Session-Managed Skills**。

推荐理由：

1. 它最符合当前架构分层：
   - `SessionEngine` 管长期 skill 状态
   - `PromptAssembler` 管 skill 如何进入模型输入
   - `QueryLoop` 继续只管普通 query loop，不承担额外 skill 控制流
2. 它能直接解决旧 stash 的失败点：
   - skill 注册失败会在 catalog 阶段暴露
   - skill 激活是显式状态变化，不再依赖模型“口头答应”
3. 它最接近 Claude Code 的真正语义，而不是表面语法：
   - `inline` skill 是会话级能力增强
   - `fork` 被明确降级到 v2，而不是在 v1 中半实现
4. 它给未来扩展留出了自然位置：
   - compact 保留 active skills
   - conditional activation
   - plugin/MCP skill source

## 推荐方案详细设计

## 核心原则

1. skill 是**一等运行时概念**，不是 prompt 拼接副作用。
2. v1 只实现**本地标准 inline skills**。
3. 激活必须**由用户显式触发**；模型只能建议，不能直接生效。
4. active skill 不写回普通聊天历史，而是在构建模型视图时注入。
5. v1 只读取 `SKILL.md`，不做 references 的按需加载。

## v1 范围

v1 的目标很窄，只闭环这 6 件事：

1. 发现本地标准 skills
2. 列出和查看 skills
3. 用户显式激活 skill
4. 会话内持续生效
5. 用户显式关闭 skill
6. skill 正文稳定进入真实模型输入

明确不做：

- fork skills
- skill 自动激活
- skill references / templates / scripts 的运行时加载
- skill 专属 allowed tools / model / agent 配置
- compact 期间的高级 skill 裁剪与恢复

## 目录与文件格式

v1 只支持本地标准 skill 目录：

```text
.harness/skills/
  <skill-id>/
    SKILL.md
```

约束：

- `SKILL.md` 是唯一必需入口
- `skill-id` 直接取目录名
- 不读取 `_meta.json`
- frontmatter 只解析标准字段，未知字段忽略

v1 支持的 frontmatter 子集：

```yaml
---
name: Analysis Report
description: Generate structured HTML analysis reports
when-to-use: Use when the user wants a finished report
---
```

说明：

- `name` 和 `description` 是 discover 所需的最小集合
- `when-to-use` 为可选字段，用于 catalog 展示
- `context`、`allowed-tools`、`model`、`agent` 不属于 v1 标准解析范围
- 如果社区 skill 带有更多 frontmatter，v1 可以忽略，但不会赋予这些字段运行时语义

## Skill 元数据模型

建议在 `core/skills/` 下引入如下结构：

```python
@dataclass(slots=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_file: Path


@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str
```

说明：

- `skill_id` 来自目录名，是运行时唯一标识
- `SkillMeta` 只承载 discover / list / show / activate 所需字段
- `SkillContent` 只在激活时加载 `SKILL.md` 正文
- v1 不在运行时读取 references 目录，也不生成 `file_index`

## SessionState 扩展

建议扩展 `SessionState`：

```python
skill_catalog: dict[str, SkillMeta]
active_skills: dict[str, ActiveSkillState]
skill_events: list[SkillEvent]
skills_revision: str | None
```

并删除旧字段：

```python
discovered_skills: set[str]
```

原因：

- `skill_catalog` 已完整覆盖“当前已发现 skills”的语义
- 保留 `discovered_skills` 会造成两套来源重叠
- v1 不需要同时维护“发现集合”和“catalog 详情”两份状态

其中：

```python
@dataclass(slots=True)
class ActiveSkillState:
    skill_id: str
    activated_at_message_index: int
    source: str
    content_digest: str


@dataclass(slots=True)
class SkillEvent:
    skill_id: str
    action: Literal["activated", "deactivated", "reload"]
    source: str
    conversation_index: int
```

设计意图：

- `active_skills` 表示当前仍生效的 inline skills
- `skill_events` 代替原本信息不足的 `invoked_skill_history`
- `skills_revision` 用于 stable prompt cache 失效控制

## Skill Catalog

系统需要把“有哪些 skill 可用”与“skill 全文是什么”分开。

### discover 阶段

`SessionEngine` 启动时或用户执行 reload 命令时执行：

```text
discover local skills
  -> scan .harness/skills/*
  -> parse SKILL.md frontmatter
  -> build skill catalog
  -> compute skills_revision
  -> cache to SessionState.skill_catalog
```

`skills_revision` 的计算规则保持简单且确定：

```text
sort by skill_id
  -> for each skill: "{skill_id}:{mtime_ns}"
  -> join with "\n"
  -> hash the joined string
```

说明：

- `mtime_ns` 取 `SKILL.md` 文件的纳秒级修改时间
- skill 文件未变化时，重复 reload 不应改变 revision
- 不使用 discover 时刻时间戳，避免无意义地失效稳定 prompt 缓存

### catalog 暴露策略

catalog 进入稳定 system prompt，但只暴露摘要：

```text
<available-skills>
  <skill id="analysis-report">
    名称：Analysis Report
    描述：Generate structured HTML analysis reports
    适用：Use when the user wants a finished report
  </skill>
</available-skills>
```

这样模型知道“本地有哪些 skill 可供建议”，但不会长期背负 skill 正文。

## 用户侧激活路径

v1 不依赖模型自行触发激活，而是新增一层**本地命令解析**。

建议引入最小命令集：

```text
/skills list
/skills show <skill-id>
/skills use <skill-id>
/skills off <skill-id>
/skills reload
```

执行位置：

- 在 CLI / REPL 层分流，在输入进入 `QueryLoop` 之前解析
- REPL 识别 `/skills` 前缀后，转发给 `SessionEngine.handle_command(...)`
- 命令结果直接返回给用户，不经过 LLM

这样可以保证：

- 激活路径显式、可预测
- 失败有结构化返回
- 不需要让模型拥有“修改 session skill 状态”的权限

这样划分的原因：

- REPL 负责“识别这是不是命令”
- `SessionEngine` 负责“执行这个命令如何改变 session 状态”
- `QueryLoop` 保持只处理正常对话 query

## 模型建议路径

模型仍然可以建议 skill，但只能停留在文本层：

```text
建议激活 `analysis-report` skill 再继续，因为当前任务是生成正式分析报告。
```

v1 中：

- 模型不能直接激活 skill
- 没有 `request_skill_activation` tool
- 没有 `<use-skill>` 或其他隐式协议

用户如果采纳建议，需要显式输入 `/skills use analysis-report`。

## Inline Skills 的运行时

`inline` skill 的激活效果：

1. 校验 `skill_id` 存在
2. 读取 `SKILL.md` 正文
3. 更新 `SessionState.active_skills`
4. 记录 `SkillEvent(action="activated")`
5. 后续每轮由视图构建层注入 skill 正文

关闭效果：

1. 从 `active_skills` 删除该 `skill_id`
2. 记录 `SkillEvent(action="deactivated")`
3. 后续模型视图不再包含该 skill 正文

## Token 预算与激活限制

v1 不做复杂裁剪，采用简单硬限制：

- 同时激活的 skill 最多 3 个
- 所有 active skills 的 `SKILL.md` 正文总长度不得超过 24,000 字符

如果超限：

- `/skills use <skill-id>` 返回错误
- 提示用户先关闭其他 skill

这样避免了“每轮偷偷截断 skill 正文”导致的语义不稳定。

## PromptAssembler 职责

推荐把 skill 相关职责明确放到 `PromptAssembler`：

### `build_stable()`

负责：

- framework prompt
- project context
- available skills catalog

缓存规则：

- cache key 必须包含 `skills_revision`
- `/skills reload` 后只刷新 catalog 和 `skills_revision`
- 旧 stable prompt 通过 cache key 自然失效，不清空整个 `prompt_cache`

这解决了 catalog 变化与稳定 prompt 缓存的冲突。

### `build_active_skill_messages()`

新增专用方法，负责：

- 将当前 `active_skills` 渲染为 1 条合成的 `system` 消息

建议格式：

```text
<active-skills>
  <active-skill id="analysis-report">
    ... SKILL.md body ...
  </active-skill>
</active-skills>
```

v1 不把 skill 正文写进 `conversation_messages`。

## MessageViewBuilder 职责

`MessageViewBuilder.build()` 不能再只是原样返回历史消息。

建议构造顺序如下：

1. 历史中的前置 `system` 消息
2. 由 `PromptAssembler.build_active_skill_messages()` 生成的合成 `system` 消息
3. 剩余 `conversation_messages`

等价伪代码：

```python
system_prefix = take_leading_system_messages(state.conversation_messages)
rest = remaining_messages(state.conversation_messages)
active_skill_messages = prompt_assembler.build_active_skill_messages(state)
return MessageView(
    messages=[*system_prefix, *active_skill_messages, *rest],
    tools=self._tools,
)
```

这样定义后，active skills 的注入位置就是明确的：

- 在稳定 system prompt 之后
- 在用户/助手对话历史之前
- 每轮都重新构建，而不是只在首次激活时写回历史

## QueryLoop 职责

v1 中 `QueryLoop` 不承担 skill 激活控制流，只承担普通 query loop。

它只需要消费已经准备好的 `MessageView`：

- 如果会话里有 active skills，模型输入自然会带上
- 如果用户未激活 skill，模型只能看到 catalog 摘要

这保持了 `QueryLoop` 的职责单纯，避免重新出现旧 `core/agent.py` 那种控制流膨胀。

## Progressive Disclosure 的 v1 取舍

v1 只实现两级 disclosure：

1. catalog 摘要长期可见
2. 被激活 skill 的 `SKILL.md` 正文进入模型输入

明确不做第三级：

- reference 文件按需读取
- template/script 运行时加载

原因：

- 当前代码还没有稳定的“skill 附属文件读取协议”
- 如果现在把 references 也纳入运行时，会立刻引入预算、触发路径和注入方式三套额外设计

## 失败处理

新版设计必须显式暴露 skill 失败，而不是静默吞掉。

### discover 失败

- `SKILL.md` 缺失
- frontmatter 非法
- `name` 或 `description` 缺失

处理：

- 不注册该 skill
- 在 `/skills list` 结果中显示错误项，或至少在日志中明确可见

### activate 失败

- `skill_id` 不存在
- `SKILL.md` 正文读取失败
- 超出 active skill 数量上限
- 超出 active skill 字符预算

处理：

- `/skills use` 返回结构化错误
- 不修改 `active_skills`
- 不记录假阳性 activation event

### deactivate 失败

- `skill_id` 当前未激活

处理：

- `/skills off` 返回幂等提示
- 不报致命错误

## Compact / 恢复设计

v1 不实现完整 compact，但保留最小恢复信息：

- `active_skills`
- `skill_events`

后续如果引入 compact：

- 优先保留 active skill 正文
- `skill_events` 只保留摘要，不保留冗余历史

## 与旧 stash 方案的取舍

明确废弃以下设计：

1. `<use-skill slug="..."/>` 作为激活主协议
2. 激活成功后把 skill 内容直接 append 到用户历史 `messages`
3. 自定义 frontmatter 强依赖
4. 让模型直接拥有 skill 状态修改权

保留的思想只有两点：

1. skill 是知识/流程增强，不是普通业务 tool
2. skill 应该按需加载，而不是开局全量读入所有正文

## 迁移策略

由于用户已经确认旧本地格式不重要，可直接删除，因此迁移策略应保持简单：

1. 新增标准 skill loader
2. 新增 `/skills` 命令路由
3. 新增 session-managed active skill 状态
4. 删除或废弃旧 stash 方案，不做兼容桥接
5. 现有 `.harness/skills/` 中不符合标准的 skill 直接视为无效，必要时手工重写

## 测试要求

至少需要覆盖以下测试：

1. skill discovery 能正确扫描标准目录
2. 非标准 `SKILL.md` 会被拒绝并给出错误信息
3. available skills catalog 会进入稳定 prompt
4. `/skills use <skill-id>` 会激活 skill 并写入 `active_skills`
5. `/skills off <skill-id>` 会移除 active skill
6. active skill 正文会进入真实模型输入
7. active skills 注入位置在稳定 system prompt 之后、对话历史之前
8. activation failure 不会产生假阳性激活状态
9. `/skills reload` 会刷新 catalog 并使 stable prompt cache 失效
10. session 连续多轮时 active inline skills 会持续生效

## 最终决策

1. 采用 **Session-Managed Skills** 作为 v1 架构。
2. 只支持**本地标准 Agent Skills**，不兼容旧格式。
3. v1 只支持**inline** 语义，不支持 `fork`。
4. skill catalog 与 active skill content 分离。
5. skill 激活和关闭都必须走显式 `/skills` 命令，不允许模型直接修改状态。
6. `PromptAssembler` 和 `MessageViewBuilder` 必须在模型视图构建阶段注入 active skills。
7. v1 只读取 `SKILL.md`，references / templates / scripts 留待后续版本。

## 后续实现建议

实现时建议按以下顺序推进：

1. skill discovery / catalog
2. `/skills` 命令路由
3. `SessionState.active_skills` 与 `skill_events`
4. stable prompt 中的 catalog 注入与缓存失效
5. active skill message 注入

这样可以先把最关键的“会发现、会激活、会真正生效、会关闭”闭环做出来，再扩展更复杂的 skill 语义。
