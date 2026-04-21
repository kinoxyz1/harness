from core.query.state import RunState
from core.query.reducers import (
    TransitionReason,
    apply_run_update,
    apply_session_update,
    apply_transition,
    collect_runtime_maintenance_updates,
)
import core.session.commands as session_commands_module
from core.session.state import SessionState
from core.skills.models import InvokedSkillRecord
from core.tools import ToolRegistry
import core.tools.context as tool_context_module
from core.tools.context import (
    FileState,
    RunUpdate,
    RunUpdateKind,
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
)
from core.tools.runtime import ToolCall, ToolExecutorRuntime, ToolBatchResult


def test_legacy_tool_result_protocol_is_removed() -> None:
    assert not hasattr(tool_context_module, "ToolResult")


def test_legacy_skill_compatibility_state_is_removed() -> None:
    state = SessionState(conversation_messages=[])
    assert not hasattr(state, "active_skills")
    assert not hasattr(session_commands_module, "MAX_ACTIVE_SKILLS")
    assert not hasattr(session_commands_module, "MAX_TOTAL_SKILL_CHARS")


def test_run_state_starts_without_runtime_overrides() -> None:
    state = RunState()
    assert state.allowed_tools_override is None
    assert state.model_override is None
    assert state.effort_override is None
    assert state.transition is None
    assert not hasattr(state, "barrier_reason")
    assert not hasattr(state, "todo_replan_required")
    assert not hasattr(state, "todo_replan_reason")


def test_session_state_tracks_invoked_skills() -> None:
    state = SessionState(conversation_messages=[])
    record = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path=".harness/skills/analysis-report/SKILL.md",
        content_digest="abc123",
        content="Skill body",
        invoked_at_turn=2,
    )
    state.invoked_skills["analysis-report"] = record
    assert state.invoked_skills["analysis-report"].invoked_at_turn == 2


def test_tool_use_context_bind_runtime_sets_public_runtime_handles(tmp_path) -> None:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    state = SessionState(conversation_messages=[])
    ctx.bind_runtime(session_state=state, skill_registry="registry")
    assert ctx.session_state is state
    assert ctx.skill_registry == "registry"


class _BarrierTool:
    SCHEMA = {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            run_updates=[
                RunUpdate(
                    kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
                    payload={"allowed_tools": {"todo"}},
                )
            ],
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": context.tool_call_id,
                    "content": "todo scope narrowed",
                }
            ],
        )


class _TodoTool:
    SCHEMA = {"name": "bash", "description": "bash", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        raise AssertionError("bash should have been rejected after allowed-tools narrowing")


def test_runtime_rejects_disallowed_tool_after_serial_narrow_update(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_BarrierTool)
    reg.register(_TodoTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="todo", call_id="toolu_todo", args={}),
        ToolCall(idx=1, name="bash", call_id="toolu_bash", args={}),
    ], run_state=run_state, apply_session_update=lambda update: apply_session_update(session_state, update), apply_run_update=apply_run_update)

    assert run_state.allowed_tools_override == {"todo"}
    assert batch.tool_names == ["todo", "bash"]
    assert batch.tool_statuses == [ToolOutcomeStatus.SUCCESS, ToolOutcomeStatus.BLOCKED]
    assert batch.messages[0]["content"] == "todo scope narrowed"
    assert "rejected" in batch.messages[1]["content"]
    assert batch.messages[1]["tool_call_id"] == "toolu_bash"
    assert batch.session_updates == []
    assert batch.run_updates == [
        RunUpdate(
            kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
            payload={"allowed_tools": {"todo"}},
        )
    ]


