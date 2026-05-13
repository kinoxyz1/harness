# Subagent Runtime Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `docs/superpowers/specs/2026-05-12-subagent-runtime-redesign.md` 落成一份可以直接执行的 implementation spec，完整修复当前 subagent runtime 的已知断点，并把协议、运行时可见性、planner 语义和并发边界一次收口。

**Architecture:** 采用“先锁定协议面，再修执行链路，最后补并发验收”的顺序。实现上不新增第二套 runtime，而是在现有 `task_plan` / `task_execute` / `SubagentRuntime` / `ToolExecutorRuntime` 上做收口：删除假能力、简化任务模型、把子代理事件通过 renderer bridge 回传父代理、把 planner 改成 upsert + history preservation。

**Tech Stack:** Python 3.10+, dataclasses, pytest, Rich renderer, 现有 `SessionEngine` / `QueryLoop` / `ToolExecutorRuntime` / `ToolUseContext` / `TaskState`

---

## Context

- 设计文档：`docs/superpowers/specs/2026-05-12-subagent-runtime-redesign.md`
- 对照参考：本地 Claude Code 镜像 ` /Users/kino/works/opensource/Claude-Code-doc `
- 已确认的关键事实：
  - harness 当前的主要问题是真实存在的，不是文档夸大：`agent_type` 被覆盖、任务上下文丢失、工具事件不是实时显示、`task_plan` schema 过窄、`depends_on` 不生效、`fork` 是假暴露能力。
  - Claude Code 确实有子代理事件可见性和分 agent 的 prompt 约束，但它不是“任务对象全继承 + 全结构化结果”的设计；因此本计划只借鉴其 runtime 边界，不照抄错误结论。

## Scope

本计划包含：

1. 删除 `fork` 的公共表面。
2. 精简 `TaskRecord` / `TaskPacket` / `TaskRunResult` 到 redesign 定稿字段。
3. 扩展 `task_plan` schema，并把 planner 变成增量 upsert。
4. 修复 `task_execute` 的 `agent_type`、`depends_on`、状态归一化和并发标记。
5. 把 `ToolExecutorRuntime` 改成实时渲染。
6. 为 `SubagentRuntime` 增加 `SubagentBridgeRenderer` 和 task-scoped 事件桥接。
7. 补齐 acceptance tests，覆盖 stop reason、files_modified、并发隔离。

本计划不包含：

- 后台异步 subagent 队列。
- Claude Code 风格的 transcript toggle UI。
- 真正的 fork runtime。
- 基于 `bash` 输出猜测 `files_modified`。

## File Map

| 文件 | 操作 | 责任 |
| --- | --- | --- |
| `core/tasks/models.py` | Modify | 删除死字段，冻结任务协议面 |
| `core/tasks/projection.py` | Modify | 兼容精简后的 `TaskRecord` 投影到 todo |
| `core/tasks/planner_runtime.py` | Modify | 实现 schema 对应的 normalize + merge 语义 |
| `core/tasks/dispatcher.py` | Modify | 编译新 `TaskPacket`，归一化新 `TaskRunResult` |
| `core/tools/builtin/task_plan.py` | Modify | 扩展输入 schema，移除 `fork_subagent` |
| `core/tools/builtin/task_execute.py` | Modify | 修复 `agent_type`、依赖检查、状态写回、并发标记 |
| `core/session/subagent.py` | Modify | 删除 fake fork，加入 bridge renderer，保证 child state 隔离 |
| `core/tools/runtime.py` | Modify | 实时渲染工具调用/结果，删除 batch replay |
| `core/session/engine.py` | Modify | 让 child engine 显式接收 `tools` 和 `renderer` |
| `tests/test_task_runtime_models.py` | Modify | 模型和 projection 契约测试 |
| `tests/session/test_task_plan_tool.py` | Modify | schema、merge、legacy mode 测试 |
| `tests/session/test_task_execute_tool.py` | Modify | `agent_type`、`depends_on`、状态写回测试 |
| `tests/session/test_subagent_runtime.py` | Modify | prompt、bridge、child isolation、stop reason 测试 |
| `tests/test_tool_runtime.py` | Create | runtime 实时渲染顺序测试 |

## Implementation Rules

1. 先补测试，再补实现。
2. 每个任务只提交一个清晰边界的 commit，不跨任务混改。
3. 删除字段时要同步清理所有读路径，不能留下“字段没了但旧逻辑还在偷偷访问”的半残状态。
4. `files_modified` 只认 `RunUpdateKind.MARK_FILE_MODIFIED`。
5. `stop_reason` 允许值固定为：`completed`、`max_turns`、`api_error`、`empty_response`、`cancelled`。
6. `fork_subagent` 一律视为 legacy error，不做兼容实现。

### Task 1: 冻结数据模型和公共协议面

**Files:**
- Modify: `core/tasks/models.py`
- Modify: `core/tasks/projection.py`
- Modify: `tests/test_task_runtime_models.py`

- [ ] **Step 1: 先写模型契约的失败测试**

在 `tests/test_task_runtime_models.py` 追加以下测试：

