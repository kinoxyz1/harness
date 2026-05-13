# Task / Subagent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 harness 上落地 `TaskState + task_plan + TaskPlanningPolicy + fresh subagent task_execute`，把多步骤任务的权威状态从 `todo` 迁移到 `Task`，同时保持现有 UI、PromptAssembler、ToolRuntime、SubagentRuntime 的兼容路径。

**Architecture:** 分三段交付。Phase A/B 先引入 `TaskState`、`task_plan`、`TaskPlanningPolicy`、`todo` 投影兼容层和 `<task-state>` runtime 渲染；Phase C 再引入显式 `task_execute(task_id)` 和 `fresh_subagent` 路由。`fork_subagent`、grouped todo projection、自动 task scheduling 全部延后，不进入这次实现。

**Tech Stack:** Python 3.10+, dataclasses, pytest, 现有 `SessionUpdate` / `RunUpdate` reducer 协议, PromptAssembler / MessageViewBuilder, ToolExecutorRuntime, SessionEngine / SubagentRuntime

---

## File Structure

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `core/tasks/__init__.py` | Create | 导出 task runtime 的公共类型和 helper |
| `core/tasks/models.py` | Create | `TaskStatus`、`TaskExecutionMode`、`TaskRecord`、`TaskState`、`TaskPacket`、`TaskRunResult` |
| `core/tasks/projection.py` | Create | `TaskState -> TodoItem[]` 投影逻辑 |
| `core/tasks/planner_runtime.py` | Create | `task_plan` 的 validate / normalize / allocate / build 流程 |
| `core/tasks/dispatcher.py` | Create | `compile_task_packet()`、`normalize_subagent_result()`、Phase C 的 `dispatch_task()` |
| `core/policy/task_planning.py` | Create | `TaskPlanningPolicy` 与 `PREPLAN_ALLOWED_TOOLS` |
| `core/tools/builtin/task_plan.py` | Create | rewrite-style planner tool |
| `core/tools/builtin/task_execute.py` | Create | Phase C 的显式 task execution 入口 |
| `core/session/state.py` | Modify | 增加 `task_state`，保留 `todo_state` 为兼容视图 |
| `core/query/state.py` | Modify | 增加 task planning runtime flags |
| `core/tools/context.py` | Modify | 增加 `SessionUpdateKind.SET_TASK_STATE` |
| `core/query/reducers.py` | Modify | 处理 `SET_TASK_STATE`，在 reducer 内同步 todo projection |
| `core/tools/builtin/todo.py` | Modify | `TaskState` 激活时拒绝写入 |
| `core/tools/runtime.py` | Modify | pre-plan tool gate，阻止无规划直接执行 |
| `core/prompt/system_context.py` | Modify | 系统提示从“多步骤必须 todo”切到“复杂任务先 task_plan” |
| `core/prompt/assembler.py` | Modify | 渲染 `<task-state>`，在 task_state 激活时替代 `<todo-state>` |
| `core/query/loop.py` | Modify | `task_plan` / `task_execute` 的 UI fallback 与 `SET_TASK_STATE` 的 display hook |
| `core/session/subagent.py` | Modify | `SubagentRequest.task_packet`、fresh packet 渲染、required_skills preload |
| `01_agent_loop.py` | Modify | 注册 `TaskPlanningPolicy`，调整 policy 顺序 |
| `tests/test_task_runtime_models.py` | Create | task datamodel / reducer / projection 基础行为 |
| `tests/session/test_task_plan_tool.py` | Create | `task_plan` schema、rewrite 语义、validation |
| `tests/test_task_planning_policy.py` | Create | `TaskPlanningPolicy` 注入与 pre-plan allowlist |
| `tests/session/test_task_execute_tool.py` | Create | `task_execute` 对 local / fresh_subagent 的路由 |
| `tests/session/test_subagent_runtime.py` | Create | `TaskPacket` 渲染和 required_skills preload |
| `tests/session/test_prompt_assembler.py` | Modify | `<task-state>` 渲染、stable prompt 文案更新 |
| `tests/session/test_state_assembled_runtime.py` | Modify | transcript-independence 证明扩展到 task_state |
| `tests/test_runtime_control_plane.py` | Modify | `SET_TASK_STATE`、run_state flags、runtime gate |
| `tests/test_tool_registry.py` | Modify | 新工具注册、required params |
| `tests/test_query_display.py` | Modify | `task_plan` 触发的 projected todo display、fallback 文案 |

---

### Task 1: 建立 Task Runtime 基础类型和状态更新协议

**Files:**
- Create: `core/tasks/__init__.py`
- Create: `core/tasks/models.py`
- Create: `core/tasks/projection.py`
- Modify: `core/session/state.py`
- Modify: `core/query/state.py`
- Modify: `core/tools/context.py`
- Modify: `core/query/reducers.py`
- Create: `tests/test_task_runtime_models.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 先写基础 failing tests，锁定 Phase A 的 state / reducer 目标**

Create `tests/test_task_runtime_models.py`:

```python
from core.query.reducers import apply_session_update
from core.session.state import SessionState, TodoItem
from core.tools.context import SessionUpdate, SessionUpdateKind
from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus


def _task(task_id: str, subject: str, status: TaskStatus = TaskStatus.PENDING) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        subject=subject,
        goal=f"Goal for {subject}",
        status=status,
        execution_mode=TaskExecutionMode.LOCAL,
    )


def test_session_state_starts_with_empty_task_state() -> None:
    state = SessionState(conversation_messages=[])
    assert state.task_state.tasks_by_id == {}
    assert state.task_state.ordered_task_ids == []
    assert state.task_state.current_task_id is None


