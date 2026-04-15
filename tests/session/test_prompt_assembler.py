from __future__ import annotations

from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.session.state import SessionState
from core.skills import SkillMeta


def make_state(tmp_path: Path) -> SessionState:
    skill_file = tmp_path / "analysis-report" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("body", encoding="utf-8")
    return SessionState(
        conversation_messages=[],
        skill_catalog={
            "analysis-report": SkillMeta(
                skill_id="analysis-report",
                name="Analysis Report",
                description="Generate HTML reports",
                when_to_use="Use when a finished report is needed",
                skill_dir=skill_file.parent,
                skill_file=skill_file,
            )
        },
        skill_events=[],
        skills_revision="rev-1",
    )


def test_build_stable_includes_available_skills_catalog(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "<available-skills>" in stable
    assert 'id="analysis-report"' in stable
    assert "Generate HTML reports" in stable


def test_build_stable_cache_key_changes_with_skills_revision(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    first = assembler.build_stable(state, project_root=str(tmp_path))
    state.skills_revision = "rev-2"
    second = assembler.build_stable(state, project_root=str(tmp_path))

    assert first != ""
    assert second != ""
    assert any("stable_system_prompt:rev-1:" in k for k in state.prompt_cache)
    assert any("stable_system_prompt:rev-2:" in k for k in state.prompt_cache)


def test_build_stable_without_skills(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[],
        skill_catalog={},
        skill_events=[],
        skills_revision=None,
    )
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "<skill id=" not in stable
    assert any("stable_system_prompt:no-skills:" in k for k in state.prompt_cache)


def test_build_stable_catalog_includes_when_to_use(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "Use when a finished report is needed" in stable


def test_build_environment_message_unchanged(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    msg = assembler.build_environment_message(working_dir=".")

    assert msg["role"] == "user"
    assert "<environment>" in msg["content"]


def test_build_dynamic_returns_empty(tmp_path: Path) -> None:
    from core.query.state import RunState
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    result = assembler.build_dynamic(state, RunState())

    assert result == []


def test_build_stable_returns_cached_value_on_second_call(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    first = assembler.build_stable(state, project_root=str(tmp_path))
    second = assembler.build_stable(state, project_root=str(tmp_path))

    assert first == second
    # Verify the cache key exists (only one entry since same revision)
    assert any("stable_system_prompt:rev-1:" in k for k in state.prompt_cache)


def test_stable_cache_key_includes_prompt_digest(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[],
        skill_catalog={},
        skill_events=[],
        skills_revision=None,
    )
    assembler = PromptAssembler()

    # Build once
    assembler.build_stable(state, project_root=str(tmp_path))

    # Cache key should include a digest, not just revision
    cache_keys = list(state.prompt_cache.keys())
    assert len(cache_keys) == 1
    # Key should be: stable_system_prompt:no-skills:<12-char-digest>
    parts = cache_keys[0].split(":")
    assert len(parts) == 3
    assert parts[0] == "stable_system_prompt"
    assert len(parts[2]) == 12  # sha256 digest truncated to 12 chars


def test_build_stable_includes_stronger_todo_guidance(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "多步骤任务必须使用 todo" in stable
    assert "如果 skill 刚展开" in stable


def test_stable_cache_key_changes_when_system_prompt_text_changes(
    tmp_path: Path, monkeypatch
) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    monkeypatch.setattr("core.prompt.assembler.get_system_context", lambda project_root=None: "prompt-v1")
    assembler.build_stable(state, project_root=str(tmp_path))
    key_after_v1 = next(k for k in state.prompt_cache if k.startswith("stable_system_prompt:rev-1:"))

    monkeypatch.setattr("core.prompt.assembler.get_system_context", lambda project_root=None: "prompt-v2")
    assembler.build_stable(state, project_root=str(tmp_path))
    stable_keys = [k for k in state.prompt_cache if k.startswith("stable_system_prompt:rev-1:")]

    assert len(stable_keys) == 2
    assert key_after_v1 in stable_keys
    assert stable_keys[0] != stable_keys[1]
