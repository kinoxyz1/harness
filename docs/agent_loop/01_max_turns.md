# 01 - 循环控制：迭代上限与 Token 预算

> 对应差异全景 #1：循环安全保障

## 问题

当前 `agent_loop` 的工具调用循环是 `while choice.finish_reason == "tool_calls"`，没有任何退出保障。如果 LLM 反复调用工具，循环永远不会停，导致：

- API 费用失控
- 用户无法得到回复
- 潜在的无限循环（模型重复执行相同命令）

## Claude Code 的实际做法（源码验证）

> 以下内容基于 `src/query.ts` 实际源码，非二手文档推测。

### 主循环没有硬编码的轮次上限

Claude Code 的主 REPL 循环 `while(true)` 中，`maxTurns` 参数**默认为 `undefined`**（无限制）。只有调用方主动传值才生效：

```typescript
// src/query.ts line 191
type QueryParams = {
  maxTurns?: number  // 可选，默认 undefined
}
```

### 不同场景使用不同的 maxTurns

| 调用场景 | maxTurns | 来源 |
|---------|---------|------|
| 主 REPL 交互循环 | `undefined`（无限制） | QueryEngine 透传 |
| fork 子代理 | 200 | forkSubagent.ts:65 |
| hook agent | 50 | execAgentHook.ts:119 |
| compact 压缩调用 | 1 | compact.ts:1194 |
| memory 提取 | 5 | extractMemories.ts:426 |
| side question | 1 | sideQuestion.ts:93 |

### 真正约束主循环的是 Token 预算，不是轮次

Claude Code 主循环的退出条件（`src/query.ts` 中的所有 `return` 语句）：

| 退出原因 | 触发条件 |
|---------|---------|
| `completed` | 模型不再调用工具（正常结束） |
| `aborted_streaming` | 用户 Ctrl+C 中断 |
| `max_turns` | 调用方设置了 maxTurns 且达到上限 |
| `blocking_limit` | token 数量达到阻塞限制 |
| `model_error` | 不可恢复的 API 错误 |
| `prompt_too_long` | 上下文过长且无法恢复 |

**关键发现**：在正常交互模式下，主循环的退出几乎全靠"模型主动停止调用工具"或"用户中断"。不是靠轮次计数。

### Token Budget Continuation（弹性续写机制）

当模型完成回复但没有用完 token 预算时，CC 会注入一条"催促"消息让模型继续工作：

```typescript
// src/query/tokenBudget.ts
// 如果已用 token < 预算的 90%，注入续写消息
const nudgeMessage = `Stopped at ${pct}% of token target. Keep working -- do not summarize.`

// 收益递减检测：连续 3 次续写后，每次新增 < 500 token 则停止
const isDiminishing =
  tracker.continuationCount >= 3 &&
  deltaSinceLastCheck < 500 &&
  tracker.lastDeltaTokens < 500
```

### 三套独立的限制系统共存

| 系统 | 作用范围 | 约束维度 |
|------|---------|---------|
| `maxTurns` | 子代理 / 特定调用 | 离散轮次计数 |
| `TOKEN_BUDGET`（客户端） | 主循环 | 输出 token 预算 + 弹性续写 |
| `taskBudget`（API 服务端） | API 调用 | 服务端强制 token 限制 |

## 设计启示

### CC 的设计思想

CC 的做法揭示了一个核心设计原则：**主循环和子代理应该用不同的限制策略**。

- **主循环**：不设轮次上限，靠 token 预算弹性控制。因为用户可能在做复杂任务（阅读大项目、多步调试），硬性轮次会截断有效工作。
- **子代理**：设轮次上限（1-200 不等）。因为子代理是"给我做一个具体的小任务"，应该有明确的完成边界。

### smolagents 的做法（对比）

smolagents 使用 `max_steps` 参数，默认值 20，纯外部计数器，触达后硬停止。这是一种**简化设计**——不区分主循环和子代理，统一用轮次限制。

**问题**：对于需要大量阅读文件的场景（学习大型开源项目），20 步可能远远不够。

## 对我们项目的建议

### 分两层实现

**第一层：安全兜底（立即实现）**

给主循环加一个**宽松的上限**，防止真正的无限循环（bug、模型异常），但值要大到不影响正常使用：

```python
# config.py
# 主循环的安全兜底，防止无限循环。正常使用不应触达此限制。
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "50"))
```

