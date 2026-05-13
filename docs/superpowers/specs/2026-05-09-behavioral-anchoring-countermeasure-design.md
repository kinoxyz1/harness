# Behavioral Anchoring Countermeasure 设计

> 日期：2026-05-09
> 状态：待评审
> 相关文档：
> - [2026-05-08-runtime-context-governance-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-08-runtime-context-governance-design.md)（上下文压缩治理，本文的互补文档）
> - [2026-05-06-context-assembly-refactor-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-06-context-assembly-refactor-design.md)（上下文分层组装）
> 当前相关实现：
> - [core/policy/base.py](/Users/kino/works/kino/harness/core/policy/base.py) — PolicyRunner / RunPolicy 协议
> - [core/policy/todo_tracking.py](/Users/kino/works/kino/harness/core/policy/todo_tracking.py) — 唯一的 system-reminder 策略
> - [core/prompt/assembler.py](/Users/kino/works/kino/harness/core/prompt/assembler.py) — PromptAssembler，`build_query_overlay_blocks` 当前为空
> - [core/query/loop.py](/Users/kino/works/kino/harness/core/query/loop.py) — QueryLoop 主循环
> - [core/session/compact_service.py](/Users/kino/works/kino/harness/core/session/compact_service.py) — Compact 后 runtime restore
> Claude Code 参考：
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/attachments.ts` — Attachment 注入管线（per-turn behavioral reinforcement）
> - `/Users/kino/works/opensource/Claude-Code-doc/src/utils/messages.ts` — Attachment 渲染为 `<system-reminder>`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/compact.ts` — Compaction 后 strip-and-reinject
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/SkillTool/prompt.ts` — Skill listing budget 管理
> - `/Users/kino/works/opensource/Claude-Code-doc/src/tools/AgentTool/loadAgentsDir.ts` — `criticalSystemReminder_EXPERIMENTAL`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/bootstrap/state.ts` — `InvokedSkillInfo` / `addInvokedSkill()`
> - `/Users/kino/works/opensource/Claude-Code-doc/src/services/compact/postCompactCleanup.ts` — Post-compact 状态清理
> - `/Users/kino/works/opensource/Claude-Code-doc/src/constants/prompts.ts` — System prompt assembly + session guidance
> - `/Users/kino/works/opensource/Claude-Code-doc/src/query.ts` — `startSkillDiscoveryPrefetch`

---

## 1. 摘要

本文档描述一个被现有设计文档遗漏的问题——**行为锚定（Behavioral Anchoring）**——及其解决方案。

### 问题

长 session 运行后，模型虽然在 thinking 中意识到"应该用 skill"，但在生成 action 时跳过了 skill 调用，直接执行了 bash / read / write。这不是因为 skill 信息缺失（skill 目录完整存在于 system prompt，上下文也只用了 18%），而是因为 **transcript 中积累了数百次直接工具调用、0 次 skill 调用的行为模式，覆盖了 system prompt 的指令**。

### 根因

LLM 的 in-context learning bias——对话历史本身就是最强指令。当 transcript 里 99% 是直接工具调用时，模型"学到"了这个行为模式并复制它。

### 解决方向

对抗行为锚定不能用上下文压缩（压缩不影响 transcript 模式分布），只能用 **每轮行为强化注入（Per-Turn Behavioral Reinforcement）**：在每轮模型调用前，注入与 transcript 模式相反的信号，打破 in-context learning bias。

### 与现有设计文档的关系

本文档是 [2026-05-08-runtime-context-governance-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-08-runtime-context-governance-design.md) 的互补文档：

- **Governance 设计**解决的是"上下文太大会爆"（量的问题）——通过 offloading、microcompact、auto-compact 压缩上下文
- **本设计**解决的是"上下文不爆但行为漂移"（质的问题）——通过 per-turn 注入保持模型行为与 system prompt 一致

两者正交：一个治理水位，一个治理行为。

---

## 2. 问题复现与根因分析

### 2.1 复现场景

同一个任务——"基于 CSV 文件生成分析报告"——在不同 session 状态下执行结果截然不同：

**Run 1（长 session 后，已执行数百次 bash/read/write，0 次 skill 调用）**：

```
>> 基于csv 文件的完整数据: .../TEST_DATA.csv, 生成一份分析报告

思考过程:
│ 让我先读取CSV文件看看数据内容，然后加载 analysis-report skill
│                                                     来指导报告生成。

实际行动: $ Bash(python3 -c "import pandas...")   ← 直接 bash，没有 skill
```

模型在 thinking 中说"要加载 skill"，但输出 action 时直接跳过了。后续整个报告生成过程中，skill 被完全忽略。

**Run 2（新 session，第一个任务）**：

```
>> 基于csv 文件的完整数据: .../TEST_DATA.csv, 生成一份分析报告

思考过程:
│ 应该使用 analysis-report skill。让我先加载这个skill。

实际行动: $ Skill(analysis-report)                ← 先调用 skill
```

模型正确识别并调用了 skill，后续完整遵循了 skill 指令。

### 2.2 根因定位

关键对比数据：

| 维度 | Run 1（长 session） | Run 2（新 session） |
|---|---|---|
| 前序行为模式 | 数百次 bash / read / write | 无（第一个任务） |
| skill 调用历史 | 全程 0 次，强锚定"直接用工具" | 无先例，平等考虑所有工具 |
| 模型推理 | "然后加载 skill" → **没执行** | "应该使用 skill" → **执行了** |
| 上下文量 | ~37k tokens（18% of 200k） | ~2k tokens |
| skill 信息完整性 | 完整（stable system prompt 中） | 完整 |

排除非根因因素：

