# 17 - 状态显示与 Token 追踪

> 对应差异全景 #17：可观测性 — Token 用量追踪 `totalUsage`
>
> 结合实际测试中发现的问题：长请求时程序像卡死一样，没有任何状态反馈。

## 问题

### 实际测试中发现的问题

当用户输入一个需要长思考的问题时，程序看起来完全卡死，没有任何输出。

### 原因分析

原代码用 `console.status("[bold green]正在思考...")` 包裹 API 调用。这个 Rich spinner 是同步阻塞的——API 调用不返回，UI 不更新。用户只看到静态文字，无法感知进度。

最初尝试了两种改进方案：

| 方案 | 做法 | 问题 |
|------|------|------|
| Rich Live + `__rich__()` | API 在主线程，Live 后台刷新 | `transient=True` 在部分终端上不渲染，计时和后续输出粘在一行 |
| Rich Live + threading | API 在后台线程，Live 在主线程 | Live 和 `input()` 存在终端控制权冲突 |

最终采用**最可靠的方案**：API 调用放后台线程，主线程用 `sys.stdout` 直接写计时，绕开 Rich。

## 实现方案

### 核心思路

```
主线程: 每秒写 "正在思考... Ns" → API 返回后清除 → 打印 token 统计
后台线程: 执行 API 调用 → 结果写入共享字典
```

为什么不用 Rich Live：
- Rich Live 需要接管终端控制，和 `input()` 交互后可能不刷新
- `transient=True` 在部分终端上导致计时不显示
- `sys.stdout` 直接写是最底层、最可靠的方式

### 完整实现

```python
import sys
import threading
import time


def _call_llm(client, messages, tools=None, stream=False):
    """调用 LLM API，显示动态计时，完成后打印耗时和 token 用量。"""
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

    # API 调用放后台线程，主线程负责刷新计时显示
    result = {}
    error = {}

    def do_call():
        try:
            result["data"] = client.chat.completions.create(**params)
        except Exception as e:
            error["data"] = e

    start = time.time()
    thread = threading.Thread(target=do_call)
    thread.start()

    # 主线程：每秒刷新计时
    while thread.is_alive():
        elapsed = int(time.time() - start)
        sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
        sys.stdout.flush()
        thread.join(timeout=1.0)

    # 清除计时行
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

    if error.get("data"):
        raise error["data"]

    response = result["data"]
    elapsed = time.time() - start

    if not stream and hasattr(response, "usage") and response.usage:
        console.print(
            f"[dim]{elapsed:.1f}s │ token {response.usage.prompt_tokens}↓ {response.usage.completion_tokens}↑[/dim]"
        )

    return response
```

### 关键细节

**`\r\033[K`** — 回车 + 清除当前行。让计时数字在同一行原地刷新，而不是每秒新增一行。

**`thread.join(timeout=1.0)`** — 最多等 1 秒检查线程状态。既实现了每秒刷新，又不会 busy loop。

**`sys.stdout.write` 而非 `console.print`** — Rich 的 `console.print` 会解析 markup、处理换行，在这里反而干扰。`sys.stdout` 直接写最可靠。

**API 调用在后台线程** — 主线程不被阻塞，才能持续刷新计时。`result` 和 `error` 通过闭包字典传递。

### 效果

```
>> 帮我分析一下这个项目的架构设计
正在思考... 0s        ← 原地刷新
正在思考... 1s
正在思考... 2s
正在思考... 3s
正在思考... 4s
正在思考... 5s
5.2s │ token 353↓ 133↑   ← 计时结束，显示统计
╭────── 思考过程 ──────╮
│ 用户要求做三件事...    │
╰──────────────────────╯
```

## 附加修复：工具输出换行问题

### 问题

`console.print(output)` 会让 Rich 对 shell 输出做自动换行，导致 `ls -la` 等命令的长文件名被截断换行。

### 解决

shell 原始输出用 `print()` 而非 `console.print()`，不经过 Rich 的格式化：

```python
# agent.py 中工具执行后的输出
output = execute_tool("bash", args)
print(output)          # ✅ 不经 Rich，保持原始格式
# console.print(output)  # ❌ Rich 会自动换行
```

## 后续扩展

- **累计 Token 追踪**：在 agent_loop 中维护 `total_input_tokens` / `total_output_tokens`，每次 API 调用后累加
- **成本估算**：根据百炼平台的 token 单价计算费用
- **流式阶段的计时**：当前流式回复阶段没有计时，可以加上

## 参考资料

- Claude Code 源码：`totalUsage` 在 `QueryEngine` 中跨轮次持久化
- 百炼 API 响应：`response.usage.prompt_tokens` / `response.usage.completion_tokens`
- ANSI 转义序列：`\r`（回车）、`\033[K`（清除行）、`\033[32m`（绿色）