这不是 smolagents 的"到 20 步就停"，而是"到 50 步说明一定是出了问题"。

**第二层：Token 预算（后续实现）**

学 CC 的 token budget continuation，作为真正的弹性约束：

```python
# 伪代码 — 后续实现
if turn_output_tokens < TOKEN_BUDGET * 0.9:
    messages.append({
        "role": "user",
        "content": "你还没有完成全部工作，请继续。",
    })
    continue  # 继续循环
```

### 触达安全上限时的处理

不是硬截断，而是注入收尾提示让模型总结已有成果：

```python
if turn_count >= MAX_TURNS:
    console.print(f"[yellow]安全限制：已达到 {MAX_TURNS} 次迭代，正在收尾...[/yellow]")
    messages.append({
        "role": "user",
        "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"
    })
    break
```

### 计数粒度

只计工具调用迭代，最终流式回复不计入。

### 触达上限是诊断信号

正常使用不应触达安全兜底。如果频繁触达，说明：
- 模型陷入循环（反复执行相同命令）
- 工具返回信息不够，模型无法做决策
- 任务太复杂，需要拆分或提高上限

## 完整实现参考

### config.py — 新增配置

```python
# 主循环安全兜底，防止无限循环。正常使用不应触达此限制。
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "50"))
```

### agent.py — 改造 agent_loop

```python
from .config import MAX_TOKENS, MAX_TURNS, MODEL

def agent_loop(messages: list[dict[str, Any]]) -> None:
    """运行一次代理循环：调用 LLM，执行工具。"""
    client = create_llm_client()
    turn_count = 0

    with console.status("[bold green]正在思考..."):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            extra_body={"enable_thinking": True},
            max_tokens=MAX_TOKENS,
        )

    choice = response.choices[0]

    # 非工具调用响应 — 打印后结束
    if choice.finish_reason != "tool_calls":
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return

    # 工具调用循环
    while choice.finish_reason == "tool_calls":
        turn_count += 1

        # 安全兜底：触达上限时注入收尾提示
        if turn_count >= MAX_TURNS:
            console.print(f"[yellow]安全限制：已达到 {MAX_TURNS} 次迭代，正在收尾...[/yellow]")
            messages.append({
                "role": "user",
                "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"
            })
            break

        msg = choice.message
        msg_dict = msg.model_dump()

        # 如果有推理内容则打印
        reasoning = msg_dict.get("reasoning_content")
        if reasoning:
            console.print(Panel(reasoning, title="思考过程", border_style="dim"))

        # 将助手消息（含工具调用）添加到历史记录
        messages.append(msg_dict)

        # 执行所有工具调用
        tool_results: list[dict[str, str]] = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)

            # 处理 JSON 解析失败的情况
            if "_parse_error" in args:
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": args["_parse_error"],
                })
                console.print(f"[red]Parse error: {args['_parse_error']}[/red]")
                continue

            command = args["command"]
            console.print(f"\n[yellow]$ {command}[/yellow]")
            output = execute_tool("bash", args)
            console.print(output)

            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_results)

        # 携带工具结果再次调用 LLM
        with console.status("[bold green]正在思考..."):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                extra_body={"enable_thinking": True},
                max_tokens=MAX_TOKENS,
            )
        choice = response.choices[0]

    # 最终响应 — 以流式方式输出
    with console.status("[bold green]正在回复..."):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            extra_body={"enable_thinking": True},
            max_tokens=MAX_TOKENS,
            stream=True,
        )

    collected_content = ""
    live_text = Text()

    with Live(live_text, console=console, refresh_per_second=15):
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                collected_content += delta.content
                live_text.append(delta.content)

    messages.append({"role": "assistant", "content": collected_content})
```

## 参考资料

- Claude Code 源码 `src/query.ts` — 主循环实现，`maxTurns` 为可选参数
- Claude Code 源码 `src/query/tokenBudget.ts` — Token 预算续写机制
- Claude Code 源码 `src/QueryEngine.ts:684` — maxTurns 透传
- Claude Code 源码 `src/tasks/forkSubagent.ts:65` — 子代理 maxTurns: 200
- Claude Code 源码 `src/services/hooks/execAgentHook.ts:119` — Hook agent MAX_AGENT_TURNS = 50
- smolagents 框架：`max_steps` 参数，默认值 20
