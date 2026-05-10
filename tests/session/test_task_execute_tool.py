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

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result) as run_mock:
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "success"
    assert run_mock.called
