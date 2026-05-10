from pathlib import Path
from types import SimpleNamespace

from core.session.subagent import _render_fresh_packet, _preload_required_skills
from core.tasks.models import TaskExecutionMode, TaskPacket


def test_render_fresh_packet_contains_expected_sections() -> None:
    packet = TaskPacket(
        task_id="task-1",
        mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_type="general",
        title="Inspect runtime",
        directive="Inspect runtime deeply",
        known_facts=["QueryLoop batches readonly tools"],
        out_of_scope=["Do not edit files"],
        expected_output=["A short diagnosis"],
        done_criteria=["At least one runtime breakpoint identified"],
    )

    rendered = _render_fresh_packet(packet)

    assert "Task: Inspect runtime" in rendered
    assert "Directive:" in rendered
    assert "Known facts:" in rendered
    assert "Do not edit files" in rendered


def test_preload_required_skills_records_invoked_skills(tmp_path: Path) -> None:
    from core.session.state import SessionState
    from core.skills import SkillRegistry
    from core.tools.context import ToolUseContext

    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Analysis Report\ndescription: test\n---\nUse the report workflow.",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    registry.discover(skill_dir.parent, working_dir=tmp_path)
    parent_context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent_context.bind_runtime(session_state=None, skill_registry=registry)

    engine = SimpleNamespace(state=SessionState(conversation_messages=[]))
    _preload_required_skills(engine, parent_context, ["analysis-report"], turn=0)

    assert "analysis-report" in engine.state.invoked_skills
