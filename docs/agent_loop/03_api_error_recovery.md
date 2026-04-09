# 03 - API 错误恢复

> 对应差异全景 #3：循环安全保障 — API 错误恢复（重试 / 模型降级）

## 问题

当前代码中所有 API 调用都没有 try/except：

```python
# agent.py — 没有任何错误处理
response = client.chat.completions.create(
    model=MODEL,
    messages=messages,
    tools=TOOLS,
    ...
)
```

使用百炼 DashScope API 时，网络波动是常态。一次超时或 5xx 错误就会导致整个会话崩溃，所有对话历史丢失。

## 实际会遇到的错误

| 错误类型 | 场景 | 频率 |
|---------|------|------|
| `openai.APIConnectionError` | 网络波动、DNS 解析失败 | 偶尔 |
| `openai.APITimeoutError` | 大上下文请求超时 | 较常见 |
| `openai.RateLimitError` | 429 限流 | 高频调用时 |
| `openai.APIStatusError` (5xx) | 服务端异常 | 偶尔 |
| `openai.APIStatusError` (413) | 上下文过长 | 长对话时 |

## 设计参考

### Claude Code 的做法

Claude Code 在 `src/query.ts` 中实现了多层恢复策略：

| 恢复机制 | 触发条件 | 行为 |
|----------|----------|------|
| 模型降级 | FallbackTriggeredError | 切换到备用模型重试 |
| prompt-too-long 恢复 | API 返回 413 | 尝试 context collapse 或 reactive compact |
| max_output_tokens 恢复 | 输出 token 超限 | 注入续写消息，最多重试 3 次 |
| reactive compact | 上下文溢出 | 异步压缩并重试 |

### 我们的做法（分阶段）

当前阶段只做**基础重试**，不实现模型降级和上下文恢复（那些依赖上下文管理模块先完成）。

## 实现方案

### 核心原则

1. **可重试的错误**（网络、超时、5xx、429）→ 自动重试，指数退避
2. **不可重试的错误**（413 上下文过长、401 认证失败）→ 友好提示，不崩溃
3. **重试有上限** → 最多 3 次，避免无限重试

### config.py — 新增配置

```python
API_MAX_RETRIES: int = int(os.environ.get("API_MAX_RETRIES", "3"))
API_RETRY_BASE_DELAY: float = float(os.environ.get("API_RETRY_BASE_DELAY", "2.0"))
```

### agent.py — 封装 API 调用

```python
import time
import openai
from .config import API_MAX_RETRIES, API_RETRY_BASE_DELAY, MAX_TOKENS, MODEL

def _call_llm(client, messages, tools=None, stream=False) -> object:
    """调用 LLM API，带重试和错误处理。"""
    params = {
        "model": MODEL,
        "messages": messages,
        "extra_body": {"enable_thinking": True},
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        params["tools"] = tools
    if stream:
        params["stream"] = True

    last_error = None
    for attempt in range(API_MAX_RETRIES):
        try:
            return client.chat.completions.create(**params)
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = e
            delay = API_RETRY_BASE_DELAY * (2 ** attempt)
            console.print(f"[yellow]网络错误，{delay:.0f}s 后重试 ({attempt + 1}/{API_MAX_RETRIES})...[/yellow]")
            time.sleep(delay)
        except openai.RateLimitError as e:
            last_error = e
            delay = API_RETRY_BASE_DELAY * (2 ** attempt) * 2
            console.print(f"[yellow]限流中，{delay:.0f}s 后重试 ({attempt + 1}/{API_MAX_RETRIES})...[/yellow]")
            time.sleep(delay)
        except openai.APIStatusError as e:
            if e.status_code == 413:
                console.print("[red]上下文过长，请开启新对话或使用 /compact 压缩历史。[/red]")
                return None
            if e.status_code == 401:
                console.print("[red]API Key 无效，请检查 DASHSCOPE_API_KEY 环境变量。[/red]")
                return None
            if e.status_code >= 500:
                last_error = e
                delay = API_RETRY_BASE_DELAY * (2 ** attempt)
                console.print(f"[yellow]服务端错误 ({e.status_code})，{delay:.0f}s 后重试...[/yellow]")
                time.sleep(delay)
            else:
                console.print(f"[red]API 错误: {e}[/red]")
                return None

    console.print(f"[red]API 调用失败（已重试 {API_MAX_RETRIES} 次）: {last_error}[/red]")
    return None
```

### agent.py — agent_loop 中使用

```python
def agent_loop(messages):
    client = create_llm_client()

    response = _call_llm(client, messages, tools=TOOLS)
    if response is None:
        return  # 错误已在 _call_llm 中打印

    # ... 后续逻辑不变 ...
```

## 后续扩展

当上下文管理模块完成后，可以增加：
- 413 错误时自动压缩上下文后重试（类似 CC 的 reactive compact）
- 模型降级（主模型不可用时切换备用模型）

## 参考资料

- Claude Code 源码 `src/query.ts` — 多种恢复策略
- OpenAI Python SDK 错误类型：`openai.APIConnectionError`、`openai.RateLimitError` 等