| 因素 | 是否为根因 | 证据 |
|---|---|---|
| 上下文窗口溢出 | 否 | 37k / 200k = 18% |
| skill 信息被 compact 掉 | 否 | `<available-skills>` 在 stable system prompt，不受 compact 影响 |
| skill 内容被 offload | 否 | skill 在 Structured State，永远不会被 offload |
| 运行时 budget 截断 | 否 | `build_runtime_blocks()` 无字符截断 |
| **行为模式锚定** | **是** | 模型 thinking 里有 skill 意识，但 action 被 transcript pattern 压倒 |

### 2.3 根因模型

```
长 session 积累的行为模式：
  数百次 → bash / read_file / write_file / edit_file
  0 次   → skill

模型的隐式权重：
  [transcript pattern: 99% 直接工具调用]  >>>  [system prompt: "考虑使用 skill"]

结果：模型 thinking 里有 "应该用 skill" 的念头，
     但 action 生成被 transcript 里的数百个 "直接工具调用" 范例压倒了
```

这是 LLM 的 in-context learning bias——对话历史本身就是最强指令。即便 system prompt 说"考虑使用 skill"，当 transcript 里全是直接工具调用的范例时，模型会"学到"这个行为模式并复制它。

**为什么上下文压缩不能解决这个问题**：即使 microcompact 清理了 80% 的旧工具结果、auto-compact 把历史替换成摘要，transcript 中保留下来的最新 6-8 条消息仍然是直接工具调用模式。压缩改变了量，但没有改变模式分布——最近的工具调用仍然是 bash/read/write，不是 skill。

---

## 3. Claude Code 的解决方案

Claude Code 在生产环境中面对相同的问题，通过 **6 层 per-turn behavioral reinforcement 机制** 对抗行为锚定。以下逐层分析其源码实现。

### 3.1 机制 1: 每轮 Skill Discovery（`skill_discovery` attachment）

这是对抗行为锚定最核心的机制——**每轮都注入一个与 transcript 模式相反的信号**，提醒模型"你有 skill 可以用"。

#### 数据结构

**文件**: `src/utils/attachments.ts:537-542`

```typescript
// skill_discovery attachment 类型定义
| {
    type: 'skill_discovery'
    skills: { name: string; description: string; shortId?: string }[]
    signal: DiscoverySignal       // 什么触发了这次发现
    source: 'native' | 'aki' | 'both'  // 来源：本地搜索 / AKI 后端 / 两者
  }
```

#### 注入时机

**Turn-0（阻塞式）**: `src/utils/attachments.ts:789-813`

Turn-0 时，用户输入被传递给 `getAttachments()`，阻塞式计算 skill 相关性。用户文本作为发现信号（signal），确保第一个任务就有 skill 提醒。

**Inter-turn（异步 prefetch）**: `src/query.ts:323-335`

```typescript
// 每轮迭代开头，启动异步 skill 发现
const pendingSkillPrefetch = skillPrefetch?.startSkillDiscoveryPrefetch(
  null,       // 无用户输入信号——使用 findWritePivot guard
  messages,
  toolUseContext,
)
```

与模型 streaming 并行运行。结果在工具执行完后收集（`src/query.ts:1617-1628`）。不增加主轮延迟。

`findWritePivot` guard 内部过滤：97% 的 Haiku 调用在生产环境中没有发现任何内容，因此 inter-turn 发现不是每轮都产生结果，而是只在有意义的上下文变化时产生。

#### 渲染效果

**文件**: `src/utils/messages.ts:3503-3519`

```typescript
// skill_discovery 渲染为 system-reminder
if (attachment.skills.length === 0) return []
const lines = attachment.skills.map(s => `- ${s.name}: ${s.description}`)
return wrapMessagesInSystemReminder([
  createUserMessage({
    content:
      `Skills relevant to your task:\n\n${lines.join('\n')}\n\n` +
      `These skills encode project-specific conventions. ` +
      `Invoke via Skill("<name>") for complete instructions.`,
    isMeta: true,
  }),
])
```

模型在每一轮看到的不是静态的"skill 目录"，而是**与当前任务具体相关的 skill 提醒**：

```
<system-reminder>
Skills relevant to your task:

- analysis-report: generate structured, styled HTML analysis reports from any data source
- data-validation: validate data quality before analysis

These skills encode project-specific conventions. Invoke via Skill("name") for complete instructions.
</system-reminder>
```

这个 `<system-reminder>` 出现在 transcript 的**最新消息**中（不是古老的 system prompt 开头），位置紧贴模型将要处理的当前输入，因此对模型行为的引导力远强于 system prompt 中不可见的 `<available-skills>` 目录。

#### 关键设计决策

1. **不是每轮都全量扫描**：使用 `findWritePivot` guard 避免无效计算。只有当 transcript 发生了"写入"（即状态发生了有意义的变化）时才重新扫描。
2. **Feature-gated**：整个 skill discovery 系统受 `EXPERIMENTAL_SKILL_SEARCH` feature flag 控制。关闭时降级为只有静态 skill listing。
3. **独立于 skill listing**：`skill_discovery` 是任务相关的动态信号，`skill_listing` 是全量静态目录。两者互不替代。

### 3.2 机制 2: Skill Listing 增量注入（`skill_listing` attachment）

#### 注入逻辑

**文件**: `src/utils/attachments.ts:2661-2741`

```typescript
// getSkillListingAttachments() — 收集可用 skills 并格式化
function getSkillListingAttachments(...) {
  // 只发送还没被发送过的新 skills
  const sentNames = sentSkillNames.get(agentId) ?? new Set()
  const newSkills = allSkills.filter(s => !sentNames.has(s.name))

  if (newSkills.length === 0) return []  // 没有新 skill，不注入

  // Budget: 上下文窗口的 1%，默认 8000 chars
  const formatted = formatCommandsWithinBudget(newSkills, charBudget)

  return [{ type: 'skill_listing', content: formatted, isInitial: sentNames.size === 0 }]
}
```