def test_set_task_state_update_replaces_authoritative_state_and_projects_todo() -> None:
    session = SessionState(conversation_messages=[])
    next_state = TaskState(
        tasks_by_id={
            "task-1": _task("task-1", "Inspect runtime", TaskStatus.IN_PROGRESS),
            "task-2": _task("task-2", "Write spec", TaskStatus.PENDING),
        },
        ordered_task_ids=["task-1", "task-2"],
        current_task_id="task-1",
        last_planned_turn=3,
    )

    apply_session_update(
        session,
        SessionUpdate(
            kind=SessionUpdateKind.SET_TASK_STATE,
            payload={"task_state": next_state},
        ),
    )

    assert session.task_state.current_task_id == "task-1"
    assert [item.content for item in session.todo_state.items] == [
        "Inspect runtime",
        "Write spec",
    ]
    assert session.todo_state.items[0].status == "in_progress"


def test_projection_keeps_todo_items_compatible_with_existing_renderer() -> None:
    session = SessionState(conversation_messages=[])
    next_state = TaskState(
        tasks_by_id={"task-1": _task("task-1", "Do work", TaskStatus.COMPLETED)},
        ordered_task_ids=["task-1"],
    )

    apply_session_update(
        session,
        SessionUpdate(kind=SessionUpdateKind.SET_TASK_STATE, payload={"task_state": next_state}),
    )

    assert session.todo_state.items == [TodoItem(content="Do work", active_form="Do work", status="completed")]
```

- [ ] **Step 2: 运行基础 tests，确认当前代码还不支持 task runtime**

Run: `pytest tests/test_task_runtime_models.py tests/test_runtime_control_plane.py -q`
Expected: FAIL，缺少 `core.tasks` 模块、`SessionUpdateKind.SET_TASK_STATE`、`SessionState.task_state`

- [ ] **Step 3: 创建 Task datamodel 模块**

Create `core/tasks/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskExecutionMode(str, Enum):
    LOCAL = "local"
    FRESH_SUBAGENT = "fresh_subagent"
    FORK_SUBAGENT = "fork_subagent"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode
    parent_todo_id: str | None = None
    active_form: str | None = None
    agent_type: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    known_context: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    owner: str | None = None
    blocked_reason: str | None = None
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    packet_revision: int = 0
    created_at_turn: int = 0
    updated_at_turn: int = 0


@dataclass(slots=True)
class TaskState:
    tasks_by_id: dict[str, TaskRecord] = field(default_factory=dict)
    ordered_task_ids: list[str] = field(default_factory=list)
    last_planned_turn: int | None = None
    last_projection_turn: int | None = None
    current_task_id: str | None = None


@dataclass(slots=True)
class TaskPacket:
    task_id: str
    mode: TaskExecutionMode
    agent_type: str | None
    title: str
    directive: str
    task_context: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    packet_revision: int = 0


@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    recommended_next_steps: list[str] = field(default_factory=list)
    failure_reason: str | None = None
```

Create `core/tasks/__init__.py`:

```python
from .models import (
    TaskExecutionMode,
    TaskPacket,
    TaskRecord,
    TaskRunResult,
    TaskState,
    TaskStatus,
)

__all__ = [
    "TaskExecutionMode",
    "TaskPacket",
    "TaskRecord",
    "TaskRunResult",
    "TaskState",
    "TaskStatus",
]
```

- [ ] **Step 4: 增加 projection helper，并把 SessionState / RunState / reducer 接到 task_state**

Create `core/tasks/projection.py`:

```python
from __future__ import annotations

from core.session.state import TodoItem
from .models import TaskState, TaskStatus


def map_task_status_to_todo_status(status: TaskStatus) -> str:
    if status == TaskStatus.IN_PROGRESS:
        return "in_progress"
    if status == TaskStatus.COMPLETED:
        return "completed"
    return "pending"


def project_task_state_to_todo_items(task_state: TaskState) -> list[TodoItem]:
    items: list[TodoItem] = []
    for task_id in task_state.ordered_task_ids:
        task = task_state.tasks_by_id[task_id]
        items.append(
            TodoItem(
                content=task.subject,
                active_form=task.active_form or task.subject,
                status=map_task_status_to_todo_status(task.status),
                workflow_ref=None,
            )
        )
    return items
```

Modify `core/session/state.py`:

```python
from core.tasks import TaskState

...
    todo_state: TodoState = field(default_factory=TodoState)
    task_state: TaskState = field(default_factory=TaskState)
```

Modify `core/query/state.py`:

```python
    task_planning_required: bool = False
    task_planning_reason: str | None = None
    task_plan_invoked_this_turn: bool = False
```

Modify `core/tools/context.py`:

```python
class SessionUpdateKind(str, Enum):
    INVOKE_SKILL = "invoke_skill"
    SET_TODO_ITEMS = "set_todo_items"
    SET_TASK_STATE = "set_task_state"
    UPSERT_FILE_STATE = "upsert_file_state"
    INVALIDATE_FILE_STATE = "invalidate_file_state"
    APPEND_SKILL_EVENT = "append_skill_event"
```

Modify `core/query/reducers.py`:

```python
from core.tasks.projection import project_task_state_to_todo_items

...
    if update.kind == SessionUpdateKind.SET_TASK_STATE:
        next_state = payload.get("task_state")
        if next_state is not None:
            session_state.task_state = next_state
            session_state.todo_state.items = project_task_state_to_todo_items(next_state)
            session_state.todo_state.last_write_turn = next_state.last_planned_turn
        return
```

- [ ] **Step 5: 补齐 runtime control plane 回归断言**

Modify `tests/test_runtime_control_plane.py`:

```python
def test_run_state_starts_with_task_planning_flags() -> None:
    state = RunState()
    assert state.task_planning_required is False
    assert state.task_planning_reason is None
    assert state.task_plan_invoked_this_turn is False


