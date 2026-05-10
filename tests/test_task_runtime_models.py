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
