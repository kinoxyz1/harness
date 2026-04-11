# Todo 系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 agent loop 添加两阶段的 todo 系统——规划阶段自动分解任务，执行阶段持续锚定进度，解决长任务中的目标偏移、步骤遗漏和重复劳动。

**Architecture:** 新增 `core/todo.py`（数据模型 + 解析），修改 `core/agent.py`（规划函数 + 上下文注入 + loop 集成），修改 `01_agent_loop.py`（入口接线）。纯逻辑部分用 pytest 测试，LLM 集成部分手动验证。

**Tech Stack:** Python 3.10+, OpenAI API, Rich, pytest

---

### Task 1: 测试基础设施 + TodoStatus/TodoItem 数据类

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_todo.py`
- Create: `core/todo.py`

- [ ] **Step 1: 添加 pytest 依赖**

在 `requirements.txt` 中添加 pytest：

```
openai
rich
pytest
```

- [ ] **Step 2: 创建 tests 目录和空 `__init__.py`**

```bash
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 3: 写 TodoStatus 和 TodoItem 的测试**

在 `tests/test_todo.py` 中：

```python
from core.todo import TodoItem, TodoStatus


class TestTodoItem:
    def test_default_status_is_pending(self):
        item = TodoItem(id=1, content="test")
        assert item.status == TodoStatus.PENDING

    def test_custom_status(self):
        item = TodoItem(id=2, content="done", status=TodoStatus.COMPLETED)
        assert item.status == TodoStatus.COMPLETED


class TestTodoStatus:
    def test_all_values(self):
        assert TodoStatus.PENDING.value == "pending"
        assert TodoStatus.IN_PROGRESS.value == "in_progress"
        assert TodoStatus.COMPLETED.value == "completed"
        assert TodoStatus.FAILED.value == "failed"
```

- [ ] **Step 4: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'core.todo'`

- [ ] **Step 5: 实现 TodoStatus 和 TodoItem**

在 `core/todo.py` 中：

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class TodoStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TodoItem:
    id: int
    content: str
    status: TodoStatus = TodoStatus.PENDING
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py -v
```

Expected: 3 passed

- [ ] **Step 7: 提交**

```bash
git add requirements.txt tests/__init__.py tests/test_todo.py core/todo.py
git commit -m "feat(todo): add TodoStatus and TodoItem data classes with test infra"
```

---

### Task 2: TodoManager CRUD 方法

**Files:**
- Modify: `tests/test_todo.py`
- Modify: `core/todo.py`

- [ ] **Step 1: 写 TodoManager 的测试**

在 `tests/test_todo.py` 中追加：

```python
from core.todo import TodoManager


class TestTodoManager:
    def test_add_auto_increments_id(self):
        mgr = TodoManager()
        item1 = mgr.add("first")
        item2 = mgr.add("second")
        assert item1.id == 1
        assert item2.id == 2

    def test_add_returns_item_with_content(self):
        mgr = TodoManager()
        item = mgr.add("读取配置文件")
        assert item.content == "读取配置文件"
        assert item.status == TodoStatus.PENDING

    def test_update_status_found(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.add("task2")
        result = mgr.update_status(1, TodoStatus.COMPLETED)
        assert result is not None
        assert result.status == TodoStatus.COMPLETED
        assert mgr.items[0].status == TodoStatus.COMPLETED

    def test_update_status_not_found(self):
        mgr = TodoManager()
        mgr.add("task1")
        result = mgr.update_status(99, TodoStatus.COMPLETED)
        assert result is None

    def test_remove(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.add("task2")
        mgr.remove(1)
        assert len(mgr.items) == 1
        assert mgr.items[0].id == 2

    def test_reorder(self):
        mgr = TodoManager()
        mgr.add("first")
        mgr.add("second")
        mgr.add("third")
        mgr.reorder([3, 1, 2])
        assert [item.id for item in mgr.items] == [3, 1, 2]

    def test_current_returns_first_non_completed(self):
        mgr = TodoManager()
        mgr.add("done")
        mgr.update_status(1, TodoStatus.COMPLETED)
        mgr.add("pending")
        mgr.add("also pending")
        current = mgr.current()
        assert current is not None
        assert current.id == 2

    def test_current_returns_none_when_all_done(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.update_status(1, TodoStatus.COMPLETED)
        assert mgr.current() is None

    def test_progress(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.add("task2")
        mgr.add("task3")
        mgr.update_status(1, TodoStatus.COMPLETED)
        assert mgr.progress() == (1, 3)

    def test_all_completed_false(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.add("task2")
        mgr.update_status(1, TodoStatus.COMPLETED)
        assert mgr.all_completed is False

    def test_all_completed_true(self):
        mgr = TodoManager()
        mgr.add("task1")
        mgr.update_status(1, TodoStatus.COMPLETED)
        assert mgr.all_completed is True

    def test_all_completed_empty(self):
        mgr = TodoManager()
        assert mgr.all_completed is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py::TestTodoManager -v
```

