# Skill Activation Enforcement 设计

> 日期：2026-05-10
> 状态：待评审
> 前序文档：
> - [2026-05-09-behavioral-anchoring-countermeasure-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-09-behavioral-anchoring-countermeasure-design.md)（行为锚定对策，已实施 SkillRelevancePolicy + SkillUsageNudgePolicy）
> 当前相关实现：
> - [core/tools/builtin/skill.py](/Users/kino/works/kino/harness/core/tools/builtin/skill.py) — Skill 工具定义（SCHEMA.description）
> - [core/prompt/system_context.py](/Users/kino/works/kino/harness/core/prompt/system_context.py) — `_FRAMEWORK_PROMPT` 中 Skills 章节的指令文本
> - [core/policy/skill_relevance.py](/Users/kino/works/kino/harness/core/policy/skill_relevance.py) — SkillRelevancePolicy 关键词匹配 + nudge 注入
> - [core/policy/skill_usage_nudge.py](/Users/kino/works/kino/harness/core/policy/skill_usage_nudge.py) — SkillUsageNudgePolicy 间隔提醒
> - [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py) — PromptAssembler，`build_active_skill_messages` 渲染已激活 skill
> - [core/skills/runtime.py](/Users/kino/works/kino/harness/core/skills/runtime.py) — `apply_skill_invocation` 写入 `state.invoked_skills`
> Claude Code 参考：
> - `src/tools/SkillTool/prompt.ts` — BLOCKING REQUIREMENT 指令嵌入 Tool description
> - `src/constants/prompts.ts` — System prompt 中 skill 相关的 session guidance
> - `src/utils/attachments.ts` — `getSkillListingAttachments()` 每轮注入 skill listing
> - `src/bootstrap/state.ts` — `addInvokedSkill()` + compaction 后重新注入

---

## 1. 摘要

本文档解决一个已存在但未被充分重视的问题：**SkillRelevancePolicy 正确检测到 skill 相关性并注入提醒，但模型仍然选择不激活 skill，直接使用基础工具完成任务。**

这不是 skill 设计问题，也不是匹配算法问题，而是**指令强度问题**：系统中所有关于 skill 激活的指令都使用了建议性语言（"应先调用"、"建议先调用"、"考虑先加载"），模型将其视为可选参考而非强制约束。

### 核心发现

通过对 Claude Code 源码的逆向分析，发现其解决方案的本质不是更聪明的匹配，而是在**模型每轮必读的 Tool Schema description** 中嵌入强制性指令（"BLOCKING REQUIREMENT"），形成无法忽略的行为约束。

---

## 2. 问题复现

### 场景

用户在 harness REPL 中输入：

```
帮我看看最近很火的 gstack 项目是干什么的
```

系统中有 `code-explorer` skill，其 `when_to_use` 包含 "探索项目"、"理解代码库"、"explain this codebase" 等触发短语。

### 实际行为

| 步骤 | 状态 | 说明 |
|---|---|---|
| 1. Skill 发现 | ✅ | `SessionEngine.bootstrap()` 扫描 `.harness/skills/*/SKILL.md`，`skill_catalog` 中有 `code-explorer` |
| 2. 关键词匹配 | ✅ | `SkillRelevancePolicy._matches()` 检测到用户问题匹配 `when_to_use` 中的触发短语 |
| 3. 注入提醒 | ✅ | 注入 `<system-reminder type="skill_relevance">` 到 transcript 尾部 |
| 4. 模型决策 | ❌ | 模型看到提醒但**选择不调用 skill 工具**，直接使用 bash/web search 完成 |
| 5. `invoked_skills` | ❌ | 保持为空字典 `{} ` |
| 6. Prompt 组装 | ❌ | `build_active_skill_messages()` 因 `invoked_skills` 为空，返回空列表 |
| 7. 最终结果 | ⚠️ | 任务完成了，但**没有按 skill 定义的结构化流程执行** |

### 根因定位

断链发生在**步骤 3→4**之间。`SkillRelevancePolicy` 成功注入了提醒消息，但该消息的指令强度不足以驱动模型执行 skill 工具调用。

---

## 3. 根因分析

### 3.1 指令强度的三个层级

