from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.query.result import QueryResult, StopReason
from core.session.state import SessionState
from core.session.subagent import (
    SubagentRequest,
    SubagentRuntime,
    SubagentStopReason,
    SubagentType,
    _render_fresh_packet,
    _preload_required_skills,
    coerce_stop_reason,
)
from core.shared.stream_events import make_event
from core.tasks.models import TaskExecutionMode, TaskPacket
from core.tools.context import ToolUseContext


def test_render_fresh_packet_contains_expected_sections() -> None:
    packet = TaskPacket(
        task_id="task-1",
        agent_type="general",
        title="Inspect runtime",
        directive="Inspect runtime deeply",
        done_criteria=["At least one runtime breakpoint identified"],
    )

    rendered = _render_fresh_packet(packet)

    assert "Task: Inspect runtime" in rendered
    assert "Directive:" in rendered
    assert "Done criteria:" in rendered


def test_preload_required_skills_records_invoked_skills(tmp_path: Path) -> None:
    from core.skills import SkillRegistry

    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Analysis Report\ndescription: test\n---\nUse the report workflow.",
        encoding="utf-8",
    )

    skill_registry = SkillRegistry()
    skill_registry.discover(skill_dir.parent, working_dir=tmp_path)
    parent_context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent_context.bind_runtime(session_state=None, skill_registry=skill_registry)

    engine = SimpleNamespace(state=SessionState(conversation_messages=[]))
    _preload_required_skills(engine, parent_context, ["analysis-report"], turn=0)

    assert "analysis-report" in engine.state.invoked_skills


def test_render_fresh_packet_includes_done_criteria() -> None:
    packet = TaskPacket(
        task_id="task-1",
        title="Inspect runtime",
        directive="Fix subagent runtime\n\nContext:\nRead runtime files first.",
        done_criteria=["List exact files", "Explain the broken data flow"],
        agent_type="plan",
    )

    rendered = _render_fresh_packet(packet)

    assert "Task: Inspect runtime" in rendered
    assert "Read runtime files first." in rendered
    assert "Done criteria:" in rendered
    assert "Explain the broken data flow" in rendered


def test_coerce_stop_reason_maps_aborted_to_cancelled() -> None:
    assert coerce_stop_reason("aborted") == SubagentStopReason.CANCELLED


def test_subagent_runtime_emits_bridge_events_and_passes_tools(tmp_path) -> None:
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)
    packet = TaskPacket(
        task_id="task-1",
        title="Inspect runtime",
        directive="Fix runtime",
        done_criteria=["List files"],
        agent_type="general",
    )
    request = SubagentRequest(task_packet=packet, agent_type=SubagentType.GENERAL)
    captured = {}
    events = []

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.state = SessionState(conversation_messages=[])

        def submit_user_message(self, prompt):
            captured["prompt"] = prompt
            captured["renderer"].show_tool_call("find", {"pattern": "*.py"})
            captured["renderer"].show_tool_result("find", "core/tasks/models.py")
            return QueryResult(
                final_output="done",
                stop_reason=StopReason.COMPLETED,
                success=True,
                turns_used=2,
                files_modified=["core/tasks/models.py"],
            )

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        result = runtime.run(request, emit=events.append)

    assert captured["tools"] is not None
    assert events[0]["event"] == "subagent_start"
    assert events[1]["event"] == "subagent_tool_call_start"
    assert events[2]["event"] == "subagent_tool_call_result"
    assert events[-1]["event"] == "subagent_done"
    assert all(event["task_id"] == "task-1" for event in events)
    assert result.stop_reason == SubagentStopReason.COMPLETED


def test_subagent_runtime_keeps_parent_session_state_isolated(tmp_path) -> None:
    parent_state = SessionState(conversation_messages=[])
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=parent_state, skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)

    class FakeEngine:
        def __init__(self, **kwargs):
            self.state = SessionState(conversation_messages=[])

        def submit_user_message(self, prompt):
            self.state.todo_state.items.append(SimpleNamespace(content="child", active_form="child", status="pending"))
            return QueryResult(final_output="done", stop_reason=StopReason.COMPLETED, success=True, turns_used=1)

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(
            SubagentRequest(
                task_packet=TaskPacket(task_id="task-1", title="Inspect runtime", directive="Fix runtime"),
                agent_type=SubagentType.GENERAL,
            )
        )

    assert parent_state.todo_state.items == []


