from pathlib import Path

from core.query.reducers import apply_session_update
from core.session.state import SessionState
from core.skills.registry import SkillRegistry
from core.tools.context import SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext


def _write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _make_context(tmp_path: Path, state: SessionState, registry: SkillRegistry) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=registry)
    ctx._set_call_identity(name="skill", call_id="toolu_skill", turn=1)
    return ctx


def test_skill_schema_does_not_expose_args() -> None:
    from core.tools.builtin.skill import SCHEMA

    input_schema = SCHEMA["input_schema"]
    assert "args" not in input_schema["properties"]
    assert input_schema["required"] == ["skill"]


def test_skill_tool_returns_outcome_with_skill_updates(tmp_path: Path) -> None:
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

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.SUCCESS
    assert result.error is None
    assert result.run_updates == []
    assert len(result.messages) == 1
    assert "Skill loaded: analysis-report" in result.messages[0]["content"]
    assert [update.kind for update in result.session_updates] == [
        SessionUpdateKind.INVOKE_SKILL,
        SessionUpdateKind.APPEND_SKILL_EVENT,
    ]

    for update in result.session_updates:
        apply_session_update(state, update)

    assert "analysis-report" in state.invoked_skills
    assert "Follow the workflow." in state.invoked_skills["analysis-report"].content
    assert len(state.skill_events) == 1
    event = state.skill_events[0]
    assert event.skill_id == "analysis-report"
    assert event.action == "activated"
    assert event.source == "model_tool_call"
    assert event.conversation_index == -1


def test_skill_tool_rejects_unknown_skill(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    registry = SkillRegistry()
    state = SessionState(conversation_messages=[], skill_catalog={})
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "missing-skill"}, ctx)

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.FAILURE
    assert result.error == "not_found"


def test_skill_tool_does_not_write_direct_stdout(tmp_path: Path, capsys) -> None:
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
    captured = capsys.readouterr()

    assert isinstance(result, ToolInvocationOutcome)
    assert result.status == ToolOutcomeStatus.SUCCESS
    assert captured.out == ""