系统中关于 skill 激活的指令分布在三个位置，全部使用建议性语言：

#### 位置 A：框架系统提示词（`core/prompt/system_context.py:35-36`）

```python
"如果任务匹配某个 skill，应先调用 skill 工具立即加载它，再基于已展开的 skill 重新评估下一步。"
```

**问题**："应先调用"是建议语气，不是强制要求。模型在权衡"直接用基础工具"和"先花一轮调用 skill 工具"时，倾向于选择更短路径。

#### 位置 B：Skill 工具描述（`core/tools/builtin/skill.py:20-24`）

```python
"description": (
    "Load a local skill into runtime state. The skill guidance will be available "
    "on the next model turn, so use this when you need additional workflow "
    "instructions before subsequent reasoning or tool use."
)
```

**问题**：描述只说明了功能（"加载 skill，下轮可用"），没有包含任何使用条件或强制要求。对比 Claude Code 在同一位置放了 BLOCKING REQUIREMENT。

#### 位置 C：Policy 注入的提醒（`core/policy/skill_relevance.py:79-84`）

```python
"以下 skill 与当前任务相关但尚未激活。如果匹配你的工作，建议先调用 skill 工具加载："
```

**问题**："如果匹配你的工作，建议先调用"——条件判断权完全交给模型，且"建议"一词明确降低了优先级。

### 3.2 `<system-reminder>` 标签的信号衰减

`SkillRelevancePolicy` 将提醒包装在 `<system-reminder>` 标签中，以 `user` 角色消息注入 transcript 尾部。这带来两个问题：

1. **标签语义**：模型在训练中习惯将 `<system-reminder>` 视为"后台辅助信息"，优先级低于 system prompt 中的显式指令
2. **角色降级**：作为 `user` 角色消息注入，而非 `system` 角色消息，进一步降低了指令权重

### 3.3 缺少"激活后可见"的行为反馈

当前 skill 工具的返回消息是：

```
"Skill loaded: {skill_id}. The skill guidance will be available on the next model turn."
```

这意味着模型调用 skill 后**当轮看不到 skill 内容**，形成"付出一轮调用但无即时收益"的负反馈循环。Claude Code 则将 skill 内容作为新 user message 直接展开到当轮对话中，模型立即就能看到并遵循。

---

## 4. Claude Code 的解决方案（逆向分析）

### 4.1 架构对比

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Harness（当前）                                          │
│                                                                             │
│  System Prompt                                                              │
│  └─ "如果任务匹配某个 skill，应先调用 skill 工具"                           │
│       (建议性，system prompt 层，模型可忽略)                                 │
│                                                                             │
│  Tool Schema                                                                │
│  └─ "Load a local skill into runtime state..."                              │
│       (纯功能描述，无使用约束)                                               │
│                                                                             │
│  Policy Nudge (每轮)                                                        │
│  └─ "<system-reminder> 建议先调用 skill 工具"                               │
│       (user 角色，system-reminder 标签，建议语气)                           │
│                                                                             │
│  ═════════════════════════════════════════════════                           │
│  结果：模型看到 3 条建议性指令，权衡后选择直接用基础工具                     │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                    Claude Code                                              │
│                                                                             │
│  Tool Schema (每轮可见)                                                     │
│  └─ "When a skill matches, this is a BLOCKING REQUIREMENT:                 │
│      invoke the relevant Skill tool BEFORE generating any other response.   │
│      NEVER mention a skill without actually calling this tool."             │
│       (强制性，Tool Schema 层，模型每次评估工具时必读)                       │
│                                                                             │
│  System Prompt                                                              │
│  └─ "/<skill-name> is shorthand... IMPORTANT: Only use Skill for            │
│      skills listed in its user-invocable skills section."                   │
│       (IMPORTANT 强调，system prompt 层)                                    │
│                                                                             │
│  Skill Listing (每轮注入)                                                   │
│  └─ "<system-reminder> The following skills are available..."               │
│       (每轮注入可用 skill 列表，保持新鲜度)                                 │
│                                                                             │
│  Skill 内容展开 (激活后)                                                    │
│  └─ 作为新 user message 展开，模型当轮可见                                  │
│       (即时反馈，激活后立即可用)                                             │
│                                                                             │
│  Compaction 后重新注入                                                      │
│  └─ "The following skills were invoked. Continue to follow these            │
│      guidelines:" + 完整 skill 内容                                         │
│       (压缩后自动恢复 skill 指令)                                           │
│                                                                             │
│  ═════════════════════════════════════════════════                           │
│  结果：模型在 Tool Schema 中看到 BLOCKING REQUIREMENT，                     │
│        必须在响应前先调用 Skill 工具                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Claude Code 的五层防护机制

