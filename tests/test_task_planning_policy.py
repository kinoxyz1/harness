from core.policy.task_planning import PREPLAN_ALLOWED_TOOLS, TaskPlanningPolicy
from core.query.state import RunState
from core.session.state import SessionState


def test_task_planning_policy_injects_reminder_when_flag_is_set() -> None:
    policy = TaskPlanningPolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState(task_planning_required=True, task_planning_reason="post_skill")

    messages = policy.before_model_call(state, run_state)

    assert len(messages) == 1
    assert "task_planning" in messages[0]["content"]
    assert "task_plan" in messages[0]["content"]
    assert run_state.allowed_tools_override == PREPLAN_ALLOWED_TOOLS


def test_task_planning_policy_is_silent_when_task_state_exists() -> None:
    from core.tasks.models import TaskExecutionMode, TaskRecord, TaskStatus

    policy = TaskPlanningPolicy()
    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id = {
        "task-1": TaskRecord(
            task_id="task-1",
            subject="Test",
            goal="Test goal",
            status=TaskStatus.IN_PROGRESS,
            execution_mode=TaskExecutionMode.LOCAL,
        )
    }
    state.task_state.ordered_task_ids = ["task-1"]
    run_state = RunState(task_planning_required=True)

    assert policy.before_model_call(state, run_state) == []


def test_task_planning_policy_autodetects_complex_request_and_requires_preplan() -> None:
    policy = TaskPlanningPolicy()
    state = SessionState(
        conversation_messages=[],
        user_intents=[
            "帮我看看明天的天气情况，然后从深圳大学城去桔钓沙做一日游规划，还要结合公共交通和景点推荐。"
        ],
    )
    run_state = RunState()

    messages = policy.before_model_call(state, run_state)

    assert len(messages) == 1
    assert run_state.task_planning_required is True
    assert run_state.task_planning_reason == "complex_user_request"
    assert run_state.allowed_tools_override == PREPLAN_ALLOWED_TOOLS


def test_task_planning_policy_does_not_require_preplan_for_simple_request() -> None:
    policy = TaskPlanningPolicy()
    state = SessionState(
        conversation_messages=[],
        user_intents=["README 一共有多少行？"],
    )
    run_state = RunState()

    messages = policy.before_model_call(state, run_state)

    assert messages == []
    assert run_state.task_planning_required is False
    assert run_state.allowed_tools_override is None


def test_preplan_allowed_tools_excludes_skill_loading() -> None:
    assert PREPLAN_ALLOWED_TOOLS == {"task_plan", "find", "read_file"}
