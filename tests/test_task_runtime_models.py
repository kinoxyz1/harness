from dataclasses import fields
from types import SimpleNamespace

from core.query.reducers import apply_session_update
from core.session.state import SessionState, TodoItem
from core.session.subagent import SubagentStopReason
from core.tasks.dispatcher import compile_task_packet, normalize_subagent_result
from core.tasks.models import TaskExecutionMode, TaskPacket, TaskRecord, TaskRunResult, TaskState, TaskStatus
from core.tasks.projection import project_task_state_to_todo_items
from core.tools.context import SessionUpdate, SessionUpdateKind


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
