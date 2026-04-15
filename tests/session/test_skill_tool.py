from pathlib import Path

from core.session.state import SessionState
from core.skills.registry import SkillRegistry
from core.tools.context import ExecutionBarrier, ToolUseContext


def _write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _make_context(tmp_path: Path, state: SessionState, registry: SkillRegistry) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=registry)
    ctx._set_call_identity(name="skill", call_id="toolu_skill", turn=1)
    return ctx


def test_skill_tool_returns_injected_runtime_message_and_barrier(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    _write_skill(
        tmp_path,
        "analysis-report",
        "---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the workflow.\n",
    )
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    state = SessionState(conversation_messages=[], skill_catalog=catalog)
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "analysis-report"}, ctx)

    assert result.success is True
    assert result.barrier == ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
    assert len(result.injected_messages) == 1
    assert "<skill-runtime>" in result.injected_messages[0]["content"]
    assert "Follow the workflow." in result.injected_messages[0]["content"]
    assert "analysis-report" in state.invoked_skills


def test_skill_tool_rejects_unknown_skill(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    registry = SkillRegistry()
    state = SessionState(conversation_messages=[], skill_catalog={})
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "missing-skill"}, ctx)

    assert result.success is False
    assert result.error == "not_found"
