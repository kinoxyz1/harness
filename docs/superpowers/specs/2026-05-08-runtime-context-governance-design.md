# Runtime Context Governance System 设计文档

> 日期: 2026-05-08
> 状态: Draft
> 范围: 上下文管理全生命周期重新设计

---

## 1. 背景与动机

### 1.1 当前系统的问题

harness 项目当前的上下文管理基于"截断历史消息"的思路：当上下文接近窗口上限时，通过 `tool_result_budget`（单条工具输出替换为占位符）、`microcompact`（按时间清理旧工具结果）和 `summary_compact`（调 LLM 生成摘要替换历史）三层策略来压缩上下文。

这套方案存在三个根本性缺陷：

**缺陷一：治理对象错位。** 当前系统盯着"对话历史"做文章，但真正吃掉上下文的不是用户和 assistant 之间的对话文字，而是工具输出。一次 `web_fetch` 抓到 200KB 的 HTML 原文、一次 `bash` 打印几十万行日志、一次 `read_file` 读取多个大文件——这些才是上下文暴涨的根源。对话文字在上下文中占比很小，却成了被优先裁剪的对象。

**缺陷二：被动救火，没有水位体系。** 当前系统只在接近上限时触发压缩（`should_trigger_summary_compact` 只有一个阈值），没有分层的水位线设计。这意味着：
- 无法在上下文压力较轻时做预防性清理
- 无法根据压力程度选择不同力度的策略
- 所有情况都走向最重的策略（summary compact），浪费 LLM 调用

**缺陷三：暴力截断导致信息丢失。** 当 summary compact 触发时，系统用一段 LLM 生成的摘要替换大量历史消息。但历史消息中往往保留了"为什么这么做"的关键背景——文件路径、错误堆栈、用户的具体要求、已经尝试过的方案。这些信息一旦被摘要替代，后续决策就失去了依据，导致任务目标漂移。

### 1.2 三种典型的上下文崩溃模式

理解为什么当前做法容易翻车，要看三种崩溃场景：

1. **直接爆窗（Prompt Too Long）**：某一轮工具返回了超大输出，下一轮直接发不出去，打断交互。当前系统的 `tool_result_budget` 尝试在事后替换大工具结果，但如果替换决策被跳过或时机不对（比如 reactive recover 路径），仍然会爆。

2. **暴力截断**：系统直接删除最早的消息来腾空间。但最早的消息里往往保留了用户的核心需求和项目背景。一旦删除，后续所有决策都会失去依据。

3. **持续失真**：每次用摘要替换历史，细节一轮轮丢失，像传话游戏。文件路径变了、错误信息被泛化了、用户的具体约束被简化了。几轮之后，任务目标开始漂移。

### 1.3 设计目标

这套 Runtime 治理系统的核心目标不是"如何压缩上下文"，而是**让系统在上下文持续增长的同时，仍然能维持工作的连续性**。

具体来说，要解决四个判断：

- **什么时候压**：基于水位线体系，不同压力级别触发不同策略
- **先压什么**：优先压缩低密度、可回放的工具输出，保护对话区域和 `read_file` 这类 working context
- **哪些绝对不能丢**：用户的核心需求、当前任务的关键文件路径、已做出的决策依据
- **压完怎么无缝继续**：通过本地文件 offloading，让被压缩的内容仍然可达（模型可以主动 read 回来）

### 1.4 版本范围

为了避免“当前要做什么”和“后面想做什么”混在一起，本文把范围明确分成两层：

**V1（本文当前实现范围）**

V1 的目标是先把 Runtime Governor 跑稳，解决“上下文突然冲高时不要直接爆窗”的问题。它只包含：

- 执行时 offload
- per-message tool result budget
- PreviewStrip
- Microcompact
- Auto-compact
- blocking gate
- `read_file` working set 保护与 compact 后最近文件恢复

**V2（本文补充设计范围）**

V2 不再只追求“能压下来”，而是追求“尽量少动 transcript、尽量少打断 cache、尽量少直接进入全量摘要”。它只包含三项中层治理能力：

- Context Collapse：把旧上下文渐进式归档，而不是一上来就整体摘要替换
- cache-aware microcompact：优先通过 cache-editing 路径释放压力，减少对 transcript 的破坏
- session memory compact：优先使用本地会话记忆替代 LLM 摘要，作为 Auto-compact 前的一层重策略

**明确不在 V1 / V2 范围内**

- artifact-aware runtime：后续单独演进。当前 V1/V2 只先打好“本地 artifact 可落盘、可引用、可回放”的基础
- 外部记忆：后续单独演进。当前 V2 只处理单会话内的本地归档与会话记忆，不做跨会话记忆检索
- 中断恢复：放在更后阶段。它依赖更稳定的本地 artifact、归档日志和状态重建能力，不与当前 V2 一起落地

---

## 2. Claude Code 源码研究

Claude Code 是目前最成熟的 Agent 系统之一，它的上下文管理经过大量实际会话的验证。我们详细分析了其核心压缩逻辑，作为 harness 设计的参考。

### 2.1 源码结构

Claude Code 的压缩逻辑主要在 `src/services/compact/` 目录下，涉及六个核心模块：

| 模块 | 职责 |
|---|---|
| `autoCompact.ts` | 顶层决策：计算阈值、判断是否需要压缩、编排策略 |
| `microCompact.ts` | 轻量清理：基于时间或缓存策略清理旧工具结果 |
| `sessionMemoryCompact.ts` | 基于本地会话记忆的压缩，避免 API 调用 |
| `compact.ts` | 全量摘要压缩：调 LLM 生成结构化摘要 |
| `prompt.ts` | 压缩提示词模板（9 段式摘要格式） |
| `src/utils/toolResultStorage.ts` | 工具结果的持久化与预算管理 |

辅助模块：`tokenEstimation.ts`（token 估算）、`compactWarningState.ts`（警告状态管理）、`timeBasedMCConfig.ts`（GrowthBook 配置）。

### 2.2 阈值体系

Claude Code 定义了一个清晰的水位线体系（`autoCompact.ts` 第 28-70 行）：

```
effectiveContextWindow = contextWindowForModel - min(maxOutputTokens, 20000)
autocompactThreshold   = effectiveContextWindow - 13,000
warningThreshold       = autocompactThreshold - 20,000
errorThreshold         = autocompactThreshold - 20,000
blockingLimit          = effectiveContextWindow - 3,000
```

以 200k 上下文窗口为例（实际有效窗口约 192k）：