关键追踪机制：`sentSkillNames` 是一个 `Map<string, Set<string>>`（per-agent），记录已经通知过模型的 skill 名称。只有**新增**的 skill 才触发注入，避免重复浪费 token。

#### Budget 管理

**文件**: `src/tools/SkillTool/prompt.ts:20-50, 70-171`

```typescript
const SKILL_BUDGET_CONTEXT_PERCENT = 0.01  // 上下文窗口的 1%
const DEFAULT_CHAR_BUDGET = 8000
const MAX_LISTING_DESC_CHARS = 250

function formatCommandsWithinBudget(skills, budget) {
  // 渐进截断策略：
  // 1. 优先保留 bundled skills（永不截断）
  // 2. 非 bundled skills 先用完整描述
  // 3. 超预算时截断非 bundled 的描述
  // 4. 再超预算时只保留名称
}
```

#### 渲染效果

**文件**: `src/utils/messages.ts:3728-3737`

```
<system-reminder>
The following skills are available for use with the Skill tool:

- analysis-report: Generate structured HTML analysis reports...
- systematic-debugging: Use for ANY technical issue...
- test-driven-development: Write tests before implementation...
</system-reminder>
```

#### Compaction 后的重新注入

**文件**: `src/services/compact/compact.ts:563-585`（full compact）

Compaction 后，`sentSkillNames` **不被重置**（`src/services/compact/postCompactCleanup.ts:62-69`），但是 post-compact 的第一条消息会通过 `deferred_tools_delta`、`agent_listing_delta` 等全量重新注入机制，确保模型在 compact 后有完整的工具感知。

`postCompactCleanup.ts:17-20` 的关键注释：

```typescript
// We intentionally do NOT clear invoked skill content here.
// Skill content must survive across multiple compactions.
```

### 3.3 机制 3: Critical System Reminder（`criticalSystemReminder_EXPERIMENTAL`）

这是对抗行为锚定最直接的机制——**每轮注入一个不可忽略的行为约束**。

#### Agent 定义接口

**文件**: `src/tools/AgentTool/loadAgentsDir.ts:121`

```typescript
export interface AgentDefinition {
  // ...
  criticalSystemReminder_EXPERIMENTAL?: string  // 每轮重新注入的短消息
}
```

#### 注入管线

**文件**: `src/utils/attachments.ts:1587-1595`

```typescript
function getCriticalSystemReminderAttachment(toolUseContext) {
  const reminder = toolUseContext.criticalSystemReminder_EXPERIMENTAL
  if (!reminder) return []

  return [{
    type: 'critical_system_reminder',
    content: reminder,
  }]
}
```

**文件**: `src/utils/attachments.ts:919-921` — 在 `getAttachments()` 的注入管线中：

```typescript
// 每轮都注入，无条件
attachments.push(...getCriticalSystemReminderAttachment(toolUseContext))
```

**文件**: `src/utils/messages.ts:3872-3876` — 渲染：

```typescript
// critical_system_reminder → <system-reminder>
if (attachment.type === 'critical_system_reminder') {
  return wrapMessagesInSystemReminder([
    createUserMessage({ content: attachment.content, isMeta: true })
  ])
}
```

#### 实际使用案例

**文件**: `src/tools/AgentTool/built-in/verificationAgent.ts:150-151`

```typescript
criticalSystemReminder_EXPERIMENTAL:
  'CRITICAL: This is a VERIFICATION-ONLY task. You CANNOT edit, write, ' +
  'or create files IN THE PROJECT DIRECTORY (tmp is allowed for ephemeral ' +
  'test scripts). You MUST end with VERDICT: PASS, VERDICT: FAIL, or ' +
  'VERDICT: PARTIAL.'
```

**传递链**: `loadAgentsDir.ts` → `runAgent.ts:711-712`（设置到 `ToolUseContext`） → `attachments.ts:1587-1595`（每轮注入） → `messages.ts:3872-3876`（渲染为 `<system-reminder>`）

**为什么这个机制有效**：它在 transcript 的**最新位置**（紧贴当前用户输入）注入约束，而不是在古老的 system prompt 开头。无论 transcript 积累了多少相反模式，最新的消息对模型行为的影响最大。

### 3.4 机制 4: 定期工具使用提醒（`todo_reminder`, `task_reminder`）

#### 注入逻辑

**文件**: `src/utils/messages.ts:3663-3678`

```typescript
// todo_reminder — 当 TodoWrite 工具长时间未使用时注入
if (attachment.type === 'todo_reminder') {
  return wrapMessagesInSystemReminder([
    createUserMessage({
      content:
        'The TodoWrite tool hasn\'t been used recently. If it would be ' +
        'relevant to your current work, consider using it to track progress.',
      isMeta: true,
    })
  ])
}
```

**文件**: `src/utils/messages.ts:3680-3700` — 类似的 `task_reminder`。

这些提醒的作用不是告知模型"有这些工具"（工具定义已经在 API 请求中），而是在模型被 transcript 模式锚定到"不用这些工具"时，**打破锚定**，提醒它重新考虑工具选择。

### 3.5 机制 5: Compaction 后的 Strip-and-Reinject

Compaction 是一个天然的"模式重置"机会——transcript 被大幅缩减时，行为锚定的模式也被打断了。Claude Code 利用这个机会做了一次完整的工具感知重建。

#### 压缩前剥离

**文件**: `src/services/compact/compact.ts:202-223`

