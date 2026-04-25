# Compact 功能研究

## 背景

在长任务（如生成分析报告）中，模型出现上下文退化：反复读取同一文件、说"用户没有新的输入"、忘记自己在做什么。根因是 `_select_transcript_slice` 直接丢弃早期消息，导致模型丢失任务上下文。

## 现状：harness 的做法

`core/session/view_builder.py` 的 `_select_transcript_slice`：24K 字符预算，从尾部贪心选取消息，超出预算的早期消息直接丢弃。

问题：
- 原始用户请求被丢弃 → 模型不知道在做什么
- 已读文件的工具调用被丢弃 → 模型反复读同一文件
- `TodoPlanningPolicy` 注入的 `todo_stale` 提醒以 `user` 角色注入 → 模型误以为"用户没说话"

## Claude Code 的 4 层上下文管理

| 层 | 机制 | 做了什么 | 触发条件 |
|---|------|---------|---------|
| 1. Snip | 标记旧消息不发给模型 | 不删除存储，只是不在 API 调用中发送 | 每轮检查 |
| 2. Microcompact | 清理旧工具输出 | 保留消息结构，把旧的 bash/read 结果替换成 `[Old tool result content cleared]` | 空闲 > 60min 或缓存过期 |
| 3. Context Collapse | 分段归档 | 把连续的工具调用归档为结构化摘要 | token 接近阈值 |
| 4. Auto-compact | LLM 摘要 | 用 LLM 把对话总结成 9 段结构化摘要 | token 达到 effectiveWindow - 13K |

核心设计原则：**永远不直接丢弃消息。要么保留、要么压缩内容、要么摘要，始终让模型知道"我在做什么"。**

### 关键源码位置（Claude Code）

| 文件 | 职责 |
|------|------|
| `src/query.ts` | 主循环，按顺序执行 4 层 pipeline |
| `src/services/compact/microCompact.ts` | Microcompact：清理旧工具输出 |
| `src/services/compact/compact.ts` | Full compact：LLM 摘要 |
| `src/services/compact/autoCompact.ts` | 自动触发决策（effectiveWindow - 13K） |
| `src/services/compact/sessionMemoryCompact.ts` | 轻量 compact：保留最近 10K-40K token + session memory |
| `src/services/compact/prompt.ts` | 摘要 prompt 模板（9 段结构） |
| `src/services/compact/grouping.ts` | 按 API round 分组消息（截断的最小单位） |
| `src/utils/context.ts` | 上下文窗口计算 |

### Auto-compact 的摘要结构（9 段）

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections（含代码片段）
4. Errors and Fixes
5. Problem Solving
6. All User Messages（所有非工具结果的用户消息）
7. Pending Tasks
8. Current Work
9. Optional Next Step

### Microcompact 的细节

两种路径：
- **时间触发**：距上次 assistant 消息超过 60 分钟 → 清理所有旧工具输出，保留最近 5 个
- **缓存触发**：利用 API 的 `cache_edits` 特性，在 API 层删除工具结果而不破坏本地缓存

可清理的工具：FileRead、Bash、Grep、Glob、WebSearch、WebFetch、FileEdit、FileWrite

### Session Memory Compact（轻量替代）

不调用 LLM，用预提取的 session memory 文件：
- 保留最近 10K-40K token 的原始消息
- 至少保留 5 条含文本的消息
- 确保保留完整的 tool_use/tool_result 配对和 thinking blocks

## harness 的改造方向

### 当前 vs 目标

```
当前 _select_transcript_slice（类似 Snip）：
  从尾部贪心选取 → 超出预算的消息直接丢弃 → 模型失忆

目标 Microcompact：
  保留所有消息 → 把旧工具输出替换成占位符 → 模型知道"已经做过什么"
```

### 需要改动的部分

1. **`_select_transcript_slice` → 改造为 Microcompact**
   - 不再丢弃消息，而是保留完整消息列表
   - 对旧的 tool 消息，用 `[tool result cleared]` 替换 content
   - 保留最近 N 个工具输出的完整内容

2. **新增：Auto-compact（后续）**
   - 当 token 接近上下文窗口阈值时，用 LLM 摘要整个对话
   - 生成结构化摘要保留任务语义

3. **思考清理策略调整**
   - `_strip_old_thinking(keep_last=2)` 可能过于激进
   - 长任务中需要保留更多 thinking 以维持推理连贯性

### 涉及文件

| 文件 | 改动 |
|------|------|
| `core/session/view_builder.py` | `_select_transcript_slice` 改造为 microcompact 逻辑 |
| `core/shared/config.py` | 新增 compact 相关配置（清理阈值、保留数量等） |
| `core/query/loop.py` | 在循环中集成 compact pipeline |
| `core/policy/todo_tracking.py` | 改善 system-reminder 的注入方式 |

## 相关文档

- `docs/adaptive-thinking-and-cleanup.md` — thinking 相关改动记录