| 水位线 | 触发值 | 含义 |
|---|---|---|
| `warningThreshold` | ~159k tokens | 提醒用户"上下文快不够了" |
| `autocompactThreshold` | ~179k tokens | 触发自动压缩 |
| `blockingLimit` | ~189k tokens | 硬门，直接拒绝发请求 |

关键设计决策：

- `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20,000`：基于 p99.99 的观测数据（实际摘要最大 17,387 tokens），为摘要输出预留足够空间。
- `AUTOCOMPACT_BUFFER_TOKENS = 13,000`：在上下文完全用尽之前留出缓冲区。
- `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`：熔断器，连续失败 3 次后停止重试。

### 2.3 工具结果处理流程

Claude Code 的工具结果处理是**四阶段流水线**，这是最值得我们学习的部分：

**阶段 1 — 执行时即时 offload（`toolResultStorage.ts`）**

每个工具声明 `maxResultSizeChars` 属性（定义在 `Tool.ts` 第 466 行），系统通过 `getPersistenceThreshold()` 确定有效限制——默认为 `Math.min(declaredMaxResultSizeChars, 50_000)`（`toolLimits.ts` 第 13 行）。

如果结果超过阈值，`maybePersistLargeToolResult()`（第 272-334 行）将完整内容写入磁盘：
```
~/.claude/projects/<sanitized-cwd>/<sessionId>/tool-results/<toolUseId>.txt
```

上下文中替换为 `<persisted-output>` 标签（第 30 行定义）：
```
<persisted-output>
Output too large (234KB). Full output saved to: /path/to/tool-results/toolu_a1b2.txt

Preview (first 2KB):
...前 2000 字节的预览...
</persisted-output>
```

预览通过 `generatePreview()`（第 339-356 行）生成，在 `PREVIEW_SIZE_BYTES = 2000`（第 109 行）内按行截断，确保不会在行中间断开。

**Bash 的额外限制**（`outputLimits.ts`）：默认 30,000 字符，上限 150,000 字符。

**Read 的例外处理**：Claude Code 明确把 `Read` 设为 `maxResultSizeChars = Infinity`，不走通用“大结果落盘”路径。原因不是 `Read` 不大，而是它被当作当前推理所需的工作材料；同时 `Read` 自身有 `maxTokens` 上限、重复读取去重、`readFileState` 缓存，以及 compact 后的最近文件恢复机制。

**阶段 2 — 聚合预算（`enforceToolResultBudget`）**

`enforceToolResultBudget()`（第 769-909 行）在同一轮多个工具执行完后，按 API 消息分组执行聚合预算。每组的工具结果总和不超过 `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000`（`toolLimits.ts` 第 49 行）。超限时将最大的结果继续 offload 到磁盘。

关键状态管理：`ContentReplacementState`（第 390-397 行）追踪所有决策：
```typescript
type ContentReplacementState = {
  seenIds: Set<string>           // 已决策过的 tool_use_id
  replacements: Map<string, string>  // tool_use_id → 替换字符串
}
```
一旦某个工具结果被 offload，其决策被永久冻结（`seenIds`），后续轮次通过 `replacements` Map 直接 re-apply，不重复判断。Session 恢复时从 transcript 的 `ContentReplacementRecord` 条目重建状态。

**阶段 3 — History Snip（历史裁剪）**

实验特性（`HISTORY_SNIP` feature flag），整条删除旧消息，释放的 token 从 autocompact 阈值中扣除（避免双重计算）。

**阶段 4 — Microcompact → Context Collapse → Autocompact**

Microcompact（`microCompact.ts`）在每次 API 调用前运行，但不是单一路径：
- 冷缓存场景优先走 **time-based microcompact**，按时间差清理旧工具结果，替换为 `[Old tool result content cleared]`
- 热缓存且模型支持 cache editing 时，优先走 **cached microcompact**，通过 API 级 cache edits 删除旧工具结果，不直接改本地 transcript

可 compactable 的工具类型包括 Bash、Grep、Glob、Read、WebFetch、WebSearch、FileEdit、Write（第 41 行）。

Autocompact（`compact.ts`）调 LLM 生成 9 段式结构化摘要，保留最近消息，注入 runtime restore 消息恢复 todo 状态和 active skills。

**Read 的保护链路**：除了 transcript 里的 `Read` tool result，Claude Code 还维护一个独立的 `readFileState`。compact 时会先清空旧的 read cache，再按最近访问顺序把一部分文件重新注入 post-compact attachments。它保护的是“最近工作集”，不是“所有历史读取文件”。

### 2.4 决策流程

`shouldAutoCompact()`（第 160-239 行）的完整决策链：

1. **递归保护**：`session_memory` 和 `compact` query source 直接返回 false（防止压缩请求触发压缩的无限循环）
2. **特性门控**：检查 `REACTIVE_COMPACT`、`CONTEXT_COLLAPSE` 等 feature flag
3. **水位判断**：`tokenCountWithEstimation(messages) - snipTokensFreed` 是否超过阈值
4. **熔断器**：连续失败 3 次后停止重试

`autoCompactIfNeeded()`（第 241-351 行）的策略选择：
1. 优先尝试 `trySessionMemoryCompaction`（基于本地会话记忆，零 API 调用）
2. 失败则 fallback 到 `compactConversation`（完整 LLM 摘要）

### 2.5 关键参考结论

从 Claude Code 的设计中，我们得出以下参考结论：

1. **工具结果 offloading 在执行时就做**，不是水位触发后才做。这是最高效的防线——大输出从一开始就不进入上下文。

2. **替换状态必须冻结**。一旦决定 offload，后续轮次永远 re-apply 同样的替换，保证 cache 友好且行为一致。

3. **水位线是 buffer-based 而非 percentage-based**。用固定 token 数（13k、20k）而非百分比来定义水位线，这样在不同大小的上下文窗口下行为更可预测。

4. **分层策略是流水线，而不是互斥选择**：先跑工具结果预算，再跑 preview strip，再跑 microcompact，再尝试 context collapse，最后才决定是否需要 autocompact。

5. **可 compactable 的工具类型是白名单制的**。不是所有工具结果都可以随意清理，只有输出密度低、且对当前推理链路不敏感的工具才列入白名单。需要注意：Claude Code 的白名单里包含 `Read`，但 harness V1 在这里会有意偏离，把 `read_file` 从常规 microcompact 白名单中拿出来，改为通过 working set 保护和 compact 后恢复来治理。

