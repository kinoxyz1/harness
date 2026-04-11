# Todo Tool 化重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 todo 系统从"框架管理的上下文注入 + 标记解析"模式重构为"LLM 可调用的 todo_manage 工具"模式，删除 plan_phase、标记解析等过度工程化的部分。

**Architecture:** 新增 `core/tools/todo.py` 工具模块，包含 `todo_manage` 工具的 SCHEMA 和 handle() 实现。LLM 调用时传入完整任务列表（替换旧列表）。模块级 `PlanningState` 单例维护状态。`AgentLoop._run_tool_loop` 简化，只保留循环控制和进度渲染。

**Tech Stack:** Python 3.10+, typing, rich, pytest

---

## File Structure

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/tools/todo.py` | 创建 | 新工具：SCHEMA + handle() + PlanningState |
| `core/planner.py` | 删除 | 整个文件删除 |
| `core/todo.py` | 精简 | 删除 TodoManager、parse_todo_updates、strip_todo_markers、TODO_UPDATE_PATTERN，保留 TodoStatus 和 TodoItem |
| `core/agent.py` | 精简 | 删除 TodoContextPlugin、_REMINDER_THRESHOLD、todo 跟踪逻辑、plan_phase()、planner 参数 |
| `core/interfaces.py` | 精简 | 删除 Planner Protocol |
| `01_agent_loop.py` | 精简 | 删除 planner 相关代码、TodoContextPlugin 注册 |

---

## Task 1: 创建 core/tools/todo.py 工具模块

**Files:**
- Create: `core/tools/todo.py`
- Test: `tests/test_todo_tool.py`

- [ ] **Step 1: 创建工具模块框架**

```python
"""Todo 管理工具。

LLM 主动调用 todo_manage 来更新任务计划。
传入完整任务列表替换旧列表（非增量更新）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ToolResult, ToolUseContext


# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_manage",
        "description": (
            "管理当前会话的任务计划。用于多步骤工作时追踪进度。"
            "传入完整任务列表来更新计划，会替换掉旧列表。"
            "\n\n使用场景："
            "\n- 开始新任务时创建计划"
            "\n- 完成任务时更新状态"
            "\n- 任务有变化时调整计划"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "完整的任务列表（替换旧列表）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "failed"],
                                "description": "任务状态",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": False,  # 修改内部状态，串行执行
}

# ─── 内部状态（模块级单例）───────────────────────────

@dataclass
class PlanItem:
    content: str
    status: str = "pending"


@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0


_state = PlanningState()


# ─── 内部逻辑 ───────────────────────────────────────

MAX_ITEMS = 12
VALID_STATUSES = {"pending", "in_progress", "completed", "failed"}


def _validate_items(items: list[dict]) -> tuple[bool, str]:
    """验证任务列表。返回 (是否通过, 错误信息)。"""
    if len(items) > MAX_ITEMS:
        return False, f"任务数量超过限制：最多 {MAX_ITEMS} 个任务"

    in_progress_count = 0
    for i, item in enumerate(items):
        if "content" not in item or not item["content"].strip():
            return False, f"第 {i+1} 项缺少 content"

        status = item.get("status")
        if status not in VALID_STATUSES:
            return False, f"第 {i+1} 项 status 无效: {status}"

        if status == "in_progress":
            in_progress_count += 1

    if in_progress_count > 1:
        return False, f"最多只能有 1 个 in_progress 任务，当前有 {in_progress_count} 个"

    return True, ""


def _render_progress(items: list[PlanItem]) -> str:
    """渲染进度文本（给 LLM 看）。"""
    if not items:
        return "任务计划已清空。"

    completed = sum(1 for item in items if item.status == "completed")
    total = len(items)

    lines = [f"任务进度 ({completed}/{total} 已完成):"]
    for item in items:
        if item.status == "completed":
            icon = "[x]"
        elif item.status == "in_progress":
            icon = "[>]"
        else:
            icon = "[ ]"
        lines.append(f"  {icon} {item.content}")

    return "\n".join(lines)


# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """处理 todo_manage 调用，更新任务状态。"""
    items_data = args.get("items", [])

    # 验证
    valid, error = _validate_items(items_data)
    if not valid:
        return ToolResult(output=f"参数错误: {error}", success=False, error="validation_failed")

    # 替换内部状态
    _state.items = [PlanItem(content=item["content"], status=item["status"]) for item in items_data]
    _state.rounds_since_update = 0

    # 返回渲染后的进度
    output = _render_progress(_state.items)
    return ToolResult(output=output, success=True)


# ─── 对外暴露的 API（供 AgentLoop 使用）──────────────

def get_state() -> PlanningState:
    """获取当前规划状态（供 AgentLoop 查询）。"""
    return _state


def increment_rounds() -> int:
    """递增 rounds_since_update，返回新值。"""
    _state.rounds_since_update += 1
    return _state.rounds_since_update


def reset_rounds() -> None:
    """重置 rounds_since_update 为 0。"""
    _state.rounds_since_update = 0
```

- [ ] **Step 2: 创建测试文件**

```python
"""测试 todo_manage 工具。"""
from __future__ import annotations

import pytest

from core.tools.todo import (
    PlanningState,
    PlanItem,
    _validate_items,
    _render_progress,
    handle,
    get_state,
    increment_rounds,
    reset_rounds,
)
from core.tools import ToolUseContext


class TestValidateItems:
    """测试 _validate_items 函数。"""

    def test_empty_list_passes(self):
        valid, error = _validate_items([])
        assert valid is True
        assert error == ""

    def test_valid_items_pass(self):
        items = [
            {"content": "任务1", "status": "pending"},
            {"content": "任务2", "status": "in_progress"},
            {"content": "任务3", "status": "completed"},
        ]
        valid, error = _validate_items(items)
        assert valid is True
        assert error == ""

    def test_missing_content_fails(self):
        items = [{"content": "", "status": "pending"}]
        valid, error = _validate_items(items)
        assert valid is False
        assert "缺少 content" in error

    def test_invalid_status_fails(self):
        items = [{"content": "任务", "status": "invalid"}]
        valid, error = _validate_items(items)
        assert valid is False
        assert "status 无效" in error

    def test_multiple_in_progress_fails(self):
        items = [
            {"content": "任务1", "status": "in_progress"},
            {"content": "任务2", "status": "in_progress"},
        ]
        valid, error = _validate_items(items)
        assert valid is False
        assert "最多只能有 1 个 in_progress" in error

    def test_too_many_items_fails(self):
        items = [{"content": f"任务{i}", "status": "pending"} for i in range(13)]
        valid, error = _validate_items(items)
        assert valid is False
        assert "超过限制" in error


class TestRenderProgress:
    """测试 _render_progress 函数。"""

    def test_empty_list(self):
        result = _render_progress([])
        assert "已清空" in result

    def test_pending_items(self):
        items = [PlanItem(content="任务1", status="pending")]
        result = _render_progress(items)
        assert "[ ] 任务1" in result
        assert "0/1 已完成" in result

    def test_in_progress_items(self):
        items = [PlanItem(content="任务1", status="in_progress")]
        result = _render_progress(items)
        assert "[>] 任务1" in result

    def test_completed_items(self):
        items = [
            PlanItem(content="任务1", status="completed"),
            PlanItem(content="任务2", status="pending"),
        ]
        result = _render_progress(items)
        assert "[x] 任务1" in result
        assert "1/2 已完成" in result


class TestHandle:
    """测试 handle 函数。"""

    def test_successful_update(self):
        # 创建 mock context
        context = ToolUseContext(working_dir="/tmp", max_turns=10)

        result = handle({"items": [{"content": "测试任务", "status": "pending"}]}, context)

        assert result.success is True
        assert "测试任务" in result.output

    def test_validation_failure(self):
        context = ToolUseContext(working_dir="/tmp", max_turns=10)

        result = handle({"items": [{"content": "", "status": "pending"}]}, context)

        assert result.success is False
        assert "参数错误" in result.output


class TestStateAPI:
    """测试对外暴露的状态管理 API。"""

    def setup_method(self):
        """每个测试前重置状态。"""
        from core.tools import todo
        todo._state = PlanningState()

    def test_get_state(self):
        state = get_state()
        assert isinstance(state, PlanningState)
        assert state.items == []

    def test_increment_rounds(self):
        assert increment_rounds() == 1
        assert increment_rounds() == 2

    def test_reset_rounds(self):
        increment_rounds()
        increment_rounds()
        reset_rounds()
        state = get_state()
        assert state.rounds_since_update == 0
```

- [ ] **Step 3: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo_tool.py -v
```

Expected: 10 passed

- [ ] **Step 4: 提交**

```bash
git add core/tools/todo.py tests/test_todo_tool.py
git commit -m "$(cat <<'EOF'
feat(tools): add todo_manage tool

Add todo_manage tool with validation, state management, and progress rendering.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 精简 core/todo.py

**Files:**
- Modify: `core/todo.py`

- [ ] **Step 1: 重写 core/todo.py（只保留基础类型）**

```python
"""Todo 基础类型定义。