```typescript
function stripReinjectedAttachments(messages) {
  // 压缩前，从消息中移除这些类型的 attachment：
  // - skill_discovery  ← 任务相关的 skill 提醒（会过时）
  // - skill_listing    ← skill 目录（会在 compact 后重新注入）
  // 这样它们不会污染 LLM 生成的摘要内容
  return messages.filter(msg => {
    const att = getAttachment(msg)
    if (!att) return true
    return att.type !== 'skill_discovery' && att.type !== 'skill_listing'
  })
}
```

#### 压缩后全量重新注入

**文件**: `src/services/compact/compact.ts:563-585`（full compact）

```typescript
// Post-compact 附件构建（并行构建多个 delta）
const [
  fileAttachments,
  asyncAgentAttachments,
  planAttachment,
  // ... 其他附件
  skillAttachment,           // ← 已调用 skills 的完整内容
] = await Promise.all([
  getFileAttachments(...),
  getAsyncAgentAttachments(...),
  getPlanAttachment(...),
  createSkillAttachmentIfNeeded(...),  // ← 保留已调用 skill 内容
])

// 全量重新注入 deltas（因为 compact 后 history 为空，diff = full set）
const deferredToolsDelta = getDeferredToolsDeltaAttachment(messages, [])      // 全量
const agentListingDelta = getAgentListingDeltaAttachment(messages, [])        // 全量
const mcpInstructionsDelta = getMcpInstructionsDeltaAttachment(messages, []) // 全量
```

#### 已调用 Skills 的跨 Compaction 保留

**文件**: `src/services/compact/compact.ts:1488-1534`

```typescript
function createSkillAttachmentIfNeeded(state, charBudget) {
  const invokedSkills = getInvokedSkillsForAgent(state, agentId)
  if (invokedSkills.length === 0) return []

  // Budget: 25K tokens 总量 / 5K per skill
  // 按 token 预算截断，但保留核心内容
  // 截断标记：[... skill content truncated for compaction; use Read on the skill path...]
  const totalBudget = POST_COMPACT_SKILLS_TOKEN_BUDGET  // 25000
  const perSkillBudget = 5000

  // 按最近调用时间排序（最新优先）
  const sorted = invokedSkills.sort((a, b) => b.timestamp - a.timestamp)

  return [{
    type: 'invoked_skills',
    skills: truncateToTokens(sorted, totalBudget, perSkillBudget),
  }]
}
```

**渲染**: `src/utils/messages.ts:3644-3662`

```typescript
if (attachment.type === 'invoked_skills') {
  const lines = attachment.skills.map(s =>
    `<invoked-skill id="${s.id}">\n${s.content}\n</invoked-skill>`
  )
  return wrapMessagesInSystemReminder([
    createUserMessage({
      content:
        'The following skills were invoked in this session. ' +
        'Continue to follow these guidelines:\n\n' + lines.join('\n'),
      isMeta: true,
    })
  ])
}
```

注意最后那句 **"Continue to follow these guidelines"** —— 这是一个显式的行为约束指令，告诉模型即使在 compact 后，仍然要遵循之前加载的 skill。

### 3.6 机制 6: System Prompt Section 缓存与隔离

#### 缓存系统

**文件**: `src/constants/systemPromptSections.ts:17-38`

两种 system prompt section：

```typescript
// 稳定 section：计算一次，缓存直到 /clear 或 /compact
function systemPromptSection(name: string, compute: () => string) {
  // 内部维护 sectionCache Map
  // 缓存 key = name
  // 缓存值只在 clearSystemPromptSections() 调用时清除
}

// 不稳定 section：每轮重新计算，值变化时打断 prompt cache
function DANGEROUS_uncachedSystemPromptSection(
  name: string,
  compute: () => string,
  reason: string  // 为什么要 uncached（例如 "MCP servers connect/disconnect between turns"）
) {
  // 每轮都重新 compute()
  // 如果值与上一轮不同，会打断 Anthropic 的 prompt cache prefix
}
```

#### 动态边界

**文件**: `src/constants/prompts.ts:107-115`

```typescript
const SYSTEM_PROMPT_DYNAMIC_BOUNDARY = '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

这个标记把 system prompt 分成两段：

1. **边界前**：静态内容（`scope: 'global'`）—— 跨会话可缓存，包含行为指令
2. **边界后**：动态内容（`scope: 'session'`）—— 每会话独立，包含 session-specific guidance

**文件**: `src/constants/prompts.ts:444-577` — `getSystemPrompt()` 组装顺序：

```
intro → system section → doing tasks → actions → using tools → tone/style → efficiency
    ↓
  DYNAMIC_BOUNDARY
    ↓
