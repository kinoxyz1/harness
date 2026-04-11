# Agent 模块重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 core/agent.py 从 705 行拆分为 5 个独立模块，使用 Protocol 定义 4 个插件接口，保持功能不变，实现可替换、可测试、可扩展的架构。

**Architecture:** 用 `typing.Protocol` 定义接口（LLMClient、ContextPlugin、Renderer、Planner），各实现模块满足方法签名即可。`AgentLoop` 类通过组合持有各接口实例。依赖关系单向：`interfaces.py` → 实现模块 → `agent.py`。

**Tech Stack:** Python 3.10+, Protocol, rich, pytest

---

## Task 1: 测试基础设施 + interfaces.py

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_interfaces.py`
- Create: `core/interfaces.py`

- [ ] **Step 1: 创建 tests 目录和空 `__init__.py`**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 2: 写 Renderer Protocol 的测试**

在 `tests/test_interfaces.py` 中：

```python
"""验证 Protocol 定义可以被正确实现。"""
from __future__ import annotations

from core.interfaces import LLMClient, ContextPlugin, Renderer, Planner
from core.todo import TodoItem, TodoManager


class TestRendererProtocol:
    """测试 Renderer Protocol 可以被正确实现。"""

    def test_minimal_implementation(self):
        class MinimalRenderer:
            def show_thinking(self, title: str, reasoning: str) -> None: pass
            def show_assistant(self, content: str) -> None: pass
            def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None: pass
            def show_current_todo(self, item: TodoItem, completed: int, total: int) -> None: pass
            def show_progress(self, items: list) -> None: pass
            def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None: pass
            def show_tool_call(self, name: str, args: dict) -> None: pass
            def show_tool_result(self, name: str, output: str) -> None: pass
            def show_error(self, message: str) -> None: pass
            def show_status(self, message: str) -> None: pass

        renderer = MinimalRenderer()
        # Protocol 验证：如果能赋值给 Renderer 类型，说明实现正确
        assert isinstance(renderer, Renderer)

    def test_context_plugin_protocol(self):
        class TestPlugin:
            def inject(self, messages: list[dict]) -> None: pass

        plugin = TestPlugin()
        assert isinstance(plugin, ContextPlugin)

    def test_llm_client_protocol(self):
        class TestClient:
            def call(self, messages: list[dict], tools: list[dict] | None = None): pass

        client = TestClient()
        assert isinstance(client, LLMClient)

    def test_planner_protocol(self):
        class TestPlanner:
            def plan(self, messages: list[dict], client) -> TodoManager | None: pass

        planner = TestPlanner()
        assert isinstance(planner, Planner)
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_interfaces.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'core.interfaces'`

- [ ] **Step 4: 实现 interfaces.py**

```python
"""Agent 插件接口定义。

四个 Protocol：LLMClient、ContextPlugin、Renderer、Planner。
实现模块只需满足方法签名即可，不需要继承这些 Protocol。
"""
from __future__ import annotations

from typing import Any


class LLMClient(Protocol):
    """LLM 调用抽象。"""

    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Any:
        """调用 LLM，返回 LLMResponse。"""
        ...


class ContextPlugin(Protocol):
    """上下文注入插件。每个插件负责向 messages 注入一类上下文。"""

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """将上下文注入到 messages 中。需要自行保证幂等性。"""
        ...


class Renderer(Protocol):
    """显示抽象。所有终端输出都通过此接口。"""

    def show_thinking(self, title: str, reasoning: str) -> None:
        """显示推理/思考过程。"""
        ...

    def show_assistant(self, content: str | None) -> None:
        """显示助手文字内容。"""
        ...

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        """显示 LLM 调用计时信息。"""
        ...

    def show_current_todo(self, item: Any, completed: int, total: int) -> None:
        """显示当前聚焦的 todo。item 为 TodoItem。"""
        ...

    def show_progress(self, items: list[Any]) -> None:
        """显示完整进度概览。items 为 TodoItem 列表。"""
        ...

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        """显示任务完成总结面板。"""
        ...

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """显示工具调用开始。"""
        ...

    def show_tool_result(self, name: str, output: str) -> None:
        """显示工具执行结果。"""
        ...

    def show_error(self, message: str) -> None:
        """显示错误信息。"""
        ...

    def show_status(self, message: str) -> None:
        """显示状态信息（灰色 dim）。"""
        ...


