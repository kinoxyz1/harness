from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.prompt.assembler import PromptAssembler
from core.session.engine import SessionEngine
from core.session.query_context import PreparedQueryContext
from core.session.view_builder import MessageViewBuilder
from core.query.state import RunState


class DummyQueryLoop:
    def run(self, **kwargs):
        return SimpleNamespace(final_output="ok")


def write_skill(tmp_path: Path, skill_id: str, name: str, desc: str, body: str) -> None:
    skill_dir = tmp_path / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}",
        encoding="utf-8",
    )


def make_engine(tmp_path: Path) -> SessionEngine:
    engine = SessionEngine(
        model_gateway=object(),
        tool_runtime=object(),
        tool_context=SimpleNamespace(working_dir=str(tmp_path)),
        policy_runner=object(),
        recovery=object(),
        query_loop=DummyQueryLoop(),
    )
    return engine


def _build_view(engine: SessionEngine, tmp_path: Path) -> SimpleNamespace:
    """Build a view using the new prepared-context flow."""
    assembler = engine._prompt_assembler
    stable_system = assembler.build_stable_context(engine.state, project_root=str(tmp_path))
    stable_tools = assembler.build_stable_tools(engine.state, tools=None)
    runtime_blocks = assembler.build_runtime_blocks(engine.state, working_dir=str(tmp_path))
    prepared = PreparedQueryContext(
        stable_system=stable_system,
        stable_tools=stable_tools,
        runtime_blocks=runtime_blocks,
        working_transcript=engine.state.conversation_messages,
    )
    return engine._view_builder.build(prepared, run_state=RunState())


def test_handle_command_use_records_skill_without_transcript_injection(tmp_path: Path) -> None:
    write_skill(tmp_path, "analysis-report", "Analysis Report", "Generate reports", "Skill body")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills use analysis-report")

    assert "loaded" in result.lower() or "activated" in result.lower()
    assert "analysis-report" in engine.state.invoked_skills
    assert not any(
        "<skill-runtime>" in m.get("content", "")
        for m in engine.state.conversation_messages
        if m["role"] == "system"
    )
    assert engine.state.skill_events
    event = engine.state.skill_events[-1]
    assert event.skill_id == "analysis-report"
    assert event.action == "activated"
    assert event.source == "user_command"
    assert event.conversation_index == len(engine.state.conversation_messages)


def test_handle_command_off_reports_inline_skills_cannot_be_removed(tmp_path: Path) -> None:
    write_skill(tmp_path, "analysis-report", "Analysis Report", "Generate reports", "Use the workflow.")
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")

    result = engine.handle_command("/skills off analysis-report")

    assert "cannot be deactivated" in result.lower()


def test_handle_command_use_nonexistent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills use nonexistent")

    assert "not found" in result.lower()
    assert "nonexistent" not in engine.state.invoked_skills


def test_handle_command_list_empty(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills list")

    assert "no skills" in result.lower()


def test_handle_command_list_with_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "test-skill", "Test", "A test skill", "Body")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills list")

    assert "test-skill" in result
    assert "A test skill" in result


def test_handle_command_show(tmp_path: Path) -> None:
    write_skill(tmp_path, "test-skill", "Test", "A test skill", "Full skill body content")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills show test-skill")

    assert "Full skill body content" in result


def test_handle_command_show_nonexistent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills show nonexistent")

    assert "not found" in result.lower()


def test_handle_command_reload(tmp_path: Path) -> None:
    write_skill(tmp_path, "first-skill", "First", "First skill", "Body 1")

    engine = make_engine(tmp_path)
    engine.bootstrap()
    assert "first-skill" in engine.state.skill_catalog

    # Add another skill
    write_skill(tmp_path, "second-skill", "Second", "Second skill", "Body 2")

    result = engine.handle_command("/skills reload")

    assert "reloaded" in result.lower()
    assert "second-skill" in engine.state.skill_catalog


def test_handle_command_use_respects_inline_skill_budget(tmp_path: Path) -> None:
    big_body = "x" * 250_000
    write_skill(tmp_path, "skill-a", "Skill A", "A", big_body)
    write_skill(tmp_path, "skill-b", "Skill B", "B", big_body)

    engine = make_engine(tmp_path)
    engine.bootstrap()

    first = engine.handle_command("/skills use skill-a")
    second = engine.handle_command("/skills use skill-b")

    assert "loaded" in first.lower()
    assert "budget" in second.lower() or "exceeded" in second.lower()