session_guidance → memory → env_info → mcp_instructions → summarize_tool_results → ...
```

`using tools` section（`src/constants/prompts.ts:269-313`）包含工具使用偏好规则：

```
Prefer dedicated tools over Bash when one fits (Read, Edit, Write)
— reserve Bash for shell-only operations.
```

这个规则在**静态层**，意味着它在所有轮次中**完全相同**地存在，提供了稳定的行为锚点。

#### Session-specific Guidance

**文件**: `src/constants/prompts.ts:352-400`

```typescript
function getSessionSpecificGuidanceSection(tools, model, ...) {
  // 包含三部分：
  // 1. Skill tool 使用指引
  // 2. Discover skills 指引（告诉模型 skill 相关性每轮都会被自动推送）
  // 3. Verification agent 指引
}
```

关键部分 — `getDiscoverSkillsGuidance()`（`src/constants/prompts.ts:322-340`）：

```typescript
function getDiscoverSkillsGuidance() {
  return `Relevant skills are automatically surfaced each turn as "Skills relevant to your task:" ` +
    `reminders. If you're about to do something those don't cover — a mid-task pivot, ` +
    `an unusual workflow, a multi-step plan — call ${DISCOVER_SKILLS_TOOL_NAME} ` +
    `with a specific description of what you're doing. Skills already visible or loaded ` +
    `are filtered automatically. Skip this if the surfaced skills already cover your next action.`
}
```

这段指令告诉模型：**skill 相关性是每轮自动推送的**。这建立了一个预期——模型知道它不需要记住 skill 目录，因为每轮都会有新的提醒。

### 3.7 Claude Code 的附件注入管线总览

**文件**: `src/utils/attachments.ts` — `getAttachments()` 函数

每轮模型调用前，`getAttachments()` 按以下顺序收集所有附件：

```
1. critical_system_reminder     ← 无条件注入，每轮
2. deferred_tools_delta         ← 工具可用性增量通知
3. agent_listing_delta          ← Agent 类型增量通知
4. mcp_instructions_delta       ← MCP 指令增量通知
5. skill_listing                ← 新 skill 可用性通知
6. skill_discovery              ← 任务相关 skill 提醒
7. compaction_reminder          ← 上下文压力提醒
8. context_efficiency           ← 上下文效率提醒
9. plan_mode                    ← 计划模式约束（每 5 个人类轮）
```

**文件**: `src/query.ts:1580` — 在主循环的每次工具迭代中：

```typescript
// 每次工具迭代都重新获取附件
const attachmentMessages = getAttachmentMessages(...)
for (const msg of attachmentMessages) {
  yield msg
}
```

这意味着不仅是每个用户消息触发一次，而是**每次工具迭代都触发**。在一次用户请求中，模型可能调用多个工具，每次工具调用返回后，附件系统都会重新评估并注入必要的提醒。

---

## 4. harness 当前状态分析

### 4.1 harness 已有的对抗机制

| 机制 | 文件 | 效果 |
|---|---|---|
| Skill 目录在 stable system prompt | `assembler.py:34-51` | 静态，不随 transcript 变化 |
| Active skills 在 runtime blocks | `assembler.py:263-272` | 仅覆盖已调用的 skills |
| Todo staleness reminder | `policy/todo_tracking.py` | 唯一的 `<system-reminder>` |
| Compact 后 runtime restore | `compact_service.py:80-123` | 恢复已调用 skills |
| System prompt caching | `assembler.py:22-31` | 行为指令在所有轮次中一致 |

### 4.2 harness 缺失的机制

| Claude Code 机制 | harness 状态 | 影响 |
|---|---|---|
| 每轮 `skill_discovery` 注入 | **无** | 模型在长 session 中不会被提醒"当前任务匹配某个 skill" |
| `skill_listing` 增量注入 | **无** | 只有 system prompt 中的静态目录，无动态提醒 |
| `criticalSystemReminder` | **无** | 无法注入每轮行为约束 |
| 定期 skill/tool 使用提醒 | **无** | 当 skill 工具长期未用时没有 nudge |
| Compaction 后 skill 可用性重建 | **部分** | 有 `skills_restore`，但只恢复已调用的 skills |
| Strip-and-reinject 模式 | **无** | Compaction 不会重建 skill 感知 |
| Per-turn attachment 管线 | **无** | 没有每轮动态注入附件的管线（`build_query_overlay_blocks` 是空实现，且它拼入 system prompt 而非 transcript） |

### 4.3 缺失的根因链

```
harness 的当前数据流：

  用户消息 → QueryLoop → [TodoPlanningPolicy（唯一的 policy）]
                       → PromptAssembler.build_stable（system prompt + 静态 skill 目录）
                       → PromptAssembler.build_runtime_blocks（已激活 skills + todo + file）
                       → PromptAssembler.build_query_overlay_blocks（空）
                       → Governor.assess（水位 + 压缩）
                       → ViewBuilder.build（最终组装）
                       → 模型调用

问题：
  1. skill 目录在 stable system prompt 的**开头**，但 transcript 的**尾部**模式更强
  2. 没有任何机制在 transcript 尾部注入"考虑使用 skill"的信号
  3. runtime blocks 只有已激活的 skills，对未激活但匹配的 skills 没有提醒
  4. `build_query_overlay_blocks` 是空的——且即使填入内容，也会拼进 system prompt 而非 transcript

结果：
  transcript 模式（99% 直接工具调用）>> system prompt（"考虑使用 skill"）
  → 行为锚定 → 模型跳过 skill 调用

修复方向：
  不通过 overlay 拼入 system prompt（那样强化信号仍在上下文开头）
  而是通过 PolicyRunner → store.extend() 注入 user 消息到 transcript 尾部
