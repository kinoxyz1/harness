from types import SimpleNamespace
from unittest.mock import patch

from core.session.state import SessionState
from core.session.subagent import SubagentStopReason
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
        stop_reason=SubagentStopReason.COMPLETED,
        turns_used=2,
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result) as run_mock:
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "success"
    assert run_mock.called


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
        stop_reason=SubagentStopReason.COMPLETED,
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
        stop_reason=SubagentStopReason.CANCELLED,
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
