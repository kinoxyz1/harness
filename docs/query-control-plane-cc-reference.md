# Query Control Plane：Claude Code 源码设计详解

> 本文通过 Claude Code 源码，详细分析其 Query Control Plane 的设计。
> 目标：理解一个生产级 Agent 系统如何管理"为什么继续循环"这个问题。

## 源码引用说明

**真实源码**：所有标注行号的代码片段来自 `Claude-Code-doc/src/query.ts`（逆工程文档仓库）。
行号和代码内容已经逐行验证，与原文一致。

**推断类型**：`Continue` 和 `Terminal` 的类型定义来自 `import type { Continue, Terminal } from './query/transitions.js'`
（`query.ts:104`），但 `transitions.ts` 文件未包含在文档仓库中。下文用 `query.ts` 中的 7 处赋值
和 10 处 return 语句唯一重建了这两个类型的结构——虽然文件不可见，但类型的形状没有歧义。

**补充文档**：`learn-claude-code/docs/{zh,en}/` 下的 `s00a-query-control-plane.md`、
`s00c-query-transition-model.md`、`s01-the-agent-loop.md` 提供了设计意图的教学解读，
与 `query.ts` 的实现一致。

---

## 一、核心数据结构

### 1.1 State — 循环内可变状态

源码位置：`query.ts:204`

```typescript
type State = {
  messages: Message[]
  toolUseContext: ToolUseContext
  autoCompactTracking: AutoCompactTrackingState | undefined
  maxOutputTokensRecoveryCount: number
  hasAttemptedReactiveCompact: boolean
  maxOutputTokensOverride: number | undefined
  pendingToolUseSummary: Promise<ToolUseSummaryMessage | null> | undefined
  stopHookActive: boolean | undefined
  turnCount: number
  // Why the previous iteration continued. Undefined on first iteration.
  // Lets tests assert recovery paths fired without inspecting message contents.
  transition: Continue | undefined
}
```

**关键注释**：`transition` 的注释明确说了两件事：
1. 记录上一轮为什么继续（首次迭代为 undefined）
2. 让测试可以断言恢复路径是否触发，不需要检查消息内容

### 1.2 Continue — 继续原因的类型

`query.ts:104` 通过 `import type { Continue, Terminal } from './query/transitions.js'` 导入。
`transitions.ts` 未包含在文档仓库中，但类型结构可从 `query.ts` 中的 7 处赋值唯一确定（无歧义）：

```typescript
// 从 query.ts 的 7 处 transition 赋值唯一重建
// query.ts:1725  →  transition: { reason: 'next_turn' }
// query.ts:1110  →  transition: { reason: 'collapse_drain_retry', committed: drained.committed }
// query.ts:1162  →  transition: { reason: 'reactive_compact_retry' }
// query.ts:1217  →  transition: { reason: 'max_output_tokens_escalate' }
// query.ts:1246  →  transition: { reason: 'max_output_tokens_recovery', attempt: number }
// query.ts:1302  →  transition: { reason: 'stop_hook_blocking' }
// query.ts:1338  →  transition: { reason: 'token_budget_continuation' }
type Continue =
  | { reason: 'next_turn' }
  | { reason: 'collapse_drain_retry', committed: number }
  | { reason: 'reactive_compact_retry' }
  | { reason: 'max_output_tokens_escalate' }
  | { reason: 'max_output_tokens_recovery', attempt: number }
  | { reason: 'stop_hook_blocking' }
  | { reason: 'token_budget_continuation' }
```

### 1.3 Terminal — 终止原因的类型

同样从 `transitions.js` 导入，从 `query.ts` 中的 10 处 return 语句唯一确定：