Expected: FAIL — `ImportError: cannot import name 'TodoManager' from 'core.todo'`

- [ ] **Step 3: 实现 TodoManager**

在 `core/todo.py` 的 `TodoItem` 之后追加：

```python
@dataclass
class TodoManager:
    """Todo 管理器：管理待办列表的完整生命周期。"""
    items: list[TodoItem] = field(default_factory=list)
    _next_id: int = 1

    def add(self, content: str, status: TodoStatus = TodoStatus.PENDING) -> TodoItem:
        """添加新的待办项。"""
        item = TodoItem(id=self._next_id, content=content, status=status)
        self.items.append(item)
        self._next_id += 1
        return item

    def update_status(self, id: int, status: TodoStatus) -> TodoItem | None:
        """按 id 更新指定项的状态。"""
        for item in self.items:
            if item.id == id:
                item.status = status
                return item
        return None

    def remove(self, id: int) -> None:
        """按 id 移除一项。"""
        self.items = [item for item in self.items if item.id != id]

    def reorder(self, ids: list[int]) -> None:
        """重排。ids 为新的 id 顺序。"""
        id_to_item = {item.id: item for item in self.items}
        self.items = [id_to_item[i] for i in ids if i in id_to_item]

    def current(self) -> TodoItem | None:
        """返回第一个未完成的项（当前聚焦）。"""
        for item in self.items:
            if item.status != TodoStatus.COMPLETED:
                return item
        return None

    def progress(self) -> tuple[int, int]:
        """返回 (已完成数, 总数)。"""
        completed = sum(1 for item in self.items if item.status == TodoStatus.COMPLETED)
        return (completed, len(self.items))

    @property
    def all_completed(self) -> bool:
        """是否所有待办都已完成。"""
        return len(self.items) > 0 and all(
            item.status == TodoStatus.COMPLETED for item in self.items
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/todo.py tests/test_todo.py
git commit -m "feat(todo): implement TodoManager with CRUD operations"
```

---

### Task 3: TodoManager.summary() 格式化

**Files:**
- Modify: `tests/test_todo.py`
- Modify: `core/todo.py`

- [ ] **Step 1: 写 summary 的测试**

在 `tests/test_todo.py` 中追加：

```python
class TestTodoManagerSummary:
    def test_summary_format(self):
        mgr = TodoManager()
        mgr.add("读取项目配置文件")
        mgr.add("分析现有代码结构")
        mgr.update_status(1, TodoStatus.COMPLETED)

        result = mgr.summary()

        assert "任务进度 (1/2 已完成)" in result
        assert "[x] 1. 读取项目配置文件" in result
        assert "[ ] 2. 分析现有代码结构" in result
        assert "当前聚焦: #2 分析现有代码结构" in result

    def test_summary_all_completed_no_current_focus(self):
        mgr = TodoManager()
        mgr.add("only task")
        mgr.update_status(1, TodoStatus.COMPLETED)

        result = mgr.summary()

        assert "当前聚焦" not in result

    def test_summary_empty_list(self):
        mgr = TodoManager()
        result = mgr.summary()
        assert "任务进度 (0/0 已完成)" in result
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py::TestTodoManagerSummary -v
```

Expected: FAIL — `AttributeError: 'TodoManager' object has no attribute 'summary'`

