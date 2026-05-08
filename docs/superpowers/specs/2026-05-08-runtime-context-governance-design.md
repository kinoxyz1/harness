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
- **先压什么**：优先压缩工具输出（占比最大、密度最低），保护对话区域（占比小、信息密度高）
- **哪些绝对不能丢**：用户的核心需求、当前任务的关键文件路径、已做出的决策依据
- **压完怎么无缝继续**：通过本地文件 offloading，让被压缩的内容仍然可达（模型可以主动 read 回来）

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
| `compactPrompt.ts` | 压缩提示词模板（9 段式摘要格式） |
| `toolResultStorage.ts` | 工具结果的持久化与预算管理 |

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

**阶段 3 — Snip（历史裁剪）**

实验特性（`HISTORY_SNIP` feature flag），整条删除旧消息，释放的 token 从 autocompact 阈值中扣除（避免双重计算）。

**阶段 4 — Microcompact → Autocompact**

Microcompact（`microCompact.ts`）在每次 API 调用前运行，针对可 compactable 的工具类型（Bash、Grep、Glob、Read、WebFetch、WebSearch、FileEdit、Write——第 41 行）清理旧结果，替换为 `[Old tool result content cleared]`。

Autocompact（`compact.ts`）调 LLM 生成 9 段式结构化摘要，保留最近消息，注入 runtime restore 消息恢复 todo 状态和 active skills。

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

4. **分层策略有优先级**：先跑轻量策略（microcompact），不够再用重策略（autocompact）。但 Claude Code 也在实验 Context Collapse 这种渐进式归档方案。

5. **可 compactable 的工具类型是白名单制的**。不是所有工具结果都可以随意清理，只有输出密度低的工具（read、bash、grep 等）才列入白名单。结构化工具（todo、skill）的结果永远不能丢。

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
│     → 选择并执行策略 (Snip / Microcompact / Auto-compact)│
│     → 返回 PreparedQueryContext                         │
│                                                         │
│  4. view_builder.build(prepared)          ← 最终组装    │
│     → ModelInputView                                    │
│                                                         │
│  5. model_gateway.call_once(...)          ← 发送请求    │
│     → 如果 prompt_too_long: governor.reactive_recover() │
└─────────────────────────────────────────────────────────┘
```

### 3.2 组件职责

#### ContextGovernor（`core/session/governor.py`）

上下文决策的唯一入口。持有水位线状态，每次查询前评估压力并选择策略。

职责：
- 计算当前水位（总 token 用量 + stable overhead）
- 根据水位线选择策略（Snip / Microcompact / Auto-compact）
- 编排策略执行（elif 互斥，只执行最匹配当前水位的那一层）
- 管理熔断器（Auto-compact 连续失败后停止）
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

#### ContentReplacementState（`core/session/content_replacement.py`）

冻结的替换决策状态。挂载在 `SessionState` 上，跨轮次持久。

```python
@dataclass
class ContentReplacementState:
    seen_ids: set[str]           # 已决策过的 tool_use_id（永久冻结）
    replacements: dict[str, str] # tool_use_id → <persisted-output> 字符串