```typescript
// 从 query.ts 的 10 处 return { reason: ... } 唯一重建
// query.ts:646   →  return { reason: 'blocking_limit' }
// query.ts:977   →  return { reason: 'image_error' }
// query.ts:996   →  return { reason: 'model_error', error }
// query.ts:1051  →  return { reason: 'aborted_streaming' }
// query.ts:1175  →  return { reason: 'prompt_too_long' }
// query.ts:1182  →  return { reason: 'prompt_too_long' }
// query.ts:1264  →  return { reason: 'completed' }
// query.ts:1279  →  return { reason: 'stop_hook_prevented' }
// query.ts:1357  →  return { reason: 'completed' }
// query.ts:1515  →  return { reason: 'aborted_tools' }
// query.ts:1520  →  return { reason: 'hook_stopped' }
// query.ts:1711  →  return { reason: 'max_turns', turnCount: nextTurnCount }
type Terminal =
  | { reason: 'completed' }
  | { reason: 'blocking_limit' }
  | { reason: 'aborted_streaming' }
  | { reason: 'aborted_tools' }
  | { reason: 'prompt_too_long' }
  | { reason: 'image_error' }
  | { reason: 'model_error', error: unknown }
  | { reason: 'stop_hook_prevented' }
  | { reason: 'hook_stopped' }
  | { reason: 'max_turns', turnCount: number }
```

---

## 二、初始化：哪些字段为空，哪些有值

源码位置：`query.ts:268`

```typescript
let state: State = {
  messages: params.messages,
  toolUseContext: params.toolUseContext,
  maxOutputTokensOverride: params.maxOutputTokensOverride,
  autoCompactTracking: undefined,          // 没有压缩跟踪
  stopHookActive: undefined,               // 没有激活的 stop hook
  maxOutputTokensRecoveryCount: 0,         // 截断恢复计数清零
  hasAttemptedReactiveCompact: false,      // 没有做过压缩
  turnCount: 1,                            // 从 1 开始
  pendingToolUseSummary: undefined,
  transition: undefined,                   // 首次，没有上一轮的原因
}
```

**设计要点**：
- `transition` 初始为 `undefined`，因为第一轮没有"上一轮"
- 恢复相关的计数器全部归零/false
- `messages` 和 `toolUseContext` 从外部参数传入（不可变）

---

## 三、每次迭代的解构

源码位置：`query.ts:307-321`

```typescript
while (true) {
  let { toolUseContext } = state
  const {
    messages,
    autoCompactTracking,
    maxOutputTokensRecoveryCount,
    hasAttemptedReactiveCompact,
    maxOutputTokensOverride,
    pendingToolUseSummary,
    stopHookActive,
    turnCount,
  } = state
  // ...
```

**设计要点**：
- 每次迭代开头解构 state，让后续代码用裸名（`messages` 而不是 `state.messages`）
- 只有 `toolUseContext` 可以在迭代内被重新赋值（queryTracking 更新）
- 其他字段在 continue 之前只读

---

## 四、7 个 Continue 站点详解

### 4.1 `next_turn` — 正常工具调用继续

源码位置：`query.ts:1715`

```typescript
const next: State = {
  messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
  toolUseContext: toolUseContextWithQueryTracking,
  autoCompactTracking: tracking,
  turnCount: nextTurnCount,
  maxOutputTokensRecoveryCount: 0,           // ← 重置
  hasAttemptedReactiveCompact: false,         // ← 重置
  pendingToolUseSummary: nextPendingToolUseSummary,
  maxOutputTokensOverride: undefined,         // ← 重置
  stopHookActive,
  transition: { reason: 'next_turn' },
}
state = next
```

**状态保留/重置策略**：
| 字段 | 操作 | 原因 |
|------|------|------|
| `maxOutputTokensRecoveryCount` | **重置为 0** | 正常工具调用成功，截断恢复已无关 |
| `hasAttemptedReactiveCompact` | **重置为 false** | 正常轮次，不需要保留压缩标记 |
| `maxOutputTokensOverride` | **重置为 undefined** | 正常轮次不需要输出 token 覆盖 |
| `autoCompactTracking` | 保留 `tracking` | 跟踪连续失败次数，跨轮累积 |
| `stopHookActive` | 保留 | stop hook 状态跨轮持续 |

**这是最重要的 continue 站点——它定义了"正常轮次"的基线，其他站点与它对比就能看出差异。**

---

### 4.2 `collapse_drain_retry` — 轻量级上下文折叠恢复

源码位置：`query.ts:1085-1117`

触发条件：API 返回 413 (prompt too long)，且上一轮不是 `collapse_drain_retry`