6. **`Read` 不是普通大输出，而是带保护的 working context**。Claude Code 没有保证“读过几十个文件后永远不丢前文细节”，但它通过 `Infinity` 持久化豁免、自身 token 上限、重复读取去重、`readFileState` 和 compact 后最近文件恢复，尽量保住最近工作集。

---

## 3. 架构设计

### 3.1 总体架构

```
┌─────────────────────────────────────────────────────────┐
│                      QueryLoop                          │
│                                                         │
│  1. tool_runtime.execute_batch(...)                     │
│     → batch.messages                                    │
│                                                         │
│  2. offloader.maybe_persist(tool_result)  ← 执行时offload│
│     → 替换大工具结果为 <persisted-output>                │
│                                                         │
│  3. governor.assess(...)                  ← 水位评估     │
│     → 计算水位线                                        │
│     → 顺序执行策略 (Budget → PreviewStrip → Microcompact │
│       → Auto-compact)                                   │
│     → 返回 PreparedQueryContext                         │
│                                                         │
│  4. view_builder.build(prepared)          ← 最终组装    │
│     → ModelInputView                                    │
│                                                         │
│  5. model_gateway.call_once(...)          ← 发送请求    │
│     → 发送前如命中 blocking gate，先本地抢救             │
│     → 如果 prompt_too_long: governor.reactive_recover() │
└─────────────────────────────────────────────────────────┘
```

### 3.2 组件职责

#### ContextGovernor（`core/session/governor.py`）

上下文决策的唯一入口。持有水位线状态，每次查询前评估压力并选择策略。

职责：
- 计算当前水位（总 token 用量 + stable overhead）
- 根据水位线决定哪些治理步骤要执行
- 按固定流水线编排策略执行（Budget → PreviewStrip → Microcompact → Auto-compact）
- 管理熔断器（Auto-compact 连续失败后停止）
- 在发送前执行 blocking gate，避免把必然失败的超长请求发给模型
- 提供 `reactive_recover` 处理 `prompt_too_long` 紧急抢救
- 返回 `PreparedQueryContext`

不负责：
- 工具结果的文件写入（由 ToolResultOffloader 负责）
- 最终的 API 消息组装（由 MessageViewBuilder 负责）
- Stable system / tools / runtime blocks 的构建（由 PromptAssembler 负责）

#### ToolResultOffloader（`core/session/offloader.py`）

工具结果的 offloading 决策、文件写入、预览生成、替换状态管理。

两个入口：
- `maybe_persist(tool_use_id, content, tool_name)` — 工具执行完立即调用
- `enforce_per_message_budget(messages)` — 每次查询前调用，确保同轮多个工具结果总和不超过 200,000 字符

注意：`ToolResultOffloader` 只处理 **ephemeral outputs**。`read_file` 不走这里的通用大结果 offload 路径。

#### ContentReplacementState（`core/session/content_replacement.py`）

冻结的替换决策状态。挂载在 `SessionState` 上，跨轮次持久。

```python
@dataclass
class ContentReplacementState:
    seen_ids: set[str]           # 已决策过的 tool_use_id（永久冻结）
    replacements: dict[str, str] # tool_use_id → <persisted-output> 字符串
```

#### Working Context（`read_file_state`）

`read_file` 不应被建模为“普通 tool result”，而应被建模为 **working context**。它代表模型当前为了做跨文件推理而读过的材料。

一期设计沿用现有 `SessionState.read_file_state` 字段，但语义上提升为受保护的工作集：
- 记录最近读过的文件路径、范围、完整/局部读取状态、时间戳
- 为 compact 后恢复提供输入
- 不参与通用大结果 offload
- 不在常规 microcompact 中优先清理

#### 策略模块

- `core/session/preview_strip.py` — PreviewStrip 策略（压缩 `<persisted-output>` 预览）
- `core/session/microcompact.py` — Microcompact 策略（从 `compact_service.py` 迁出）
- `core/session/compact_service.py` — 精简为只保留 `summarize_and_compact` 和 `build_runtime_restore_messages`

### 3.3 水位线体系

基于 Claude Code 的阈值体系扩展，加入 PreviewStrip 和 pre-send blocking gate：

```python
# 常量定义
EFFECTIVE_CONTEXT_WINDOW = context_window_tokens - max_output_tokens
PREVIEW_STRIP_THRESHOLD = EFFECTIVE_CONTEXT_WINDOW - 40_000
MICROCOMPACT_THRESHOLD  = EFFECTIVE_CONTEXT_WINDOW - 20_000
AUTOCOMPACT_THRESHOLD   = EFFECTIVE_CONTEXT_WINDOW - 13_000
BLOCKING_LIMIT          = EFFECTIVE_CONTEXT_WINDOW - 3_000
```

以 200k 上下文窗口为例（有效窗口 ~190k）：

| 水位线 | 触发值 | 策略 | 力度 |
|---|---|---|---|
| `PREVIEW_STRIP_THRESHOLD` | ~150k | PreviewStrip | 压缩 `<persisted-output>` 标签，去掉预览只留路径 |
| `MICROCOMPACT_THRESHOLD` | ~170k | Microcompact | 清理旧工具结果 + 已 offload 结果深度压缩 |
| `AUTOCOMPACT_THRESHOLD` | ~177k | Auto-compact | LLM 摘要替换历史 |
| `BLOCKING_LIMIT` | ~187k | 阻断 | 发送前拒绝构建最终请求，必须先抢救 |

水位计算（必须在 `assess()` 内部计算 stable_overhead，不能缓存为实例变量，因为 stable_system / stable_tools 每轮可能不同）：

```python
def _calc_water_level(self, session_state, stable_overhead: int) -> int:
    estimated = estimate_messages_tokens(session_state.conversation_messages)
    used = calibrated_input_tokens(
        estimated=estimated,
        observed=session_state.compact_state["last_prompt_tokens"],
    )
    return used + stable_overhead

def _recalc_water_level(self, messages: list[dict], stable_overhead: int) -> int:
    """策略执行后重新估算水位。用于判断轻策略是否已足够，避免不必要的 auto_compact。"""
    estimated = estimate_messages_tokens(messages)
    used = calibrated_input_tokens(
        estimated=estimated,
        observed=0,  # 策略后没有新的 observed prompt_tokens，用估算值
    )
    return used + stable_overhead
```

**`_recalc_water_level` 为什么是必须的**：没有它，governor 只在策略执行前算一次水位。如果 preview_strip + microcompact 已经把水位降到安全区，governor 仍然会基于旧水位触发 auto_compact——白白做了一次昂贵的 LLM 摘要调用，且摘要本身会导致信息损失。