class Planner(Protocol):
    """规划策略抽象。"""

    def plan(self, messages: list[dict[str, Any]], client: LLMClient) -> Any | None:
        """分析用户消息，返回 TodoManager 或 None（直接回答）。"""
        ...
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_interfaces.py -v
```

Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add tests/__init__.py tests/test_interfaces.py core/interfaces.py
git commit -m "feat(refactor): add Protocol interfaces for agent module"
```

---

## Task 2: 抽出 llm_client.py

**Files:**
- Create: `core/llm_client.py`
- Modify: `core/agent.py` — 删除搬出的代码，改为 import

- [ ] **Step 1: 创建 `core/llm_client.py`**

```python
"""LLM 调用层封装。"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .config import MODEL, MAX_TOKENS, ENABLE_THINKING, SHOW_THINKING
from .llm import create_llm_client
from .protocol import normalize_messages


class LLMResponse:
    """对 API response 的结构化封装。

    所有调用者通过这个类访问响应，不需要关心底层 SDK 的差异。
    提供 has_content / is_tool_call / is_truncated 等语义化属性，
    而不是让调用者自己去猜 finish_reason。
    """

    def __init__(self, response) -> None:
        self._response = response
        self._choice = response.choices[0]
        self._msg = self._choice.message
        self._finish_reason = self._choice.finish_reason

        # 安全提取字段（兼容不同 SDK 版本）
        self.content: str | None = self._msg.content if hasattr(self._msg, "content") else None
        self.reasoning: str | None = getattr(self._msg, "reasoning_content", None)
        self.tool_calls = getattr(self._msg, "tool_calls", None)
        self.finish_reason: str = self._finish_reason or "unknown"

        # token 用量
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        if hasattr(response, "usage") and response.usage:
            self.prompt_tokens = response.usage.prompt_tokens or 0
            self.completion_tokens = response.usage.completion_tokens or 0

    @property
    def has_content(self) -> bool:
        """是否有用户可见的文字内容（忽略纯空白）。"""
        return bool(self.content and self.content.strip())

    @property
    def is_tool_call(self) -> bool:
        """模型是否请求工具调用。"""
        if self.finish_reason == "tool_calls":
            return True
        # 防御：某些思考模型可能返回 tool_calls 但 finish_reason 不是 "tool_calls"
        if self.tool_calls and not self.has_content:
            return True
        return False

    @property
    def is_truncated(self) -> bool:
        """模型是否被 token 限制截断。"""
        return self.finish_reason == "length"

    @property
    def raw_response(self) -> Any:
        """原始 response 对象（需要时使用）。"""
        return self._response

    def to_message_dict(self) -> dict[str, Any]:
        """转换为可追加到 messages 的字典。"""
        try:
            return self._msg.model_dump()
        except Exception:
            d: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
            if self.tool_calls:
                d["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in self.tool_calls
                ]
            return d


class OpenAIClient:
    """OpenAI API 客户端，实现 LLMClient 协议。"""

    def __init__(self, console: Console | None = None) -> None:
        self._client = create_llm_client()
        self._console = console or Console()

    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, stream: bool = False) -> LLMResponse:
        """调用 LLM API，返回结构化的 LLMResponse。

        显示动态计时，完成后打印耗时和 token 用量。
        """
        params = {
            "model": MODEL,
            "messages": normalize_messages(messages, enable_thinking=ENABLE_THINKING),
            "extra_body": {"enable_thinking": ENABLE_THINKING, "parallel_tool_calls": True},
            "max_tokens": MAX_TOKENS,
        }
        if tools:
            params["tools"] = tools
        if stream:
            params["stream"] = True

        # 流式调用：直接返回（暂不封装 LLMResponse）
        if stream:
            raw = self._client.chat.completions.create(**params)
            return raw  # type: ignore[return-value]

        # 非流式调用：API 放后台线程，主线程负责刷新计时显示
        result: dict[str, Any] = {}
        error: dict[str, Any] = {}

        def do_call():
            try:
                result["data"] = self._client.chat.completions.create(**params)
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

        llm_resp = LLMResponse(response)

        self._console.print(
            f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
            f" │ finish={llm_resp.finish_reason}[/dim]"
        )

        return llm_resp


def _parse_tool_args(raw: str | None) -> dict[str, Any]:
    """解析工具调用参数，解析失败时返回包含错误信息的字典。"""
    if raw is None:
        return {"_parse_error": "Arguments is None"}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {"_parse_error": f"Invalid JSON: {e}"}
```