```typescript
if (isWithheld413) {
  // 关键防御：检查上一轮的 transition 防止无限循环
  if (
    feature('CONTEXT_COLLAPSE') &&
    contextCollapse &&
    state.transition?.reason !== 'collapse_drain_retry'  // ← 防止循环
  ) {
    const drained = contextCollapse.recoverFromOverflow(
      messagesForQuery,
      querySource,
    )
    if (drained.committed > 0) {
      const next: State = {
        messages: drained.messages,
        toolUseContext,
        autoCompactTracking: tracking,
        maxOutputTokensRecoveryCount,        // ← 保留
        hasAttemptedReactiveCompact,          // ← 保留
        maxOutputTokensOverride: undefined,   // ← 重置
        pendingToolUseSummary: undefined,
        stopHookActive: undefined,
        turnCount,
        transition: {
          reason: 'collapse_drain_retry',
          committed: drained.committed,        // ← 附加数据：折叠了多少
        },
      }
      state = next
      continue
    }
  }
}
```

**防循环机制**：
```
第一轮: transition=undefined → 尝试 collapse drain → committed=5 → continue
第二轮: transition=collapse_drain_retry → 跳过 collapse drain → 走 reactive compact
```

如果第二轮还是 413，`state.transition?.reason !== 'collapse_drain_retry'` 为 false，
直接跳过轻量级恢复，进入重量级恢复（reactive_compact）。

---

### 4.3 `reactive_compact_retry` — 重量级上下文压缩恢复

源码位置：`query.ts:1119-1166`

触发条件：413 且 collapse drain 无效或不可用

```typescript
if ((isWithheld413 || isWithheldMedia) && reactiveCompact) {
  const compacted = await reactiveCompact.tryReactiveCompact({
    hasAttempted: hasAttemptedReactiveCompact,  // ← 传入标记，防止重复压缩
    // ...
  })

  if (compacted) {
    const next: State = {
      messages: postCompactMessages,
      toolUseContext,
      autoCompactTracking: undefined,            // ← 重置（新的开始）
      maxOutputTokensRecoveryCount,              // ← 保留
      hasAttemptedReactiveCompact: true,          // ← 设为 true！关键！
      maxOutputTokensOverride: undefined,
      pendingToolUseSummary: undefined,
      stopHookActive: undefined,
      turnCount,
      transition: { reason: 'reactive_compact_retry' },
    }
    state = next
    continue
  }

  // 恢复失败 → 终止循环
  return { reason: isWithheldMedia ? 'image_error' : 'prompt_too_long' }
}
```

**`hasAttemptedReactiveCompact` 的防循环作用**：

注释原文（`query.ts:1296`）：
```
// Preserve the reactive compact guard — if compact already ran and
// couldn't recover from prompt-too-long, retrying after a stop-hook
// blocking error will produce the same result. Resetting to false
// here caused an infinite loop: compact → still too long → error →
// stop hook blocking → compact → … burning thousands of API calls.
```

翻译：如果压缩已经试过且没能解决 413，stop hook blocking 之后重试只会产生同样的结果。
把 `hasAttemptedReactiveCompact` 重置为 false 会导致无限循环：
**compact → 还是太长 → error → stop hook blocking → compact → ... 消耗数千次 API 调用。**

---

### 4.4 `max_output_tokens_escalate` — 输出截断，提升 token 上限

源码位置：`query.ts:1188-1220`

触发条件：输出被 max_tokens 截断，且当前使用默认 8k 上限

```typescript
if (isWithheldMaxOutputTokens(lastMessage)) {
  const capEnabled = getFeatureValue_CACHED_MAY_BE_STALE('tengu_otk_slot_v1', false)
  if (
    capEnabled &&
    maxOutputTokensOverride === undefined &&  // ← 只触发一次
    !process.env.CLAUDE_CODE_MAX_OUTPUT_TOKENS
  ) {
    const next: State = {
      messages: messagesForQuery,              // ← 相同消息，不注入恢复提示
      toolUseContext,
      autoCompactTracking: tracking,
      maxOutputTokensRecoveryCount,
      hasAttemptedReactiveCompact,
      maxOutputTokensOverride: ESCALATED_MAX_TOKENS,  // ← 64000！
      pendingToolUseSummary: undefined,
      stopHookActive: undefined,
      turnCount,
      transition: { reason: 'max_output_tokens_escalate' },
    }
    state = next
    continue
  }
```