```

#### 策略模块

- `core/session/snip.py` — Snip 策略
- `core/session/microcompact.py` — Microcompact 策略（从 `compact_service.py` 迁出）
- `core/session/compact_service.py` — 精简为只保留 `summarize_and_compact` 和 `build_runtime_restore_messages`

### 3.3 水位线体系

基于 Claude Code 的三层水位线扩展，加入 Snip 层：

```python
# 常量定义
EFFECTIVE_CONTEXT_WINDOW = context_window_tokens - max_output_tokens
SNIP_THRESHOLD          = EFFECTIVE_CONTEXT_WINDOW - 40_000
MICROCOMPACT_THRESHOLD  = EFFECTIVE_CONTEXT_WINDOW - 20_000
AUTOCOMPACT_THRESHOLD   = EFFECTIVE_CONTEXT_WINDOW - 13_000
BLOCKING_LIMIT          = EFFECTIVE_CONTEXT_WINDOW - 3_000
```

以 200k 上下文窗口为例（有效窗口 ~190k）：

| 水位线 | 触发值 | 策略 | 力度 |
|---|---|---|---|
| `SNIP_THRESHOLD` | ~150k | Snip | 压缩 `<persisted-output>` 标签，去掉预览只留路径 |
| `MICROCOMPACT_THRESHOLD` | ~170k | Microcompact | 清理旧工具结果 + 已 offload 结果深度压缩 |
| `AUTOCOMPACT_THRESHOLD` | ~177k | Auto-compact | LLM 摘要替换历史 |
| `BLOCKING_LIMIT` | ~187k | 阻断 | 拒绝发请求，必须先抢救 |

水位计算：

```python
def _calc_water_level(self, session_state) -> int:
    estimated = estimate_messages_tokens(session_state.conversation_messages)
    used = calibrated_input_tokens(
        estimated=estimated,
        observed=session_state.compact_state["last_prompt_tokens"],
    )
    return used + self._stable_overhead_tokens
```

`_stable_overhead_tokens` 在每次查询开始时根据 `stable_system + stable_tools + required_runtime_blocks` 计算，确保水位反映真实的总占用。

### 3.4 策略详细设计

#### Snip（轻量裁剪）

**触发条件**：水位 >= SNIP_THRESHOLD 且 < MICROCOMPACT_THRESHOLD

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

**关键约束**：
- 只处理已经被 offload 过的工具结果（有 `<persisted-output>` 标签的），不碰未 offload 的内容
- 不改变消息结构，只替换 content 字符串
- 替换状态同步更新（`ContentReplacementState.replacements`）

#### Microcompact（旧工具清理）

**触发条件**：水位 >= MICROCOMPACT_THRESHOLD 且 < AUTOCOMPACT_THRESHOLD

**两种子策略**：

**策略 A — 时间清理**（从现有 `compact_service.apply_time_based_microcompact` 迁出）：
- 超过 `age_cutoff_seconds`（默认 1800s）的 compactable 工具结果
- 不在最近 `keep_recent_trajectories`（默认 2）轮中的
- 内容替换为 `[Old tool result content cleared]`

可 compactable 的工具白名单：`read_file`, `bash`, `find`, `grep`, `glob`, `web_fetch`, `web_search`, `write_file`。这些工具的输出天然具有"可重新获取"的特性——文件可以重新读、命令可以重新跑、搜索可以重新做。

不可 compactable 的工具：`todo`、`skill` 等结构化工具——它们的结果短且有结构意义，清理后会导致状态丢失。

**策略 B — 已 offload 结果的深度清理**：
- 对于已经被 offload 到文件的结果（有 `<persisted-output>` 或已 snip 的标签），可以做更激进的压缩
- 将 `<persisted-output>` 标签直接替换为单行路径引用（和 Snip 一样的效果，但此时是对所有 offload 结果执行，不限于最旧的）
- 或者对于非常旧的结果，将整个 tool_result 消息的内容设为空字符串（保留消息结构以维护 API 配对）

```python
def _run_microcompact(self, messages):
    messages = self._time_based_clear(messages)
    messages = self._offloaded_result_compact(messages)
    return messages
```

#### Auto-compact（摘要压缩）

**触发条件**：水位 >= AUTOCOMPACT_THRESHOLD

**做什么**：和现有逻辑一致，但作为 Governor 的一个策略被调用：
1. 调 LLM 生成 9 段式结构化摘要（Primary Request / Technical Concepts / Files & Code / Errors / Problem Solving / User Messages / Pending Tasks / Current Work / Optional Next Step）
2. 用摘要替换旧消息，保留最近 4 条
3. 注入 runtime restore 消息（todo state、active skills、最近读过的文件）
4. 带熔断器：3 次连续失败后停止，60s 冷却期

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
- `todo`、`skill` 等结构化工具：阈值为 `Infinity`（永远不 offload）

#### 聚合预算

每次查询前，Governor 调用 `offloader.enforce_per_message_budget()`：

```python
def enforce_per_message_budget(self, messages: list[dict]) -> list[dict]:
    budget = 200_000  # MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
    # 按 API 消息分组（同一轮的多个 tool_result 属于同一个 user message）
    # 对每组的 tool_result 按大小排序
    # 冻结已决策的（seen_ids 中的直接 re-apply）
    # 新的超限结果：offload 最大的几条直到总额在预算内
    return messages