class _SilentReadonlyTool:
    SCHEMA = {"name": "find", "description": "find", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(status=ToolOutcomeStatus.FAILURE, messages=[])


class _BlockedReadonlyReadTool:
    SCHEMA = {"name": "read_file", "description": "read_file", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        raise AssertionError("read_file should have been rejected after allowed-tools narrowing")


class _BlockedReadonlyFindTool:
    SCHEMA = {"name": "find", "description": "find", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        raise AssertionError("find should have been rejected after allowed-tools narrowing")


def test_runtime_rejects_disallowed_readonly_tools_in_parallel_batch_after_serial_narrow_update(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_BarrierTool)
    reg.register(_BlockedReadonlyReadTool)
    reg.register(_BlockedReadonlyFindTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="todo", call_id="toolu_todo", args={}),
        ToolCall(idx=1, name="read_file", call_id="toolu_read", args={"path": "README.md"}),
        ToolCall(idx=2, name="find", call_id="toolu_find", args={"pattern": "*.py"}),
    ], run_state=run_state, apply_session_update=lambda update: apply_session_update(session_state, update), apply_run_update=apply_run_update)

    assert run_state.allowed_tools_override == {"todo"}
    assert batch.tool_statuses == [ToolOutcomeStatus.SUCCESS, ToolOutcomeStatus.BLOCKED, ToolOutcomeStatus.BLOCKED]
    rejected_contents = [m["content"] for m in batch.messages if "rejected" in m["content"]]
    assert len(rejected_contents) == 2


class _MultiMessageReadonlyTool:
    SCHEMA = {"name": "read_file", "description": "read_file", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            messages=[
                {"role": "tool", "tool_call_id": context.tool_call_id, "content": "part-1"},
                {"role": "tool", "tool_call_id": context.tool_call_id, "content": "part-2"},
            ],
        )


def test_runtime_batch_messages_expand_all_outcome_messages_in_call_order(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_MultiMessageReadonlyTool)
    reg.register(_SilentReadonlyTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="read_file", call_id="toolu_read", args={"path": "README.md"}),
        ToolCall(idx=1, name="find", call_id="toolu_find", args={}),
    ], run_state=run_state, apply_session_update=lambda update: apply_session_update(session_state, update), apply_run_update=apply_run_update)

    assert batch.messages == [
        {"role": "tool", "tool_call_id": "toolu_read", "content": "part-1"},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "part-2"},
    ]
    assert batch.tool_statuses == [ToolOutcomeStatus.SUCCESS, ToolOutcomeStatus.FAILURE]


class _ParallelIdentityReadTool:
    SCHEMA = {"name": "read_file", "description": "read_file", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": context.tool_call_id,
                    "content": f"from:{context.tool_call_id}",
                }
            ],
        )


class _ParallelIdentityFindTool:
    SCHEMA = {"name": "find", "description": "find", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": context.tool_call_id,
                    "content": f"from:{context.tool_call_id}",
                }
            ],
        )


def test_runtime_parallel_readonly_calls_keep_independent_tool_call_identity(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_ParallelIdentityReadTool)
    reg.register(_ParallelIdentityFindTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="read_file", call_id="toolu_read", args={"path": "README.md"}),
        ToolCall(idx=1, name="find", call_id="toolu_find", args={"pattern": "*.py"}),
    ], run_state=run_state, apply_session_update=lambda update: apply_session_update(session_state, update), apply_run_update=apply_run_update)

    assert batch.messages == [
        {"role": "tool", "tool_call_id": "toolu_read", "content": "from:toolu_read"},
        {"role": "tool", "tool_call_id": "toolu_find", "content": "from:toolu_find"},
    ]


class _MissingToolCallIdTool:
    SCHEMA = {"name": "read_file", "description": "read_file", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            messages=[
                {"role": "tool", "content": "dict-no-id"},
                "raw-non-dict",
            ],
        )


def test_runtime_batch_messages_backfills_missing_tool_call_id(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_MissingToolCallIdTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()
    session_state = SessionState(conversation_messages=[])

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="read_file", call_id="toolu_read", args={"path": "README.md"}),
    ], run_state=run_state, apply_session_update=lambda update: apply_session_update(session_state, update), apply_run_update=apply_run_update)

    assert batch.messages == [
        {"role": "tool", "tool_call_id": "toolu_read", "content": "dict-no-id"},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "raw-non-dict"},
    ]