def test_apply_session_update_sets_task_state_and_projects_todo() -> None:
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

    session = SessionState(conversation_messages=[])
    task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Inspect runtime",
                goal="Inspect runtime deeply",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
        last_planned_turn=5,
    )

    apply_session_update(
        session,
        SessionUpdate(
            kind=SessionUpdateKind.SET_TASK_STATE,
            payload={"task_state": task_state},
        ),
    )

    assert session.task_state.current_task_id == "task-1"
    assert session.todo_state.items[0].content == "Inspect runtime"
    assert session.todo_state.last_write_turn == 5
```

- [ ] **Step 6: 运行 foundation 测试并提交**

Run: `pytest tests/test_task_runtime_models.py tests/test_runtime_control_plane.py -q`
Expected: PASS

```bash
git add core/tasks/__init__.py core/tasks/models.py core/tasks/projection.py core/session/state.py core/query/state.py core/tools/context.py core/query/reducers.py tests/test_task_runtime_models.py tests/test_runtime_control_plane.py
git commit -m "feat: add task runtime foundation"
```

### Task 2: 实现 `task_plan` 工具和 planner runtime

**Files:**
- Create: `core/tasks/planner_runtime.py`
- Create: `core/tools/builtin/task_plan.py`
- Create: `tests/session/test_task_plan_tool.py`
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: 先写 `task_plan` tool 的 failing tests**

Create `tests/session/test_task_plan_tool.py`:

```python
from core.query.reducers import apply_session_update
from core.query.state import RunState
from core.session.state import SessionState
from core.tools.context import SessionUpdateKind, ToolUseContext


def _context(state: SessionState, tmp_path) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry="registry")
    ctx._set_call_identity(name="task_plan", call_id="toolu_task_plan", turn=3)
    return ctx


def test_task_plan_returns_set_task_state_update(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle(
        {
            "tasks": [
                {
                    "subject": "Inspect runtime",
                    "goal": "Find the runtime boundary bug",
                    "status": "in_progress",
                    "execution_mode": "local",
                }
            ]
        },
        _context(state, tmp_path),
    )

    assert result.status.value == "success"
    assert [u.kind for u in result.session_updates] == [SessionUpdateKind.SET_TASK_STATE]

    apply_session_update(state, result.session_updates[0])
    assert state.task_state.current_task_id == "task-1"
    assert state.todo_state.items[0].content == "Inspect runtime"


def test_task_plan_rejects_multiple_in_progress_tasks(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle(
        {
            "tasks": [
                {"subject": "A", "goal": "A", "status": "in_progress"},
                {"subject": "B", "goal": "B", "status": "in_progress"},
            ]
        },
        _context(state, tmp_path),
    )

    assert result.status.value == "failure"
    assert result.error == "validation_failed"


def test_task_plan_allows_empty_rewrite_to_clear_task_state(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle({"tasks": []}, _context(state, tmp_path))
    assert result.status.value == "success"
```

- [ ] **Step 2: 运行 tests，确认 `task_plan` 尚未实现**

Run: `pytest tests/session/test_task_plan_tool.py tests/test_tool_registry.py -q`
Expected: FAIL，缺少 `task_plan` 工具与 registry 期望

- [ ] **Step 3: 创建 planner runtime，负责 normalize / allocate / build**

Create `core/tasks/planner_runtime.py`:

```python
from __future__ import annotations

from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

MAX_TASKS = 20


def normalize_task_payload(raw: dict, *, index: int, previous: TaskState) -> TaskRecord:
    task_id = raw.get("task_id") or f"task-{index + 1}"
    subject = str(raw["subject"]).strip()
    goal = str(raw["goal"]).strip()
    status = TaskStatus(raw.get("status", "pending"))
    execution_mode = TaskExecutionMode(raw.get("execution_mode", "local"))
    return TaskRecord(
        task_id=task_id,
        subject=subject,
        goal=goal,
        status=status,
        execution_mode=execution_mode,
        parent_todo_id=raw.get("parent_todo_id"),
        active_form=raw.get("active_form"),
        agent_type=raw.get("agent_type"),
        allowed_tools=list(raw.get("allowed_tools") or []),
        write_scope=list(raw.get("write_scope") or []),
        inputs=list(raw.get("inputs") or []),
        known_context=list(raw.get("known_context") or []),
        out_of_scope=list(raw.get("out_of_scope") or []),
        required_skills=list(raw.get("required_skills") or []),
        expected_output=list(raw.get("expected_output") or []),
        done_criteria=list(raw.get("done_criteria") or []),
        depends_on=list(raw.get("depends_on") or []),
        owner=raw.get("owner"),
        blocked_reason=raw.get("blocked_reason"),
    )


def validate_tasks(raw_tasks: list[dict], *, previous: TaskState) -> None:
    if len(raw_tasks) > MAX_TASKS:
        raise ValueError(f"task count exceeds limit: {MAX_TASKS}")
    normalized_statuses = [task.get("status", "pending") for task in raw_tasks]
    if sum(1 for status in normalized_statuses if status == "in_progress") > 1:
        raise ValueError("at most one in_progress task is allowed")


def build_task_state(raw_tasks: list[dict], *, previous: TaskState, turn_count: int) -> TaskState:
    validate_tasks(raw_tasks, previous=previous)
    tasks = [normalize_task_payload(raw, index=i, previous=previous) for i, raw in enumerate(raw_tasks)]
    tasks_by_id = {task.task_id: task for task in tasks}
    ordered_task_ids = [task.task_id for task in tasks]
    current_task_id = next((task.task_id for task in tasks if task.status == TaskStatus.IN_PROGRESS), None)
    return TaskState(
        tasks_by_id=tasks_by_id,
        ordered_task_ids=ordered_task_ids,
        current_task_id=current_task_id,
        last_planned_turn=turn_count,
    )
```

- [ ] **Step 4: 创建 `task_plan` tool，并让它返回 `SET_TASK_STATE`**

Create `core/tools/builtin/task_plan.py`:

```python
from __future__ import annotations

from typing import Any

from core.tasks.planner_runtime import build_task_state
from ..context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "task_plan",
    "description": (
        "Rewrite the current TaskState for non-trivial multi-step work. "
        "Use this before execution when the request has multiple independent goals, "
        "requires research + implementation + verification, needs subagent dispatch, "
        "or spans multiple files/modules. Submit the full replacement task list each time; "
        "this tool does not do incremental patching. When TaskState is active, do not use todo "
        "as the source of truth."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "subject": {"type": "string"},
                        "goal": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "blocked", "completed", "failed", "cancelled"],
                        },
                        "execution_mode": {
                            "type": "string",
                            "enum": ["local", "fresh_subagent", "fork_subagent"],
                        },
                    },
                    "required": ["subject", "goal"],
                },
            }
        },
        "required": ["tasks"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    state = context.session_state
    if state is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="no_state",
            messages=[make_tool_message(context, "No session state available")],
        )

    try:
        next_state = build_task_state(
            args.get("tasks", []),
            previous=state.task_state,
            turn_count=context.turn_count,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="validation_failed",
            messages=[make_tool_message(context, f"Task planning failed: {exc}")],
        )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.SET_TASK_STATE,
                payload={"task_state": next_state},
            )
        ],
        messages=[make_tool_message(context, f"TaskState rewritten with {len(next_state.ordered_task_ids)} tasks.")],
    )