```python
from dataclasses import fields

from core.session.state import SessionState
from core.tasks.models import (
    TaskExecutionMode,
    TaskPacket,
    TaskRecord,
    TaskRunResult,
    TaskState,
    TaskStatus,
)
from core.tasks.projection import project_task_state_to_todo_items


def test_task_execution_mode_only_exposes_local_and_fresh_subagent() -> None:
    assert [mode.value for mode in TaskExecutionMode] == ["local", "fresh_subagent"]


def test_task_record_fields_match_redesign_contract() -> None:
    assert [field.name for field in fields(TaskRecord)] == [
        "task_id",
        "subject",
        "goal",
        "status",
        "execution_mode",
        "agent_type",
        "description",
        "done_criteria",
        "depends_on",
        "result_summary",
        "files_modified",
        "stop_reason",
        "created_at_turn",
        "turns_used",
    ]


def test_task_packet_fields_match_redesign_contract() -> None:
    assert [field.name for field in fields(TaskPacket)] == [
        "task_id",
        "title",
        "directive",
        "done_criteria",
        "agent_type",
    ]


def test_task_run_result_fields_match_redesign_contract() -> None:
    assert [field.name for field in fields(TaskRunResult)] == [
        "task_id",
        "success",
        "status",
        "summary",
        "files_modified",
        "stop_reason",
        "turns_used",
    ]


def test_projection_uses_subject_for_active_form_after_task_simplification() -> None:
    task = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Find the runtime boundary bug",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.LOCAL,
    )
    state = TaskState(tasks_by_id={"task-1": task}, ordered_task_ids=["task-1"], current_task_id="task-1")

    items = project_task_state_to_todo_items(state)

    assert items[0].content == "Inspect runtime"
    assert items[0].active_form == "Inspect runtime"
    assert items[0].status == "in_progress"
```

- [ ] **Step 2: 运行测试，确认它们先失败**

Run: `pytest tests/test_task_runtime_models.py -v`

Expected:
- `TaskExecutionMode` 仍包含 `fork_subagent`
- `TaskRecord` / `TaskPacket` / `TaskRunResult` 字段列表不匹配
- `projection.py` 仍在访问 `task.active_form`

- [ ] **Step 3: 最小实现模型收口**

修改 `core/tasks/models.py`，把三个 dataclass 收口为 redesign 定稿结构：

```python
class TaskExecutionMode(str, Enum):
    LOCAL = "local"
    FRESH_SUBAGENT = "fresh_subagent"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode
    agent_type: str | None = None
    description: str | None = None
    done_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    result_summary: str | None = None
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    created_at_turn: int = 0
    turns_used: int = 0


@dataclass(slots=True)
class TaskPacket:
    task_id: str
    title: str
    directive: str
    done_criteria: list[str] = field(default_factory=list)
    agent_type: str | None = None


@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    turns_used: int = 0
```

同时修改 `core/tasks/projection.py`，移除对 `active_form` 的依赖：

```python
items.append(
    TodoItem(
        content=task.subject,
        active_form=task.subject,
        status=map_task_status_to_todo_status(task.status),
        workflow_ref=None,
    )
)
```

- [ ] **Step 4: 再跑模型测试，确认通过**

Run: `pytest tests/test_task_runtime_models.py -v`

Expected: `5 passed`

- [ ] **Step 5: 提交模型协议收口**

```bash
git add core/tasks/models.py core/tasks/projection.py tests/test_task_runtime_models.py
git commit -m "refactor: simplify task runtime data models"
```

### Task 2: 实现 `task_plan` 新 schema 和 merge 语义

**Files:**
- Modify: `core/tools/builtin/task_plan.py`
- Modify: `core/tasks/planner_runtime.py`
- Modify: `tests/session/test_task_plan_tool.py`

- [ ] **Step 1: 先写 planner 的失败测试**

在 `tests/session/test_task_plan_tool.py` 追加以下测试：

```python
from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus
from core.tasks.planner_runtime import build_task_state


def test_task_plan_schema_exposes_redesign_fields_without_fork() -> None:
    from core.tools.builtin.task_plan import SCHEMA

    item_props = SCHEMA["input_schema"]["properties"]["tasks"]["items"]["properties"]
    assert sorted(item_props) == sorted(
        [
            "task_id",
            "subject",
            "goal",
            "status",
            "execution_mode",
            "agent_type",
            "description",
            "done_criteria",
            "depends_on",
        ]
    )
    assert item_props["execution_mode"]["enum"] == ["local", "fresh_subagent"]


def test_build_task_state_preserves_terminal_tasks_and_cancels_omitted_live_tasks() -> None:
    previous = TaskState(
        tasks_by_id={
            "task-old-done": TaskRecord(
                task_id="task-old-done",
                subject="Done",
                goal="Done",
                status=TaskStatus.COMPLETED,
                execution_mode=TaskExecutionMode.LOCAL,
            ),
            "task-old-live": TaskRecord(
                task_id="task-old-live",
                subject="Live",
                goal="Live",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            ),
        },
        ordered_task_ids=["task-old-done", "task-old-live"],
    )

    next_state = build_task_state(
        [
            {
                "task_id": "task-new",
                "subject": "New",
                "goal": "New",
                "status": "in_progress",
                "execution_mode": "local",
            }
        ],
        previous=previous,
        turn_count=9,
    )

    assert next_state.ordered_task_ids == ["task-new", "task-old-done", "task-old-live"]
    assert next_state.tasks_by_id["task-old-done"].status == TaskStatus.COMPLETED
    assert next_state.tasks_by_id["task-old-live"].status == TaskStatus.CANCELLED
    assert next_state.current_task_id == "task-new"


def test_build_task_state_rejects_reopening_terminal_task_id() -> None:
    previous = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Done",
                goal="Done",
                status=TaskStatus.COMPLETED,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
    )

    try:
        build_task_state(
            [
                {
                    "task_id": "task-1",
                    "subject": "Done again",
                    "goal": "Done again",
                    "status": "pending",
                    "execution_mode": "local",
                }
            ],
            previous=previous,
            turn_count=4,
        )
    except ValueError as exc:
        assert "cannot reopen terminal task_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_task_plan_persists_description_done_criteria_and_depends_on(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle(
        {
            "tasks": [
                {
                    "task_id": "task-1",
                    "subject": "Inspect runtime",
                    "goal": "Fix subagent runtime",
                    "status": "in_progress",
                    "execution_mode": "fresh_subagent",
                    "agent_type": "plan",
                    "description": "Read task runtime and explain the breakpoints.",
                    "done_criteria": ["List root causes", "Name exact files"],
                    "depends_on": ["task-0"],
                }
            ]
        },
        _context(state, tmp_path),
    )

    next_state = result.session_updates[0].payload["task_state"]
    task = next_state.tasks_by_id["task-1"]
    assert task.agent_type == "plan"
    assert task.description == "Read task runtime and explain the breakpoints."
    assert task.done_criteria == ["List root causes", "Name exact files"]
    assert task.depends_on == ["task-0"]
```

