# Agent Loop 代码质量重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Agent Loop 从单文件脚本重构为结构清晰的模块化实用工具，提升代码质量、安全性和用户体验。

**Architecture:** 将 `01_agent_loop.py` 拆分为 `config.py`（配置）、`llm.py`（客户端）、`tools.py`（工具）、`agent.py`（核心循环）、`main.py`（入口）五个模块。逐步迁移，每一步都产出可运行的代码。

**Tech Stack:** Python 3.12, OpenAI SDK, Rich

**Note:** 根据设计文档，本项目为个人工具，暂不添加单元测试。每个 task 通过手动运行 `python main.py` 验证。

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `.gitignore` | Create | 忽略 `.env`、`__pycache__/`、`.idea/` |
| `requirements.txt` | Create | 依赖声明 |
| `config.py` | Create | 环境变量 + 默认配置 |
| `llm.py` | Modify | 从 config 读取，去掉硬编码 Key |
| `tools.py` | Create | 工具 schema + bash 执行（改进安全检查） |
| `agent.py` | Create | Agent Loop 核心逻辑 + 消息打印 |
| `main.py` | Create | REPL 入口 |
| `01_agent_loop.py` | Delete | 被 main.py 替代 |
| `.env.example` | Already created | 环境变量模板 |

---

### Task 1: 基础设施 — .gitignore + requirements.txt + config.py

**Files:**
- Create: `.gitignore`
- Create: `requirements.txt`
- Create: `config.py`

- [ ] **Step 1: 创建 .gitignore**

```gitignore
.env
__pycache__/
.idea/
```

- [ ] **Step 2: 创建 requirements.txt**

```
openai
rich
```

- [ ] **Step 3: 创建 config.py**

```python
from __future__ import annotations

import os

API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")
MODEL: str = os.environ.get("LLM_MODEL", "deepseek-v3.2")
BASE_URL: str = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
BASH_TIMEOUT: int = int(os.environ.get("BASH_TIMEOUT", "120"))
```

- [ ] **Step 4: 验证 config.py 可导入**

Run: `cd /Users/kino/works/kino/harness && python -c "from config import MODEL; print(MODEL)"`
Expected: `deepseek-v3.2`

- [ ] **Step 5: Commit**

```bash
git add .gitignore requirements.txt config.py
git commit -m "feat: add config, requirements, and gitignore"
```

---

### Task 2: 重构 llm.py

**Files:**
- Modify: `llm.py`

- [ ] **Step 1: 重写 llm.py，从 config 读取**

将当前硬编码的 API Key 和 base_url 改为从 config 模块读取：

```python
from __future__ import annotations

from openai import OpenAI

from core.config import API_KEY, BASE_URL


def create_llm_client() -> OpenAI:
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)
```

- [ ] **Step 2: 验证可导入**

Run: `cd /Users/kino/works/kino/harness && python -c "from llm import create_llm_client; print(type(create_llm_client()))"`
Expected: `<class 'openai.OpenAI'>`

- [ ] **Step 3: Commit**

```bash
git add llm.py
git commit -m "refactor: llm.py reads config from config module, remove hardcoded key"
```

---

### Task 3: 创建 tools.py — 工具定义与执行

**Files:**
- Create: `tools.py`

- [ ] **Step 1: 创建 tools.py**

从 `01_agent_loop.py` 提取工具相关代码，改进 bash 安全检查：