- [ ] **Step 3: 实现 summary 方法**

在 `core/todo.py` 的 `TodoManager` 类中，`all_completed` 属性之后追加：

```python
    def summary(self) -> str:
        """格式化为可注入 LLM 上下文的文本块。"""
        completed, total = self.progress()
        lines = [f"## 任务进度 ({completed}/{total} 已完成)\n"]
        for item in self.items:
            mark = "x" if item.status == TodoStatus.COMPLETED else " "
            lines.append(f"- [{mark}] {item.id}. {item.content}")
        current_item = self.current()
        if current_item:
            lines.append(f"\n当前聚焦: #{current_item.id} {current_item.content}")
        return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/todo.py tests/test_todo.py
git commit -m "feat(todo): add TodoManager.summary() context formatting"
```

---

### Task 4: 标记解析 — parse_todo_updates 和 strip_todo_markers

**Files:**
- Modify: `tests/test_todo.py`
- Modify: `core/todo.py`

- [ ] **Step 1: 写标记解析的测试**

在 `tests/test_todo.py` 中追加：

```python
from core.todo import parse_todo_updates, strip_todo_markers


class TestParseTodoUpdates:
    def test_single_marker(self):
        text = "好的，我完成了 [TODO-UPDATE: status=completed, id=1]"
        result = parse_todo_updates(text)
        assert result == [("completed", 1)]

    def test_multiple_markers(self):
        text = (
            "完成了第一个 [TODO-UPDATE: status=completed, id=1] "
            "然后第二个 [TODO-UPDATE: status=completed, id=2]"
        )
        result = parse_todo_updates(text)
        assert result == [("completed", 1), ("completed", 2)]

    def test_no_markers(self):
        text = "这是一段普通文本"
        assert parse_todo_updates(text) == []

    def test_failed_status(self):
        text = "[TODO-UPDATE: status=failed, id=3]"
        result = parse_todo_updates(text)
        assert result == [("failed", 3)]

    def test_whitespace_tolerance(self):
        text = "[TODO-UPDATE:  status = completed,  id = 5 ]"
        # 注意：正则不匹配多余空格在 = 两侧，只匹配标准格式
        # 标准格式: [TODO-UPDATE: status=completed, id=5]
        assert parse_todo_updates(text) == []


class TestStripTodoMarkers:
    def test_strips_single_marker(self):
        text = "完成了 [TODO-UPDATE: status=completed, id=1] 继续"
        result = strip_todo_markers(text)
        assert result == "完成了 继续"

    def test_no_markers_unchanged(self):
        text = "普通文本"
        assert strip_todo_markers(text) == "普通文本"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py::TestParseTodoUpdates tests/test_todo.py::TestStripTodoMarkers -v
```

Expected: FAIL — `ImportError: cannot import name 'parse_todo_updates'`

- [ ] **Step 3: 实现标记解析和剥离**

在 `core/todo.py` 的末尾追加：

```python
TODO_UPDATE_PATTERN = re.compile(
    r'\[TODO-UPDATE:\s*status=(\w+),\s*id=(\d+)\]'
)


def parse_todo_updates(text: str) -> list[tuple[str, int]]:
    """解析 [TODO-UPDATE: status=X, id=N] 标记。

    返回 (status, id) 元组列表。
    """
    matches = TODO_UPDATE_PATTERN.findall(text)
    return [(status, int(id_str)) for status, id_str in matches]


def strip_todo_markers(text: str) -> str:
    """从文本中移除所有 TODO-UPDATE 标记。"""
    return TODO_UPDATE_PATTERN.sub('', text).strip()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_todo.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/todo.py tests/test_todo.py
git commit -m "feat(todo): add parse_todo_updates and strip_todo_markers"
```

---

### Task 5: plan_phase 和 _parse_plan_response

**Files:**
- Create: `tests/test_plan.py`
- Modify: `core/agent.py`

这是涉及 LLM 的部分。`_parse_plan_response` 是纯逻辑，可以测试；`plan_phase` 需要 LLM 调用，手动验证。

- [ ] **Step 1: 写 _parse_plan_response 的测试**

创建 `tests/test_plan.py`：