| 层级 | 位置 | 机制 | 指令强度 |
|---|---|---|---|
| L1 | Tool Schema description | `BLOCKING REQUIREMENT` 嵌入工具描述 | **强制** |
| L2 | System prompt session guidance | `IMPORTANT:` 前缀的 skill 使用说明 | **强** |
| L3 | 每轮 skill listing 注入 | `<system-reminder>` 附带可用 skill 列表 | 信息 |
| L4 | Skill 内容即时展开 | 激活后作为 user message 立即可见 | 反馈 |
| L5 | Compaction 后重新注入 | `invoked_skills` attachment 恢复 skill 指令 | 持久化 |

### 4.3 关键设计差异总结

| 维度 | Harness | Claude Code |
|---|---|---|
| Tool description 中的指令 | 纯功能描述 | BLOCKING REQUIREMENT 强制指令 |
| 系统提示词语气 | "应先调用" | "IMPORTANT: Only use Skill for..." |
| Policy nudge 语气 | "建议先调用" | N/A（不需要 nudge，Tool Schema 已强制） |
| Skill 内容可见时机 | 激活后下一轮 | 激活后当轮（user message 展开） |
| Compaction 后恢复 | `invoked_skills` 写入 system prompt | `invoked_skills` attachment 重新注入为 system-reminder |
| Skill listing 更新频率 | 启动时一次性 | 每轮注入 delta |

---

## 5. 改进方案

### 设计原则

1. **把强制指令放在模型最不可能忽略的位置**：Tool Schema description（每轮工具评估时必读）
2. **用梯级递进的指令强度**：从 Tool Schema 的强制要求，到 system prompt 的 IMPORTANT 强调，到 policy nudge 的定向提醒
3. **消除激活的负反馈**：让 skill 内容在激活后立即可见，而非等到下一轮

### 5.1 改动 A：强化 Skill Tool 描述（最高优先级）

**文件**：`core/tools/builtin/skill.py`

**当前**：

```python
SCHEMA: dict[str, Any] = {
    "name": "skill",
    "description": (
        "Load a local skill into runtime state. The skill guidance will be available "
        "on the next model turn, so use this when you need additional workflow "
        "instructions before subsequent reasoning or tool use."
    ),
    ...
}
```

**改为**：

```python
SCHEMA: dict[str, Any] = {
    "name": "skill",
    "description": (
        "当用户任务与 <available-skills> 中某个 skill 的描述或触发场景匹配时，"
        "这是阻塞要求(BLOCKING REQUIREMENT)：必须先调用此工具激活该 skill，"
        "再生成任何其他响应或工具调用。\n"
        "绝不能只提到 skill 存在但不实际调用此工具。\n"
        "如果当前对话轮次中已看到 <command-name> 标签，说明 skill 已加载，"
        "直接遵循其指令即可，不要重复调用。"
    ),
    ...
}
```

**原理**：Tool Schema description 是模型在每一轮评估"我要不要调用这个工具"时直接读取的内容。在此处放置 BLOCKING REQUIREMENT，等同于在决策点设置路障。

### 5.2 改动 B：强化框架提示词

**文件**：`core/prompt/system_context.py`

**当前**（第 33-36 行）：

```python
## Skills

系统提示词中包含 <available-skills> 目录。
如果任务匹配某个 skill，应先调用 skill 工具立即加载它，再基于已展开的 skill 重新评估下一步。
```

**改为**：

```python
## Skills

系统提示词中包含 <available-skills> 目录，列出所有可用的 skill。
当任务匹配某个 skill 的描述或触发场景时，这是阻塞要求(BLOCKING REQUIREMENT)：
必须先调用 skill 工具激活它，再基于已展开的 skill 重新评估下一步。
即使只有很小的可能性匹配，也必须先激活再行动——这不是可选的。
```