```

---

## 5. 设计方案

### 5.1 设计原则

1. **注入位置在 transcript 尾部，不在 system prompt 头部**。LLM 对最新消息的注意力远强于对开头消息的注意力。因此必须通过 PolicyRunner 注入 user 消息到 transcript（`store.extend()`），而不是通过 `build_query_overlay_blocks` 拼入 system prompt。
2. **每轮动态计算，不是静态配置**。注入内容应该与当前任务相关，而不是所有 skill 的全量列表。
3. **状态必须跨 query 持久化**。行为锚定是长 session 漂移问题，计数器和冷却状态必须放在 SessionState（跨 query 生存），不能放在 RunState（单次 `QueryLoop.run()` 内部重新创建）。
4. **Budget 通过 transcript 隐式参与水位计算**。Policy 注入的消息进入 `conversation_messages`，Governor 的 `_calc_water_level()` 通过 `estimate_messages_tokens()` 自动计入。无需单独核算。

### 5.2 新增组件

#### 组件 A: `SkillRelevancePolicy`

**文件**: `core/policy/skill_relevance.py`（新建）

**注入通道**: PolicyRunner 的 `before_model_call()` → `store.extend()` → `conversation_messages` 尾部。

这是注入位置的关键：**不通过 `build_query_overlay_blocks`**（它会拼入 system prompt），而是通过 PolicyRunner 注入为 user 消息进入 transcript 尾部。这与现有 `TodoPlanningPolicy` 使用完全相同的通道（`core/query/loop.py:244-246`），确保强化信号出现在 transcript 最新位置。

```python
class SkillRelevancePolicy:
    """每轮分析当前任务上下文，如果匹配某个 skill 的 when_to_use 描述，
    注入 <system-reminder> 提醒模型该 skill 可用。

    对抗行为锚定的核心机制：在 transcript 尾部注入与 transcript 模式相反的信号。

    注入通道：PolicyRunner.before_model_call() → store.extend() → conversation_messages
    不是 build_query_overlay_blocks（那个拼进 system prompt，达不到尾部强化的目标）。
    """

    RELEVANCE_BUDGET_CHARS = 1500   # 单次注入的字符预算
    COOLDOWN_QUERIES = 3            # 同一 skill 的提醒冷却 query 数

    def before_model_call(self, session_state, run_state) -> list[dict]:
        # 1. 获取最近一条用户消息的文本作为任务上下文
        # 2. 遍历 skill_catalog 中未激活的 skills（不在 invoked_skills 中的）
        # 3. 对每个 skill 的 when_to_use 做关键词匹配
        # 4. 检查冷却（session_state.skill_relevance_cooldown，避免重复提醒同一 skill）
        # 5. 构造 <system-reminder> user 消息注入
        # 6. 更新冷却状态到 session_state
        ...
```

注入格式（作为 `role: "user"` 消息进入 transcript 尾部）：

```xml
<system-reminder type="skill_relevance">
以下 skill 与当前任务相关，但尚未激活。如果匹配你的工作，建议先调用 skill 工具加载它：

- analysis-report: 适用于"基于数据生成结构化报告"的任务
- data-validation: 适用于"数据质量检查"的任务

调用方式：skill({"skill": "analysis-report"})
</system-reminder>
```

#### 组件 B: `SkillUsageNudgePolicy`

**文件**: `core/policy/skill_usage_nudge.py`（新建）

**注入通道**: 与组件 A 相同，通过 PolicyRunner → `store.extend()` → transcript 尾部。

**skill 使用检测**：基于 `session_state.invoked_skills` 的**变化**，而不是工具调用名称。这样覆盖了两条激活路径：
- 模型调用 `skill` 工具（经过 tool batch）
- 用户执行 `/skills use <id>`（不经过 tool batch，直接写入 `invoked_skills`）

具体实现：在每次 `before_model_call` 时记录 `invoked_skills` 的 key 集合快照，与上次快照对比。如果集合增长（有新 skill 被激活），重置 stale 计数器。

```python
class SkillUsageNudgePolicy:
    """当 skill 长时间未被激活但 skill_catalog 非空时，
    注入一个温和的提醒，防止模型被 transcript 模式锚定。

    类似 Claude Code 的 todo_reminder / task_reminder。

    skill 使用检测：基于 session_state.invoked_skills 的变化，
    覆盖模型 tool call 和 /skills use 两条激活路径。
    """

    STALE_QUERIES = 6  # 连续多少次 query 没有新 skill 激活后触发

    def before_model_call(self, session_state, run_state) -> list[dict]:
        if not session_state.skill_catalog:
            return []

        current_skills = set(session_state.invoked_skills.keys())
        # 检查是否有新 skill 被激活（覆盖 /skills use 和 tool call 两条路径）
        if current_skills != session_state.last_known_skill_keys:
            session_state.queries_since_skill_activation = 0
            session_state.last_known_skill_keys = current_skills
        else:
            session_state.queries_since_skill_activation += 1

        if session_state.queries_since_skill_activation < self.STALE_QUERIES:
            return []
        # 注入温和提醒
        ...
```

注入格式（作为 `role: "user"` 消息进入 transcript 尾部）：

```xml
<system-reminder type="skill_nudge">
当前会话有 {N} 个可用 skill，但最近 {M} 次查询未激活新 skill。
如果当前任务涉及数据分析、报告生成、调试、TDD 等场景，考虑先加载对应 skill。
</system-reminder>
```

#### 组件 C: Compact 后 Skill 可用性重建

**文件**: `core/session/compact_service.py`（修改）

在 `build_runtime_restore_messages` 中新增 `skill_catalog_restore` 类型：

```python
def build_runtime_restore_messages(state, ...):
    restored = []

    # 现有：user_intent_restore
    # 现有：todo_restore
    # 现有：skills_restore（已调用的 skills）
    # 现有：read_file_restore

    # 新增：skill_catalog_restore —— 重建完整 skill 可用性感知
    if state.skill_catalog:
        catalog_lines = []
        for skill_id, meta in sorted(state.skill_catalog.items()):
            if skill_id not in state.invoked_skills:  # 未激活的才需要提醒
                line = f"- {skill_id}: {meta.description}"
                if meta.when_to_use:
                    line += f"（适用：{meta.when_to_use}）"
                catalog_lines.append(line)
        if catalog_lines:
            restored.append({
                "role": "meta_runtime_restore",
                "kind": "skill_catalog_restore",
                "content": (
                    "以下 skill 可用但尚未激活。如果当前任务匹配，建议先调用 skill 工具：\n"
                    + "\n".join(catalog_lines)
                ),
            })

    return restored