```python
from core.agent import _parse_plan_response
from core.todo import TodoManager


class TestParsePlanResponse:
    def test_plan_with_numbered_items(self):
        text = "PLAN:\n1. 打开冰箱\n2. 把大象放进去\n3. 关上冰箱"
        result = _parse_plan_response(text)
        assert result is not None
        assert len(result.items) == 3
        assert result.items[0].content == "打开冰箱"
        assert result.items[1].content == "把大象放进去"
        assert result.items[2].content == "关上冰箱"

    def test_plan_with_parenthesis_numbers(self):
        text = "PLAN:\n1) 第一步\n2) 第二步"
        result = _parse_plan_response(text)
        assert result is not None
        assert len(result.items) == 2
        assert result.items[0].content == "第一步"

    def test_plan_with_dash_items(self):
        text = "PLAN:\n- 读取文件\n- 修改文件\n- 测试"
        result = _parse_plan_response(text)
        assert result is not None
        assert len(result.items) == 3

    def test_direct_returns_none(self):
        text = "DIRECT: 这是一个简单的问题，答案是这样的。"
        result = _parse_plan_response(text)
        assert result is None

    def test_case_insensitive(self):
        result1 = _parse_plan_response("plan:\n1. 任务")
        result2 = _parse_plan_response("Plan:\n1. 任务")
        assert result1 is not None
        assert result2 is not None

    def test_ambiguous_returns_none(self):
        text = "我觉得这个任务可以直接回答。"
        result = _parse_plan_response(text)
        assert result is None

    def test_plan_with_empty_items_returns_none(self):
        text = "PLAN:\n\n\n"
        result = _parse_plan_response(text)
        assert result is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_plan.py -v
```

Expected: FAIL — `ImportError: cannot import name '_parse_plan_response' from 'core.agent'`

- [ ] **Step 3: 实现 _parse_plan_response 和 plan_phase**

在 `core/agent.py` 中，添加 `import re`（在文件顶部 import 区域），然后在 `_inject_user_context` 函数之后、`_execute_tool_turn` 之前，添加以下代码：

```python
from .todo import TodoManager, parse_todo_updates, strip_todo_markers

# ─── 规划阶段 ──────────────────────────────────────────────

_PLAN_PROMPT = """\
分析用户的请求，判断是否需要多步执行。

如果需要多步执行，按以下格式输出你的计划：
PLAN:
1. [描述]
2. [描述]
...

当你完成一项任务时，在回复中包含：
[TODO-UPDATE: status=completed, id=N]

如果你可以直接回答而无需工具使用，输出：
DIRECT: [你的回答]
"""


def _parse_plan_response(text: str) -> TodoManager | None:
    """解析规划阶段的 LLM 输出。"""
    text = text.strip()

    if text.upper().startswith("DIRECT:"):
        return None

    if not text.upper().startswith("PLAN:"):
        # 模糊情况，按 DIRECT 处理
        return None

    # 提取 PLAN: 之后的内容
    plan_text = text[5:].strip()
    items = []
    for line in plan_text.split("\n"):
        line = line.strip()
        # 匹配 "1. xxx" 或 "1) xxx" 或 "- xxx"
        match = re.match(r'(\d+[\.\)]\s*|-)\s*(.+)', line)
        if match:
            items.append(match.group(2).strip())

    if not items:
        return None

    manager = TodoManager()
    for item_text in items:
        manager.add(item_text)
    return manager


def plan_phase(messages: list[dict[str, Any]], client) -> TodoManager | None:
    """规划阶段：让 LLM 分析任务并产出 TodoManager。

    Returns:
        TodoManager — 如果任务需要多步执行。
        None — 如果任务足够简单，可以直接处理。
    """
    # 构建规划专用的消息列表（不携带 tools）
    plan_messages = [{"role": "system", "content": _PLAN_PROMPT}]

    # 提取用户最后一条实际消息（跳过注入的上下文消息）
    for msg in reversed(messages):
        if msg.get("role") == "user" and "<!-- " not in (msg.get("content") or ""):
            plan_messages.append(msg)
            break

    try:
        llm_resp = _call_llm(client, plan_messages)
    except Exception as e:
        console.print(f"[dim]规划阶段失败: {e}，直接进入执行[/dim]")
        return None

    if not llm_resp.has_content:
        return None

    return _parse_plan_response(llm_resp.content or "")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_plan.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 运行全量测试确认无回归**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/ -v
```

Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add core/agent.py tests/test_plan.py core/todo.py
git commit -m "feat(todo): add plan_phase and _parse_plan_response"
```

---

### Task 6: 上下文注入 — _inject_todo_context

**Files:**
- Modify: `tests/test_plan.py`
- Modify: `core/agent.py`

- [ ] **Step 1: 写注入函数的测试**

在 `tests/test_plan.py` 中追加：

```python
from core.agent import _inject_todo_context