```

- [ ] **Step 5: 更新 registry expectations**

Modify `tests/test_tool_registry.py`:

```python
    def test_expected_tools_registered(self):
        names = {schema["name"] for schema in registry.schemas()}
        expected = {
            "bash",
            "edit_file",
            "find",
            "read_file",
            "skill",
            "task_plan",
            "todo",
            "write_file",
        }
        assert names == expected
```

- [ ] **Step 6: 运行 planner tool 测试并提交**

Run: `pytest tests/session/test_task_plan_tool.py tests/test_tool_registry.py -q`
Expected: PASS

```bash
git add core/tasks/planner_runtime.py core/tools/builtin/task_plan.py tests/session/test_task_plan_tool.py tests/test_tool_registry.py
git commit -m "feat: add task plan tool"
```

### Task 3: 让模型看到权威 `TaskState`，并保持现有 todo UI 兼容

**Files:**
- Modify: `core/prompt/system_context.py`
- Modify: `core/prompt/assembler.py`
- Modify: `core/query/loop.py`
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/session/test_state_assembled_runtime.py`
- Modify: `tests/test_query_display.py`

- [ ] **Step 1: 先写 failing tests，锁定 `<task-state>` 渲染与 display 兼容**

Modify `tests/session/test_prompt_assembler.py`:

```python
def test_build_stable_switches_from_todo_guidance_to_task_plan_guidance(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "复杂多步骤任务优先使用 task_plan" in stable
    assert "TaskState 激活时不要再把 todo 当权威状态" in stable


def test_build_runtime_context_prefers_task_state_over_todo_state(tmp_path: Path) -> None:
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

    state = make_state(tmp_path)
    state.todo_state.items = [TodoItem(content="Legacy todo", active_form="Legacy todo", status="in_progress")]
    state.task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Inspect runtime",
                goal="Inspect runtime deeply",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
    )

    result = PromptAssembler().build_runtime_context(state, working_dir=str(tmp_path))

    assert "<task-state" in result
    assert "Inspect runtime" in result
    assert "<todo-state>" not in result
```

Modify `tests/session/test_state_assembled_runtime.py`:

```python
def test_runtime_view_survives_when_task_state_replaces_todo_state(tmp_path: Path) -> None:
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

    state = SessionState(
        conversation_messages=[{"role": "user", "content": "Inspect the runtime"}],
    )
    state.task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Inspect runtime",
                goal="Inspect runtime deeply",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
    )

    builder = MessageViewBuilder()
    assembler = PromptAssembler()
    prepared = _build_prepared(state, assembler, tmp_path)
    view = builder.build(prepared, run_state=RunState())

    assert "<task-state" in view.system
    assert "Inspect runtime" in view.system
    assert "<todo-state>" not in view.system
```

Modify `tests/test_query_display.py` with one regression:

```python
def test_query_loop_treats_set_task_state_as_progress_update() -> None:
    ...
    assert renderer.progress_calls, "task_plan should still refresh projected todo UI"
```

- [ ] **Step 2: 运行相关 tests，确认当前 prompt 仍然写死 `todo`**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_state_assembled_runtime.py tests/test_query_display.py -q`
Expected: FAIL，stable prompt 仍然要求 `todo`，runtime context 没有 `<task-state>`

- [ ] **Step 3: 更新 system prompt 文案**

Modify `core/prompt/system_context.py`:

```python
_FRAMEWORK_PROMPT = """\
你是一个 AI 助手，运行在 harness 代理框架中。
你有以下可用工具：文件读写、文件搜索、文件编辑、bash 命令执行。工具的详细用法见各工具的描述。

判断用户意图：日常对话直接回答，需要操作时使用工具。
复杂多步骤任务优先使用 task_plan 生成 TaskState，再开始执行。
TaskState 激活时不要再把 todo 当权威状态；todo 只是从任务投影出的用户视图。
如果 skill 刚展开，而任务明显进入多步骤工作流，在继续深入执行之前先考虑 task_plan。
优先使用工具而非文字描述。

## Skills
...
"""
```

- [ ] **Step 4: 在 PromptAssembler 中渲染 `<task-state>`**

Modify `core/prompt/assembler.py`:

```python
def _render_task_state(task_state) -> str:
    if not task_state.tasks_by_id:
        return ""
    current = task_state.current_task_id or ""
    lines = [f'<task-state current_task_id="{current}">']
    for task_id in task_state.ordered_task_ids:
        task = task_state.tasks_by_id[task_id]
        label = task.active_form or task.subject
        lines.append(
            f'  <task id="{task.task_id}" status="{task.status}" mode="{task.execution_mode}">{label}</task>'
        )
    lines.append("</task-state>")
    return "\n".join(lines)