- [ ] **Step 2: 运行 planner 测试，确认先失败**

Run: `pytest tests/session/test_task_plan_tool.py -v`

Expected:
- schema 断言失败，因为当前只有 5 个字段且还包含 `fork_subagent`
- merge 断言失败，因为当前是整表替换
- `description` / `done_criteria` / `depends_on` 尚未持久化到新模型字段

- [ ] **Step 3: 实现 schema 扩展和 merge 规则**

先改 `core/tools/builtin/task_plan.py`：

```python
"execution_mode": {
    "type": "string",
    "enum": ["local", "fresh_subagent"],
},
"agent_type": {
    "type": "string",
    "enum": ["explore", "plan", "general"],
},
"description": {"type": "string"},
"done_criteria": {"type": "array", "items": {"type": "string"}},
"depends_on": {"type": "array", "items": {"type": "string"}},
```

再改 `core/tasks/planner_runtime.py`，显式实现终态保留和未出现活任务自动取消：

```python
from dataclasses import replace

from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def normalize_task_payload(raw: dict, *, index: int, previous: TaskState | None) -> TaskRecord:
    task_id = raw.get("task_id") or f"task-{index + 1}"
    return TaskRecord(
        task_id=task_id,
        subject=str(raw["subject"]).strip(),
        goal=str(raw["goal"]).strip(),
        status=TaskStatus(raw.get("status", "pending")),
        execution_mode=TaskExecutionMode(raw.get("execution_mode", "local")),
        agent_type=raw.get("agent_type"),
        description=raw.get("description"),
        done_criteria=list(raw.get("done_criteria") or []),
        depends_on=list(raw.get("depends_on") or []),
    )


def build_task_state(raw_tasks: list[dict], *, previous: TaskState | None, turn_count: int) -> TaskState:
    validate_tasks(raw_tasks, previous=previous or TaskState())
    previous = previous or TaskState()
    incoming: list[TaskRecord] = []
    incoming_ids: set[str] = set()

    for index, raw in enumerate(raw_tasks):
        task = normalize_task_payload(raw, index=index, previous=previous)
        prev = previous.tasks_by_id.get(task.task_id)
        if prev is not None and prev.status in TERMINAL_STATUSES and task.status != prev.status:
            raise ValueError(f"cannot reopen terminal task_id: {task.task_id}")
        incoming.append(task)
        incoming_ids.add(task.task_id)

    preserved: list[TaskRecord] = []
    for task_id in previous.ordered_task_ids:
        if task_id in incoming_ids:
            continue
        prev = previous.tasks_by_id[task_id]
        preserved.append(prev if prev.status in TERMINAL_STATUSES else replace(prev, status=TaskStatus.CANCELLED))

    ordered = incoming + preserved
    tasks_by_id = {task.task_id: task for task in ordered}
    ordered_task_ids = [task.task_id for task in ordered]
    current_task_id = next((task.task_id for task in incoming if task.status == TaskStatus.IN_PROGRESS), None)
    return TaskState(
        tasks_by_id=tasks_by_id,
        ordered_task_ids=ordered_task_ids,
        current_task_id=current_task_id,
        last_planned_turn=turn_count,
    )
```

- [ ] **Step 4: 运行 planner 测试，确认通过**

Run: `pytest tests/session/test_task_plan_tool.py -v`

Expected: `6 passed`

- [ ] **Step 5: 提交 planner 语义修复**

```bash
git add core/tools/builtin/task_plan.py core/tasks/planner_runtime.py tests/session/test_task_plan_tool.py
git commit -m "feat: implement task plan merge semantics"
```

### Task 3: 编译新 `TaskPacket` 并定稿结果归一化

**Files:**
- Modify: `core/tasks/dispatcher.py`
- Modify: `tests/test_task_runtime_models.py`

- [ ] **Step 1: 先写 dispatcher 的失败测试**

在 `tests/test_task_runtime_models.py` 继续追加：