```

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

Session 恢复时从 transcript 的 meta 记录重建状态。

#### 文件目录

```
<project_root>/.harness/tool-results/<toolUseId>.txt
```

简洁的单层目录，不嵌套 session 子目录。原因：
- harness 一次只有一个活跃 session
- 不需要跨 session 隔离
- 文件名中已包含唯一的 toolUseId

清理策略：30 天默认保留，不做主动清理。

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
        stable_tools_tokens = _estimate_tools_tokens(stable_tools)
        required_runtime_tokens = sum(b.token_estimate for b in runtime_blocks if b.required)
        self._stable_overhead_tokens = stable_system_tokens + stable_tools_tokens + required_runtime_tokens

        # 计算水位
        water_level = self._calc_water_level(session_state)
        effective_window = self._context_window_tokens - self._max_output_tokens

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

        # 按水位线选择策略（elif 互斥）
        if water_level >= effective_window - AUTOCOMPACT_BUFFER:
            observability["strategies_run"].append("auto_compact")
            messages = self._run_auto_compact(messages, session_state, store, observability)
        elif water_level >= effective_window - MICROCOMPACT_BUFFER:
            observability["strategies_run"].append("microcompact")
            messages = self._run_microcompact(messages, session_state, observability)
        elif water_level >= effective_window - SNIP_BUFFER:
            observability["strategies_run"].append("snip")
            messages = self._run_snip(messages, observability)

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

策略选择是 `elif` 而非独立 `if`：如果触发了 Auto-compact，上下文已被大幅压缩，不需要再跑 Snip/Microcompact。只执行最匹配当前水位的那一层策略。

### 3.7 Reactive Recover

处理 API 返回 `prompt_too_long` 时的紧急抢救：

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
```

`SessionStore` 新增：
- `tool_result_dir: Path` 属性，指向 `.harness/tool-results/`，在 `bootstrap()` 时自动创建

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
                msg["content"] = offloader.maybe_persist(
                    msg["tool_call_id"],
                    msg["content"],
                    tool_name=...,  # 从 tool_calls 中查找
                )

        # Governor 水位评估
        prepared = governor.assess(...)

        # View builder 最终组装
        view = view_builder.build(prepared, run_state=state)

        # API 调用
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
- **SNIP** → "上下文整理: 已压缩工具输出"
- **MICROCOMPACT** → "上下文整理: 已清理旧工具结果"
- **AUTO_COMPACT** → "上下文压缩: 已生成摘要"

---

## 5. 测试策略

每层独立可测：

| 组件 | 测试重点 | 测试文件 |
|---|---|---|
| `ToolResultOffloader` | 阈值判断、文件写入、预览生成、替换状态冻结、re-apply 一致性 | `test_offloader.py` |
| `ContentReplacementState` | session 恢复后状态重建、冻结决策不可逆 | `test_content_replacement.py` |
| `Snip` | 压缩 `<persisted-output>` 标签、不影响非 offload 内容、替换状态同步更新 | `test_snip.py` |
| `Microcompact` | 时间窗口判断、可 compactable 工具过滤、API 配对保持、已 offload 结果深度清理 | `test_microcompact.py` |
| `ContextGovernor` | 水位线计算、策略选择逻辑、elif 互斥、reactive recover、熔断器 | `test_governor.py` |
| 集成测试 | QueryLoop 全流程（工具结果 offload → Governor → View）、reactive recover 路径 | `test_query_display.py`, `test_query_logging.py` |