- [ ] **Step 2: 修改 `core/agent.py` 删除搬出的代码，添加 import**

在文件顶部添加：
```python
from .llm_client import LLMResponse, OpenAIClient, _parse_tool_args
```

删除原文件中的：
- `class LLMResponse`（原 L27-91）
- `_call_llm` 函数（原 L93-153）
- `_parse_tool_args` 函数（原 L159-166）
- 所有用到 `console` 全局变量的地方改为从参数传入或删除

- [ ] **Step 3: 验证 import 正常**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.llm_client import LLMResponse, OpenAIClient; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add core/llm_client.py core/agent.py
git commit -m "feat(refactor): extract llm_client.py from agent.py"
```

---

## Task 3: 抽出 renderer.py

**Files:**
- Create: `core/renderer.py`
- Modify: `core/agent.py` — 删除 `_print_assistant_msg`，改为使用 Renderer

- [ ] **Step 1: 创建 `core/renderer.py`**

```python
"""显示渲染实现。"""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

from .interfaces import Renderer
from .todo import TodoItem, TodoStatus
from .config import SHOW_THINKING


class RichRenderer:
    """基于 Rich Console 的终端渲染器。"""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def show_thinking(self, title: str, reasoning: str) -> None:
        """显示推理/思考过程。"""
        if SHOW_THINKING and reasoning and reasoning.strip():
            self._console.print(Panel(reasoning.strip(), title=title, border_style="dim"))

    def show_assistant(self, content: str | None) -> None:
        """显示助手文字内容。"""
        if content and content.strip():
            print(content)

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        """显示 LLM 调用计时信息。"""
        self._console.print(
            f"[dim]{elapsed:.1f}s │ token {prompt_tokens}↓ {completion_tokens}↑"
            f" │ finish={finish_reason}[/dim]"
        )

    def show_current_todo(self, item: TodoItem, completed: int, total: int) -> None:
        """显示当前聚焦的 todo。"""
        self._console.print(
            f"[bold dim]▶ #{item.id} {item.content}  ({completed}/{total})[/bold dim]"
        )

    def show_progress(self, items: list[TodoItem]) -> None:
        """显示完整进度概览。"""
        completed = sum(1 for item in items if item.status == TodoStatus.COMPLETED)
        total = len(items)
        self._console.print(f"[bold dim]进度 ({completed}/{total}):[/bold dim]")
        for item in items:
            if item.status == TodoStatus.COMPLETED:
                icon = "[green]✓[/green]"
            elif item.status == TodoStatus.FAILED:
                icon = "[red]✗[/red]"
            else:
                icon = "[dim]○[/dim]"
            self._console.print(f"  {icon} [dim]{item.id}. {item.content}[/dim]")

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        """显示任务完成总结面板。"""
        self._console.print(Panel(
            f"[bold green]所有任务已完成[/bold green]\n\n"
            f"完成: {completed}/{total} 个任务\n"
            f"耗时: {elapsed:.1f}s",
            title="任务总结",
            border_style="green",
        ))

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """显示工具调用开始。"""
        self._console.print(f"\n[yellow]$ {args.get('command', name)}[/yellow]")

    def show_tool_result(self, name: str, output: str) -> None:
        """显示工具执行结果。"""
        print(output)

    def show_error(self, message: str) -> None:
        """显示错误信息。"""
        self._console.print(f"[red]{message}[/red]")

    def show_status(self, message: str) -> None:
        """显示状态信息（灰色 dim）。"""
        self._console.print(f"[dim]{message}[/dim]")