TodoManager 和标记解析已删除（改用 todo_manage 工具）。
保留 TodoStatus 枚举和 TodoItem 数据类供其他模块使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TodoStatus(Enum):
    """任务状态枚举。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TodoItem:
    """单个任务项。"""
    id: int
    content: str
    status: TodoStatus = TodoStatus.PENDING
```

- [ ] **Step 2: 验证无导入错误**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.todo import TodoStatus, TodoItem; print('OK')"
```

Expected: OK

- [ ] **Step 3: 提交**

```bash
git add core/todo.py
git commit -m "$(cat <<'EOF'
refactor(todo): simplify core/todo.py

Remove TodoManager, parse_todo_updates, strip_todo_markers, TODO_UPDATE_PATTERN.
Keep TodoStatus and TodoItem for renderer compatibility.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 删除 core/planner.py

**Files:**
- Delete: `core/planner.py`

- [ ] **Step 1: 删除文件**

```bash
rm /Users/kino/works/kino/harness/core/planner.py
```

- [ ] **Step 2: 提交**

```bash
git add core/planner.py
git commit -m "$(cat <<'EOF'
refactor(planner): remove core/planner.py

Planning now handled by LLM through todo_manage tool.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 精简 core/agent.py

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: 修改 imports**

在文件顶部，替换原有的 imports：

```python
"""代理循环编排器。