### 3.4 策略详细设计

#### 工具输出分类

为了避免把所有工具结果一视同仁地治理，一期把工具结果分成三类：

1. **Ephemeral outputs**
   例如 `bash`、`web_fetch`、`web_search`、大错误日志。
   这类结果优先进入 offload / preview strip / microcompact 流水线。

2. **Working context**
   主要是 `read_file`。
   这类结果是模型当前做推理的材料，不走通用 offload，常规 microcompact 也不应优先清理。

3. **Structured state**
   例如 `todo`、`skill`。
   这类结果短但语义强，不应被压缩成普通占位符。

#### PreviewStrip（轻量裁剪）

**触发条件**：水位 >= PREVIEW_STRIP_THRESHOLD

**做什么**：遍历 transcript 中所有包含 `<persisted-output>` 标签的工具结果消息，将其进一步压缩——去掉 preview 部分，只保留路径引用。

```
压缩前：
<persisted-output>
Output too large (234KB). Full output saved to: /path/to/tool-results/toolu_a1b2.txt

Preview (first 2KB):
...大量预览内容...
</persisted-output>

压缩后：
[Tool result offloaded to: /path/to/tool-results/toolu_a1b2.txt]
```

**释放量**：每条大约节省 1-2KB。一轮有 5-10 个工具调用时，积少成多。

**命名说明**：这里故意不用 `Snip` 命名。Claude Code 里的 `HISTORY_SNIP` 是删除旧消息组的历史裁剪机制，而这里做的是对已 offload 结果的预览瘦身，两者不是一回事，避免概念混用。

**关键约束**：
- 只处理已经被 offload 过的工具结果（有 `<persisted-output>` 标签的），不碰未 offload 的内容
- 不改变消息结构，只替换 content 字符串
- 替换状态同步更新（`ContentReplacementState.replacements`）

#### Microcompact（旧工具清理）

**触发条件**：满足 Microcompact 运行条件时执行；是否真正生效取决于缓存状态、query source 和工具类型。它不是简单的“高于某个水位才运行”的互斥策略，而是治理流水线中的一环。

**两种子策略**：

**策略 A — 时间清理**（从现有 `compact_service.apply_time_based_microcompact` 迁出）：
- 超过 `age_cutoff_seconds`（默认 1800s）的 compactable 工具结果
- 不在最近 `keep_recent_trajectories`（默认 2）轮中的
- 内容替换为 `[Old tool result content cleared]`
- 只在明确判断为冷缓存时触发；目的是在“反正要 cache miss”的前提下，缩小本轮真正要重写的 prompt

可 compactable 的工具白名单：`bash`, `find`, `grep`, `glob`, `web_fetch`, `web_search`, `write_file`。这些工具的输出天然具有"可重新获取"的特性——命令可以重新跑、搜索可以重新做、网页可以重新抓取、写入确认消息本身信息密度也较低。

`read_file` 不进入这个常规白名单。理由：
- 模型连续读取几十个文件时，前面读过的内容往往仍在当前推理链路里
- 如果把前面的 `read_file` 结果像日志一样清掉，容易出现“读了前二十个文件，再读后面的时忘了前面的具体内容”
- 一期设计里，`read_file` 应优先通过 working set 和 compact 后恢复机制来治理，而不是直接清占位符

不可 compactable 的工具：`todo`、`skill` 等结构化工具——它们的结果短且有结构意义，清理后会导致状态丢失。

**策略 B — Cache-aware microcompact**：
- 如果底层模型/API 支持 cache editing，则优先使用缓存友好的删除路径
- 这种路径不直接修改本地 transcript，而是向 API 注入 cache edits
- 优点是能释放上下文压力，同时尽量不打断 prompt cache

```python
def _run_microcompact(self, messages):
    if self._can_use_cached_microcompact(messages):
        return self._run_cached_microcompact(messages)
    messages = self._time_based_clear(messages)
    return messages
```

#### 用户意图保留（User Intent Preservation）

**这是 V1 的硬性要求，不是 V2 推迟项。**

设计目标 1.3 节明确提出”哪些绝对不能丢：用户的核心需求”。但在没有显式机制保障的情况下，auto_compact 会把用户原始指令压缩进摘要，而摘要质量不可控。一旦摘要丢失了关键细节（如”还要优化复杂度””最后要删除临时文件”），模型就会发生任务目标漂移。

**机制设计**：在 `SessionState` 中新增 `user_intents: list[str]` 字段，存储每个用户消息中需要保留的意图摘要。由 `build_runtime_restore_messages` 在 auto_compact 后作为 `user_intent_restore` 类型注入。

```python
# SessionState 新增
user_intents: list[str] = field(default_factory=list)
```

**意图提取时机**：每次用户消息进入系统时，将用户原始消息文本追加到 `user_intents`。不做 LLM 摘要，保留原文。

**注入规则**：
- 每次 `build_runtime_restore_messages` 被调用时，将 `user_intents` 渲染为 `user_intent_restore` 类型的 meta_runtime_restore 消息
- 如果 `user_intents` 总长度超过 2000 字符，只保留最近 5 条
- 注入位置在 summary 之后、kept messages 之前
- 即使 todo 为空、skill 为空、read_file_state 为空，这条消息也会存在，确保模型在 compact 后仍能看到用户的核心需求

```python
# runtime restore 新增的 user_intent_restore 块
if state.user_intents:
    intent_lines = state.user_intents[-5:]
    restored.append({
        “role”: “meta_runtime_restore”,
        “kind”: “user_intent_restore”,
        “content”: “用户原始需求（请严格遵守）：\n” + “\n”.join(f”- {intent}” for intent in intent_lines),
    })
```

**为什么不依赖 summary 中的 “Primary Request and Intent” 栏目**：summary 由 LLM 生成，质量不可控。在 token 压力大时，LLM 可能将”基于 csv 文件的完整数据生成分析报告”压缩为”用户要求分析数据”，丢失”完整数据””HTML 格式””保存到 ~/Downloads/a.html”等关键约束。显式保留原文是零成本高收益的保底。

#### Auto-compact（摘要压缩）

**触发条件**：水位 >= AUTOCOMPACT_THRESHOLD