```

### 5.3 SessionState 扩展

**文件**: `core/session/state.py`

行为锚定是长 session 漂移问题，所有计数器和冷却状态必须放在 **SessionState**（跨 query 持久化），不能放在 RunState（单次 `QueryLoop.run()` 内部重新创建，见 `core/query/loop.py:233`）。

```python
# core/session/state.py — SessionState 新增字段
@dataclass(slots=True)
class SessionState:
    # ... 现有字段 ...

    # 新增：行为锚定对抗状态（跨 query 持久化）
    queries_since_skill_activation: int = 0          # 距离上次新 skill 激活的 query 数
    last_known_skill_keys: set[str] = field(default_factory=set)  # 上次检查时的 invoked_skills 快照
    skill_relevance_cooldown: dict[str, int] = field(default_factory=dict)  # skill_id → 上次提醒的 query 序号
```

**为什么不能放 RunState**：RunState 在每次 `QueryLoop.run()` 开头重新创建（`core/query/loop.py:233: state = RunState()`），其生命周期仅覆盖单次用户请求。而行为锚定需要跨数十次用户请求累积的 stale 计数——如果把计数器放在 RunState 里，每个新请求都会重置为 0，"连续 6 次 query 没激活 skill"的判断永远不会触发。

### 5.4 QueryLoop 调用链变更

**文件**: `core/query/loop.py`

```python
# SessionEngine 中 PolicyRunner 注册变更
# （policy_runner 在 engine 构造时创建，不在 loop.run() 内部）
policies = [
    TodoPlanningPolicy(),
    SkillRelevancePolicy(),     # 新增
    SkillUsageNudgePolicy(),    # 新增
]
policy_runner = PolicyRunner(policies)
```

**关键：不需要在工具执行后手动更新计数器。** 两个 Policy 都通过 `session_state.invoked_skills` 的快照对比来检测 skill 使用情况（见组件 B 的设计），覆盖了模型 tool call 和 `/skills use` 命令两条路径。计数器递增在 `SkillUsageNudgePolicy.before_model_call()` 内部完成。

**`build_query_overlay_blocks` 保持空实现不变。** 行为强化信号通过 PolicyRunner → `store.extend()` 注入 transcript 尾部，不通过 overlay 拼入 system prompt。

### 5.5 水位预算

Policy 注入的消息通过 `store.extend()` 进入 `conversation_messages`，被 Governor 的 `_calc_water_level()` 通过 `estimate_messages_tokens()` **自动计入水位**（`core/session/governor.py:132-138`）。无需在 `stable_overhead` 中单独核算。

这与 `required=False` 的 overlay block 有本质区别：
- **overlay block**（`required=False`）：进入 system prompt 但**不参与水位计算**（`governor.py:57` 只统计 `block.required` 为 True 的），存在预算盲区
- **Policy 注入消息**：进入 transcript，通过 `estimate_messages_tokens()` 自动计入水位，**无预算盲区**

各注入类型的增量估算：

| 注入类型 | 字符预算 | Token 预算（约） | 触发频率 | 水位核算 |
|---|---|---|---|---|
| `skill_relevance` | 1,500 | ~375 | 每 query（有匹配时） | 自动（transcript 内） |
| `skill_nudge` | 500 | ~125 | 每 6 query（skill 未用时） | 自动（transcript 内） |
| `skill_catalog_restore` | 2,000 | ~500 | compact 后一次性 | 自动（restore 消息） |

总计最大增量：~500 tokens/query（约上下文窗口的 0.25%），远小于 Claude Code 的 1% budget。且因为参与水位计算，当上下文压力接近 blocking line 时，microcompact 和 auto-compact 会正常处理这些消息。

### 5.6 完整数据流（变更后）

```
用户消息 → QueryLoop.run()
         │
         ├─ PolicyRunner.before_model_call()           ← transcript 注入点
         │   ├─ TodoPlanningPolicy（现有：todo stale → store.extend()）
         │   ├─ SkillRelevancePolicy（新增：匹配 skills → store.extend()）
         │   └─ SkillUsageNudgePolicy（新增：skill stale nudge → store.extend()）
         │
         ├─ PromptAssembler.build_stable（system prompt + 静态 skill 目录）
         ├─ PromptAssembler.build_runtime_blocks（已激活 skills + todo + file）
         ├─ PromptAssembler.build_query_overlay_blocks（保持空实现）
         │
         ├─ Governor.assess（水位 + 压缩，水位自动包含 Policy 注入的消息）
         ├─ ViewBuilder.build（最终组装）
         └─ 模型调用

关键区别：
  Policy 注入的消息 → conversation_messages → transcript 尾部（最新位置）
  Runtime blocks → system prompt（固定位置）
  行为强化信号在 transcript 尾部，对模型行为的引导力远强于 system prompt 开头。
