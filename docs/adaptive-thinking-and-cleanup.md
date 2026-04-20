# Adaptive Thinking 与 Thinking Block 清理

## 问题

用户反映系统响应越来越慢，简单问题（如"你可以帮我干什么"）需要 65 秒才能响应。

## 排查过程

### 现象

| 问题 | 输入 tokens | 耗时 |
|------|------------|------|
| "你好" | 2480↓ | 7.5s |
| "今天多少号" | 197↓ | 3.0s |
| "你可以帮我干什么" | ??? | **65s+** |

### 根因

两个独立问题叠加导致：

**1. 固定 thinking budget 不适配任务复杂度**

原代码 `anthropic_client.py:119` 硬编码：

```python
params["thinking"] = {"type": "enabled", "budget_tokens": min(MAX_TOKENS, 200)}
```

固定 budget 导致：简单对话也被迫等待 thinking，复杂任务又思考不够深。

**2. Thinking 块无限累积导致上下文膨胀**

`view_builder.py` 的 `_content_char_cost()` 不计算 `reasoning` 字段长度，thinking 块绕过了 24K transcript 字符预算。每次 assistant 响应的 thinking 文本都被持久化到 `conversation_messages`，并通过 `protocol.py` 重新发送给 API，随对话轮次持续增长。

### Claude Code 的做法

参考 Claude Code 源码（`src/utils/thinking.ts`、`src/services/api/claude.ts`）：

- **新模型（Opus 4.6 / Sonnet 4.6）**：使用 `{ type: "adaptive" }`，不设 budget_tokens，模型自行决定思考深度
- **旧模型**：使用很大的固定 budget（`upperLimit - 1`），通过 `effort` 参数控制推理深度
- **上下文管理**：空闲超过 1 小时后，只保留最近 1 轮的 thinking 块

关键设计决策：Claude Code 不按任务复杂度动态调整 budget_tokens，而是让模型自己决定（adaptive）或用 effort 参数控制。

## 解决方案

### 改动 1：Adaptive Thinking

借鉴 Claude Code，优先使用 adaptive thinking，不支持时自动 fallback。

**`core/shared/config.py`** — 新增配置：

```python
THINKING_MODE: str = os.environ.get("LLM_THINKING_MODE", "auto")  # auto | enabled | disabled
THINKING_BUDGET: int = int(os.environ.get("LLM_THINKING_BUDGET", "4096"))  # fallback budget
```

**`core/llm/anthropic_client.py`** — 替换硬编码为自适应逻辑：

```
首次调用 → 发送 { type: "adaptive" }
  ├─ 成功 → 缓存 _adaptive_supported=True，后续直接用 adaptive
  └─ 400 错误 → 自动重试 { type: "enabled", budget_tokens: THINKING_BUDGET }
              → 缓存 _adaptive_supported=False，后续直接用 enabled
```

三种模式：
- `auto`（默认）：自动检测模型能力，优先 adaptive
- `enabled`：强制使用固定 budget_tokens
- `disabled`：关闭 thinking

### 改动 2：Thinking Block Cleanup

在 view 层清理旧 thinking 块，防止上下文膨胀。

**`core/session/view_builder.py`**：

1. **修复预算计算**：新增 `_message_char_cost()` 方法，将 `reasoning` 字段纳入字符预算计算
2. **清理旧 thinking**：新增 `_strip_old_thinking()` 方法，在 transcript slice 中只保留最近 2 个 assistant 消息的 reasoning，更早的清除
3. **不污染原始数据**：清理操作在 `_select_transcript_slice` 返回的副本上进行，不修改 `conversation_messages`

```
conversation_messages（append-only，不变）
  → _select_transcript_slice（按字符预算截取）
  → _strip_old_thinking（清理旧 reasoning，只保留最近 2 个）
  → normalize_messages（转为 API 格式）
  → 发送给模型
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_THINKING_MODE` | `auto` | `auto`：自动检测 / `enabled`：固定预算 / `disabled`：关闭 |
| `LLM_THINKING_BUDGET` | `4096` | `enabled` 模式下的 budget_tokens |

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `core/shared/config.py` | 新增 `THINKING_MODE`、`THINKING_BUDGET` |
| `core/llm/anthropic_client.py` | 新增 `_apply_thinking()`、adaptive fallback 逻辑 |
| `core/session/view_builder.py` | 新增 `_message_char_cost()`、`_strip_old_thinking()`，修复预算计算 |

## 验证

- 177 个现有测试全部通过
- 简单问题（"你好"）秒回，复杂任务模型自动深度思考
- 多轮对话后 input tokens 稳定，不再膨胀