def build_runtime_context(self, state: SessionState, *, working_dir: str, char_budget: int | None = None) -> str:
    ...
    task_xml = _render_task_state(state.task_state)
    if task_xml:
        parts.append(task_xml)
    else:
        todo_xml = _render_todo_state(state.todo_state.items)
        if todo_xml:
            parts.append(todo_xml)
    ...


def build_internal_runtime_view(self, state: SessionState, run_state: RunState) -> dict[str, object]:
    return {
        "invoked_skills": list(state.invoked_skills.keys()),
        "todo_items": [item.active_form for item in state.todo_state.items],
        "task_ids": list(state.task_state.ordered_task_ids),
        "current_task_id": state.task_state.current_task_id,
        "read_file_state": dict(state.read_file_state),
        "transition": run_state.transition.value if run_state.transition is not None else None,
    }
```

- [ ] **Step 5: 让 QueryLoop 把 `SET_TASK_STATE` 当成一次可展示的进度写入**

Modify `core/query/loop.py`:

```python
def _todo_write_succeeded(batch: ToolBatchResult) -> bool:
    return any(
        update.kind in {SessionUpdateKind.SET_TODO_ITEMS, SessionUpdateKind.SET_TASK_STATE}
        for update in batch.session_updates
    )


def _tool_fallback_fragment(call: ToolCall) -> str | None:
    ...
    if name == "task_plan":
        return "更新任务计划"
```

- [ ] **Step 6: 运行 prompt / display 回归并提交**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_state_assembled_runtime.py tests/test_query_display.py -q`
Expected: PASS

```bash
git add core/prompt/system_context.py core/prompt/assembler.py core/query/loop.py tests/session/test_prompt_assembler.py tests/session/test_state_assembled_runtime.py tests/test_query_display.py
git commit -m "feat: render task state in runtime prompt"
```

### Task 4: 落地 `TaskPlanningPolicy`、pre-plan gate 与 legacy `todo` 拒绝逻辑

**Files:**
- Create: `core/policy/task_planning.py`
- Modify: `core/tools/runtime.py`
- Modify: `core/tools/builtin/todo.py`
- Modify: `01_agent_loop.py`
- Create: `tests/test_task_planning_policy.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 先写 policy 和 gate 的 failing tests**

Create `tests/test_task_planning_policy.py`:

```python
from core.policy.task_planning import PREPLAN_ALLOWED_TOOLS, TaskPlanningPolicy
from core.query.state import RunState
from core.session.state import SessionState


def test_task_planning_policy_injects_reminder_when_flag_is_set() -> None:
    policy = TaskPlanningPolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState(task_planning_required=True, task_planning_reason="post_skill")

    messages = policy.before_model_call(state, run_state)

    assert len(messages) == 1
    assert "task_planning" in messages[0]["content"]
    assert "task_plan" in messages[0]["content"]
    assert run_state.allowed_tools_override == PREPLAN_ALLOWED_TOOLS


def test_task_planning_policy_is_silent_when_task_state_exists() -> None:
    policy = TaskPlanningPolicy()
    state = SessionState(conversation_messages=[])
    state.task_state.ordered_task_ids = ["task-1"]
    run_state = RunState(task_planning_required=True)

    assert policy.before_model_call(state, run_state) == []
```

Append to `tests/test_runtime_control_plane.py`:

```python
def test_runtime_rejects_non_preplan_tools_when_gate_is_active(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_BarrierTool)
    reg.register(_TodoTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState(task_planning_required=True, allowed_tools_override={"task_plan", "skill", "find", "read_file"})
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch(
        [ToolCall(idx=0, name="bash", call_id="toolu_bash", args={})],
        run_state=run_state,
        apply_session_update=lambda update: apply_session_update(session_state, update),
        apply_run_update=apply_run_update,
    )

    assert batch.tool_statuses == [ToolOutcomeStatus.BLOCKED]
    assert "Task planning required before execution" in batch.messages[0]["content"]
```

Modify `tests/session/test_todo_tool.py`:

```python
def test_todo_rejects_writes_when_task_state_is_active(tmp_path) -> None:
    from core.query.reducers import apply_session_update
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    state.task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Inspect runtime",
                goal="Inspect runtime deeply",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
    )
    context = _make_context(tmp_path, state)

    result = handle(
        {
            "items": [
                {
                    "content": "Legacy todo write",
                    "active_form": "Legacy todo write",
                    "status": "in_progress",
                }
            ]
        },
        context,
    )

    assert result.status.value == "failure"
    assert result.error == "task_state_active"
```

- [ ] **Step 2: 运行 tests，确认当前没有 TaskPlanningPolicy**

Run: `pytest tests/test_task_planning_policy.py tests/test_runtime_control_plane.py tests/session/test_todo_tool.py -q`
Expected: FAIL，缺少 `core.policy.task_planning`，`todo` 仍然允许在 task_state 激活时写入

- [ ] **Step 3: 创建 `TaskPlanningPolicy`，只负责提醒和 gate，不做 fuzzy intent parsing**

Create `core/policy/task_planning.py`:

```python
from __future__ import annotations

PREPLAN_ALLOWED_TOOLS = {"task_plan", "skill", "find", "read_file"}