测试中 `ToolResultOffloader` 使用 `tmp_path` 替代真实目录，`ContextGovernor` 注入 fake 策略实现。

---

## 6. 文件变更清单

### 6.1 新增文件

| 文件 | 职责 |
|---|---|
| `core/session/governor.py` | ContextGovernor：水位线 + 策略调度 |
| `core/session/offloader.py` | ToolResultOffloader：offload + 预算 |
| `core/session/snip.py` | Snip 策略实现 |
| `core/session/microcompact.py` | Microcompact 策略实现（从 compact_service 迁出） |
| `core/session/content_replacement.py` | ContentReplacementState 数据类 |

对应测试文件：`test_governor.py`、`test_offloader.py`、`test_snip.py`、`test_microcompact.py`、`test_content_replacement.py`

### 6.2 删除文件

| 文件 | 原因 |
|---|---|
| `core/session/context_manager.py` | 被 `governor.py` 替代 |
| `tests/session/test_context_manager.py` | 替换为 `test_governor.py` |

### 6.3 修改文件

| 文件 | 变化 | 原因 |
|---|---|---|
| `core/session/compact_service.py` | 删除 `apply_tool_result_budget` 和 `apply_time_based_microcompact`，只保留 `summarize_and_compact` 和 `build_runtime_restore_messages` | 函数迁入新模块 |
| `core/session/token_budget.py` | `should_trigger_summary_compact` 替换为水位线计算函数 `calc_water_level` | 水位线体系替代单一阈值 |
| `core/session/state.py` | 新增 `session_id: str` 和 `content_replacement_state: ContentReplacementState` 字段 | Session 基础设施 |
| `core/session/store.py` | 新增 `tool_result_dir: Path` 属性，`bootstrap()` 时创建 `.harness/tool-results/` 目录 | offload 目录管理 |
| `core/session/__init__.py` | 导出更新：移除 ContextManager，新增 ContextGovernor、ToolResultOffloader 等 | 新模块导出 |
| `core/query/loop.py` | `context_manager` 参数改为 `governor`，新增 `offloader` 参数和调用点 | 接口变更 |
| `core/session/engine.py` | `context_manager` 改为 `governor`，注入 `offloader` | 接口变更 |
| `tests/test_query_display.py` | `FakeContextManager` → `FakeGovernor` | 适配新接口 |
| `tests/test_query_logging.py` | `FakeContextManager` → `FakeGovernor` | 适配新接口 |
| `tests/session/test_engine_commands.py` | 适配新接口 | 适配新接口 |

### 6.4 保留不动

| 文件 | 原因 |
|---|---|
| `core/session/query_context.py` | `PreparedQueryContext` 和 `ContextBlock` 继续使用 |
| `core/session/view_builder.py` | 不需要再动 |
| `core/prompt/assembler.py` | `build_stable_tools` / `build_runtime_blocks` 继续使用 |

---

## 7. 未来扩展

### 7.1 Context Collapse（后续）

Context Collapse 是 Claude Code 正在实验的渐进式归档方案。它维护一个 commit log，压力增大时逐步 commit 归档消息。在 90% 水位时 commit，95% 时进入 blocking spawn 流程。

当 harness 需要实现 Context Collapse 时，只需在 Governor 中加一层判断：

```python
if water_level >= COLLAPSE_THRESHOLD:
    messages = self._run_context_collapse(messages, ...)
```

不需要重构现有的 Snip/Microcompact/Auto-compact 逻辑。

### 7.2 缓存友好的 Microcompact

Claude Code 有一种基于 cache-editing API 的 microcompact（`cachedMicrocompact`），可以在不破坏 prompt cache 的情况下删除工具结果。当 harness 接入支持 cache-editing 的 API 时，可以在 Microcompact 中增加这个路径。

### 7.3 Session Memory Compact

Claude Code 的 session memory compact 用本地生成的会话记忆替代 LLM 摘要，零 API 调用。当 harness 实现会话记忆功能时，可以在 Auto-compact 中优先尝试这个路径。
