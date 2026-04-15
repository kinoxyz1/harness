from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState
from core.tools.context import ExecutionBarrier
from core.tools.runtime import ToolBatchResult


def test_before_model_call_emits_post_skill_replan_reminder() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(conversation_messages=[])
    run_state = RunState(todo_replan_required=True, todo_replan_reason="skill_expanded")

    messages = policy.before_model_call(session_state, run_state)

    assert len(messages) == 1
    assert "skill 刚刚展开" in messages[0]["content"]


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


def test_skill_expanded_barrier_sets_todo_replan_flag() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState()
    batch = ToolBatchResult(
        tool_results=[],
        files_modified=[],
        tool_names=["skill"],
        tool_successes=[True],
        injected_messages=[],
        context_patches=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is True
    assert state.todo_replan_reason == "skill_expanded"


def test_successful_todo_batch_clears_todo_replan_flag() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState(
        todo_replan_required=True,
        todo_replan_reason="skill_expanded",
        assistant_turns_since_todo=3,
    )
    batch = ToolBatchResult(
        tool_results=[],
        files_modified=[],
        tool_names=["todo"],
        tool_successes=[True],
        injected_messages=[],
        context_patches=[],
        barrier=None,
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is False
    assert state.todo_replan_reason is None
    assert state.assistant_turns_since_todo == 0


def test_skill_barrier_with_skipped_todo_keeps_replan_flag() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState()
    batch = ToolBatchResult(
        tool_results=[
            {"role": "tool", "tool_call_id": "toolu_skill", "content": "skill expanded"},
            {
                "role": "tool",
                "tool_call_id": "toolu_todo",
                "content": "(skipped: superseded by skill_expanded barrier; re-issue after re-evaluation if still needed)",
            },
        ],
        files_modified=[],
        tool_names=["skill", "todo"],
        tool_successes=[True, False],
        injected_messages=[],
        context_patches=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is True
    assert state.todo_replan_reason == "skill_expanded"


def test_failed_todo_batch_keeps_todo_replan_flag() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState(
        todo_replan_required=True,
        todo_replan_reason="skill_expanded",
        assistant_turns_since_todo=4,
    )
    batch = ToolBatchResult(
        tool_results=[{"role": "tool", "tool_call_id": "toolu_todo", "content": "todo failed"}],
        files_modified=[],
        tool_names=["todo"],
        tool_successes=[False],
        injected_messages=[],
        context_patches=[],
        barrier=None,
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is True
    assert state.todo_replan_reason == "skill_expanded"
    assert state.assistant_turns_since_todo == 4


def test_skill_expanded_barrier_wins_over_earlier_successful_todo() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState(
        todo_replan_required=True,
        todo_replan_reason="skill_expanded",
        assistant_turns_since_todo=2,
    )
    batch = ToolBatchResult(
        tool_results=[
            {"role": "tool", "tool_call_id": "toolu_todo", "content": "todo updated"},
            {"role": "tool", "tool_call_id": "toolu_skill", "content": "skill expanded"},
        ],
        files_modified=[],
        tool_names=["todo", "skill"],
        tool_successes=[True, True],
        injected_messages=[],
        context_patches=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is True
    assert state.todo_replan_reason == "skill_expanded"