class TaskPlanningPolicy:
    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        if session_state.task_state.tasks_by_id:
            run_state.allowed_tools_override = None
            return []
        if not run_state.task_planning_required:
            return []

        run_state.allowed_tools_override = set(PREPLAN_ALLOWED_TOOLS)
        return [{
            "role": "user",
            "content": (
                "<system-reminder type=\"task_planning\">\n"
                "Current request requires task planning before execution.\n"
                "Call `task_plan` with the full task list first.\n"
                "You may still use lightweight read-only discovery tools if needed, "
                "but do not edit files, dispatch subagents, or start execution until TaskState exists.\n"
                "</system-reminder>"
            ),
        }]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        if any(name == "task_plan" for name in batch_result.tool_names):
            run_state.task_planning_required = False
            run_state.task_planning_reason = None
            run_state.allowed_tools_override = None
        elif any(name == "skill" for name in batch_result.tool_names) and not session_state.task_state.tasks_by_id:
            run_state.task_planning_required = True
            run_state.task_planning_reason = "post_skill"
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
```

- [ ] **Step 4: 在 todo tool 和 ToolRuntime 中补上拒绝 / gate 文案**

Modify `core/tools/builtin/todo.py`:

```python
    if getattr(state, "task_state", None) is not None and state.task_state.tasks_by_id:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="task_state_active",
            messages=[
                make_tool_message(
                    context,
                    "TaskState is active. Update tasks via `task_plan` / task runtime, not `todo`.",
                )
            ],
        )
```

Modify `core/tools/runtime.py`:

```python
    def _make_rejected_outcome(self, call: ToolCall, allowed_tools: set[str]) -> ToolInvocationOutcome:
        allowed_str = ", ".join(sorted(allowed_tools))
        message = (
            "Task planning required before execution. Call `task_plan` first."
            if "task_plan" in allowed_tools
            else f"Tool '{call.name}' rejected: not allowed by runtime policy. allowed_tools=[{allowed_str}]"
        )
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.BLOCKED,
            error="runtime_policy_block",
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": message,
                }
            ],
        )
```

- [ ] **Step 5: 把新 policy 接进主入口，并放到 `SkillRelevancePolicy` 前面**

Modify `01_agent_loop.py`:

```python
from core.policy.task_planning import TaskPlanningPolicy

...
        policy_runner=PolicyRunner([
            TaskPlanningPolicy(),
            TodoPlanningPolicy(),
            SkillRelevancePolicy(model_gateway=model_gateway),
            SkillUsageNudgePolicy(),
            MaxTurnsPolicy(MAX_TURNS),
        ]),
```

- [ ] **Step 6: 运行 gate / todo / policy 测试并提交**

Run: `pytest tests/test_task_planning_policy.py tests/test_runtime_control_plane.py tests/session/test_todo_tool.py -q`
Expected: PASS

```bash
git add core/policy/task_planning.py core/tools/runtime.py core/tools/builtin/todo.py 01_agent_loop.py tests/test_task_planning_policy.py tests/test_runtime_control_plane.py tests/session/test_todo_tool.py
git commit -m "feat: enforce task planning before execution"
```

### Task 5: 引入显式 `task_execute(task_id)` 和 fresh subagent 路由

**Files:**
- Create: `core/tasks/dispatcher.py`
- Create: `core/tools/builtin/task_execute.py`
- Modify: `core/session/subagent.py`
- Create: `tests/session/test_task_execute_tool.py`
- Create: `tests/session/test_subagent_runtime.py`
- Modify: `tests/test_tool_registry.py`
- Modify: `core/query/loop.py`

- [ ] **Step 1: 先写 execution path 的 failing tests**

Create `tests/session/test_task_execute_tool.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

from core.session.state import SessionState
from core.tools.context import ToolUseContext
from core.tasks.models import TaskExecutionMode, TaskRecord, TaskStatus


def _context(state: SessionState, tmp_path) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=None)
    ctx._set_call_identity(name="task_execute", call_id="toolu_task_execute", turn=5)
    return ctx


def test_task_execute_rejects_local_tasks(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Edit file inline",
        goal="Edit file inline",
        status=TaskStatus.PENDING,
        execution_mode=TaskExecutionMode.LOCAL,
    )
    state.task_state.ordered_task_ids = ["task-1"]

    result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "failure"
    assert result.error == "local_task"


def test_task_execute_routes_fresh_subagent_tasks(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Inspect runtime deeply",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
    )
    state.task_state.ordered_task_ids = ["task-1"]
    state.task_state.current_task_id = "task-1"

    fake_result = SimpleNamespace(
        success=True,
        output="done",
        files_modified=[],
        stop_reason=SimpleNamespace(value="completed"),
    )

    with patch("core.tools.builtin.task_execute.SubagentRuntime.run", return_value=fake_result) as run_mock:
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "success"
    assert run_mock.called
```

- [ ] **Step 2: 运行首批 tests，确认 execution path 尚未存在**

Run: `pytest tests/session/test_task_execute_tool.py tests/test_tool_registry.py -q`
Expected: FAIL，缺少 `task_execute` 工具、`dispatcher.py` 和 `SubagentRequest.task_packet`

- [ ] **Step 3: 创建 dispatcher，先只实现 Phase C 需要的最小 helper**

Create `core/tasks/dispatcher.py`:

```python
from __future__ import annotations

from core.session.subagent import SubagentRunResult
from .models import TaskPacket, TaskRecord, TaskRunResult, TaskStatus


def compile_task_packet(task: TaskRecord) -> TaskPacket:
    expected_output = task.expected_output or [task.goal]
    return TaskPacket(
        task_id=task.task_id,
        mode=task.execution_mode,
        agent_type=task.agent_type,
        title=task.subject,
        directive=task.goal,
        task_context=list(task.inputs),
        known_facts=list(task.known_context),
        out_of_scope=list(task.out_of_scope),
        expected_output=expected_output,
        done_criteria=list(task.done_criteria),
        required_skills=list(task.required_skills),
        allowed_tools=list(task.allowed_tools),
        write_scope=list(task.write_scope),
        packet_revision=task.packet_revision + 1,
    )


