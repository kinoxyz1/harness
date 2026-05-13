from core.query.reducers import apply_session_update
from core.session.state import SessionState
from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus
from core.tasks.planner_runtime import build_task_state
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
            "required_skill_ids",
        ]
    )
    assert item_props["execution_mode"]["enum"] == ["local", "fresh_subagent"]


def test_build_task_state_drops_old_tasks_not_in_new_plan() -> None:
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

    assert next_state.ordered_task_ids == ["task-new"]
    assert "task-old-done" not in next_state.tasks_by_id
    assert "task-old-live" not in next_state.tasks_by_id
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
                    "required_skill_ids": ["weather", "serper-search"],
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
    assert task.required_skill_ids == ["weather", "serper-search"]