**两阶段截断恢复策略**：
1. 阶段一（escalate）：同一条消息，把输出上限从 8k 提到 64k，不注入任何 meta 消息
2. 阶段二（recovery）：如果 64k 还不够，注入恢复消息让模型续写

`maxOutputTokensOverride === undefined` 是防循环守卫——只在默认上限时触发一次。

---

### 4.5 `max_output_tokens_recovery` — 输出截断，注入恢复消息

源码位置：`query.ts:1223-1252`

触发条件：escalate 后仍然截断，或 escalate 不可用

```typescript
if (maxOutputTokensRecoveryCount < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT) {  // 最多 3 次
  const recoveryMessage = createUserMessage({
    content:
      `Output token limit hit. Resume directly — no apology, no recap of what you were doing. ` +
      `Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces.`,
    isMeta: true,
  })

  const next: State = {
    messages: [...messagesForQuery, ...assistantMessages, recoveryMessage],
    toolUseContext,
    autoCompactTracking: tracking,
    maxOutputTokensRecoveryCount: maxOutputTokensRecoveryCount + 1,  // ← 递增，不重置
    hasAttemptedReactiveCompact,
    maxOutputTokensOverride: undefined,
    pendingToolUseSummary: undefined,
    stopHookActive: undefined,
    turnCount,
    transition: {
      reason: 'max_output_tokens_recovery',
      attempt: maxOutputTokensRecoveryCount + 1,  // ← 附加数据：第几次尝试
    },
  }
  state = next
  continue
}

// Recovery exhausted — surface the withheld error now.
yield lastMessage
```

**恢复消息的设计智慧**：
- "no apology, no recap" — 防止模型浪费 token 道歉和回顾
- "Pick up mid-thought" — 告诉模型从截断处继续
- "Break remaining work into smaller pieces" — 防止再次截断

**防循环机制**：`MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3`，最多恢复 3 次。

---

### 4.6 `stop_hook_blocking` — Stop Hook 阻止终止

源码位置：`query.ts:1267-1306`

触发条件：模型正常完成回复，但 stop hook 返回了 blocking errors

```typescript
const stopHookResult = yield* handleStopHooks(
  messagesForQuery, assistantMessages, systemPrompt,
  userContext, systemContext, toolUseContext,
  querySource, stopHookActive,
)

if (stopHookResult.preventContinuation) {
  return { reason: 'stop_hook_prevented' }
}

if (stopHookResult.blockingErrors.length > 0) {
  const next: State = {
    messages: [
      ...messagesForQuery,
      ...assistantMessages,
      ...stopHookResult.blockingErrors,   // ← 注入 hook 返回的错误
    ],
    toolUseContext,
    autoCompactTracking: tracking,
    maxOutputTokensRecoveryCount: 0,       // ← 重置（新的开始）
    hasAttemptedReactiveCompact,           // ← 保留！不重置！
    maxOutputTokensOverride: undefined,
    pendingToolUseSummary: undefined,
    stopHookActive: true,                  // ← 设为 true
    turnCount,
    transition: { reason: 'stop_hook_blocking' },
  }
  state = next
  continue
}
```

**`hasAttemptedReactiveCompact` 不重置的原因**：

这就是源码注释说的"death spiral"场景。如果 stop hook 在压缩失败后触发，
重置 `hasAttemptedReactiveCompact` 会导致系统认为"没试过压缩"，再跑一次
注定失败的压缩。保留这个标记，让系统知道"压缩已经试过了，别再试了"。

---

### 4.7 `token_budget_continuation` — Token 预算未用完，继续工作

源码位置：`query.ts:1308-1341`

触发条件：Token Budget 功能开启，且当前消耗 < 预算的 90%

