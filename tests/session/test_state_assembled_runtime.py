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
from core.session.state import SessionState, TodoItem, TodoState
from core.session.view_builder import MessageViewBuilder
from core.skills.models import InvokedSkillRecord
from core.tools.context import FileState


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
    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert "Follow the report workflow." in view.system
    assert "Drafting the final report" in view.system
    assert "report.md" in view.system
    assert view.messages == [{"role": "user", "content": "Generate the analysis report"}]


def test_runtime_view_includes_no_system_role_messages_in_transcript(
    tmp_path: Path,
) -> None:
    """The transcript slice should never contain system-role messages injected
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
    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    # No system-role messages in the transcript
    assert not any(m["role"] == "system" for m in view.messages)
    # But skill content IS in the assembled system
    assert "Skill instructions" in view.system