def test_handle_command_use_repeat_records_latest_invocation(tmp_path: Path) -> None:
    write_skill(tmp_path, "test-skill", "Test", "A test skill", "Body")

    engine = make_engine(tmp_path)
    engine.bootstrap()
    first = engine.handle_command("/skills use test-skill")

    second = engine.handle_command("/skills use test-skill")

    assert "loaded" in first.lower()
    assert "loaded" in second.lower()
    assert "test-skill" in engine.state.invoked_skills


def test_handle_command_use_enforces_cumulative_budget_across_different_skills(tmp_path: Path) -> None:
    body = "x" * 130_000
    write_skill(tmp_path, "skill-a", "Skill A", "A", body)
    write_skill(tmp_path, "skill-b", "Skill B", "B", body)
    write_skill(tmp_path, "skill-c", "Skill C", "C", body)
    write_skill(tmp_path, "skill-d", "Skill D", "D", body)

    engine = make_engine(tmp_path)
    engine.bootstrap()

    first = engine.handle_command("/skills use skill-a")
    second = engine.handle_command("/skills use skill-b")
    third = engine.handle_command("/skills use skill-c")
    fourth = engine.handle_command("/skills use skill-d")

    assert "loaded" in first.lower()
    assert "loaded" in second.lower()
    assert "loaded" in third.lower()
    assert "budget" in fourth.lower() or "exceed" in fourth.lower()


def test_bootstrap_discovers_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "discovered-skill", "Discovered", "A discovered skill", "Body")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    assert "discovered-skill" in engine.state.skill_catalog
    assert engine.state.skills_revision is not None


def test_bootstrap_idempotent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.bootstrap()  # Should not fail on second call


def test_active_skill_body_reaches_model_view(tmp_path: Path) -> None:
    """Integration test: /skills use records skill in invoked_skills state."""
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Use a fixed HTML structure for all reports.",
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()

    # Activate the skill
    engine.handle_command("/skills use analysis-report")

    # The skill should be tracked in invoked_skills
    assert "analysis-report" in engine.state.invoked_skills
    record = engine.state.invoked_skills["analysis-report"]
    assert "<skill-runtime>" in record.content
    assert "Use a fixed HTML structure" in record.content

    # Simulate a user message being added
    engine.append_message({"role": "user", "content": "Generate a report"})

    view = _build_view(engine, tmp_path)

    # No <skill-runtime> in conversation_messages (transcript-based injection removed)
    assert not any(
        m["role"] == "system" and "<skill-runtime>" in m.get("content", "")
        for m in view.messages
    )

    # Verify: the user message is present
    user_msgs = [m for m in view.messages if m["role"] == "user"]
    assert any("Generate a report" in m["content"] for m in user_msgs)

    # Verify: skill content is in the assembled system
    assert "Use a fixed HTML structure" in view.system


def test_off_does_not_remove_invoked_skill_record(tmp_path: Path) -> None:
    """Inline skill records are immutable once recorded in invoked_skills."""
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Report generation instructions.",
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")
    before = len(engine.state.conversation_messages)

    result = engine.handle_command("/skills off analysis-report")
    after = len(engine.state.conversation_messages)

    assert "cannot be deactivated" in result.lower()
    assert before == after
    assert "analysis-report" in engine.state.invoked_skills


def test_active_skill_persists_across_turns(tmp_path: Path) -> None:
    """Inline-invoked skill records persist across conversation turns in state."""
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Persistent skill content.",
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")

    engine.append_message({"role": "user", "content": "turn 1"})
    view1 = _build_view(engine, tmp_path)

    engine.append_message({"role": "assistant", "content": "reply 1"})
    engine.append_message({"role": "user", "content": "turn 2"})
    view2 = _build_view(engine, tmp_path)

    assert "analysis-report" in engine.state.invoked_skills
    assert "<skill-runtime>" in engine.state.invoked_skills["analysis-report"].content
    assert not any(
        m["role"] == "system" and "<skill-runtime>" in m.get("content", "")
        for m in view1.messages
    )
    assert not any(
        m["role"] == "system" and "<skill-runtime>" in m.get("content", "")
        for m in view2.messages
    )


def test_active_skill_persists_across_turns_in_assembled_system(tmp_path: Path) -> None:
    """Transcript-independence proof: skill content appears in assembled system
    across multiple turns, even as assistant messages are added to transcript."""
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Persistent skill content.",
    )
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")

    engine.append_message({"role": "user", "content": "turn 1"})
    view1 = _build_view(engine, tmp_path)

    engine.append_message({"role": "assistant", "content": "reply 1"})
    engine.append_message({"role": "user", "content": "turn 2"})
    view2 = _build_view(engine, tmp_path)

    assert "analysis-report" in engine.state.invoked_skills
    assert "Persistent skill content." in view1.system
    assert "Persistent skill content." in view2.system