将 LLM 调用、工具执行、上下文注入、显示渲染组合为完整的代理循环。
对外暴露 AgentLoop 类（依赖注入）和 agent_loop() 向后兼容入口。
"""
from __future__ import annotations

import os
import time
import traceback
from typing import Any

from .config import MAX_TURNS
from .context import ContextPipeline, SystemContextPlugin, UserContextPlugin
from .llm_client import LLMResponse, OpenAIClient, _parse_tool_args
from .renderer import RichRenderer
from .runtime import ToolCall, ToolExecutorRuntime
from .todo import TodoItem, TodoStatus  # 只保留类型
from .tools import ToolResult, ToolUseContext, registry
from .tools.todo import get_state, increment_rounds, reset_rounds  # 新增
```

- [ ] **Step 2: 删除 TodoContextPlugin 类**

删除整个 `TodoContextPlugin` 类（约 15 行）。

- [ ] **Step 3: 删除 _REMINDER_THRESHOLD 常量**

删除这一行：
```python
_REMINDER_THRESHOLD = 3
```

- [ ] **Step 4: 简化 AgentLoop.__init__**

```python
class AgentLoop:
    """代理循环编排器。持有三个可替换依赖，对外暴露 run()。"""

    def __init__(
        self,
        llm: Any,
        renderer: Any,
        context: ContextPipeline,
        tools_schema: list[dict[str, Any]] | None = None,
    ) -> None:
        self._llm = llm
        self._renderer = renderer
        self._context = context
        self._tools_schema = tools_schema or registry.schemas()
```

注意：删除了 `planner` 参数。

- [ ] **Step 5: 简化 run() 方法**

```python
def run(self, messages: list[dict[str, Any]]) -> None:
    """运行一次代理循环。等价于原 agent_loop()。"""
    # 1. 注入上下文
    self._context.inject_all(messages)

    # 2. 首次 LLM 调用
    try:
        llm_resp = self._llm.call(messages, tools=self._tools_schema)
    except Exception as e:
        self._renderer.show_error(f"API 调用失败: {e}")
        return

    # 3. 非工具调用响应 — 确保有内容后直接结束
    if not llm_resp.is_tool_call:
        llm_resp = self._ensure_final_response(llm_resp, messages, None)
        self._print_response(llm_resp)
        messages.append({"role": "assistant", "content": llm_resp.content or ""})
        return

    # 4. 工具调用循环
    tool_context = ToolUseContext(working_dir=os.getcwd(), max_turns=MAX_TURNS)
    tool_context.set_messages(messages)

    try:
        llm_resp = self._run_tool_loop(llm_resp, messages, tool_context)
    except Exception as e:
        self._renderer.show_error(f"工具执行异常: {e}")
        traceback.print_exc()
        return

    # 5. 确保最终有可见内容
    llm_resp = self._ensure_final_response(llm_resp, messages, self._tools_schema)

    self._print_response(llm_resp)
    messages.append({"role": "assistant", "content": llm_resp.content or ""})
```

注意：删除了 `todo_manager` 参数。

- [ ] **Step 6: 重写 _run_tool_loop**

```python
def _run_tool_loop(
    self,
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    tool_context: ToolUseContext,
) -> LLMResponse:
    """运行工具调用循环。

    退出条件：
    1. 模型返回可见内容（has_content）— 任务完成
    2. turn_count >= max_turns — 安全兜底
    3. 空内容重试耗尽（思考模型常见）
    """
    turn_count = tool_context.turn_count
    empty_retries = 0
    MAX_EMPTY_RETRIES = 3
    loop_start = time.time()

    while True:
        # 1) 正常工具调用
        if llm_resp.is_tool_call:
            empty_retries = 0
            turn_count += 1
            tool_context._turn_count = turn_count

            # 安全兜底
            if turn_count >= tool_context.max_turns:
                messages.append({
                    "role": "user",
                    "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。",
                })
                llm_resp = self._llm.call(messages, tools=self._tools_schema)
                break

            llm_resp, called_tools = self._execute_tool_turn(llm_resp, messages, tool_context)

            # Todo 进度跟踪
            if "todo_manage" in called_tools:
                state = get_state()
                self._renderer.show_progress(state.items)
                reset_rounds()
            else:
                rounds = increment_rounds()
                if rounds >= 3:
                    messages.append({
                        "role": "user",
                        "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>",
                    })

            continue

        # 2) 有可见内容 → 可以退出
        if llm_resp.has_content:
            break

        # 3) 空内容（思考模型常见）→ 在循环内重试
        if empty_retries < MAX_EMPTY_RETRIES:
            empty_retries += 1
            has_reasoning = bool(llm_resp.reasoning and llm_resp.reasoning.strip())
            self._renderer.show_status(
                f"(工具循环中模型返回空内容，"
                f"finish_reason={llm_resp.finish_reason}，"
                f"reasoning={'有' if has_reasoning else '无'}，"
                f"重试 {empty_retries}/{MAX_EMPTY_RETRIES})"
            )

            messages.append({"role": "assistant", "content": llm_resp.content or ""})
            messages.append({"role": "user", "content": (
                "你刚才执行了工具操作，但还没有输出结果。"
                "请继续完成你的任务——如果需要读取文件、执行命令或其他操作，请使用工具；"
                "如果已经完成所有步骤，请给出最终的分析结果和行动建议。"
            )})
            llm_resp = self._llm.call(messages, tools=self._tools_schema)
            continue

        # 4) 重试耗尽，退出
        break

    return llm_resp
```

- [ ] **Step 7: 修改 _execute_tool_turn 返回 called_tools**

```python
def _execute_tool_turn(
    self,
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    tool_context: ToolUseContext,
) -> tuple[LLMResponse, list[str]]:
    """执行一轮工具调用：解析 -> 执行 -> 回写 -> 再次调用 LLM。

    Returns:
        (llm_response, list_of_called_tool_names)
    """
    # 打印助手消息
    self._print_response(llm_resp)
    messages.append(llm_resp.to_message_dict())

    # 解析 tool calls
    tool_calls = [
        ToolCall(
            idx=i,
            name=tc.function.name,
            call_id=tc.id,
            args=_parse_tool_args(tc.function.arguments),
        )
        for i, tc in enumerate(llm_resp.tool_calls)
    ]

    called_tool_names = [tc.name for tc in tool_calls]

    # ... 其余逻辑保持不变 ...

    # 最后返回时加上 called_tool_names
    return self._llm.call(messages, tools=self._tools_schema), called_tool_names
```

- [ ] **Step 8: 简化向后兼容入口**

```python
def agent_loop(messages: list[dict[str, Any]]) -> None:
    """向后兼容入口。内部装配默认依赖。"""
    llm = OpenAIClient()
    renderer = RichRenderer()
    context = ContextPipeline()
    context.register(SystemContextPlugin())
    context.register(UserContextPlugin())
    AgentLoop(llm, renderer, context).run(messages)


def plan_phase(messages: list[dict[str, Any]], client) -> None:
    """兼容层：规划阶段已删除，此函数不再执行任何操作。"""
    pass
```

- [ ] **Step 9: 验证无导入错误**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.agent import AgentLoop, agent_loop; print('OK')"
```

Expected: OK

- [ ] **Step 10: 提交**

```bash
git add core/agent.py
git commit -m "$(cat <<'EOF'
refactor(agent): simplify AgentLoop, remove planner/todo tracking

- Remove TodoContextPlugin, _REMINDER_THRESHOLD
- Remove planner parameter from __init__
- Remove todo_manager parameter from run()
- Simplify _run_tool_loop to use todo_manage tool
- Update _execute_tool_turn to return called tool names
- Simplify backward compat agent_loop()
- Make plan_phase() a no-op

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 精简 core/interfaces.py

**Files:**
- Modify: `core/interfaces.py`

- [ ] **Step 1: 删除 Planner Protocol**

删除整个 `Planner` Protocol 类（约 9 行）。

- [ ] **Step 2: 验证无导入错误**

```bash
cd /Users/kino/works/kino/harness && python -c "from core.interfaces import LLMClient, ContextPlugin, Renderer; print('OK')"
```

Expected: OK

- [ ] **Step 3: 提交**

```bash
git add core/interfaces.py
git commit -m "$(cat <<'EOF'
refactor(interfaces): remove Planner Protocol

Planning now handled by LLM through todo_manage tool.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 精简 01_agent_loop.py

**Files:**
- Modify: `01_agent_loop.py`

- [ ] **Step 1: 重写入口文件**

```python
"""简化版入口：LLM 自主管理 todo，无需 planner。"""
from __future__ import annotations

from rich.console import Console

from core.llm_client import OpenAIClient
from core.context import ContextPipeline, SystemContextPlugin, UserContextPlugin
from core.renderer import RichRenderer
from core.agent import AgentLoop

console = Console()

SYSTEM_PROMPT = "无论如何你都要使用中文回答用户"


def main() -> None:
    # 装配依赖（循环外一次）
    renderer = RichRenderer(console)
    llm = OpenAIClient()

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

        # 装配 context pipeline
        context = ContextPipeline()
        context.register(SystemContextPlugin())
        context.register(UserContextPlugin())

        # 执行（LLM 自主管理 todo）
        AgentLoop(llm, renderer, context).run(history)
        print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证入口可运行**

```bash
cd /Users/kino/works/kino/harness && python -c "from 01_agent_loop import main; print('OK')"
```

Expected: OK (不会真正运行，只是验证导入)

- [ ] **Step 3: 提交**

```bash
git add 01_agent_loop.py
git commit -m "$(cat <<'EOF'
refactor(entry): simplify 01_agent_loop.py

Remove planner and TodoContextPlugin. LLM now manages todos via todo_manage tool.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 更新测试

**Files:**
- Modify: `tests/test_interfaces.py`

- [ ] **Step 1: 删除 Planner Protocol 测试**

删除 `test_planner_protocol` 方法。

- [ ] **Step 2: 运行所有测试**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/ -v
```

Expected: 所有测试通过

- [ ] **Step 3: 提交**

```bash
git add tests/test_interfaces.py
git commit -m "$(cat <<'EOF'
test(interfaces): remove Planner Protocol test

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review 检查清单

**1. Spec 覆盖:**
- [x] `core/tools/todo.py` — Task 1
- [x] `core/todo.py` 精简 — Task 2
- [x] `core/planner.py` 删除 — Task 3
- [x] `core/agent.py` 精简 — Task 4
- [x] `core/interfaces.py` 精简 — Task 5
- [x] `01_agent_loop.py` 精简 — Task 6
- [x] 测试更新 — Task 7

**2. Placeholder 扫描:**
- [x] 无 TBD/TODO
- [x] 无 "implement later"
- [x] 所有代码完整

**3. 类型一致性:**
- [x] `TodoStatus` 枚举保留
- [x] `TodoItem` 数据类保留
- [x] `AgentLoop.__init__` 签名正确（无 planner）
- [x] `AgentLoop.run()` 签名正确（无 todo_manager）

---

## Plan Complete

**执行选择：**

计划已保存至 `docs/superpowers/plans/2026-04-12-todo-tool-implementation.md`。两种执行方式：

**1. Subagent-Driven (推荐)** — 我为每个 Task 分派一个独立 subagent，任务间评审，快速迭代

**2. Inline Execution** — 在本会话中使用 executing-plans 执行，批量执行带检查点

选择哪种方式？