```

- [ ] **Step 2: 修改 `core/agent.py` 使用 Renderer**

删除原 `_print_assistant_msg` 函数，所有显示调用改为通过 Renderer 实例。

- [ ] **Step 3: 验证**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.renderer import RichRenderer; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add core/renderer.py core/agent.py
git commit -m "feat(refactor): extract renderer.py from agent.py"
```

---

## Task 4: 扩展 context.py + 抽出 planner.py

**Files:**
- Modify: `core/context.py` — 新增 ContextPipeline 和 Plugins
- Create: `core/planner.py` — 从 agent.py 搬出规划逻辑
- Modify: `core/agent.py` — 删除规划代码，改为 import

- [ ] **Step 1: 扩展 `core/context.py`**

在文件末尾追加：

```python
from .interfaces import ContextPlugin


class ContextPipeline:
    """上下文注入管道。管理多个 ContextPlugin，按注册顺序执行。"""

    def __init__(self) -> None:
        self._plugins: list[ContextPlugin] = []

    def register(self, plugin: ContextPlugin) -> None:
        """注册一个插件。"""
        self._plugins.append(plugin)

    def inject_all(self, messages: list[dict[str, Any]]) -> None:
        """执行所有已注册插件的注入。"""
        for plugin in self._plugins:
            plugin.inject(messages)


class SystemContextPlugin:
    """注入系统提示词。幂等（marker 检查）。"""

    def __init__(self, project_root: str | None = None) -> None:
        self._project_root = project_root or os.getcwd()

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """将通用系统提示词追加到已有的系统消息中。"""
        marker = "<!-- system-context-injected -->"
        for msg in messages:
            if msg.get("role") == "system" and marker in (msg.get("content") or ""):
                return

        system_ctx = get_system_context(self._project_root)

        for msg in messages:
            if msg.get("role") == "system":
                existing = msg.get("content") or ""
                msg["content"] = f"{existing}\n\n{marker}\n\n{system_ctx}"
                return

        messages.insert(0, {
            "role": "system",
            "content": f"{marker}\n\n{system_ctx}",
        })


class UserContextPlugin:
    """注入环境信息。幂等（marker 检查）。"""

    def __init__(self, working_dir: str | None = None) -> None:
        self._working_dir = working_dir or os.getcwd()

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """在消息列表中注入环境信息。"""
        marker = "<!-- user-context-injected -->"
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content", "").startswith(marker):
                return

        user_ctx = get_user_context(self._working_dir)
        content = f"{marker}\n{user_ctx}"

        insert_pos = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                insert_pos = i
                break
        else:
            insert_pos = len(messages)

        messages.insert(insert_pos, {"role": "user", "content": content})
```

- [ ] **Step 2: 创建 `core/planner.py`**