**做什么**：和现有逻辑一致，但作为 Governor 的一个策略被调用：
1. 调 LLM 生成 9 段式结构化摘要（Primary Request / Technical Concepts / Files & Code / Errors / Problem Solving / User Messages / Pending Tasks / Current Work / Optional Next Step）
2. 用摘要替换旧消息，保留最近 N 条（`keep_last_messages` 见下方说明）
3. 注入 runtime restore 消息（user_intent_restore、todo state、active skills、最近读过的文件）
4. 带熔断器：3 次连续失败后停止，60s 冷却期

**`keep_last_messages` 取值依据**：

auto_compact 保留的尾部长度必须至少覆盖一轮完整的 tool-use cycle（assistant tool_calls → tool results → assistant response），这样模型才能看到”上一个动作做了什么、结果是什么”。

最小值为 6（2 轮 tool cycle = assistant + tool + assistant + tool + assistant + tool），推荐值为 8（留出 1 轮余量给 assistant 的纯文本回复）。低于 6 会导致模型在 compact 后只看到当前工具调用的输入，看不到上一步的输出，无法形成连续的推理链。

```python
AUTO_COMPACT_KEEP_LAST_MESSAGES = 8   # 正常路径
BLOCKING_GATE_KEEP_LAST_MESSAGES = 4  # 紧急路径（更激进但至少保留 1 轮完整 cycle）
```

对于 `read_file`，Auto-compact 不要求保留所有历史读取全文，而是保留”最近 working set 可继续工作”的最小闭环：
- 最近访问的少量文件
- 每个文件受单文件 token 上限约束
- 已在 preserved tail 中的读取结果不重复注入

#### Blocking Gate（发送前阻断）

**触发条件**：水位 >= BLOCKING_LIMIT

**做什么**：
1. 不直接发送模型请求
2. 先本地执行最激进的可逆治理步骤：PreviewStrip、Microcompact、必要时 Auto-compact
3. 重新估算水位
4. 只有降回 `BLOCKING_LIMIT` 以下，才允许真正调用模型

如果连续抢救后仍然无法降到安全范围，直接返回一条本地错误/状态消息，而不是把一个必然 `prompt_too_long` 的请求发出去。

**与现有代码的关系**：`_summarize_with_breaker`、`_mark_summary_failure`、`_mark_summary_success`、`_summary_breaker_open` 全部迁入 Governor，逻辑不变。

### 3.5 ToolResultOffloader 详细设计

#### 执行时 offload

工具执行完后，QueryLoop 调用 `offloader.maybe_persist()`：

```python
def maybe_persist(self, tool_use_id: str, content: str, tool_name: str) -> str:
    threshold = self._get_persistence_threshold(tool_name)
    if len(content) <= threshold:
        return content  # 不超限，原样返回

    filepath = self._tool_result_dir / f"{tool_use_id}.txt"
    filepath.write_text(content, encoding="utf-8")

    preview = content[:2000]
    last_newline = preview.rfind("\n")
    if last_newline > 0:
        preview = preview[:last_newline]
    has_more = len(content) > 2000

    size_str = self._human_readable_size(len(content))
    preview_size_str = self._human_readable_size(min(len(preview), 2000))

    replacement = (
        f"<persisted-output>\n"
        f"Output too large ({size_str}). Full output saved to: {filepath}\n\n"
        f"Preview (first {preview_size_str}):\n"
        f"{preview}{'...' if has_more else ''}\n"
        f"</persisted-output>"
    )

    # 更新冻结状态
    self._state.seen_ids.add(tool_use_id)
    self._state.replacements[tool_use_id] = replacement

    return replacement
```

每个工具的 offload 阈值：
- 默认：`DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000`（参考 Claude Code `toolLimits.ts` 第 13 行）
- `bash`：额外有 `BASH_MAX_OUTPUT_DEFAULT = 30_000` 的预执行限制
- `read_file`：`Infinity`，不走通用 offload，而是依赖自身读取 token 上限和 `read_file_state`
- `todo`、`skill` 等结构化工具：阈值为 `Infinity`（永远不 offload）

#### 聚合预算

每次查询前，Governor 调用 `offloader.enforce_per_message_budget()`：

```python
def enforce_per_message_budget(self, messages: list[dict]) -> list[dict]:
    budget = 200_000  # MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
    # 按 API 消息分组（同一轮的多个 tool_result 属于同一个 user message）
    # 对每组的 tool_result 按大小排序
    # 跳过 read_file / todo / skill 这类不应走通用替换的结果
    # 冻结已决策的（seen_ids 中的直接 re-apply）
    # 新的超限结果：offload 最大的几条直到总额在预算内
    return messages
```

`read_file` 在这里应被明确排除。否则同一轮读很多文件时，系统可能会把前面读过的文件结果替换成路径/占位符，破坏跨文件分析的连续性。

#### 冻结状态管理

`ContentReplacementState` 挂载在 `SessionState` 上：

```python
@dataclass
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)
```

- `seen_ids`：所有经过 offload 决策的 tool_use_id。一旦加入，永不移除。
- `replacements`：被 offload 的 tool_use_id 到替换字符串的映射。

每次 `enforce_per_message_budget` 运行时：
1. 先对 `seen_ids` 中的结果 re-apply 冻结的替换（保证一致性）
2. 再对新结果做 offload 决策

但真正的契约不止于此，还需要显式记录：
- **seen but unreplaced**：某些结果曾被看过，但当时没有替换；后续也不能再替换，否则会破坏历史前缀一致性
- **replacement records**：持久化的不是“重新推导规则”，而是模型实际见过的替换字符串
- **resume reconstruction**：会话恢复时，从 transcript 中重建 `seen_ids` 和 `replacements`
- **fork/subagent inheritance**：共享前缀缓存的子线程需要克隆父线程的 replacement state，而不是重新决策

建议 transcript 中增加显式记录：

```python
@dataclass
class ContentReplacementRecord:
    kind: Literal["tool-result"]
    tool_use_id: str
    replacement: str
```

#### 文件目录

```
<project_root>/.harness/sessions/<session_id>/tool-results/<toolUseId>.txt
```

按 session 分层。原因：
- 便于 resume / replay / 审计
- 便于并发会话隔离
- 便于按 session 做清理和 retention
- 避免单层目录长期积累垃圾文件

清理策略：30 天默认保留，不做主动清理。

### 3.5.1 `read_file` 的工作集治理

`read_file` 的治理目标不是“尽快压出去”，而是“在读很多文件时尽量保住最近工作集”。

