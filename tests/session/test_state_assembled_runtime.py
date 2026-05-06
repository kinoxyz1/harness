"""Transcript-independence proof tests.

These tests prove that the state-assembled runtime carries all necessary
context in the assembled system view -- not in the transcript.  Even when
assistant/tool messages are absent from the transcript, every piece of
runtime state (active skills, todos, file runtime) is present in the
assembled system string that the model receives.
"""

from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.query_context import ContextBlock, PreparedQueryContext
from core.session.state import SessionState, TodoItem, TodoState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder
from core.skills.models import InvokedSkillRecord
from core.tools.context import FileState


def _build_prepared(
    state: SessionState,
    assembler: PromptAssembler,
    tmp_path: Path,
    *,
    tools: list[dict] | None = None,
    messages: list[dict] | None = None,
) -> PreparedQueryContext:
    stable_system = assembler.build_stable_context(state, project_root=str(tmp_path))
    stable_tools = assembler.build_stable_tools(state, tools=tools)
    runtime_blocks = assembler.build_runtime_blocks(state, working_dir=str(tmp_path))
    return PreparedQueryContext(
        stable_system=stable_system,
        stable_tools=stable_tools,
        runtime_blocks=runtime_blocks,
        working_transcript=messages if messages is not None else state.conversation_messages,
    )


def test_runtime_view_survives_when_assistant_and_tool_transcript_is_removed(
    tmp_path: Path,
) -> None:
    """Core integration proof: assembled system contains all runtime context
    even when assistant/tool transcript messages are absent."""
    state = SessionState(
        conversation_messages=[{"role": "user", "content": "Generate the analysis report"}],
    )
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path=str(tmp_path / ".harness" / "skills" / "analysis-report" / "SKILL.md"),
        content_digest="digest-1",
        content=(
            '  <skill id="analysis-report" source="local-inline">\n'
            "    <instruction>\nFollow the report workflow.\n    </instruction>\n"
            "  </skill>"
        ),
        invoked_at_turn=1,
    )
    state.todo_state = TodoState(
        items=[
            TodoItem(
                content="Draft the final report",
                active_form="Drafting the final report",
                status="in_progress",
                workflow_ref="3",
            )
        ]
    )
    file_path = tmp_path / "report.md"
    state.read_file_state[str(file_path)] = FileState(
        content="# Report\nalpha\nbeta",
        timestamp=10.0,
        offset=None,
        limit=None,
    )

    builder = MessageViewBuilder()
    assembler = PromptAssembler()
    prepared = _build_prepared(state, assembler, tmp_path)
    view = builder.build(prepared, run_state=RunState())

    assert "Follow the report workflow." in view.system
    assert "Drafting the final report" in view.system
    assert "report.md" in view.system
    assert view.messages == [{"role": "user", "content": "Generate the analysis report"}]


def test_runtime_view_includes_no_system_role_messages_in_transcript(
    tmp_path: Path,
) -> None:
    """The transcript should never contain system-role messages injected
    by the runtime -- all runtime context lives in the assembled system."""
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    )
    state.invoked_skills["my-skill"] = InvokedSkillRecord(
        skill_id="my-skill",
        skill_path="/skills/my-skill/SKILL.md",
        content_digest="d1",
        content="<skill-runtime>Skill instructions</skill-runtime>",
        invoked_at_turn=0,
    )

    builder = MessageViewBuilder()
    assembler = PromptAssembler()
    prepared = _build_prepared(state, assembler, tmp_path)
    view = builder.build(prepared, run_state=RunState())

    # No system-role messages in the transcript
    assert not any(m["role"] == "system" for m in view.messages)
    # But skill content IS in the assembled system
    assert "Skill instructions" in view.system


def test_runtime_view_survives_after_transcript_rewrite(
    tmp_path: Path,
) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "tool", "content": "old tool"},
        ],
    )
    state.invoked_skills["rewrite-skill"] = InvokedSkillRecord(
        skill_id="rewrite-skill",
        skill_path="/skills/rewrite-skill/SKILL.md",
        content_digest="digest-2",
        content="<skill-runtime>Rewrite-safe skill instructions</skill-runtime>",
        invoked_at_turn=4,
    )
    state.todo_state = TodoState(
        items=[
            TodoItem(
                content="Preserve runtime truth",
                active_form="Preserving runtime truth",
                status="in_progress",
                workflow_ref="1",
            )
        ]
    )
    file_path = tmp_path / "rewrite.md"
    state.read_file_state[str(file_path)] = FileState(
        content="rewrite content",
        timestamp=20.0,
        offset=None,
        limit=None,
    )
    store = SessionStore(state)
    store.replace_working_transcript(
        [
            {"role": "user", "content": "hello"},
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact;summarized_messages=2"},
            {"role": "meta_compact_summary", "kind": "compact_summary", "content": "summary"},
            {"role": "user", "content": "follow-up"},
        ]
    )

    builder = MessageViewBuilder()
    assembler = PromptAssembler()
    prepared = _build_prepared(state, assembler, tmp_path)
    view = builder.build(prepared, run_state=RunState())

    assert "Rewrite-safe skill instructions" in view.system
    assert "Preserving runtime truth" in view.system
    assert "rewrite.md" in view.system
    assert [message["role"] for message in view.messages] == [
        "user",
        "meta_compact_boundary",
        "meta_compact_summary",
        "user",
    ]


def test_runtime_view_survives_after_transcript_rewrite_with_stable_tools(
    tmp_path: Path,
) -> None:
    """Integration proof: stable tools and runtime survive transcript rewrite."""
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact;summarized_messages=1"},
            {"role": "meta_compact_summary", "kind": "compact_summary", "content": "summary"},
            {"role": "user", "content": "follow-up"},
        ],
    )
    state.invoked_skills["rewrite-skill"] = InvokedSkillRecord(
        skill_id="rewrite-skill",
        skill_path="/skills/rewrite-skill/SKILL.md",
        content_digest="digest-2",
        content="<skill-runtime>Rewrite-safe skill instructions</skill-runtime>",
        invoked_at_turn=4,
    )

    assembler = PromptAssembler()
    tools = [{"name": "todo", "description": "todo", "input_schema": {"type": "object"}}]
    prepared = _build_prepared(state, assembler, tmp_path, tools=tools)

    view = MessageViewBuilder().build(prepared, run_state=RunState())

    assert [tool["name"] for tool in view.tools] == ["todo"]
    assert "Rewrite-safe skill instructions" in view.system
    assert view.messages[-1]["content"] == "follow-up"