def test_tool_invocation_outcome_defaults_success_with_empty_updates() -> None:
    outcome = ToolInvocationOutcome()
    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert outcome.session_updates == []
    assert outcome.run_updates == []
    assert outcome.messages == []


def test_apply_run_update_intersects_allowed_tools_override() -> None:
    state = RunState(allowed_tools_override={"bash", "read_file", "todo"})
    apply_run_update(
        state,
        RunUpdate(
            kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
            payload={"allowed_tools": {"read_file", "todo"}},
        ),
    )
    apply_run_update(
        state,
        RunUpdate(
            kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
            payload={"allowed_tools": {"todo"}},
        ),
    )
    assert state.allowed_tools_override == {"todo"}


def test_apply_run_update_narrow_allowed_tools_accepts_list_payload() -> None:
    state = RunState(allowed_tools_override={"bash", "read_file", "todo"})
    apply_run_update(
        state,
        RunUpdate(
            kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
            payload={"allowed_tools": ["read_file", "todo"]},
        ),
    )
    assert state.allowed_tools_override == {"read_file", "todo"}


def test_apply_run_update_can_reset_assistant_turns_since_todo() -> None:
    state = RunState(assistant_turns_since_todo=3)
    apply_run_update(state, RunUpdate(kind=RunUpdateKind.RESET_TODO_TURN_COUNTER))
    assert state.assistant_turns_since_todo == 0


def test_apply_session_update_sets_todo_items() -> None:
    from core.session.state import TodoItem

    session = SessionState(conversation_messages=[])
    items = [TodoItem(content="A", active_form="Doing A", status="pending")]
    apply_session_update(
        session,
        SessionUpdate(
            kind=SessionUpdateKind.SET_TODO_ITEMS,
            payload={"items": items, "last_write_turn": 3},
        ),
    )
    assert session.todo_state.items == items
    assert session.todo_state.last_completed_items == []
    assert session.todo_state.last_write_turn == 3


def test_apply_transition_next_turn_resets_empty_retry_count() -> None:
    state = RunState(empty_retry_count=2)
    apply_transition(state, TransitionReason.NEXT_TURN)
    assert state.transition == TransitionReason.NEXT_TURN
    assert state.empty_retry_count == 0


def test_apply_transition_empty_response_retry_increments_empty_retry_count() -> None:
    state = RunState(empty_retry_count=1)
    apply_transition(state, TransitionReason.EMPTY_RESPONSE_RETRY)
    assert state.transition == TransitionReason.EMPTY_RESPONSE_RETRY
    assert state.empty_retry_count == 2


def test_apply_transition_max_turns_recovery_preserves_stop_reason() -> None:
    state = RunState(stop_reason="max_turns")
    apply_transition(state, TransitionReason.MAX_TURNS_RECOVERY)
    assert state.transition == TransitionReason.MAX_TURNS_RECOVERY
    assert state.stop_reason == "max_turns"


def test_collect_runtime_maintenance_updates_returns_invalidate_file_state(tmp_path) -> None:
    session = SessionState(conversation_messages=[])
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello")
    stale_state = FileState(content="hello", timestamp=0.0)
    session.read_file_state[str(file_path)] = stale_state

    updates = collect_runtime_maintenance_updates(session)

    assert updates == [
        SessionUpdate(
            kind=SessionUpdateKind.INVALIDATE_FILE_STATE,
            payload={"path": str(file_path)},
        )
    ]
    assert str(file_path) in session.read_file_state


def test_apply_session_update_raises_on_unknown_kind() -> None:
    session = SessionState(conversation_messages=[])
    bad_update = SessionUpdate(kind="unknown_kind")  # type: ignore[arg-type]
    try:
        apply_session_update(session, bad_update)
    except ValueError as exc:
        assert "Unsupported session update kind" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported session update kind")


def test_apply_run_update_raises_on_unknown_kind() -> None:
    state = RunState()
    bad_update = RunUpdate(kind="unknown_kind")  # type: ignore[arg-type]
    try:
        apply_run_update(state, bad_update)
    except ValueError as exc:
        assert "Unsupported run update kind" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported run update kind")