```python
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from core.config import BASH_TIMEOUT

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

BLOCKED_COMMANDS: set[str] = {"mkfs", "dd"}
CONFIRM_COMMANDS: set[str] = {"rm", "sudo", "shutdown", "reboot", "halt", "init"}


def _extract_command_name(command: str) -> str:
    """Extract the base command name from a shell command string."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    # Get basename of the command path (e.g. /sbin/shutdown -> shutdown)
    return parts[0].rsplit("/", 1)[-1]


def _is_blocked(command: str) -> bool:
    """Check if the command is in the blocked list."""
    cmd_name = _extract_command_name(command)
    return cmd_name in BLOCKED_COMMANDS


def _needs_confirmation(command: str) -> bool:
    """Check if the command needs user confirmation before execution."""
    cmd_name = _extract_command_name(command)
    return cmd_name in CONFIRM_COMMANDS


def run_bash(command: str) -> str:
    if _is_blocked(command):
        return "Error: Command blocked for safety."

    if _needs_confirmation(command):
        answer = input(f"\033[31m⚠ Command '{command}' looks dangerous. Run anyway? [y/N]: \033[0m")
        if answer.strip().lower() not in ("y", "yes"):
            return "Error: Command cancelled by user."

    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )
        out = (r.stdout + r.stderr).strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({BASH_TIMEOUT}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name with the given arguments."""
    if name == "bash":
        return run_bash(arguments["command"])
    return f"Error: Unknown tool '{name}'"
```

- [ ] **Step 2: 验证 tools.py 可导入且安全检查工作**

Run: `cd /Users/kino/works/kino/harness && python -c "from tools import _is_blocked, _needs_confirmation, execute_tool; print(_is_blocked('mkfs /dev/sda1'), _needs_confirmation('rm foo.txt'), execute_tool('bash', {'command': 'echo hello'}))"`
Expected: `True True hello`

- [ ] **Step 3: Commit**

```bash
git add tools.py
git commit -m "feat: add tools.py with improved bash safety checks"
```

---

### Task 4: 创建 agent.py — Agent Loop 核心逻辑

**Files:**
- Create: `agent.py`

- [ ] **Step 1: 创建 agent.py**

从 `01_agent_loop.py` 提取 agent loop 逻辑，修复消息历史 bug 和 JSON 解析问题：

```python
from __future__ import annotations

import json
from typing import Any

from core.config import MAX_TOKENS, MODEL
from core.llm import create_llm_client
from core.tools import TOOLS, execute_tool


def _parse_tool_args(raw: str) -> dict[str, Any]:
    """Parse tool call arguments, returning error dict on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"_parse_error": f"Invalid JSON: {e}"}


def print_response(msg: dict[str, Any]) -> None:
    """Print assistant message with optional reasoning content."""
    reasoning = msg.get("reasoning_content")
    if reasoning:
        print("\n" + "=" * 20 + " 思考过程 " + "=" * 20 + "\n")
        print(reasoning)
        print("\n" + "=" * 20 + " 完整回复 " + "=" * 20 + "\n")

    content = msg.get("content")
    if content:
        print(content)


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """Run one iteration of the agent loop: call LLM, execute tools, repeat."""
    client = create_llm_client()

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        extra_body={"enable_thinking": True},
        max_tokens=MAX_TOKENS,
    )

    choice = response.choices[0]

    # Non-tool response — print and done
    if choice.finish_reason != "tool_calls":
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return

    # Tool calls loop
    while choice.finish_reason == "tool_calls":
        msg = choice.message
        msg_dict = msg.model_dump()

        # Print reasoning if present
        reasoning = msg_dict.get("reasoning_content")
        if reasoning:
            print("\n" + "=" * 20 + " 思考过程 " + "=" * 20 + "\n")
            print(reasoning)

        # Add assistant message (with tool_calls) to history
        messages.append(msg_dict)

        # Execute all tool calls
        tool_results: list[dict[str, str]] = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)

            # Handle JSON parse failure
            if "_parse_error" in args:
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": args["_parse_error"],
                })
                print(f"\n\033[31mParse error: {args['_parse_error']}\033[0m")
                continue

            command = args["command"]
            print(f"\n\033[33m$ {command}\033[0m")
            output = execute_tool("bash", args)
            print(output)

            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_results)

        # Call LLM again with tool results
        print("\n" + "=" * 20 + " 思考过程 " + "=" * 20 + "\n")
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            extra_body={"enable_thinking": True},
            max_tokens=MAX_TOKENS,
        )
        choice = response.choices[0]

    # Final response
    final_dict = choice.message.model_dump()
    print_response(final_dict)
    messages.append({"role": "assistant", "content": final_dict.get("content") or ""})
```

- [ ] **Step 2: 验证 agent.py 可导入**