class TestInjectTodoContext:
    def _make_manager(self):
        from core.todo import TodoManager
        mgr = TodoManager()
        mgr.add("任务一")
        mgr.add("任务二")
        return mgr

    def test_injects_todo_message(self):
        mgr = self._make_manager()
        messages = []
        _inject_todo_context(messages, mgr)
        assert len(messages) == 1
        assert "<!-- todo-context -->" in messages[0]["content"]
        assert "任务一" in messages[0]["content"]

    def test_replaces_previous_todo_context(self):
        mgr = self._make_manager()
        messages = []
        _inject_todo_context(messages, mgr)
        assert len(messages) == 1

        # 更新状态后重新注入
        mgr.update_status(1, TodoStatus.COMPLETED)
        _inject_todo_context(messages, mgr)
        assert len(messages) == 1  # 旧的被移除，新的被添加
        assert "[x] 1. 任务一" in messages[0]["content"]

    def test_preserves_non_todo_messages(self):
        mgr = self._make_manager()
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]
        _inject_todo_context(messages, mgr)
        assert len(messages) == 3
        assert messages[0]["content"] == "system prompt"
        assert messages[1]["content"] == "hello"
```

注意：需要在 `tests/test_plan.py` 顶部添加 `from core.todo import TodoStatus`。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_plan.py::TestInjectTodoContext -v
```

Expected: FAIL — `ImportError: cannot import name '_inject_todo_context'`

- [ ] **Step 3: 实现 _inject_todo_context**

在 `core/agent.py` 中，`plan_phase` 函数之后追加：

```python
# ─── Todo 上下文注入 ──────────────────────────────────────

_TODO_MARKER = "<!-- todo-context -->"
_REMINDER_THRESHOLD = 3


def _inject_todo_context(
    messages: list[dict[str, Any]], todo_manager: TodoManager
) -> None:
    """注入当前 todo 进度。先移除旧的再注入新的。"""
    messages[:] = [
        msg for msg in messages
        if not (msg.get("role") == "user" and _TODO_MARKER in (msg.get("content") or ""))
    ]

    todo_text = todo_manager.summary()
    content = f"{_TODO_MARKER}\n{todo_text}"
    messages.append({"role": "user", "content": content})
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/test_plan.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add core/agent.py tests/test_plan.py
git commit -m "feat(todo): add _inject_todo_context with replace logic"
```

---

### Task 7: agent_loop 集成 — 接受 todo_manager 并在循环中跟踪

**Files:**
- Modify: `core/agent.py`

这是最关键的集成步骤。修改 `agent_loop` 和 `_run_tool_loop` 以支持 todo_manager。

- [ ] **Step 1: 修改 agent_loop 签名和首轮注入**

在 `core/agent.py` 中，将 `agent_loop` 函数签名改为：

```python
def agent_loop(
    messages: list[dict[str, Any]],
    todo_manager: TodoManager | None = None,
) -> None:
```

在函数体内，`_inject_user_context(messages)` 之后追加：

```python
    # 注入 todo 上下文（首轮）
    if todo_manager:
        _inject_todo_context(messages, todo_manager)
```

- [ ] **Step 2: 修改 _run_tool_loop 签名，添加 todo 跟踪逻辑**

将 `_run_tool_loop` 签名改为：

```python
def _run_tool_loop(
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    tool_context: ToolUseContext,
    client,
    tools_schema: list[dict[str, Any]],
    todo_manager: TodoManager | None = None,
) -> LLMResponse:
```

