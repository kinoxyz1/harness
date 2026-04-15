from core.query.state import RunState
from core.session.state import SessionState
from core.skills.models import InvokedSkillRecord
from core.tools import ToolRegistry
from core.tools.context import ContextPatch, ExecutionBarrier, ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime, ToolBatchResult


def test_tool_result_exposes_runtime_protocol_fields() -> None:
    result = ToolResult(output="ok", success=True)
    assert result.injected_messages == []
    assert result.context_patch is None
    assert result.barrier is None


def test_run_state_starts_without_runtime_overrides() -> None:
    state = RunState()
    assert state.allowed_tools_override is None
    assert state.model_override is None
    assert state.effort_override is None
    assert state.barrier_reason is None


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
    SCHEMA = {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        return ToolResult(
            output="skill expanded",
            success=True,
            injected_messages=[{"role": "system", "content": "<skill-runtime>expanded</skill-runtime>"}],
            barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
        )


class _TodoTool:
    SCHEMA = {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        raise AssertionError("todo should have been skipped after skill barrier")


def test_runtime_returns_explicit_skipped_result_after_skill_barrier(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_BarrierTool)
    reg.register(_TodoTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="skill", call_id="toolu_skill", args={}),
        ToolCall(idx=1, name="todo", call_id="toolu_todo", args={}),
    ])

    assert batch.barrier == ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
    assert batch.tool_results[1]["content"].startswith("(skipped: superseded by skill_expanded barrier")
    assert batch.injected_messages == [{"role": "system", "content": "<skill-runtime>expanded</skill-runtime>"}]


def test_apply_batch_control_plane_merges_patches() -> None:
    from core.query.state import RunState
    from core.tools.context import ContextPatch
    from core.query.loop import _apply_batch_control_plane

    state = RunState()

    class FakeBatch:
        context_patches = [
            ContextPatch(allowed_tools={"skill", "todo"}, model_override="claude-3"),
        ]
        barrier = None

    _apply_batch_control_plane(state, FakeBatch())

    assert state.allowed_tools_override == {"skill", "todo"}
    assert state.model_override == "claude-3"


def test_apply_batch_control_plane_intersects_allowed_tools() -> None:
    from core.query.state import RunState
    from core.tools.context import ContextPatch
    from core.query.loop import _apply_batch_control_plane

    state = RunState(allowed_tools_override={"skill", "todo", "bash"})

    class FakeBatch:
        context_patches = [
            ContextPatch(allowed_tools={"skill", "todo"}),
        ]
        barrier = None

    _apply_batch_control_plane(state, FakeBatch())

    assert state.allowed_tools_override == {"skill", "todo"}


def test_apply_batch_control_plane_sets_barrier_reason() -> None:
    from core.query.state import RunState
    from core.query.loop import _apply_batch_control_plane

    state = RunState()

    class FakeBatch:
        context_patches = []
        barrier = ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")

    _apply_batch_control_plane(state, FakeBatch())

    assert state.barrier_reason == "skill_expanded"