```typescript
if (feature('TOKEN_BUDGET')) {
  const decision = checkTokenBudget(
    budgetTracker!,
    toolUseContext.agentId,
    getCurrentTurnTokenBudget(),
    getTurnOutputTokens(),
  )

  if (decision.action === 'continue') {
    state = {
      messages: [
        ...messagesForQuery,
        ...assistantMessages,
        createUserMessage({
          content: decision.nudgeMessage,  // "Stopped at 45% of token target. Keep working."
          isMeta: true,
        }),
      ],
      toolUseContext,
      autoCompactTracking: tracking,
      maxOutputTokensRecoveryCount: 0,      // ← 重置
      hasAttemptedReactiveCompact: false,    // ← 重置
      maxOutputTokensOverride: undefined,
      pendingToolUseSummary: undefined,
      stopHookActive: undefined,
      turnCount,
      transition: { reason: 'token_budget_continuation' },
    }
    continue
  }
}
```

**收益递减检测**（`tokenBudget.ts:59`）：

```typescript
const isDiminishing =
  tracker.continuationCount >= 3 &&
  deltaSinceLastCheck < DIMINISHING_THRESHOLD &&    // 500 tokens
  tracker.lastDeltaTokens < DIMINISHING_THRESHOLD   // 500 tokens
```

连续 3 次续写，且最近两次的 token 增量都 < 500，判定为收益递减，停止继续。

---

## 五、10 个 Terminal 站点

| reason | 行号 | 触发条件 |
|--------|------|---------|
| `blocking_limit` | 646 | Token 数达到硬性上限，且 auto-compact 被禁用 |
| `image_error` | 977 | 图片尺寸错误，无法恢复 |
| `model_error` | 996 | API 调用抛出不可恢复的异常 |
| `aborted_streaming` | 1051 | 用户在模型流式输出期间按 Ctrl+C |
| `prompt_too_long` | 1175, 1182 | 413 且 collapse drain 和 reactive compact 都失败 |
| `completed` | 1264, 1357 | 正常完成（模型回复文本，stop hook 通过） |
| `stop_hook_prevented` | 1279 | Stop hook 显式设置 `preventContinuation: true` |
| `aborted_tools` | 1515 | 用户在工具执行期间按 Ctrl+C |
| `hook_stopped` | 1520 | 工具执行 hook 设置了 `hook_stopped_continuation` |
| `max_turns` | 1711 | 工具调用轮次达到上限 |

---

## 六、Withholding 模式 — 错误恢复的核心机制

这是 CC Query Control Plane 中最容易被忽略但最关键的设计。

### 6.1 什么是 Withholding

正常情况下，API 返回的错误消息会立即 yield 给消费者（终端/SDK）。
但在某些场景下，CC 会**拦截（withhold）错误消息，先尝试恢复，恢复成功就不让消费者看到这个错误**。

### 6.2 哪些错误会被 Withhold

源码位置：`query.ts` 流式循环中的 withheld 检查

| 错误类型 | 恢复方式 | transition |
|---------|---------|------------|
| 413 prompt-too-long | collapse drain → reactive compact | `collapse_drain_retry` / `reactive_compact_retry` |
| media-size error | reactive compact (strip-retry) | `reactive_compact_retry` |
| max_output_tokens | escalate → recovery message | `max_output_tokens_escalate` / `max_output_tokens_recovery` |

### 6.3 Withhold 的流程

```
API 返回错误
  → 流式循环 withhold（不 yield）
  → 推入 assistantMessages（供后续检查）
  → needsFollowUp = false（不是工具调用）
  → 进入恢复分支
    → 恢复成功 → state.transition = xxx → continue（消费者不知道出过错）
    → 恢复失败 → yield lastMessage（消费者才看到错误）→ return Terminal
```

### 6.4 为什么需要 Withhold

如果不 withhold，消费者（终端用户）会先看到一条 "prompt too long" 错误，
然后系统自动恢复后继续工作。用户体验是断裂的——"出错了？没出错？到底怎么了？"

Withhold 让恢复过程对消费者透明：要么成功恢复（消费者无感知），要么彻底失败（消费者只看到最终错误）。

---

## 七、整体赋值 vs 逐字段修改

CC 使用 `state = { ... }` 整体赋值，而不是 `state.turnCount += 1` 这样的逐字段修改。