```python
from types import SimpleNamespace

from core.session.subagent import SubagentStopReason
from core.tasks.dispatcher import compile_task_packet, normalize_subagent_result


def test_compile_task_packet_prefers_description_and_done_criteria() -> None:
    task = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Fix subagent runtime",
        status=TaskStatus.PENDING,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_type="plan",
        description="Read runtime files and summarize the broken data flow.",
        done_criteria=["Name broken functions", "List exact files to edit"],
    )

    packet = compile_task_packet(task)

    assert packet.task_id == "task-1"
    assert packet.title == "Inspect runtime"
    assert packet.agent_type == "plan"
    assert "Fix subagent runtime" in packet.directive
    assert "Read runtime files and summarize the broken data flow." in packet.directive
    assert packet.done_criteria == ["Name broken functions", "List exact files to edit"]


def test_normalize_subagent_result_maps_stop_reason_and_turns() -> None:
    result = SimpleNamespace(
        success=False,
        output="max turns reached",
        files_modified=["core/tasks/models.py"],
        stop_reason=SubagentStopReason.MAX_TURNS,
        turns_used=12,
    )

    normalized = normalize_subagent_result("task-1", result)

    assert normalized.task_id == "task-1"
    assert normalized.status == TaskStatus.FAILED
    assert normalized.stop_reason == "max_turns"
    assert normalized.turns_used == 12
    assert normalized.files_modified == ["core/tasks/models.py"]


def test_normalize_subagent_result_marks_cancelled_tasks() -> None:
    result = SimpleNamespace(
        success=False,
        output="cancelled",
        files_modified=[],
        stop_reason=SubagentStopReason.CANCELLED,
        turns_used=3,
    )

    normalized = normalize_subagent_result("task-1", result)

    assert normalized.status == TaskStatus.CANCELLED
    assert normalized.stop_reason == "cancelled"
```

- [ ] **Step 2: 运行测试，确认 compile / normalize 断言先失败**

Run: `pytest tests/test_task_runtime_models.py -v`

Expected:
- `compile_task_packet()` 仍在生成旧字段
- `normalize_subagent_result()` 仍在写 `failure_reason`，且没有 `turns_used` / `stop_reason`

- [ ] **Step 3: 实现新的 packet 编译和 stop reason 归一化**

修改 `core/tasks/dispatcher.py`：

```python
from core.session.subagent import SubagentRunResult, SubagentStopReason
from .models import TaskPacket, TaskRecord, TaskRunResult, TaskStatus


def compile_task_packet(task: TaskRecord) -> TaskPacket:
    description = (task.description or "").strip()
    directive = task.goal.strip()
    if description:
        directive = f"{directive}\n\nContext:\n{description}"
    return TaskPacket(
        task_id=task.task_id,
        title=task.subject,
        directive=directive,
        done_criteria=list(task.done_criteria),
        agent_type=task.agent_type,
    )


def normalize_subagent_result(task_id: str, result: SubagentRunResult) -> TaskRunResult:
    status_map = {
        SubagentStopReason.COMPLETED: TaskStatus.COMPLETED,
        SubagentStopReason.CANCELLED: TaskStatus.CANCELLED,
        SubagentStopReason.MAX_TURNS: TaskStatus.FAILED,
        SubagentStopReason.API_ERROR: TaskStatus.FAILED,
        SubagentStopReason.EMPTY_RESPONSE: TaskStatus.FAILED,
    }
    return TaskRunResult(
        task_id=task_id,
        success=result.success,
        status=status_map[result.stop_reason],
        summary=result.output,
        files_modified=list(result.files_modified),
        stop_reason=result.stop_reason.value,
        turns_used=result.turns_used,
    )
```

- [ ] **Step 4: 再跑模型测试**

Run: `pytest tests/test_task_runtime_models.py -v`

Expected: `8 passed`

- [ ] **Step 5: 提交 dispatcher 收口**

```bash
git add core/tasks/dispatcher.py tests/test_task_runtime_models.py
git commit -m "feat: normalize subagent task packets and results"
```

### Task 4: 修复 `task_execute` 执行契约

**Files:**
- Modify: `core/tools/builtin/task_execute.py`
- Modify: `tests/session/test_task_execute_tool.py`

- [ ] **Step 1: 先写 `task_execute` 的失败测试**

在 `tests/session/test_task_execute_tool.py` 追加：

```python
from types import SimpleNamespace
from unittest.mock import patch

from core.tasks.models import TaskExecutionMode, TaskRecord, TaskStatus


def test_task_execute_uses_task_agent_type(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Inspect runtime deeply",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_type="plan",
    )
    state.task_state.ordered_task_ids = ["task-1"]

    fake_result = SimpleNamespace(
        success=True,
        output="done",
        files_modified=[],
        stop_reason=SimpleNamespace(value="completed"),
        turns_used=2,
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result) as run_mock:
        handle({"task_id": "task-1"}, _context(state, tmp_path))

    request = run_mock.call_args.args[0]
    assert request.agent_type.value == "plan"


def test_task_execute_fails_when_dependency_missing(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Inspect runtime deeply",
        status=TaskStatus.PENDING,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        depends_on=["task-0"],
    )
    state.task_state.ordered_task_ids = ["task-1"]

    result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "failure"
    assert result.error == "missing_dependency"


def test_task_execute_fails_when_dependency_not_completed(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-0"] = TaskRecord(
        task_id="task-0",
        subject="Prereq",
        goal="Prereq",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.LOCAL,
    )
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Inspect runtime deeply",
        status=TaskStatus.PENDING,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        depends_on=["task-0"],
    )
    state.task_state.ordered_task_ids = ["task-0", "task-1"]

    result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "failure"
    assert result.error == "dependency_not_ready"


def test_task_execute_writes_stop_reason_turns_and_files(tmp_path) -> None:
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

    fake_result = SimpleNamespace(
        success=False,
        output="cancelled by policy",
        files_modified=["core/tasks/models.py"],
        stop_reason=SimpleNamespace(value="cancelled"),
        turns_used=4,
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result):
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    next_task = result.session_updates[0].payload["task_state"].tasks_by_id["task-1"]
    assert next_task.status == TaskStatus.CANCELLED
    assert next_task.stop_reason == "cancelled"
    assert next_task.turns_used == 4
    assert next_task.files_modified == ["core/tasks/models.py"]


def test_task_execute_is_marked_concurrency_safe() -> None:
    from core.tools.builtin.task_execute import ANNOTATIONS

    assert ANNOTATIONS["concurrency_safe"] is True
```