### 5.3 改动 C：强化 SkillRelevancePolicy 的 nudge 语言

**文件**：`core/policy/skill_relevance.py`

**当前**（第 79-84 行）：

```python
content = (
    "<system-reminder type=\"skill_relevance\">\n"
    "以下 skill 与当前任务相关但尚未激活。如果匹配你的工作，建议先调用 skill 工具加载：\n\n"
    + "\n".join(lines)
    + "\n</system-reminder>"
)
```

**改为**：

```python
content = (
    "<system-reminder type=\"skill_relevance\">\n"
    "以下 skill 与当前任务高度匹配但尚未激活。\n"
    "阻塞要求：必须先调用 skill 工具激活匹配的 skill，再继续处理任务。\n\n"
    + "\n".join(lines)
    + "\n</system-reminder>"
)
```

### 5.4 改动 D：强化 SkillUsageNudgePolicy 的提醒语言

**文件**：`core/policy/skill_usage_nudge.py`

**当前**（第 45-52 行）：

```python
content = (
    "<system-reminder type=\"skill_nudge\">\n"
    f"当前会话有 {len(catalog)} 个可用 skill，"
    f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
    "如果当前任务涉及数据分析、报告生成、调试、TDD 等场景，"
    "考虑先加载对应 skill。\n"
    "</system-reminder>"
)
```

**改为**：

```python
content = (
    "<system-reminder type=\"skill_nudge\">\n"
    f"当前会话有 {len(catalog)} 个可用 skill，"
    f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
    "请检查 <available-skills> 中是否有匹配当前任务的 skill。"
    "如果有，必须在继续之前先激活它。\n"
    "</system-reminder>"
)
```

### 5.5 改动 E：Skill 激活后内容即时可见（可选，优先级较低）

**当前**：模型调用 skill 工具后，返回 `"Skill loaded: {skill_id}. The skill guidance will be available on the next model turn."`。skill 内容在下一轮才通过 `build_active_skill_messages()` 注入 system prompt。

**改进方向**：在 skill 工具的返回消息中直接包含 skill 的核心指令（body），而非等到下一轮。这样模型在当轮就能看到并遵循 skill 工作流。

**涉及文件**：`core/tools/builtin/skill.py` 的 `handle()` 函数

**注意**：此改动需要考虑 skill 内容可能很长（code-explorer 超过 500 行）对 tool_result 消息大小的影响。可以只返回 body 的前 N 行或摘要，完整内容仍通过下一轮 system prompt 注入。

---

## 6. 改动影响评估

### 6.1 风险分析

| 改动 | 风险 | 等级 | 缓解措施 |
|---|---|---|---|
| A: Tool description | 模型过度激活不相关的 skill | 低 | `SkillRelevancePolicy` 的关键词匹配已提供相关性过滤；模型仍有判断力 |
| B: 框架提示词 | prompt token 增加 | 极低 | 增加约 50 个字符 |
| C: Relevance nudge | 模型在不应激活时被迫激活 | 低 | 保留"高度匹配"的措辞，依赖 `_matches()` 的精确度 |
| D: Usage nudge | 提醒过于频繁 | 低 | `STALE_QUERIES = 6` 的冷却机制不变 |
| E: 即时可见 | tool_result 消息过大 | 中 | 限制返回的 body 长度，或仅返回摘要 |

### 6.2 向后兼容性

- 所有改动仅修改文本内容，不改变数据结构、API 或协议
- `SkillRelevancePolicy` 的冷却机制、`SkillUsageNudgePolicy` 的计数逻辑不变
- Skill 工具的输入输出 schema 不变
- `invoked_skills` 的写入路径和 PromptAssembler 的读取路径不变

### 6.3 预期效果

改动 A-D 实施后，当 `SkillRelevancePolicy` 检测到匹配时：

```
当前：模型看到 3 条建议 → 选择忽略 → 直接用基础工具
改进后：模型在 Tool Schema 中看到 BLOCKING REQUIREMENT → 必须先调用 skill → 再执行任务
```