Run: `cd /Users/kino/works/kino/harness && python -c "from agent import agent_loop, print_response; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent.py
git commit -m "feat: add agent.py with fixed message history and JSON error handling"
```

---

### Task 5: 创建 main.py — REPL 入口

**Files:**
- Create: `main.py`

- [ ] **Step 1: 创建 main.py**

```python
from __future__ import annotations

from core.agent import agent_loop

SYSTEM_PROMPT = "无论如何你都要使用中文回答用户"


def main() -> None:
    history: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    print("Agent Loop 已启动。输入 exit 或 quit 退出。\n")

    while True:
        try:
            query = input(">> ")
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break

        if query.strip().lower() in ("exit", "quit"):
            print("再见！")
            break

        if not query.strip():
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证 main.py 可启动（无 LLM 调用测试，仅检查导入）**

Run: `cd /Users/kino/works/kino/harness && python -c "from main import main; print('OK')"`
Expected: `OK`

- [ ] **Step 3: 删除旧文件 `01_agent_loop.py`**

```bash
rm 01_agent_loop.py
```

- [ ] **Step 4: 确保设置环境变量后可以运行**

Run: `cd /Users/kino/works/kino/harness && DASHSCOPE_API_KEY=sk-test python -c "from main import main; print('Import OK')"`
Expected: `Import OK`

- [ ] **Step 5: Commit**

```bash
git add main.py
git rm 01_agent_loop.py
git commit -m "feat: add main.py REPL entry point, remove old 01_agent_loop.py"
```

---

### Task 6: Rich 格式化输出

**Files:**
- Modify: `agent.py`

- [ ] **Step 1: 安装 rich（如果尚未安装）**

Run: `pip install rich`

- [ ] **Step 2: 更新 agent.py，用 Rich 替换 print 语句**

在文件顶部添加导入：

```python
from rich.console import Console
from rich.panel import Panel

console = Console()
```

替换 `print_response` 函数：

```python
def print_response(msg: dict[str, Any]) -> None:
    """Print assistant message with optional reasoning content."""
    reasoning = msg.get("reasoning_content")
    if reasoning:
        console.print(Panel(reasoning, title="思考过程", border_style="dim"))

    content = msg.get("content")
    if content:
        console.print(content)
```

替换 `agent_loop` 中 LLM 调用部分，用 `Status` spinner 包裹。将 `agent_loop` 函数改为：

```python
def agent_loop(messages: list[dict[str, Any]]) -> None:
    """Run one iteration of the agent loop: call LLM, execute tools, repeat."""
    client = create_llm_client()

    with console.status("[bold green]正在思考..."):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            extra_body={"enable_thinking": True},
            max_tokens=MAX_TOKENS,
        )

    choice = response.choices[0]

    # Non-tool response — print and done
    if choice.finish_reason != "tool_calls":
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return

    # Tool calls loop
    while choice.finish_reason == "tool_calls":
        msg = choice.message
        msg_dict = msg.model_dump()

        # Print reasoning if present
        reasoning = msg_dict.get("reasoning_content")
        if reasoning:
            console.print(Panel(reasoning, title="思考过程", border_style="dim"))

        # Add assistant message (with tool_calls) to history
        messages.append(msg_dict)

        # Execute all tool calls
        tool_results: list[dict[str, str]] = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)

            # Handle JSON parse failure
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

        # Call LLM again with tool results
        with console.status("[bold green]正在思考..."):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                extra_body={"enable_thinking": True},
                max_tokens=MAX_TOKENS,
            )
        choice = response.choices[0]

    # Final response
    final_dict = choice.message.model_dump()
    print_response(final_dict)
    messages.append({"role": "assistant", "content": final_dict.get("content") or ""})
```

同时更新 `main.py` 的提示信息，用 Rich 打印：

```python
from rich.console import Console