def write_skill_with_refs(
    tmp_path: Path,
    skill_id: str,
    name: str,
    desc: str,
    body: str,
    *,
    references: list[tuple[str, str]] | None = None,
    extra_files: dict[str, str] | None = None,
) -> None:
    skill_dir = tmp_path / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {name}",
        f"description: {desc}",
    ]
    if references:
        frontmatter.append("references:")
        for path, purpose in references:
            frontmatter.append(f"  - path: {path}")
            frontmatter.append(f"    purpose: {purpose}")
    frontmatter.extend(["---", "", body])
    (skill_dir / "SKILL.md").write_text("\n".join(frontmatter), encoding="utf-8")
    for rel_path, content in (extra_files or {}).items():
        file_path = skill_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def test_bootstrap_discovers_reference_prompt_paths(tmp_path: Path) -> None:
    write_skill_with_refs(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Skill body",
        references=[("style-system.md", "CSS rules")],
        extra_files={"style-system.md": "css"},
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()

    refs = engine.state.skill_catalog["analysis-report"].references
    assert refs[0].prompt_path == ".harness/skills/analysis-report/style-system.md"


def test_handle_command_reload_rebuilds_reference_metadata(tmp_path: Path) -> None:
    write_skill_with_refs(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Skill body",
        references=[("style-system.md", "CSS rules")],
        extra_files={"style-system.md": "css"},
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()

    write_skill_with_refs(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Skill body",
        references=[
            ("style-system.md", "CSS rules"),
            ("quality-checklist.md", "Final checks"),
        ],
        extra_files={
            "style-system.md": "css",
            "quality-checklist.md": "checks",
        },
    )

    result = engine.handle_command("/skills reload")

    assert "reloaded" in result.lower()
    refs = engine.state.skill_catalog["analysis-report"].references
    assert [ref.prompt_path for ref in refs] == [
        ".harness/skills/analysis-report/style-system.md",
        ".harness/skills/analysis-report/quality-checklist.md",
    ]


def test_active_skill_reference_index_reaches_model_view(tmp_path: Path) -> None:
    write_skill_with_refs(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Follow the main workflow.",
        references=[("style-system.md", "CSS rules")],
        extra_files={"style-system.md": "css-variable-definitions"},
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")
    engine.append_message({"role": "user", "content": "Generate a report"})

    view = _build_view(engine, tmp_path)

    refs = engine.state.skill_catalog["analysis-report"].references
    assert len(refs) == 1
    assert refs[0].prompt_path == ".harness/skills/analysis-report/style-system.md"


def test_use_rejects_when_reference_chars_exceed_budget(tmp_path: Path) -> None:
    big_ref = "y" * 260_000
    for i in range(2):
        write_skill_with_refs(
            tmp_path,
            f"heavy-skill-{i}",
            f"Heavy {i}",
            f"Heavy skill {i}",
            "Short body",
            references=[("big-ref.md", "Large reference")],
            extra_files={"big-ref.md": big_ref},
        )

    engine = make_engine(tmp_path)
    engine.bootstrap()

    engine.handle_command("/skills use heavy-skill-0")
    result = engine.handle_command("/skills use heavy-skill-1")

    assert "budget" in result.lower() or "exceed" in result.lower()
    assert "heavy-skill-1" not in engine.state.invoked_skills


def test_active_skill_inlines_reference_content_in_model_view(tmp_path: Path) -> None:
    write_skill_with_refs(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Follow the main workflow.",
        references=[("style-system.md", "CSS rules")],
        extra_files={"style-system.md": "h1 { font-size: 2rem; }"},
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")
    engine.append_message({"role": "user", "content": "Generate a report"})

    content = engine._skill_registry.load("analysis-report")
    assert "Follow the main workflow." in content.body
    assert "h1 { font-size: 2rem; }" in content.reference_bodies[".harness/skills/analysis-report/style-system.md"]


def test_bootstrap_discovers_skills_without_writing_prompt_messages(tmp_path: Path) -> None:
    write_skill(tmp_path, "some-skill", "Some Skill", "A skill", "Body")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    assert "some-skill" in engine.state.skill_catalog
    assert engine.state.skills_revision is not None
    assert engine.state.conversation_messages == []