在函数体内的 `while True:` 循环中，`empty_retries = 0` 之后、`while True:` 之前，添加：

```python
    rounds_since_update = 0
```

在循环体内部，`# 1) 正常工具调用` 分支中（`turn_count += 1` 之后），`llm_resp = _execute_tool_turn(...)` 之后追加 todo 跟踪逻辑：

```python
            # Todo 跟踪：解析标记 + 注入上下文
            if todo_manager:
                updates = parse_todo_updates(llm_resp.content or "")
                if updates:
                    for status_str, item_id in updates:
                        try:
                            todo_manager.update_status(item_id, TodoStatus(status_str))
                        except ValueError:
                            pass
                    rounds_since_update = 0
                else:
                    rounds_since_update += 1

                # 注入下一轮的 todo 上下文
                _inject_todo_context(messages, todo_manager)
                if rounds_since_update >= _REMINDER_THRESHOLD:
                    messages.append({
                        "role": "user",
                        "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>",
                    })
                    rounds_since_update = 0

                # 所有 todo 完成，注入停止指令
                if todo_manager.all_completed:
                    messages.append({
                        "role": "user",
                        "content": "所有任务已完成。请给出最终总结。",
                    })
```

- [ ] **Step 3: 修改 agent_loop 中的 _run_tool_loop 调用**

在 `agent_loop` 中，将：

```python
        llm_resp = _run_tool_loop(llm_resp, messages, tool_context, client, tools_schema)
```

改为：

```python
        llm_resp = _run_tool_loop(
            llm_resp, messages, tool_context, client, tools_schema,
            todo_manager=todo_manager,
        )
```

- [ ] **Step 4: 在最终输出中剥离标记**

在 `agent_loop` 的 `_print_assistant_msg(llm_resp)` 之前追加：

```python
    # 剥离 TODO-UPDATE 标记
    if todo_manager and llm_resp.has_content:
        llm_resp.content = strip_todo_markers(llm_resp.content or "")
```

（注意：`LLMResponse.content` 是一个实例属性，直接赋值即可。）

- [ ] **Step 5: 手动验证**

运行 agent loop 测试一个简单任务：

```bash
cd /Users/kino/works/kino/harness && python 01_agent_loop.py
```

输入一个简单问题（如"你好"）验证 DIRECT 路径，输入一个多步任务（如"帮我看看当前目录下有哪些文件，然后读一下 requirements.txt"）验证 PLAN 路径。

- [ ] **Step 6: 提交**

```bash
git add core/agent.py
git commit -m "feat(todo): integrate todo_manager into agent_loop and _run_tool_loop"
```

---

### Task 8: 入口接线 — 01_agent_loop.py

**Files:**
- Modify: `01_agent_loop.py`

- [ ] **Step 1: 修改入口文件**

将 `01_agent_loop.py` 中：

```python
from core.agent import agent_loop
```

改为：

```python
from core.agent import agent_loop, plan_phase
from core.llm import create_llm_client
```

将主循环中的：

```python
        history.append({"role": "user", "content": query})
        agent_loop(history)
```

改为：

```python
        history.append({"role": "user", "content": query})
        client = create_llm_client()
        todos = plan_phase(history, client)
        if todos:
            console.print(f"[dim]计划: {len(todos.items)} 个任务[/dim]\n")
            agent_loop(history, todo_manager=todos)
        else:
            agent_loop(history)
```

- [ ] **Step 2: 手动端到端验证**

```bash
cd /Users/kino/works/kino/harness && python 01_agent_loop.py
```

测试场景：
1. 简单问题："你好" → 应走 DIRECT 路径，直接回复
2. 多步任务："帮我查看当前目录结构，然后读取 requirements.txt 的内容" → 应先显示计划，再逐步执行

- [ ] **Step 3: 运行全量测试确认无回归**

```bash
cd /Users/kino/works/kino/harness && python -m pytest tests/ -v
```

Expected: 全部 passed

- [ ] **Step 4: 提交**

```bash
git add 01_agent_loop.py
git commit -m "feat(todo): wire up plan_phase in entry point"
```