---

## 7. 实施计划

| 步骤 | 改动 | 文件 | 预估工作量 |
|---|---|---|---|
| 1 | A: Tool description | `core/tools/builtin/skill.py` | 修改 SCHEMA.description |
| 2 | B: 框架提示词 | `core/prompt/system_context.py` | 修改 `_FRAMEWORK_PROMPT` |
| 3 | C: Relevance nudge | `core/policy/skill_relevance.py` | 修改 nudge 文本 |
| 4 | D: Usage nudge | `core/policy/skill_usage_nudge.py` | 修改 nudge 文本 |
| 5 | 验证测试 | `tests/test_skill_relevance_policy.py` | 更新断言中的预期文本 |
| 6（可选） | E: 即时可见 | `core/tools/builtin/skill.py` | 需额外设计 |

步骤 1-4 可在一次提交中完成，步骤 5 同步更新。

---

## 8. 验证方案

### 回归测试

1. 更新 `tests/test_skill_relevance_policy.py` 中对 nudge 文本的断言
2. 运行现有 skill 相关测试套件，确认无破坏性变更

### 行为验证

使用原始问题复现场景验证：

```
输入：帮我看看 gstack 项目是干什么的
预期：模型首先调用 skill 工具激活 code-explorer，然后按 4-phase 流程执行
```

### 边界场景

| 场景 | 预期行为 |
|---|---|
| 日常闲聊（不匹配任何 skill） | 正常对话，不触发 skill 激活 |
| 模糊匹配（可能相关但不明确） | 模型根据 Tool Schema 中的 BLOCKING REQUIREMENT 判断是否激活 |
| 多个 skill 同时匹配 | 模型选择最相关的激活（现有机制不变） |
| 用户显式要求不用 skill | 用户指令优先级高于 BLOCKING REQUIREMENT |

---

## 9. 与行为锚定对策的关系

本文档是 [2026-05-09-behavioral-anchoring-countermeasure-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-09-behavioral-anchoring-countermeasure-design.md) 的直接后续。

行为锚定对策文档解决了"长 session 中 transcript 积累直接工具调用模式，覆盖 system prompt 指令"的问题，通过 `SkillRelevancePolicy` 和 `SkillUsageNudgePolicy` 注入尾部提醒来打破 in-context learning bias。

本文档解决的是更基础的一层：**即使提醒被正确注入、正确检测到匹配，模型仍然可以选择不激活 skill**。根因不是 transcript 模式覆盖，而是指令本身不够强——模型把提醒当成了可选建议。

两个文档的关系：

```
行为锚定对策                    Skill Activation Enforcement
(2026-05-09)                    (2026-05-10, 本文档)
    │                                   │
    │  解决：长 session transcript       │  解决：单轮中模型看到提醒
    │  积累直接工具调用模式，             │  但选择不调用 skill 工具
    │  覆盖 system prompt 指令           │
    │                                   │
    ▼                                   ▼
  SkillRelevancePolicy               Tool Schema BLOCKING
  SkillUsageNudgePolicy              REQUIREMENT + 强化提示词
  (注入尾部提醒)                      (在决策点设置路障)
    │                                   │
    └──────────────┬────────────────────┘
                   ▼
           模型可靠地激活匹配的 skill
```

---

## 10. 开放问题

1. **是否需要"高置信度匹配时自动激活"机制？** 当前方案仍然依赖模型调用 skill 工具。如果模型仍然忽略 BLOCKING REQUIREMENT（极端情况），可以考虑在 `SkillRelevancePolicy` 中增加一个 `auto_activate` 阈值：当关键词精确匹配时，直接写入 `invoked_skills`，跳过模型决策。但这会与"用户选择权"设计原则冲突。

2. **Tool description 的中文 vs 英文**：当前系统提示词混合了中英文。Tool description 使用英文（因为模型对英文指令的遵循度通常更高），system prompt 使用中文。需要确认这种混合是否影响指令强度。

3. **改动 E（即时可见）是否必要？** 如果改动 A-D 已经足够驱动模型激活 skill，那么"激活后当轮不可见"的问题可能不值得额外解决。需要通过实际测试验证。