- [ ] **Step 2: 运行 `task_execute` 测试，确认先失败**

Run: `pytest tests/session/test_task_execute_tool.py -v`

Expected:
- `request.agent_type` 仍然是 `general`
- 缺失依赖检查
- 任务状态里还没有 `stop_reason` / `turns_used`
- `concurrency_safe` 仍为 `False`

- [ ] **Step 3: 实现依赖检查和结果写回**

修改 `core/tools/builtin/task_execute.py`：

```python
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": True}


def _check_dependencies(task, state, context):
    for dep_id in task.depends_on:
        dep = state.task_state.tasks_by_id.get(dep_id)
        if dep is None:
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="missing_dependency",
                messages=[make_tool_message(context, f"Missing dependency: {dep_id}")],
            )
        if dep.status != TaskStatus.COMPLETED:
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="dependency_not_ready",
                messages=[make_tool_message(context, f"Dependency not completed: {dep_id}")],
            )
    return None


dep_error = _check_dependencies(task, state, context)
if dep_error is not None:
    return dep_error

agent_type = SubagentType(task.agent_type) if task.agent_type else SubagentType.GENERAL
sub_result = runtime.run(SubagentRequest(task_packet=packet, agent_type=agent_type))

next_task = replace(
    task,
    status=normalized.status,
    result_summary=normalized.summary,
    files_modified=list(normalized.files_modified),
    stop_reason=normalized.stop_reason,
    turns_used=normalized.turns_used,
)
```

- [ ] **Step 4: 跑 `task_execute` 测试**

Run: `pytest tests/session/test_task_execute_tool.py -v`

Expected: `7 passed`

- [ ] **Step 5: 提交 `task_execute` 修复**

```bash
git add core/tools/builtin/task_execute.py tests/session/test_task_execute_tool.py
git commit -m "feat: enforce subagent task execution contract"
```

### Task 5: 把 `ToolExecutorRuntime` 改成实时渲染

**Files:**
- Modify: `core/tools/runtime.py`
- Create: `tests/test_tool_runtime.py`

- [ ] **Step 1: 先写 runtime 渲染顺序测试**

Create `tests/test_tool_runtime.py`：

```python
from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState
from core.tools import ToolRegistry
from core.tools.context import ToolInvocationOutcome, ToolUseContext, make_tool_message
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class RecorderRenderer:
    def __init__(self) -> None:
        self.events = []

    def show_tool_call(self, name, args):
        self.events.append(("call", name))

    def show_tool_result(self, name, output):
        self.events.append(("result", name, output))

    def show_status(self, message):
        self.events.append(("status", message))


def _tool(name, *, readonly, content):
    class Module:
        SCHEMA = {"name": name, "input_schema": {"type": "object", "properties": {}}}
        READONLY = readonly
        ANNOTATIONS = {"readonly": readonly, "destructive": False, "idempotent": True, "concurrency_safe": readonly}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(messages=[make_tool_message(context, content)])

    return Module


def test_parallel_tools_render_all_calls_before_any_result(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_tool("find_a", readonly=True, content="A"))
    registry.register(_tool("find_b", readonly=True, content="B"))
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    renderer = RecorderRenderer()
    runtime = ToolExecutorRuntime(registry, context, renderer=renderer)

    runtime.execute_batch(
        [
            ToolCall(idx=0, name="find_a", call_id="call_0", args={}),
            ToolCall(idx=1, name="find_b", call_id="call_1", args={}),
        ],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(context.session_state, update),
        apply_run_update=apply_run_update,
    )

    assert renderer.events[0] == ("call", "find_a")
    assert renderer.events[1] == ("call", "find_b")
    assert {renderer.events[2][1], renderer.events[3][1]} == {"find_a", "find_b"}


def test_serial_tool_renders_call_then_result_before_next_tool(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_tool("write_a", readonly=False, content="write ok"))
    registry.register(_tool("write_b", readonly=False, content="write ok"))
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    renderer = RecorderRenderer()
    runtime = ToolExecutorRuntime(registry, context, renderer=renderer)

    runtime.execute_batch(
        [
            ToolCall(idx=0, name="write_a", call_id="call_0", args={}),
            ToolCall(idx=1, name="write_b", call_id="call_1", args={}),
        ],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(context.session_state, update),
        apply_run_update=apply_run_update,
    )

    assert renderer.events[:4] == [
        ("call", "write_a"),
        ("result", "write_a", "write ok"),
        ("call", "write_b"),
        ("result", "write_b", "write ok"),
    ]
```

- [ ] **Step 2: 运行 runtime 测试，确认先失败**

Run: `pytest tests/test_tool_runtime.py -v`

Expected: 当前实现会在 batch 结束后统一 replay，因此顺序断言失败。

- [ ] **Step 3: 把渲染移动到执行路径内**

修改 `core/tools/runtime.py`，新增两个 helper，并删除 `execute_batch()` 末尾的统一 replay：