```python
"""规划策略实现。"""
from __future__ import annotations

import re
from typing import Any

from .interfaces import Planner, Renderer
from .llm_client import LLMResponse
from .todo import TodoManager


_PLAN_PROMPT = """\
你是一个任务规划助手。分析用户的请求，输出执行计划。

规则：
- 每个任务必须是具体的、可执行的操作步骤（如"读取文件X"、"编写函数Y"、"运行测试"）
- 不要输出建议、对话、说明性内容——只要可执行的步骤
- 任务数量控制在 3-7 个，粒度适中
- 按执行顺序排列

输出格式：
PLAN:
1. [具体操作描述]
2. [具体操作描述]
...

如果你可以直接回答而无需执行任何操作，输出：
DIRECT: [你的回答]
"""


def _parse_plan_response(text: str) -> TodoManager | None:
    """解析规划阶段的 LLM 输出。"""
    text = text.strip()

    # 检查是否包含 PLAN:（不要求必须在开头）
    plan_match = re.search(r'PLAN:\s*\n', text, re.IGNORECASE)
    direct_match = re.search(r'DIRECT:', text, re.IGNORECASE)

    # 如果只有 DIRECT 没有 PLAN，返回 None
    if not plan_match and direct_match:
        return None

    # 都没有 — 无法识别，返回 None
    if not plan_match:
        return None

    plan_text = text[plan_match.end():].strip()
    items = []
    for line in plan_text.split("\n"):
        line = line.strip()
        match = re.match(r'(\d+[\.\)]\s*|-)\s*(.+)', line)
        if match:
            items.append(match.group(2).strip())

    if not items:
        return None

    manager = TodoManager()
    for item_text in items:
        manager.add(item_text)
    return manager


class DefaultPlanner:
    """默认规划策略：单次 LLM 调用 + 文本解析。"""

    def __init__(self, renderer: Renderer | None = None) -> None:
        self._renderer = renderer

    def plan(self, messages: list[dict[str, Any]], client) -> TodoManager | None:
        """规划阶段：让 LLM 分析任务并产出 TodoManager。"""
        plan_messages = [{"role": "system", "content": _PLAN_PROMPT}]

        for msg in reversed(messages):
            if msg.get("role") == "user" and "<!-- " not in (msg.get("content") or ""):
                plan_messages.append(msg)
                break

        try:
            llm_resp = client.call(plan_messages)
        except Exception as e:
            if self._renderer:
                self._renderer.show_status(f"规划阶段失败: {e}，直接进入执行")
            return None

        if not llm_resp.has_content:
            return None

        # 展示规划思考过程
        if llm_resp.reasoning and llm_resp.reasoning.strip():
            if self._renderer:
                self._renderer.show_thinking("规划思考", llm_resp.reasoning.strip())

        return _parse_plan_response(llm_resp.content or "")
```

- [ ] **Step 3: 修改 `core/agent.py` 删除规划代码**

删除：
- `_PLAN_PROMPT`
- `_parse_plan_response`
- `plan_phase`

添加 import：
```python
from .planner import DefaultPlanner, _parse_plan_response
```

- [ ] **Step 4: 验证**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.planner import DefaultPlanner; print('OK')"
cd /Users/kino/works/kino/harness && python -c "from core.context import ContextPipeline, SystemContextPlugin, UserContextPlugin; print('OK')"
```

- [ ] **Step 5: 提交**

```bash
git add core/context.py core/planner.py core/agent.py
git commit -m "feat(refactor): extract planner.py, extend context.py with pipeline"
```

---

## Task 5: 改造 agent.py 为 AgentLoop 类

**Files:**
- Modify: `core/agent.py` — 重构为 AgentLoop 类 + 向后兼容函数
- Modify: `01_agent_loop.py` — 可选，改为显式装配

- [ ] **Step 1: 重写 `core/agent.py`**

保留原有所有逻辑，但改为类结构。关键点：
- `AgentLoop` 类持有 `_llm`, `_renderer`, `_planner`, `_context`, `_tools_schema`
- `run()` 方法 = 原 `agent_loop()`
- `_execute_tool_turn()`、`_ensure_final_response()`、`_run_tool_loop()` = 原同名函数，但改用实例方法调用
- `TodoContextPlugin` 类 = 原 `_inject_todo_context` 的面向对象封装
- 保留模块级 `agent_loop()` 函数作为向后兼容入口

- [ ] **Step 2: 运行现有测试确认无回归**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/ -v
```