```

### 5.7 为什么不通过 `build_query_overlay_blocks` 注入

一个直觉方案是把 skill_relevance 放进 `build_query_overlay_blocks`——它已经预留了位置。但这不能解决行为锚定，因为：

1. **注入通道错误**：`build_query_overlay_blocks` 返回的 `ContextBlock` 在 `governor.py:118` 被并入 `runtime_blocks`，然后在 `view_builder.py:90` 拼进 system prompt 字符串。最终结果是 skill_relevance 出现在 system prompt（上下文最开头），而不是 transcript 尾部（最新位置）。这与文档"尾部位置优势"的前提矛盾。

2. **预算盲区**：如果 `required=False`，overlay block 不参与 `stable_overhead` 计算（`governor.py:57` 只统计 `block.required` 为 True 的），但最终仍进入模型输入（`view_builder.py:91`），存在预算盲区。如果改为 `required=True`，则会在上下文压力较大时挤占其他必需 block 的空间。

3. **Claude Code 的对照**：Claude Code 的 `skill_discovery` 和 `skill_listing` 通过 **attachment 系统** 注入为独立的 `<system-reminder>` 消息（`messages.ts:3503-3519`），不是拼进 system prompt。attachment 出现在 transcript 中，紧贴当前轮次的消息。

正确方案是通过 PolicyRunner 注入为 user 消息，与 `TodoPlanningPolicy` 使用完全相同的通道。

---

## 6. 测试策略

| 组件 | 测试重点 | 测试文件 |
|---|---|---|
| `SkillRelevancePolicy` | 关键词匹配准确性、冷却逻辑、budget 截断、无匹配时返回空 | `test_skill_relevance_policy.py` |
| `SkillUsageNudgePolicy` | invoked_skills 快照对比、skill 为空时不触发、`/skills use` 后重置计数 | `test_skill_usage_nudge_policy.py` |
| SessionState 新字段 | `queries_since_skill_activation` 跨 query 累积、不因 RunState 重建而重置 | `test_session_state.py`（扩展） |
| `skill_catalog_restore` | Compact 后恢复未激活 skill、已激活 skill 不重复 | `test_compact_service.py`（扩展） |
| 集成测试 | 长 session 模拟：50 次 query 后 skill_relevance 仍然注入、注入位置在 transcript 尾部而非 system prompt | `test_behavioral_anchoring.py` |

关键测试场景——**模拟行为锚定**：

```python
def test_skill_relevance_overrides_anchoring():
    """模拟长 session 的行为锚定场景。

    50 次 query 后，skill_relevance 仍然注入，
    且注入位置在 transcript 尾部（role=user）。
    """
    state = SessionState(conversation_messages=[...])  # 50 次查询的历史
    state.skill_catalog = {"analysis-report": SkillMeta(...)}
    state.queries_since_skill_activation = 50
    # invoked_skills 为空 = 从未激活过 skill

    policy = SkillRelevancePolicy()
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"  # 注入到 transcript，不是 system
    assert "analysis-report" in messages[0]["content"]

def test_skills_use_resets_stale_counter():
    """用户通过 /skills use 命令激活 skill 后，stale 计数器重置。"""
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": SkillMeta(...)}
    state.queries_since_skill_activation = 5

    # 模拟 /skills use 命令直接激活 skill
    apply_skill_invocation(state, "analysis-report", content=..., turn=0)
    state.last_known_skill_keys = set(state.invoked_skills.keys())

    policy = SkillUsageNudgePolicy()
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)

    # 刚激活过 skill，不应触发 nudge
    assert len(messages) == 0
```

---

## 7. 文件变更清单

### 7.1 新增文件

| 文件 | 职责 |
|---|---|
| `core/policy/skill_relevance.py` | SkillRelevancePolicy：每轮 skill 相关性匹配与注入 |
| `core/policy/skill_usage_nudge.py` | SkillUsageNudgePolicy：skill 工具使用间隔提醒 |
| `tests/test_skill_relevance_policy.py` | SkillRelevancePolicy 测试 |
| `tests/test_skill_usage_nudge_policy.py` | SkillUsageNudgePolicy 测试 |
| `tests/test_behavioral_anchoring.py` | 行为锚定集成测试 |

### 7.2 修改文件

| 文件 | 变化 | 原因 |
|---|---|---|
| `core/session/state.py` | SessionState 新增 `queries_since_skill_activation`、`last_known_skill_keys`、`skill_relevance_cooldown` | 跨 query 持久化的行为锚定对抗状态（不能放 RunState） |
| `core/session/compact_service.py` | `build_runtime_restore_messages` 新增 `skill_catalog_restore` | Compact 后重建 skill 可用性 |
| `core/session/engine.py` | PolicyRunner 注册新增两个 Policy | 接入新策略 |
| `tests/test_compact_service.py` | 扩展 compact restore 测试覆盖 `skill_catalog_restore` | 测试覆盖 |

### 7.3 保留不动

| 文件 | 原因 |
|---|---|
| `core/policy/base.py` | PolicyRunner / RunPolicy 协议不变，新策略通过注册接入 |
| `core/session/governor.py` | Governor 管理水位，不管理行为注入。Policy 注入的消息通过 transcript 自动参与水位计算 |
| `core/prompt/assembler.py` | `build_query_overlay_blocks` 保持空实现。行为强化通过 PolicyRunner 注入 transcript，不通过 overlay 拼入 system prompt |
| `core/prompt/system_context.py` | Framework prompt 不需要加强 skill 指令 |
| `core/query/loop.py` | 不需要修改。PolicyRunner 在 engine 层注册，`store.extend()` 是已有的注入通道 |

---

## 8. 开放问题

1. **Skill 相关性匹配算法**：当前设计使用关键词匹配（skill 的 `when_to_use` vs 最近消息文本）。是否需要引入 embedding-based 语义匹配？关键词匹配的准确率是否足够？

2. **提醒频率与模型疲劳**：过频繁的提醒可能导致模型忽视 `<system-reminder>`（"banner blindness"）。3 轮冷却是否合适？是否需要根据 skill 匹配置信度调整频率？

3. **与 Governance 的交互**：当 Governor 执行 microcompact 或 auto-compact 时，policy 注入的 `<system-reminder>` 是否应被视为 compactable？如果被清理掉，下一轮会重新注入，但当前轮会丢失。

4. **`criticalSystemReminder` 的通用化**：Claude Code 的 `criticalSystemReminder_EXPERIMENTAL` 是一个通用的 per-turn 行为约束机制。harness 是否需要类似的通用机制（不只是 skill），用于未来的其他行为约束？

5. **异步 prefetch 的必要性**：Claude Code 的 skill discovery 使用异步 prefetch 避免增加延迟。harness 的 skill_catalog 量级较小（通常 < 20 个 skills），关键词匹配的开销是否低到可以同步执行？