def normalize_subagent_result(task_id: str, result: SubagentRunResult) -> TaskRunResult:
    return TaskRunResult(
        task_id=task_id,
        success=result.success,
        status=TaskStatus.COMPLETED if result.success else TaskStatus.FAILED,
        summary=result.output,
        files_modified=list(result.files_modified),
        failure_reason=None if result.success else result.stop_reason.value,
    )
```

- [ ] **Step 4: 升级 SubagentRuntime，接收 `TaskPacket` 并 preload required skills**

Modify `core/session/subagent.py`:

```python
from dataclasses import dataclass, field

from core.skills.runtime import build_invoked_skill_record
from core.tasks.models import TaskPacket

...
@dataclass
class SubagentRequest:
    task_packet: TaskPacket
    agent_type: SubagentType = SubagentType.GENERAL
    description: str | None = None
    max_turns: int | None = None
    preloaded_skill_ids: list[str] = field(default_factory=list)


def _render_fresh_packet(packet: TaskPacket) -> str:
    sections = [
        f"Task: {packet.title}",
        "",
        "Directive:",
        packet.directive,
    ]
    if packet.known_facts:
        sections.extend(["", "Known facts:"] + [f"- {item}" for item in packet.known_facts])
    if packet.out_of_scope:
        sections.extend(["", "Out of scope:"] + [f"- {item}" for item in packet.out_of_scope])
    if packet.expected_output:
        sections.extend(["", "Expected output:"] + [f"- {item}" for item in packet.expected_output])
    if packet.done_criteria:
        sections.extend(["", "Done criteria:"] + [f"- {item}" for item in packet.done_criteria])
    return "\n".join(sections)


def _preload_required_skills(engine: SessionEngine, parent_context: ToolUseContext | None, skill_ids: list[str], turn: int) -> None:
    if parent_context is None or parent_context.skill_registry is None:
        return
    for skill_id in skill_ids:
        content = parent_context.skill_registry.load(skill_id)
        record = build_invoked_skill_record(
            state=engine.state,
            skill_id=skill_id,
            content=content,
            turn=turn,
        )
        engine.state.invoked_skills[skill_id] = record


def run(self, request: SubagentRequest) -> SubagentRunResult:
    ...
    prompt_text = _render_fresh_packet(request.task_packet)
    ...
    _preload_required_skills(engine, self._parent_context, request.preloaded_skill_ids, turn=0)
    result = engine.submit_user_message(prompt_text)
```

- [ ] **Step 5: 补上 subagent runtime focused tests**

Create `tests/session/test_subagent_runtime.py`:

```python
from pathlib import Path
from types import SimpleNamespace

from core.session.subagent import _render_fresh_packet, _preload_required_skills
from core.tasks.models import TaskExecutionMode, TaskPacket


def test_render_fresh_packet_contains_expected_sections() -> None:
    packet = TaskPacket(
        task_id="task-1",
        mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_type="general",
        title="Inspect runtime",
        directive="Inspect runtime deeply",
        known_facts=["QueryLoop batches readonly tools"],
        out_of_scope=["Do not edit files"],
        expected_output=["A short diagnosis"],
        done_criteria=["At least one runtime breakpoint identified"],
    )

    rendered = _render_fresh_packet(packet)

    assert "Task: Inspect runtime" in rendered
    assert "Directive:" in rendered
    assert "Known facts:" in rendered
    assert "Do not edit files" in rendered


def test_preload_required_skills_records_invoked_skills(tmp_path: Path) -> None:
    from core.session.state import SessionState
    from core.skills import SkillRegistry
    from core.tools.context import ToolUseContext

    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Analysis Report\ndescription: test\n---\nUse the report workflow.",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    registry.discover(skill_dir.parent, working_dir=tmp_path)
    parent_context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent_context.bind_runtime(session_state=None, skill_registry=registry)

    engine = SimpleNamespace(state=SessionState(conversation_messages=[]))
    _preload_required_skills(engine, parent_context, ["analysis-report"], turn=0)

    assert "analysis-report" in engine.state.invoked_skills
```

- [ ] **Step 6: 创建 `task_execute` tool，并用 `SubagentRuntime` 执行 fresh task**

Create `core/tools/builtin/task_execute.py`:

```python
from __future__ import annotations

from typing import Any

from core.session.subagent import SubagentRequest, SubagentRuntime, SubagentType
from core.tasks.dispatcher import compile_task_packet, normalize_subagent_result
from core.tasks.models import TaskExecutionMode, TaskState, TaskStatus
from ..context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "task_execute",
    "description": "Execute a planned TaskState task by task_id. Use this only after task_plan has created TaskState.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": False}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    state = context.session_state
    if state is None or not state.task_state.tasks_by_id:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="missing_task_state",
            messages=[make_tool_message(context, "No TaskState available. Call `task_plan` first.")],
        )

    task_id = args["task_id"]
    task = state.task_state.tasks_by_id.get(task_id)
    if task is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_found",
            messages=[make_tool_message(context, f"Unknown task: {task_id}")],
        )

    if task.execution_mode == TaskExecutionMode.LOCAL:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="local_task",
            messages=[make_tool_message(context, "This task is marked local. Execute it in the main thread with normal tools.")],
        )
    if task.execution_mode != TaskExecutionMode.FRESH_SUBAGENT:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="unsupported_execution_mode",
            messages=[make_tool_message(context, f"Unsupported execution_mode for task_execute: {task.execution_mode}")],
        )

    packet = compile_task_packet(task)
    runtime = SubagentRuntime(parent_context=context)
    sub_result = runtime.run(
        SubagentRequest(
            task_packet=packet,
            agent_type=SubagentType.GENERAL,
            preloaded_skill_ids=list(task.required_skills),
        )
    )
    normalized = normalize_subagent_result(task.task_id, sub_result)

    next_task = task
    next_task.packet_revision = packet.packet_revision
    next_task.status = normalized.status
    next_task.result_summary = normalized.summary
    next_task.files_modified = list(normalized.files_modified)
    next_task.failure_reason = normalized.failure_reason

    next_state = TaskState(
        tasks_by_id={**state.task_state.tasks_by_id, task.task_id: next_task},
        ordered_task_ids=list(state.task_state.ordered_task_ids),
        current_task_id=task.task_id,
        last_planned_turn=state.task_state.last_planned_turn,
    )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        session_updates=[SessionUpdate(kind=SessionUpdateKind.SET_TASK_STATE, payload={"task_state": next_state})],
        messages=[make_tool_message(context, normalized.summary)],
    )