- [ ] **Step 3: 提交**

```bash
git add core/agent.py
git commit -m "feat(refactor): transform agent.py to AgentLoop class"
```

---

## Task 6: 可选 — 更新入口文件

**Files:**
- Modify: `01_agent_loop.py` — 改为显式装配

- [ ] **Step 1: 更新 `01_agent_loop.py`**

```python
from __future__ import annotations

from rich.console import Console

from core.llm_client import OpenAIClient
from core.context import ContextPipeline, SystemContextPlugin, UserContextPlugin
from core.renderer import RichRenderer
from core.planner import DefaultPlanner
from core.agent import AgentLoop, TodoContextPlugin

console = Console()

SYSTEM_PROMPT = "无论如何你都要使用中文回答用户"


def main() -> None:
    # 装配依赖（循环外一次）
    renderer = RichRenderer(console)
    llm = OpenAIClient(console)
    planner = DefaultPlanner(renderer)

    history: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    console.print("[bold green]Agent Loop 已启动。[/bold green] 输入 [dim]exit[/dim] 或 [dim]quit[/dim] 退出。\n")

    while True:
        try:
            query = input(">> ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]再见！[/dim]")
            break

        if query.strip().lower() in ("exit", "quit"):
            console.print("[dim]再见！[/dim]")
            break

        if not query.strip():
            continue

        history.append({"role": "user", "content": query})

        # 规划
        todos = planner.plan(history, llm)
        if todos:
            renderer.show_status(f"计划: {len(todos.items)} 个任务")
            for item in todos.items:
                renderer.show_status(f"  {item.id}. {item.content}")
            print()

        # 装配 context pipeline（每轮重建，因为 todo 可能不同）
        context = ContextPipeline()
        context.register(SystemContextPlugin())
        context.register(UserContextPlugin())
        if todos:
            context.register(TodoContextPlugin(todos))

        # 执行
        AgentLoop(llm, renderer, planner, context).run(history, todo_manager=todos)
        print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行端到端测试**

```bash
cd /Users/kino/works/kino/harness && python 01_agent_loop.py
# 输入简单问题测试 DIRECT 路径
# 输入多步任务测试 PLAN 路径
```

- [ ] **Step 3: 提交**

```bash
git add 01_agent_loop.py
git commit -m "feat(refactor): update entry point to use explicit dependency injection"
```

---

## Self-Review 检查清单

**1. Spec 覆盖:**
- [x] `interfaces.py` 4 个 Protocol — Task 1
- [x] `llm_client.py` LLMResponse + OpenAIClient — Task 2
- [x] `renderer.py` RichRenderer — Task 3
- [x] `context.py` ContextPipeline + Plugins — Task 4
- [x] `planner.py` DefaultPlanner — Task 4
- [x] `agent.py` AgentLoop 类 — Task 5
- [x] 向后兼容 `agent_loop()` 函数 — Task 5
- [x] 可选入口更新 — Task 6

**2. 类型一致性:**
- `LLMClient.call` 返回 `Any`（避免 interfaces 依赖实现）
- `Renderer.show_current_todo` 接收 `item: Any`（避免 interfaces 依赖 todo）
- `Renderer.show_progress` 接收 `items: list[Any]`（同上）
- 实际实现中使用 `TodoItem` 具体类型

**3. 依赖方向:**
- `interfaces.py` ← 无依赖 ✓
- `llm_client.py` ← interfaces, config ✓
- `renderer.py` ← interfaces, todo ✓
- `planner.py` ← interfaces, todo ✓
- `context.py` ← interfaces, todo ✓
- `agent.py` ← 所有上面 ✓

---

## 执行选择

**Plan complete and saved to `docs/specs/2026-04-12-agent-refactor-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?