```python
def _render_tool_call(self, call: ToolCall) -> None:
    if self._renderer is None or self._display.quiet:
        return
    if not self._should_render_generic_tool_event(call.name):
        return
    self._renderer.show_tool_call(call.name, call.args)


def _render_tool_result(self, call: ToolCall, outcome: ToolInvocationOutcome) -> None:
    if self._renderer is None or self._display.quiet:
        return
    if not self._should_render_generic_tool_event(call.name):
        return
    self._renderer.show_tool_result(call.name, self._first_content(outcome))
```

并在执行路径中使用它们：

```python
for call in batch.calls:
    self._render_tool_call(call)

with ThreadPoolExecutor(max_workers=len(executable_calls)) as pool:
    for future in as_completed(futures):
        call = futures[future]
        result = future.result()
        results[call.idx] = result
        self._render_tool_result(call, result)
```

串行路径同样调整：

```python
for call in batch.calls:
    self._render_tool_call(call)
    outcome = self._run_single(call, turn=turn)
    self._render_tool_result(call, outcome)
```

- [ ] **Step 4: 运行 runtime 测试**

Run: `pytest tests/test_tool_runtime.py -v`

Expected: `2 passed`

- [ ] **Step 5: 提交实时渲染修复**

```bash
git add core/tools/runtime.py tests/test_tool_runtime.py
git commit -m "fix: render tool events in real time"
```

### Task 6: 为 `SubagentRuntime` 增加 bridge renderer，并删除 fake fork

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `core/session/engine.py`
- Modify: `tests/session/test_subagent_runtime.py`

- [ ] **Step 1: 先写 subagent runtime 的失败测试**

在 `tests/session/test_subagent_runtime.py` 追加：

```python
from types import SimpleNamespace
from unittest.mock import patch

from core.query.result import QueryResult, StopReason
from core.session.state import SessionState
from core.tasks.models import TaskPacket
from core.tools.context import ToolUseContext


def test_render_fresh_packet_includes_done_criteria() -> None:
    packet = TaskPacket(
        task_id="task-1",
        title="Inspect runtime",
        directive="Fix subagent runtime\n\nContext:\nRead runtime files first.",
        done_criteria=["List exact files", "Explain the broken data flow"],
        agent_type="plan",
    )

    rendered = _render_fresh_packet(packet)

    assert "Task: Inspect runtime" in rendered
    assert "Read runtime files first." in rendered
    assert "Done criteria:" in rendered
    assert "Explain the broken data flow" in rendered


def test_coerce_stop_reason_maps_aborted_to_cancelled() -> None:
    assert coerce_stop_reason("aborted") == SubagentStopReason.CANCELLED


def test_subagent_runtime_emits_bridge_events_and_passes_tools(tmp_path) -> None:
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)
    packet = TaskPacket(
        task_id="task-1",
        title="Inspect runtime",
        directive="Fix runtime",
        done_criteria=["List files"],
        agent_type="general",
    )
    request = SubagentRequest(task_packet=packet, agent_type=SubagentType.GENERAL)
    captured = {}
    events = []

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.state = SessionState(conversation_messages=[])

        def submit_user_message(self, prompt):
            captured["prompt"] = prompt
            captured["renderer"].show_tool_call("find", {"pattern": "*.py"})
            captured["renderer"].show_tool_result("find", "core/tasks/models.py")
            return QueryResult(
                final_output="done",
                stop_reason=StopReason.COMPLETED,
                success=True,
                turns_used=2,
                files_modified=["core/tasks/models.py"],
            )

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        result = runtime.run(request, emit=events.append)

    assert captured["tools"] == captured["view_builder"].tools
    assert events[0]["event"] == "subagent_start"
    assert events[1]["event"] == "subagent_tool_call"
    assert events[2]["event"] == "subagent_tool_result"
    assert events[-1]["event"] == "subagent_done"
    assert all(event["task_id"] == "task-1" for event in events)
    assert result.stop_reason == SubagentStopReason.COMPLETED


def test_subagent_runtime_keeps_parent_session_state_isolated(tmp_path) -> None:
    parent_state = SessionState(conversation_messages=[])
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=parent_state, skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)

    class FakeEngine:
        def __init__(self, **kwargs):
            self.state = SessionState(conversation_messages=[])

        def submit_user_message(self, prompt):
            self.state.todo_state.items.append(SimpleNamespace(content="child", active_form="child", status="pending"))
            return QueryResult(final_output="done", stop_reason=StopReason.COMPLETED, success=True, turns_used=1)

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(
            SubagentRequest(
                task_packet=TaskPacket(task_id="task-1", title="Inspect runtime", directive="Fix runtime"),
                agent_type=SubagentType.GENERAL,
            )
        )

    assert parent_state.todo_state.items == []
```

- [ ] **Step 2: 运行 subagent runtime 测试，确认先失败**

Run: `pytest tests/session/test_subagent_runtime.py -v`

Expected:
- `coerce_stop_reason("aborted")` 目前返回 `EMPTY_RESPONSE`
- 当前 `run()` 不接受 `emit`
- 当前没有 `SubagentBridgeRenderer`
- 当前 `SessionEngine` 初始化未显式传 `tools=sub_schemas`

- [ ] **Step 3: 实现 bridge renderer、删掉 fork、显式 child 隔离**

修改 `core/session/subagent.py`：

```python
class SubagentType(str, Enum):
    EXPLORE = "explore"
    PLAN = "plan"
    GENERAL = "general"


class SubagentContextMode(str, Enum):
    FRESH = "fresh"


class SubagentStopReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    API_ERROR = "api_error"
    EMPTY_RESPONSE = "empty_response"
    CANCELLED = "cancelled"
```