console = Console()
```

将 `main()` 中的 `print` 改为 `console.print`：
- `print("Agent Loop 已启动...")` → `console.print("[bold green]Agent Loop 已启动。[/bold green] 输入 [dim]exit[/dim] 或 [dim]quit[/dim] 退出。\n")`
- `print("\n再见！")` → `console.print("\n[dim]再见！[/dim]")`
- `print("再见！")` → `console.print("[dim]再见！[/dim]")`

- [ ] **Step 3: 验证导入正常**

Run: `cd /Users/kino/works/kino/harness && python -c "from main import main; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add agent.py main.py
git commit -m "feat: add Rich formatting for console output"
```

---

### Task 7: 流式输出

**Files:**
- Modify: `agent.py`

- [ ] **Step 1: 在 agent.py 中添加流式输出支持**

在文件顶部添加：

```python
from rich.live import Live
from rich.text import Text
```

在 `agent_loop` 中，将最终回复部分（`finish_reason != "tool_calls"` 的情况）改为流式：

```python
    # Non-tool response — stream it
    if choice.finish_reason != "tool_calls":
        # For the first call, check if it's already done (non-streaming)
        # Fall through to stream the final response after tool calls
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return
```

这段暂不改动。流式输出改为在 **最终回复**（循环结束后）使用。将 agent_loop 末尾的最终回复部分替换为：

```python
    # Final response — stream it
    with console.status("[bold green]正在回复..."):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
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

同时将 `while` 循环内的再次调用改为在最后一步不调用，而是在循环结束后统一流式输出。完整的重构后 `agent_loop`：

```python
def agent_loop(messages: list[dict[str, Any]]) -> None:
    """Run one iteration of the agent loop: call LLM, execute tools, repeat."""
    client = create_llm_client()

    with console.status("[bold green]正在思考..."):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            extra_body={"enable_thinking": True},
            max_tokens=MAX_TOKENS,
        )

    choice = response.choices[0]

    # Non-tool response — print and done
    if choice.finish_reason != "tool_calls":
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return

    # Tool calls loop
    while choice.finish_reason == "tool_calls":
        msg = choice.message
        msg_dict = msg.model_dump()

        reasoning = msg_dict.get("reasoning_content")
        if reasoning:
            console.print(Panel(reasoning, title="思考过程", border_style="dim"))

        messages.append(msg_dict)

        tool_results: list[dict[str, str]] = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)

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

        with console.status("[bold green]正在思考..."):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                extra_body={"enable_thinking": True},
                max_tokens=MAX_TOKENS,
            )
        choice = response.choices[0]

    # Final response — stream it
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

注意：这里做了一个权衡——while 循环最后一次非流式调用已经拿到了完整回复，但我们选择丢弃它，再发起一次流式调用（不传 `tools`）。这多消耗一次 API 调用，但换来流式输出的用户体验。如果后续觉得浪费，可以去掉流式，直接用 `choice.message.model_dump()` 打印。

- [ ] **Step 2: 验证导入正常**

Run: `cd /Users/kino/works/kino/harness && python -c "from main import main; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent.py
git commit -m "feat: add streaming output for final LLM response"
```

---

### Task 8: 最终验证与清理

**Files:**
- Verify all files

- [ ] **Step 1: 确认文件结构正确**

Run: `cd /Users/kino/works/kino/harness && ls -la *.py`
Expected: 看到 `config.py`, `llm.py`, `tools.py`, `agent.py`, `main.py`，没有 `01_agent_loop.py`

- [ ] **Step 2: 确认所有模块可导入**

Run: `cd /Users/kino/works/kino/harness && python -c "from config import MODEL; from llm import create_llm_client; from tools import execute_tool, TOOLS; from agent import agent_loop; from main import main; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: 确认 .env.example 存在**

Run: `cat /Users/kino/works/kino/harness/.env.example`
Expected: 显示环境变量模板

- [ ] **Step 4: 端到端测试（手动）**

设置环境变量 `DASHSCOPE_API_KEY` 后运行：
```bash
cd /Users/kino/works/kino/harness && python main.py
```
验证：
1. 显示启动消息
2. 输入 "你好"，能收到中文回复
3. 输入 "运行 echo hello"，能调用 bash 工具并返回结果
4. 输入 "exit" 能正常退出
5. Ctrl+C 能正常退出

- [ ] **Step 5: 最终 Commit**

```bash
git add -A
git commit -m "chore: final cleanup and verification"
```
