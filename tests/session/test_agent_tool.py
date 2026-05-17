from types import SimpleNamespace
from unittest.mock import patch

from core.session.state import SessionState
from core.session.subagent import SubagentStopReason
from core.tools.context import ToolUseContext


def _context(state: SessionState, tmp_path) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=None)
    ctx._set_call_identity(name="agent", call_id="toolu_agent", turn=4)
    return ctx


def test_agent_uses_dispatch_helper_for_prompt_visibility(tmp_path) -> None:
    from core.tools.builtin.agent import handle

    fake_exec = SimpleNamespace(
        run_id="run-1",
        record=SimpleNamespace(run_id="run-1"),
        result=SimpleNamespace(
            success=True,
            output="done",
            files_modified=[],
            stop_reason=SubagentStopReason.COMPLETED,
            turns_used=1,
        ),
    )

    with patch("core.session.subagent.dispatch_subagent", return_value=fake_exec) as dispatch_mock, \
         patch("core.session.subagent.render_subagent_summary", return_value="ok"):
        result = handle(
            {
                "description": "Inspect runtime",
                "prompt": "Read the runtime stack and report in under 100 words.",
                "subagent_type": "plan",
            },
            _context(SessionState(conversation_messages=[]), tmp_path),
        )

    assert result.status.value == "success"
    kwargs = dispatch_mock.call_args.kwargs
    assert kwargs["source_tool"] == "agent"
    assert kwargs["task_id"] is None
    assert kwargs["prompt_text"] == "Read the runtime stack and report in under 100 words."


def test_agent_rejects_when_fresh_subagent_task_is_active(tmp_path) -> None:
    from core.tools.builtin.agent import handle
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

    state = SessionState(conversation_messages=[])
    state.task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Inspect runtime",
                goal="Inspect runtime deeply",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
    )

    result = handle(
        {
            "description": "Inspect runtime",
            "prompt": "Do the planned task for me.",
            "subagent_type": "plan",
        },
        _context(state, tmp_path),
    )

    assert result.status.value == "failure"
    assert result.error == "task_state_active_use_task_execute"


def test_agent_rejects_when_local_task_is_active(tmp_path) -> None:
    from core.tools.builtin.agent import handle
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

    state = SessionState(conversation_messages=[])
    state.task_state = TaskState(
        tasks_by_id={
            "task-1": TaskRecord(
                task_id="task-1",
                subject="Edit runtime",
                goal="Edit runtime locally",
                status=TaskStatus.IN_PROGRESS,
                execution_mode=TaskExecutionMode.LOCAL,
            )
        },
        ordered_task_ids=["task-1"],
        current_task_id="task-1",
    )

    result = handle(
        {
            "description": "Edit runtime",
            "prompt": "Implement the current task.",
            "subagent_type": "general",
        },
        _context(state, tmp_path),
    )

    assert result.status.value == "failure"
    assert result.error == "task_state_active_use_local_tools"