添加 bridge renderer：

```python
class SubagentBridgeRenderer:
    def __init__(self, *, task_id: str, agent_type: str, emit):
        self._task_id = task_id
        self._agent_type = agent_type
        self._emit = emit

    def _base(self, event: str) -> dict[str, Any]:
        return {"event": event, "task_id": self._task_id, "agent_type": self._agent_type}

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._emit({**self._base("subagent_tool_call"), "tool_name": name, "tool_args": args})

    def show_tool_result(self, name: str, output: str) -> None:
        self._emit({**self._base("subagent_tool_result"), "tool_name": name, "content": output})

    def show_status(self, message: str) -> None:
        self._emit({**self._base("subagent_status"), "content": message})

    def show_thinking(self, title: str, reasoning: str) -> None:
        self._emit({**self._base("subagent_thinking"), "title": title, "content": reasoning})

    def show_assistant(self, content: str | None) -> None:
        if content:
            self._emit({**self._base("subagent_message"), "content": content})

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        return None

    def show_current_todo(self, item, completed: int, total: int) -> None:
        return None

    def show_progress(self, items) -> None:
        return None

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        return None

    def show_error(self, message: str) -> None:
        self._emit({**self._base("subagent_error"), "content": message})
```

把 `run()` 改成：

```python
def coerce_stop_reason(value: str) -> SubagentStopReason:
    if value == "aborted":
        return SubagentStopReason.CANCELLED
    try:
        return SubagentStopReason(value)
    except ValueError:
        return SubagentStopReason.EMPTY_RESPONSE


def run(self, request: SubagentRequest, emit=None) -> SubagentRunResult:
    definition = get_subagent_definition(request.agent_type)
    working_dir = self._parent_context.working_dir if self._parent_context else os.getcwd()
    max_turns = request.max_turns or definition.default_max_turns
    bridge = (
        SubagentBridgeRenderer(
            task_id=request.task_packet.task_id,
            agent_type=request.agent_type.value,
            emit=emit,
        )
        if emit is not None
        else None
    )
    child_display = RunDisplayOptions(quiet=emit is None)
    tool_context = ToolUseContext(working_dir=working_dir, max_turns=max_turns)
    engine = SessionEngine(
        model_gateway=ModelGateway(self._llm_factory()),
        tool_runtime=ToolExecutorRuntime(sub_registry, tool_context, display=child_display, renderer=bridge),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(max_turns)]),
        recovery=RecoveryManager(),
        view_builder=MessageViewBuilder(tools=sub_schemas),
        tools=sub_schemas,
        renderer=bridge,
    )
    if emit is not None:
        emit({"event": "subagent_start", "task_id": request.task_packet.task_id, "agent_type": request.agent_type.value})
    result = engine.submit_user_message(_render_fresh_packet(request.task_packet))
    stop_reason = coerce_stop_reason(result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason))
    if emit is not None:
        emit(
            {
                "event": "subagent_done",
                "task_id": request.task_packet.task_id,
                "agent_type": request.agent_type.value,
                "stop_reason": stop_reason.value,
                "turns_used": result.turns_used,
            }
        )
```

并把 `_render_fresh_packet()` 调整为只消费新 packet 字段：

```python
def _render_fresh_packet(packet: TaskPacket) -> str:
    sections = [f"Task: {packet.title}", "", "Directive:", packet.directive]
    if packet.done_criteria:
        sections.extend(["", "Done criteria:"] + [f"- {item}" for item in packet.done_criteria])
    return "\n".join(sections)
```

如 `core/session/engine.py` 的 `MessageViewBuilder` 没有 `tools` property，可把测试改成断言 `captured["tools"] is not None`；实现里仍然必须显式传 `tools=sub_schemas`。

- [ ] **Step 4: 运行 subagent runtime 测试**

Run: `pytest tests/session/test_subagent_runtime.py -v`

Expected: `6 passed`

- [ ] **Step 5: 提交 subagent runtime 改造**

```bash
git add core/session/subagent.py core/session/engine.py tests/session/test_subagent_runtime.py
git commit -m "feat: bridge subagent runtime events to parent renderer"
```

### Task 7: 端到端收尾验收

**Files:**
- Modify: `tests/session/test_task_execute_tool.py`
- Modify: `tests/session/test_subagent_runtime.py`
- Modify: `tests/test_tool_runtime.py`

- [ ] **Step 1: 补最终 acceptance tests**

在现有测试文件中再补三组回归：