def test_subagent_bridge_forwards_consume_event_with_subagent_prefix() -> None:
    from core.session.subagent import SubagentBridgeRenderer

    events = []
    bridge = SubagentBridgeRenderer(task_id="task-1", agent_type="general", emit=events.append)

    bridge.consume_event(
        make_event(
            "content_delta",
            "turn-child",
            1,
            "model",
            "live",
            {"text": "子代理输出"},
        )
    )

    assert events == [
        {
            "event": "subagent_content_delta",
            "task_id": "task-1",
            "agent_type": "general",
            "source_mode": "live",
            "content": "子代理输出",
        }
    ]


def test_subagent_bridge_legacy_show_assistant_becomes_replayed_event() -> None:
    from core.session.subagent import SubagentBridgeRenderer

    events = []
    bridge = SubagentBridgeRenderer(task_id="task-1", agent_type="general", emit=events.append)

    bridge.show_assistant("完整输出")

    assert events == [
        {
            "event": "subagent_content_delta",
            "task_id": "task-1",
            "agent_type": "general",
            "source_mode": "replayed",
            "content": "完整输出",
        }
    ]


def test_existing_subagent_runtime_tool_event_name_uses_start_suffix(tmp_path) -> None:
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)
    packet = TaskPacket(task_id="task-1", title="Inspect runtime", directive="Fix runtime", agent_type="general")
    request = SubagentRequest(task_packet=packet, agent_type=SubagentType.GENERAL)
    events = []

    class FakeEngine:
        def __init__(self, **kwargs):
            self.state = SessionState(conversation_messages=[])
            self._renderer = kwargs["renderer"]

        def submit_user_message(self, prompt):
            self._renderer.show_tool_call("find", {"pattern": "*.py"})
            self._renderer.show_tool_result("find", "core/tasks/models.py")
            return QueryResult(final_output="done", stop_reason=StopReason.COMPLETED, success=True, turns_used=1)

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(request, emit=events.append)

    assert events[1]["event"] == "subagent_tool_call_start"


def test_dispatch_subagent_emits_prompt_event_and_records_run(tmp_path) -> None:
    from core.session.state import DispatchRunRecord, DispatchState, SessionState
    from core.session.subagent import (
        SubagentRequest,
        SubagentType,
        dispatch_subagent,
    )

    state = SessionState(conversation_messages=[])
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=state, skill_registry=None)
    parent._set_call_identity(name="task_execute", call_id="toolu_task_execute", turn=5)
    seen_events = []

    class FakeRuntime:
        def __init__(self, parent_context):
            self.parent_context = parent_context

        def run(self, request, emit=None):
            if emit is not None:
                emit({"event": "subagent_content_delta", "content": "done", "task_id": "task-1", "agent_type": "plan"})
                emit({"event": "subagent_done", "task_id": "task-1", "agent_type": "plan", "stop_reason": "completed", "turns_used": 2})
            return SimpleNamespace(
                request=request,
                output="done",
                success=True,
                stop_reason=SubagentStopReason.COMPLETED,
                turns_used=2,
                files_modified=[],
            )

    with patch("core.session.subagent.SubagentRuntime", FakeRuntime):
        result = dispatch_subagent(
            parent_context=parent,
            source_tool="task_execute",
            task_id="task-1",
            task_subject="Inspect runtime",
            prompt_text="Task: Inspect runtime\n\nDirective:\nInspect deeply",
            request=SubagentRequest(
                task_packet=TaskPacket(task_id="task-1", title="Inspect runtime", directive="Inspect deeply"),
                agent_type=SubagentType.PLAN,
            ),
            emit=seen_events.append,
        )

    assert seen_events[0]["event"] == "subagent_prompt"
    assert seen_events[-1]["event"] == "subagent_done"
    record = state.dispatch_state.runs_by_id[result.run_id]
    assert record.source_tool == "task_execute"
    assert record.status == "completed"
    assert record.result_summary == "done"