一期采用最小策略：
- `read_file` 不走通用大结果 offload
- `read_file` 不进入常规 microcompact 清理白名单
- `SessionState.read_file_state` 继续维护最近读过的文件内容与范围
- Auto-compact 后，根据 `read_file_state` 重新注入最近访问的少量文件
- 如果 preserved tail 已经保留了某个文件的 Read 结果，则不重复恢复

这仍然不能保证“读过几十个文件后，一个字都不忘”，但它能避免最糟糕的情况：把 `read_file` 当作普通日志一样立即清空。

### 3.6 ContextGovernor 核心流程

```python
class ContextGovernor:
    def __init__(
        self,
        *,
        offloader: ToolResultOffloader,
        compact_service,          # 保留的 summarize_and_compact
        summary_gateway,
        context_window_tokens: int = 100_000,
        max_output_tokens: int = 10_000,
        summary_breaker_cooldown_seconds: float = 60.0,
        time_fn=time.monotonic,
    ) -> None:
        ...

    def assess(
        self,
        *,
        session_state,
        run_state,
        store,
        stable_system: str,
        stable_tools: list[dict] | None,
        runtime_blocks: list[ContextBlock],
        overlay_blocks: list[ContextBlock],
        query_source: str | None = None,
    ) -> PreparedQueryContext:
        messages = list(session_state.conversation_messages)

        # 计算 stable overhead
        stable_system_tokens = max(1, len(stable_system) // 4)
        stable_tools_tokens = 0 if not stable_tools else max(1, len(str(stable_tools)) // 4)
        required_runtime_tokens = sum(b.token_estimate for b in runtime_blocks if b.required)
        stable_overhead = stable_system_tokens + stable_tools_tokens + required_runtime_tokens

        # 计算水位（必须在 assess 内部计算 stable_overhead，不能缓存为实例变量）
        water_level = self._calc_water_level(session_state, stable_overhead)
        waterlines = calc_waterlines(
            context_window_tokens=self._context_window_tokens,
            max_output_tokens=self._max_output_tokens,
        )

        # 每次都跑：runtime blocks 裁剪
        kept_runtime_blocks = _prune_optional_runtime_blocks(
            list(runtime_blocks) + list(overlay_blocks),
            optional_budget=12_000,
        )

        # 每次都跑：工具结果聚合预算
        messages = self._offloader.enforce_per_message_budget(messages)

        # 初始化可观测性
        steps = ["estimate", "per_message_budget"]
        observability = {
            "water_level": water_level,
            "water_line": self._water_line_name(water_level, effective_window),
            "strategies_run": [],
            "before_tokens": water_level,
            "after_tokens": water_level,
            "steps": steps,
        }

        # 轻量策略按流水线叠加执行，不互斥
        if water_level >= waterlines["preview_strip"]:
            observability["strategies_run"].append("preview_strip")
            messages = self._run_preview_strip(messages, observability)

        messages = self._run_microcompact(messages, session_state, observability)
        if observability.get("microcompact_effective"):
            observability["strategies_run"].append("microcompact")

        # 重新估算，决定是否还需要重策略（关键：轻策略执行后必须重算水位）
        water_level = self._recalc_water_level(messages, stable_overhead)
        if water_level >= waterlines["auto_compact"]:
            observability["strategies_run"].append("auto_compact")
            messages = self._run_auto_compact(messages, session_state, store, observability)

        # 发送前 blocking gate
        water_level = self._recalc_water_level(messages, stable_overhead)
        if water_level >= waterlines["blocking"]:
            observability["strategies_run"].append("blocking_gate")
            messages = self._run_blocking_recover(messages, session_state, store, observability)

        # 更新可观测性
        observability["after_tokens"] = estimate_messages_tokens(messages)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability

        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=kept_runtime_blocks,
            working_transcript=messages,
            observability=observability,
            budget={
                "stable_system_tokens": stable_system_tokens,
                "stable_tools_tokens": stable_tools_tokens,
                "required_runtime_tokens": required_runtime_tokens,
            },
        )
```

策略执行采用固定流水线，而不是 `elif` 互斥分支：轻量治理步骤先尽量削峰，只有在仍然高压时才进入 Auto-compact 和 blocking gate。

### 3.7 Reactive Recover

处理 API 返回 `prompt_too_long` 时的紧急抢救。注意它是 **blocking gate 之后的兜底**，不是主要治理路径：

```python
def reactive_recover(self, *, session_state, run_state, store, **kwargs) -> PreparedQueryContext:
    # 紧急 Microcompact（最激进参数：所有旧结果都清理）
    messages = self._run_microcompact(messages, aggressive=True)

    # 如果还是超，跑 Auto-compact（只保留最近 2 条）
    water_level = self._calc_water_level(session_state)
    effective_window = self._context_window_tokens - self._max_output_tokens
    if water_level >= effective_window - AUTOCOMPACT_BUFFER:
        messages = self._run_auto_compact(
            messages, session_state, store, observability,
            keep_last_messages=2,
        )

    # 带熔断器，防止无限循环
    ...
```

### 3.8 SessionState 扩展

```python
@dataclass
class SessionState:
    # 现有字段 ...
    session_id: str = field(default_factory=lambda: uuid4().hex[:16])  # 新增
    content_replacement_state: ContentReplacementState = field(default_factory=ContentReplacementState)  # 新增
    user_intents: list[str] = field(default_factory=list)  # 新增：用户原始意图保留
```

**`user_intents` 写入时机**：在 `QueryLoop.run()` 中，当用户消息进入 `store` 之前，将用户原始消息文本追加到 `session_state.user_intents`：

```python
# core/query/loop.py — 用户消息处理处
if user_message_content:
    session_state.user_intents.append(user_message_content)
```

`SessionStore` 新增：
- `tool_result_dir: Path` 属性，指向 `.harness/sessions/<session_id>/tool-results/`，在 `bootstrap()` 时自动创建

### 3.9 QueryLoop 调用链变更

```python
# 新流程
class QueryLoop:
    def run(self, ..., governor, offloader, ...):
        # ... 模型调用返回 tool_calls ...

        # 工具执行
        batch = tool_runtime.execute_batch(...)

        # 工具结果即时 offload（新增）
        for msg in batch.messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                tool_name = ...  # 从 tool_calls 中查找
                if tool_name != "read_file":
                    msg["content"] = offloader.maybe_persist(
                        msg["tool_call_id"],
                        msg["content"],
                        tool_name=tool_name,
                    )

        # Governor 水位评估
        prepared = governor.assess(...)

        # View builder 最终组装
        view = view_builder.build(prepared, run_state=state)

        # API 调用
        # assess 内部已执行 blocking gate，理论上只会发送安全请求
        response = model_gateway.call_once(...)

        # 如果 prompt_too_long: reactive recover
        if is_context_window_exceeded(response):
            prepared = governor.reactive_recover(...)
            view = view_builder.build(prepared, run_state=state)
            response = model_gateway.call_once(...)
```