```python
def test_task_execute_rejects_legacy_execution_mode_value(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    legacy_task = TaskRecord(
        task_id="task-1",
        subject="Legacy",
        goal="Legacy",
        status=TaskStatus.PENDING,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
    )
    object.__setattr__(legacy_task, "execution_mode", "fork_subagent")
    state.task_state.tasks_by_id["task-1"] = legacy_task
    state.task_state.ordered_task_ids = ["task-1"]

    result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "failure"
    assert result.error == "unsupported_execution_mode"


def test_normalize_subagent_result_maps_every_supported_stop_reason() -> None:
    cases = {
        SubagentStopReason.COMPLETED: TaskStatus.COMPLETED,
        SubagentStopReason.CANCELLED: TaskStatus.CANCELLED,
        SubagentStopReason.MAX_TURNS: TaskStatus.FAILED,
        SubagentStopReason.API_ERROR: TaskStatus.FAILED,
        SubagentStopReason.EMPTY_RESPONSE: TaskStatus.FAILED,
    }
    for reason, expected_status in cases.items():
        normalized = normalize_subagent_result(
            "task-1",
            SimpleNamespace(
                success=(reason == SubagentStopReason.COMPLETED),
                output=reason.value,
                files_modified=[],
                stop_reason=reason,
                turns_used=1,
            ),
        )
        assert normalized.status == expected_status
        assert normalized.stop_reason == reason.value


def test_parallel_runtime_keeps_files_modified_scoped_per_run(tmp_path) -> None:
    registry = ToolRegistry()

    class WriteA:
        SCHEMA = {"name": "write_a", "input_schema": {"type": "object", "properties": {}}}
        READONLY = False
        ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(
                messages=[make_tool_message(context, "ok")],
                run_updates=[RunUpdate(kind=RunUpdateKind.MARK_FILE_MODIFIED, payload={"path": "/tmp/a.txt"})],
            )

    class WriteB:
        SCHEMA = {"name": "write_b", "input_schema": {"type": "object", "properties": {}}}
        READONLY = False
        ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(
                messages=[make_tool_message(context, "ok")],
                run_updates=[RunUpdate(kind=RunUpdateKind.MARK_FILE_MODIFIED, payload={"path": "/tmp/b.txt"})],
            )

    registry.register(WriteA)
    registry.register(WriteB)

    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    state = SessionState(conversation_messages=[])
    context.bind_runtime(session_state=state, skill_registry=None)
    runtime = ToolExecutorRuntime(registry, context)

    run_state_a = RunState()
    runtime.execute_batch(
        [ToolCall(idx=0, name="write_a", call_id="call_a", args={})],
        run_state=run_state_a,
        apply_session_update=lambda update: apply_session_update(state, update),
        apply_run_update=apply_run_update,
    )

    run_state_b = RunState()
    runtime.execute_batch(
        [ToolCall(idx=0, name="write_b", call_id="call_b", args={})],
        run_state=run_state_b,
        apply_session_update=lambda update: apply_session_update(state, update),
        apply_run_update=apply_run_update,
    )

    assert run_state_a.files_modified == ["/tmp/a.txt"]
    assert run_state_b.files_modified == ["/tmp/b.txt"]
```

- [ ] **Step 2: 运行整个相关测试集合**

Run:

```bash
pytest \
  tests/test_task_runtime_models.py \
  tests/session/test_task_plan_tool.py \
  tests/session/test_task_execute_tool.py \
  tests/session/test_subagent_runtime.py \
  tests/test_tool_runtime.py -v
```

Expected:
- 全部通过
- 没有 `fork_subagent` 相关断言残留
- `task_execute` / `task_plan` / runtime 三条主链都被覆盖

- [ ] **Step 3: 跑一次更接近真实链路的回归子集**

Run:

```bash
pytest \
  tests/test_runtime_control_plane.py \
  tests/test_query_display.py \
  tests/test_runtime_logging.py -v
```

Expected:
- 新的实时渲染不会破坏已有 renderer / control-plane 行为
- `task_state -> todo` 投影仍可显示

- [ ] **Step 4: 提交最终验收**

```bash
git add tests/session/test_task_execute_tool.py tests/session/test_subagent_runtime.py tests/test_tool_runtime.py
git commit -m "test: add acceptance coverage for subagent runtime redesign"
```

- [ ] **Step 5: 手工 smoke check**

在本地 CLI 手工跑一个最小场景：

1. 先让主代理调用 `task_plan`，写入一个 `fresh_subagent` 任务：

```json
{
  "tasks": [
    {
      "task_id": "task-1",
      "subject": "Inspect runtime",
      "goal": "Explain current subagent bugs",
      "status": "in_progress",
      "execution_mode": "fresh_subagent",
      "agent_type": "explore",
      "description": "Read task_execute.py and subagent.py, then summarize the breakpoints.",
      "done_criteria": ["Name exact files", "Explain data loss points"]
    }
  ]
}
```

2. 再调用：

```json
{"task_id": "task-1"}
```

Expected:
- 主界面先看到 `subagent_start`
- 然后看到子代理工具调用/结果
- 结束时任务状态写回 `completed` 或其他规范化 `stop_reason`

## Spec Coverage Check

- `5.1 简化后的数据模型`：由 Task 1 和 Task 3 落地。
- `5.2 task_plan Schema 扩展`：由 Task 2 落地。
- `5.3 SubagentRuntime 改造`：由 Task 4 和 Task 6 落地。
- `5.4 ToolExecutorRuntime 实时渲染`：由 Task 5 落地。
- `5.5 并发派遣支持`：由 Task 4、Task 6、Task 7 落地。
- `5.6 task_plan 增量更新`：由 Task 2 落地。
- `5.7 行为契约与验收标准`：由 Task 2、Task 4、Task 6、Task 7 共同覆盖。

## Placeholder Scan

- 本计划没有保留常见占位词或空白步骤。
- 代码步骤都给出了真实测试代码、目标函数签名和命令，没有留空白实现位。

## Execution Notes

- 推荐按 Task 1 → Task 7 顺序执行，不要先改 runtime 再删模型字段，否则会陷入大面积红测。
- 如果在 Task 1 删除字段后出现跨文件连锁失败，不要回滚设计；继续按计划推进后续任务，把所有旧读路径清完。
- 若某个测试依赖 `SimpleNamespace(stop_reason=SimpleNamespace(value="completed"))` 过于脆弱，执行时可统一替换为真实 `SubagentStopReason.COMPLETED`，但断言目标不能变。