**好处**：
1. **原子性**：每次 continue 的状态变更是原子的，不会出现"改了 A 忘了改 B"
2. **可追溯**：git diff 可以看到每次 continue 的完整状态快照
3. **防遗漏**：State 有 10 个字段，整体赋值强制你考虑每个字段
4. **测试友好**：测试可以断言 transition.reason，不需要检查消息内容

**对比 Harness 的方式**：
```python
# Harness：逐字段修改
state.turn_count += 1
state.tool_calls_executed += len(parsed_calls)
state.files_modified.extend(batch.files_modified)
```

这种方式的问题是：当你加一个新字段时，很容易忘记在某个 continue 站点处理它。

---

## 八、防循环机制总结

CC 的 Query Control Plane 中至少有 5 层防循环机制：

| 机制 | 位置 | 防止什么 |
|------|------|---------|
| `transition !== 'collapse_drain_retry'` | query.ts:1092 | collapse drain 无限重试 |
| `hasAttemptedReactiveCompact` | query.ts:1121, 1297 | reactive compact 无限重试 |
| `maxOutputTokensRecoveryCount < 3` | query.ts:1223 | 截断恢复无限重试 |
| `maxOutputTokensOverride === undefined` | query.ts:1201 | escalate 无限重试 |
| `isDiminishing` 检测 | tokenBudget.ts:59 | token budget 无限续写 |

这些机制的共同模式：**通过检查 state 中记录的历史，判断"这个恢复路径是否已经尝试过了"**。

没有 transition，这些检查都做不到。这就是 transition 的核心价值——**让系统知道"之前发生了什么"，从而避免"再次做同样的事"。**

---

## 九、状态保留/重置决策矩阵

每个 continue 站点对 state 字段的处理策略：

| 字段 | next_turn | collapse_drain | reactive_compact | max_otk_escalate | max_otk_recovery | stop_hook_blocking | token_budget |
|------|-----------|----------------|------------------|------------------|------------------|-------------------|-------------|
| `maxOutputTokensRecoveryCount` | **0** | 保留 | 保留 | 保留 | **+1** | **0** | **0** |
| `hasAttemptedReactiveCompact` | **false** | 保留 | **true** | 保留 | 保留 | **保留** | **false** |
| `maxOutputTokensOverride` | **undef** | **undef** | **undef** | **64000** | **undef** | **undef** | **undef** |
| `stopHookActive` | 保留 | **undef** | **undef** | **undef** | **undef** | **true** | **undef** |
| `autoCompactTracking` | tracking | tracking | **undef** | tracking | tracking | tracking | tracking |
| `turnCount` | +1 | 不变 | 不变 | 不变 | 不变 | 不变 | 不变 |

**规律**：
- `next_turn`（正常路径）会**重置所有恢复标记**
- 恢复路径之间**保留彼此的状态**（压缩标记在截断恢复中保留，反之亦然）
- 只有成功回到正常路径时，恢复状态才被清除

这个矩阵是理解 CC control plane 的钥匙：**每个 continue 站点精确控制"哪些过去的记忆要保留，哪些要清除"。**

---

## 十、与 Harness 的差距

| 维度 | Harness | Claude Code |
|------|---------|-------------|
| 循环状态记录 | `RunState` 有计数器，无 transition | `State` 有 `transition` 字段 |
| continue 原因 | 隐式（4 处 continue 无标记） | 显式（7 种 reason + 附加数据） |
| 防循环 | 仅 `stop_reason == "max_turns"` | 5 层机制（transition + 标记 + 计数器 + 阈值 + 收益递减） |
| 恢复后状态管理 | 无区分 | 精确控制每个字段的保留/重置 |
| 截断恢复 | 注入一条 user 消息 | 两阶段：escalate 64k → 恢复消息（最多 3 次） |
| 压缩恢复 | 不存在 | 两级：collapse drain → reactive compact |
| 错误拦截（withhold） | 不存在 | 对 413 / media / max_tokens 做拦截恢复 |
| 赋值方式 | 逐字段修改 | 整体赋值 `state = { ... }` |
| stop hook | 不存在 | 支持 blocking errors 和 preventContinuation |
| token budget | 不存在 | 收益递减检测 + nudge 续写 |