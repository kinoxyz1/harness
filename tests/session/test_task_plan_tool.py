from core.query.reducers import apply_session_update
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
