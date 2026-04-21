from core.session.state import SessionState
from core.query.reducers import apply_session_update, apply_run_update
from core.query.state import RunState
from core.tools.context import ToolUseContext
from core.tools.builtin.todo import SCHEMA


def _make_context(tmp_path, state: SessionState) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)
    ctx._set_call_identity(name="todo", call_id="toolu_todo", turn=3)
    return ctx


def test_todo_returns_updates_for_session_and_run_state(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    run_state = RunState(assistant_turns_since_todo=4)
    ctx = _make_context(tmp_path, state)
    result = handle(
        {
            "items": [
                {
                    "content": "Perform primary analysis",
                    "active_form": "Performing primary analysis",
                    "status": "in_progress",
                    "workflow_ref": "2",
                }
            ]
        },
        ctx,
    )

    assert result.status.value == "success"
    assert state.todo_state.items == []
    assert len(result.session_updates) == 1
    assert len(result.run_updates) == 1

    for update in result.session_updates:
        apply_session_update(state, update)
    for update in result.run_updates:
        apply_run_update(run_state, update)

    assert state.todo_state.items[0].content == "Perform primary analysis"
    assert state.todo_state.items[0].active_form == "Performing primary analysis"
    assert state.todo_state.items[0].workflow_ref == "2"
    assert state.todo_state.last_write_turn == 3
    assert run_state.assistant_turns_since_todo == 0


def test_todo_rejects_missing_active_form(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    result = handle({"items": [{"content": "Analyze", "status": "pending"}]}, ctx)

    assert result.status.value == "failure"
    assert result.error == "validation_failed"


def test_todo_normalizes_all_completed_to_completed_snapshot(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    result = handle(
        {
            "items": [
                {
                    "content": "Verify report completeness",
                    "active_form": "Verifying report completeness",
                    "status": "completed",
                    "workflow_ref": "4",
                }
            ]
        },
        ctx,
    )

    assert result.status.value == "success"
    for update in result.session_updates:
        apply_session_update(state, update)

    assert state.todo_state.items == []
    assert len(state.todo_state.last_completed_items) == 1
    assert state.todo_state.last_completed_items[0].active_form == "Verifying report completeness"
    assert state.todo_state.last_completed_items[0].workflow_ref == "4"
    assert state.todo_state.last_write_turn == 3


def test_todo_drops_legacy_mutating_helper_apis() -> None:
    from core.tools.builtin import todo

    assert not hasattr(todo, "save_snapshot")
    assert not hasattr(todo, "restore_snapshot")
    assert not hasattr(todo, "clear_state")
    assert not hasattr(todo, "increment_rounds")
    assert not hasattr(todo, "reset_rounds")


def test_todo_rejects_non_list_items_payload(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)

    result = handle({"items": 1}, ctx)

    assert result.status.value == "failure"
    assert result.error == "validation_failed"


def test_todo_rejects_non_object_item(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)

    result = handle({"items": [1]}, ctx)

    assert result.status.value == "failure"
    assert result.error == "validation_failed"


def test_todo_rejects_non_string_content_or_active_form(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)

    result_content = handle(
        {"items": [{"content": 1, "active_form": "Analyzing", "status": "pending"}]},
        ctx,
    )
    result_active_form = handle(
        {"items": [{"content": "Analyze", "active_form": 1, "status": "pending"}]},
        ctx,
    )

    assert result_content.status.value == "failure"
    assert result_content.error == "validation_failed"
    assert result_active_form.status.value == "failure"
    assert result_active_form.error == "validation_failed"


def test_todo_schema_description_mentions_workflow_and_verification() -> None:
    description = SCHEMA["description"]

    assert "newly loaded skill changes the workflow" in description.lower()
    assert "workflow" in description.lower()
    assert "verification" in description.lower()
    assert "exactly one" in description.lower()
    assert "post-skill replanning after a skill was just expanded" not in description.lower()