```

- [ ] **Step 7: 更新 registry 与 UI fallback，并提交**

Modify `tests/test_tool_registry.py` expected names to include `task_execute`.

Modify `core/query/loop.py`:

```python
    if name == "task_execute":
        task_id = args.get("task_id", "")
        return f"执行任务 {task_id}" if task_id else "执行任务"
```

Run: `pytest tests/session/test_task_execute_tool.py tests/session/test_subagent_runtime.py tests/test_tool_registry.py -q`
Expected: PASS

```bash
git add core/tasks/dispatcher.py core/tools/builtin/task_execute.py core/session/subagent.py core/query/loop.py tests/session/test_task_execute_tool.py tests/session/test_subagent_runtime.py tests/test_tool_registry.py
git commit -m "feat: add explicit task execution path"
```

### Task 6: 回归、收尾与阶段边界确认

**Files:**
- Modify: `tests/test_query_display.py`
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/test_runtime_control_plane.py`
- Modify: `tests/session/test_state_assembled_runtime.py`

- [ ] **Step 1: 运行 Phase A/B 的完整回归子集**

Run:

```bash
pytest \
  tests/test_task_runtime_models.py \
  tests/session/test_task_plan_tool.py \
  tests/test_task_planning_policy.py \
  tests/session/test_todo_tool.py \
  tests/session/test_prompt_assembler.py \
  tests/session/test_state_assembled_runtime.py \
  tests/test_runtime_control_plane.py \
  tests/test_tool_registry.py \
  tests/test_query_display.py -q
```

Expected: PASS

- [ ] **Step 2: 运行 Phase C 的 explicit execution 回归子集**

Run:

```bash
pytest \
  tests/session/test_task_execute_tool.py \
  tests/session/test_subagent_runtime.py -q
```

Expected: PASS

- [ ] **Step 3: 运行更大一层的 smoke regression，确认没有把旧 skill/todo/runtime 主路径打坏**

Run:

```bash
pytest \
  tests/session/test_todo_tool.py \
  tests/session/test_skill_tool.py \
  tests/test_skill_relevance_policy.py \
  tests/test_todo_planning_policy.py \
  tests/test_todo_planning_integration.py \
  tests/test_runtime_logging.py \
  tests/test_runtime_display_state.py -q
```

Expected: PASS

- [ ] **Step 4: 明确这次不做的事情，防止实现者 scope creep**

本计划完成后仍然**不要**实现：

```text
- fork_subagent runtime
- grouped todo projection (1 TODO -> N TASK)
- QueryLoop 自动 select_next_runnable_task()
- QueryLoop 自动根据 execution_mode 派发 subagent
- task_create / task_update / task_list / task_get 全家桶
```

- [ ] **Step 5: 最终提交**

```bash
git add 01_agent_loop.py core/tasks/__init__.py core/tasks/models.py core/tasks/projection.py core/tasks/planner_runtime.py core/tasks/dispatcher.py core/policy/task_planning.py core/tools/context.py core/tools/runtime.py core/tools/builtin/todo.py core/tools/builtin/task_plan.py core/tools/builtin/task_execute.py core/query/state.py core/query/reducers.py core/query/loop.py core/prompt/system_context.py core/prompt/assembler.py core/session/state.py core/session/subagent.py tests/test_task_runtime_models.py tests/session/test_task_plan_tool.py tests/test_task_planning_policy.py tests/session/test_task_execute_tool.py tests/session/test_subagent_runtime.py tests/session/test_todo_tool.py tests/session/test_prompt_assembler.py tests/session/test_state_assembled_runtime.py tests/test_runtime_control_plane.py tests/test_tool_registry.py tests/test_query_display.py
git commit -m "feat: land task-centered runtime foundation"
```

---

## Coverage Check

- `TaskState` / `TaskRecord` / `TaskPacket` / `TaskRunResult`：Task 1, Task 5
- `task_plan` schema / description / rewrite 语义：Task 2
- `todo_state` projection-only 模式：Task 1, Task 4
- `PromptAssembler` 渲染 `<task-state>`：Task 3
- `TaskPlanningPolicy` 与 pre-plan gate：Task 4
- `task_execute(task_id)` + `fresh_subagent`：Task 5
- `fork` 延后、不做自动 scheduler：Task 6

## Notes For Implementers

- 这份实现计划故意把“复杂请求的自动 intent detection”压到最小。Phase A 的 planning enforcement 主要靠：
  - `task_plan` tool description
  - 更新后的 stable system prompt
  - `skill` 之后触发的 replanning gate
  - runtime 对非 pre-plan 工具的拒绝
- 不要把 `TodoProjectionItem` 直接塞进 `TodoState.items`。Phase A 必须保留 `TodoItem` 类型兼容。
- 不要在 `task_plan` tool handler 里直接 mutate `SessionState`。必须走 `SessionUpdateKind.SET_TASK_STATE -> reducer`。
- `task_execute` 是 Phase C 的显式入口；不要让 QueryLoop 因为 `TaskRecord.execution_mode == fresh_subagent` 自动触发 subagent。
