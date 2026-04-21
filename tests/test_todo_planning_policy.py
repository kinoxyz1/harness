from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState
from core.tools.builtin.todo import handle
from core.tools.context import RunUpdateKind, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext


def _make_todo_context(session_state: SessionState) -> ToolUseContext:
    context = ToolUseContext(working_dir=".", max_turns=20)
    context.bind_runtime(session_state=session_state)
    context._set_call_identity(name="todo", call_id="toolu_todo", turn=3)
    return context


def test_stale_reminder_requires_existing_plan_and_four_turns() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[
                TodoItem(
                    content="Cross-check findings",
                    active_form="Cross-checking findings",
                    status="in_progress",
                    workflow_ref="2.5",
                )
            ],
            last_write_turn=1,
        ),
    )
    run_state = RunState(assistant_turns_since_todo=4)

    messages = policy.before_model_call(session_state, run_state)

    assert len(messages) == 1
    assert "Cross-check findings" in messages[0]["content"]
    assert "2.5" in messages[0]["content"]


def test_stale_reminder_does_not_fire_below_four_turns() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[
                TodoItem(
                    content="Cross-check findings",
                    active_form="Cross-checking findings",
                    status="in_progress",
                    workflow_ref="2.5",
                )
            ],
            last_write_turn=1,
        ),
    )
    run_state = RunState(assistant_turns_since_todo=3)

    messages = policy.before_model_call(session_state, run_state)

    assert messages == []


def test_stale_reminder_does_not_fire_without_existing_plan() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(
        conversation_messages=[],
        todo_state=TodoState(items=[], last_write_turn=1),
    )
    run_state = RunState(assistant_turns_since_todo=4)

    messages = policy.before_model_call(session_state, run_state)

    assert messages == []


def test_todo_tool_returns_updates_for_session_and_run_state() -> None:
    session_state = SessionState(conversation_messages=[])
    run_state = RunState(assistant_turns_since_todo=5)
    context = _make_todo_context(session_state)

    result = handle(
        {
            "items": [
                {
                    "content": "Cross-check findings",
                    "active_form": "Cross-checking findings",
                    "status": "in_progress",
                    "workflow_ref": "2.5",
                },
                {
                    "content": "Write summary",
                    "active_form": "Writing summary",
                    "status": "pending",
                },
            ]
        },
        context,
    )

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.SUCCESS
    assert result.error is None
    assert [update.kind for update in result.session_updates] == [SessionUpdateKind.SET_TODO_ITEMS]
    assert [update.kind for update in result.run_updates] == [RunUpdateKind.RESET_TODO_TURN_COUNTER]
    assert result.session_updates[0].payload["last_write_turn"] == context.turn_count
    assert len(result.session_updates[0].payload["items"]) == 2
    assert len(result.messages) == 1
    assert "计划已更新 (0/2 完成)。 当前: Cross-check findings" == result.messages[0]["content"]

    for update in result.session_updates:
        apply_session_update(session_state, update)
    for update in result.run_updates:
        apply_run_update(run_state, update)

    assert len(session_state.todo_state.items) == 2
    assert session_state.todo_state.items[0].content == "Cross-check findings"
    assert session_state.todo_state.items[0].workflow_ref == "2.5"
    assert run_state.assistant_turns_since_todo == 0


def test_todo_tool_returns_failure_outcome_on_validation_error() -> None:
    session_state = SessionState(conversation_messages=[])
    context = _make_todo_context(session_state)

    result = handle({"items": {"not": "a-list"}}, context)

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.FAILURE
    assert result.error == "validation_failed"
    assert result.session_updates == []
    assert result.run_updates == []


def test_todo_tool_rejects_multiple_in_progress_items() -> None:
    session_state = SessionState(conversation_messages=[])
    context = _make_todo_context(session_state)

    result = handle(
        {
            "items": [
                {"content": "Step 1", "active_form": "Doing step 1", "status": "in_progress"},
                {"content": "Step 2", "active_form": "Doing step 2", "status": "in_progress"},
            ]
        },
        context,
    )

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.FAILURE
    assert result.error == "validation_failed"
    assert "最多只能有 1 个 in_progress 任务" in result.messages[0]["content"]
    assert result.session_updates == []
    assert result.run_updates == []


def test_todo_tool_all_completed_clears_items_and_keeps_completed_snapshot() -> None:
    session_state = SessionState(conversation_messages=[])
    context = _make_todo_context(session_state)

    result = handle(
        {
            "items": [
                {"content": "Step 1", "active_form": "Doing step 1", "status": "completed"},
                {"content": "Step 2", "active_form": "Doing step 2", "status": "completed"},
            ]
        },
        context,
    )

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.SUCCESS
    assert result.messages[0]["content"] == "计划已清空。"

    for update in result.session_updates:
        apply_session_update(session_state, update)

    assert session_state.todo_state.items == []
    assert [item.content for item in session_state.todo_state.last_completed_items] == ["Step 1", "Step 2"]


def test_todo_updates_stay_isolated_per_session_state() -> None:
    session_a = SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[TodoItem(content="A item", active_form="Doing A", status="in_progress")]
        ),
    )
    session_b = SessionState(conversation_messages=[])
    context_b = _make_todo_context(session_b)
    result_b = handle(
        {
            "items": [
                {"content": "B item", "active_form": "Doing B", "status": "in_progress"},
            ]
        },
        context_b,
    )
    for update in result_b.session_updates:
        apply_session_update(session_b, update)

    assert [item.content for item in session_a.todo_state.items] == ["A item"]
    assert [item.content for item in session_b.todo_state.items] == ["B item"]