---

## 4. 可观测性

### 4.1 Observability 扩展

`PreparedQueryContext.observability` 扩展为：

```python
{
    "water_level": 187500,                    # 当前水位（token 数）
    "water_line": "autocompact",              # 命中的水位线名称
    "strategies_run": ["auto_compact"],        # 实际执行的策略
    "before_tokens": 187500,                   # 策略执行前的 token 数
    "after_tokens": 85000,                     # 策略执行后的 token 数
    "offloaded_count": 12,                     # 累计 offload 的工具结果数
    "offloaded_bytes": 3400000,                # 累计 offload 的字节数
    "steps": ["estimate", "per_message_budget", "auto_compact"],
}
```

### 4.2 渲染层

渲染层（Renderer）根据水位线显示不同的状态消息：
- **正常水位**（无策略触发）→ 无状态消息
- **PREVIEW_STRIP** → "上下文整理: 已压缩工具输出"
- **MICROCOMPACT** → "上下文整理: 已清理旧工具结果"
- **AUTO_COMPACT** → "上下文压缩: 已生成摘要"
- **READ WORKING SET RESTORED** → "上下文恢复: 已恢复最近读取文件"

---

## 5. 测试策略

每层独立可测：

| 组件 | 测试重点 | 测试文件 |
|---|---|---|
| `ToolResultOffloader` | 阈值判断、文件写入、预览生成、替换状态冻结、re-apply 一致性 | `test_offloader.py` |
| `read_file working set` | `read_file` 不走通用 offload、compact 后最近文件恢复、preserved tail 去重 | `test_read_working_set.py` |
| `ContentReplacementState` | session 恢复后状态重建、冻结决策不可逆 | `test_content_replacement.py` |
| `PreviewStrip` | 压缩 `<persisted-output>` 标签、不影响非 offload 内容、替换状态同步更新 | `test_preview_strip.py` |
| `Microcompact` | 时间窗口判断、可 compactable 工具过滤、API 配对保持、跳过 `read_file` working context | `test_microcompact.py` |
| `ContextGovernor` | 水位线计算、流水线策略执行、blocking gate、reactive recover、熔断器 | `test_governor.py` |
| 集成测试 | QueryLoop 全流程（工具结果 offload → Governor → View）、reactive recover 路径 | `test_query_display.py`, `test_query_logging.py` |

测试中 `ToolResultOffloader` 使用 `tmp_path` 替代真实目录，`ContextGovernor` 注入 fake 策略实现。

---

## 6. 文件变更清单

### 6.1 新增文件

| 文件 | 职责 |
|---|---|
| `core/session/governor.py` | ContextGovernor：水位线 + 策略调度 |
| `core/session/offloader.py` | ToolResultOffloader：offload + 预算 |
| `core/session/read_working_set.py` | `read_file` 工作集保护与 compact 后恢复逻辑 |
| `core/session/preview_strip.py` | PreviewStrip 策略实现 |
| `core/session/microcompact.py` | Microcompact 策略实现（从 compact_service 迁出） |
| `core/session/content_replacement.py` | ContentReplacementState 数据类 |

对应测试文件：`test_governor.py`、`test_offloader.py`、`test_read_working_set.py`、`test_preview_strip.py`、`test_microcompact.py`、`test_content_replacement.py`

### 6.2 删除文件

| 文件 | 原因 |
|---|---|
| `core/session/context_manager.py` | 被 `governor.py` 替代 |
| `tests/session/test_context_manager.py` | 替换为 `test_governor.py` |

### 6.3 修改文件

| 文件 | 变化 | 原因 |
|---|---|---|
| `core/session/compact_service.py` | 删除 `apply_tool_result_budget` 和 `apply_time_based_microcompact`，只保留 `summarize_and_compact` 和 `build_runtime_restore_messages`，补 `read_file` working set 恢复和 `user_intent_restore` | 函数迁入新模块 |
| `core/session/token_budget.py` | `should_trigger_summary_compact` 替换为水位线计算函数 `calc_water_level` | 水位线体系替代单一阈值 |
| `core/session/state.py` | 新增 `session_id: str`、`content_replacement_state: ContentReplacementState`、`user_intents: list[str]` 字段 | Session 基础设施 |
| `core/session/store.py` | 新增 `tool_result_dir: Path` 属性，`bootstrap()` 时创建 `.harness/sessions/<session_id>/tool-results/` 目录 | offload 目录管理 |
| `core/session/__init__.py` | 导出更新：移除 ContextManager，新增 ContextGovernor、ToolResultOffloader 等 | 新模块导出 |
| `core/query/loop.py` | `context_manager` 参数改为 `governor`，新增 `offloader` 参数和调用点，新增 `user_intents` 追加逻辑 | 接口变更 |
| `core/session/engine.py` | `context_manager` 改为 `governor`，注入 `offloader` | 接口变更 |
| `core/session/view_builder.py` | 模块说明和接口文案从 ContextManager 改为 ContextGovernor / PreparedQueryContext | 适配新数据流命名 |
| `tests/test_query_display.py` | `FakeContextManager` → `FakeGovernor` | 适配新接口 |
| `tests/test_query_logging.py` | `FakeContextManager` → `FakeGovernor` | 适配新接口 |
| `tests/session/test_engine_commands.py` | 适配新接口 | 适配新接口 |
| `tests/session/test_compact_service.py` | 拆分 `apply_tool_result_budget` / `apply_time_based_microcompact` 相关测试，补充 Governor / Offloader / PreviewStrip 测试 | 测试随模块迁移 |
| `tests/session/test_token_budget.py` | 从单一 `should_trigger_summary_compact` 迁移到水位线计算与校准逻辑测试 | 适配水位线体系 |

### 6.4 保留不动

| 文件 | 原因 |
|---|---|
| `core/session/query_context.py` | `PreparedQueryContext` 和 `ContextBlock` 继续使用 |
| `core/prompt/assembler.py` | `build_stable_tools` / `build_runtime_blocks` 继续使用 |

---

## 7. V2 设计（行为 / 流程级）

### 7.1 V2 目标与边界

V1 解决的是“不要直接爆窗”；V2 解决的是“在高压区尽量不要立刻走全量摘要替换”。

V2 的核心目标有三个：

1. **减少 Auto-compact 触发频率**：让更多高压场景在进入全量摘要前就被中层治理吸收掉
2. **降低 transcript 破坏性**：优先采用归档、cache edit、本地会话记忆，而不是直接把大量历史替换成一段摘要
3. **提升连续性**：让模型在继续工作时，仍然能通过本地 artifact 和会话记忆回到关键上下文

V2 只新增三类能力：

- Context Collapse
- cache-aware microcompact
- session memory compact

不在 V2 范围内：

- artifact-aware runtime 的完整体系化设计
- 跨会话外部记忆检索
- 中断恢复 / 恢复到未完成 turn

### 7.2 V2 在 Runtime Pipeline 中的位置

V2 不是推翻 V1，而是在 V1 流水线中插入两层中间治理：

```text
V1:
Budget → PreviewStrip → Microcompact → Auto-compact → Blocking Gate

V2:
Budget → PreviewStrip → Microcompact
      → Context Collapse
      → Session Memory Compact / Auto-compact
      → Blocking Gate
```

其中，Microcompact 在 V2 中优先尝试 cache-aware 路径：

```text
Microcompact
  ├─ cached path（支持 cache editing 时优先）
  └─ transcript path（不支持时回退到 V1 的时间清理）
```

整体原则：

- 轻策略仍然先跑，V2 不是把重策略提前
- Context Collapse 负责“归档旧上下文”，不是生成最终摘要
- Session Memory Compact 负责“用本地记忆压缩已归档内容”，不是取代全部 compact 逻辑
- 如果前面几层仍然压不下来，才回退到现有 Auto-compact

### 7.3 V2 运行流程

当一次请求进入 Governor，高压处理顺序应改为：

1. **Budget / PreviewStrip / Microcompact 先执行**
   - 与 V1 一致
   - 不同点是 Microcompact 优先走 cache-aware path

2. **如果水位仍高于 Collapse 触发线，执行 Context Collapse**
   - 选择“足够旧、语义上可归档”的消息段
   - 把这段消息写入本地 collapse log / archive artifact
   - transcript 中不再保留完整原文，而替换为一条轻量归档引用块
   - preserved tail、用户最近目标、todo、active skills、`read_file` 最近 working set 不进入 collapse 候选

3. **重新估算水位**
   - 如果 collapse 后已经回到安全区，直接继续构建请求
   - 如果仍然处于高压区，进入 Session Memory Compact / Auto-compact 判定

4. **优先尝试 Session Memory Compact**
   - 从 collapse log、tool-result artifacts、working set 元数据生成本地会话记忆
   - 用这份本地记忆替换较重的归档引用块或旧摘要块
   - 成功则继续工作，不调用摘要 LLM

5. **本地记忆不足或效果不够时，才回退到 Auto-compact**
   - 仍然保留 V1 的熔断器
   - 仍然保留最近 preserved tail 和 runtime restore

6. **最后由 Blocking Gate 兜底**
   - 如果前面几层都做完仍然过高，不发送请求
   - 继续本地抢救，或者直接返回本地状态消息

### 7.4 Context Collapse 的行为定义

Context Collapse 在 V2 中不是“再做一次摘要”，而是“把旧上下文从主 transcript 挪到本地归档层”。

它的行为应满足以下约束：

- **输入**：当前 transcript、归档日志状态、preserved tail、working context 保护名单
- **选择对象**：优先选择较旧的 assistant / tool / user 消息段，且这些消息段已经脱离当前直接推理链路
- **输出一**：本地 collapse log，记录被归档的消息段、时间、来源范围、关键锚点
- **输出二**：transcript 中的 collapse reference block，告诉模型“这段上下文已归档，可在需要时通过本地 artifact 回放”
- **不碰的内容**：最近消息尾部、todo、active skills、`read_file` 最近 working set、最近错误上下文、用户的当前目标约束

它解决的问题不是“把 token 变少”这么简单，而是把“旧但可能仍有价值的上下文”从主工作区移到可回放的归档区，避免一上来就被全量摘要抹平。

### 7.5 Cache-aware Microcompact 的行为定义

V2 的 Microcompact 先判断底层 API 是否支持 cache editing：

- **支持时**
  - 优先产生 cache edit 指令
  - 不直接改写本地 transcript
  - 目标是释放本轮 prompt 压力，同时尽量保持历史前缀稳定

- **不支持时**
  - 回退到 V1 的 transcript-based time microcompact
  - 行为与 V1 一致

这层的关键不是“删更多”，而是“在删的同时少破坏 cache”。因此它仍然只处理 compactable 的 ephemeral outputs，不扩展到 `read_file` working context。

### 7.6 Session Memory Compact 的行为定义

Session Memory Compact 是 V2 的“本地重策略”，定位在 Context Collapse 之后、Auto-compact 之前。

它的处理顺序应是：

1. 从本地 collapse log 提取已归档上下文
2. 合并工具结果 artifact 的轻量索引
3. 合并 `read_file` working set 的最近锚点信息
4. 生成结构化 session memory
5. 用 session memory 替换较重的旧归档引用块或旧压缩块

它与 V1 Auto-compact 的区别：

- **V1 Auto-compact**：调用 LLM，直接把大量历史改写成一段结构化摘要
- **V2 Session Memory Compact**：优先基于本地已知材料生成会话记忆，不依赖额外 API 调用

这层的目的，是尽量把“高压后的继续工作”建立在本地会话资产上，而不是每次都重新求助于摘要模型。

### 7.7 与 artifact-aware runtime、外部记忆、中断恢复的关系

这三者都和 V2 有关联，但不在 V2 本次实现范围内。

**artifact-aware runtime**

V2 会为它打基础，但不直接把它做完整：

- V1/V2 已经有 `tool-results/`、collapse log、session memory 这些本地 artifact
- 后续真正的 artifact-aware runtime，需要再补统一索引、artifact 类型系统、按需回放协议
- 因此它应被视为 **V2 之后的上层治理体系**，而不是当前 V2 的必做项

**外部记忆**

- V2 的 session memory 只处理单会话内的本地记忆
- 不做跨会话召回
- 不做长期用户偏好或项目长期知识库注入

**中断恢复**

- 中断恢复依赖稳定的 artifact、collapse log、session memory、replacement state 重建
- 但它关注的是“turn 被打断后怎么恢复执行”，不是“当前轮上下文怎么治理”
- 因此它应放在 V2 之后，作为独立阶段设计
