from __future__ import annotations

from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState
from core.skills import SkillMeta
from core.skills.models import InvokedSkillRecord
from core.tools.context import FileState


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


# ── build_active_skill_messages ──────────────────────────────


def test_build_active_skill_messages_empty_when_no_skills(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    result = assembler.build_active_skill_messages(state)

    assert result == []


def test_build_active_skill_messages_renders_invoked_skills(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path="/skills/analysis-report/SKILL.md",
        content_digest="abc123",
        content="<skill-content>report body</skill-content>",
        invoked_at_turn=0,
    )
    assembler = PromptAssembler()

    result = assembler.build_active_skill_messages(state)

    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert "<active-skills>" in result[0]["content"]
    assert "</active-skills>" in result[0]["content"]
    assert "<skill-content>report body</skill-content>" in result[0]["content"]


def test_build_active_skill_messages_multiple_skills(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["skill-a"] = InvokedSkillRecord(
        skill_id="skill-a",
        skill_path="/skills/a/SKILL.md",
        content_digest="a1",
        content="<skill-content>A</skill-content>",
        invoked_at_turn=0,
    )
    state.invoked_skills["skill-b"] = InvokedSkillRecord(
        skill_id="skill-b",
        skill_path="/skills/b/SKILL.md",
        content_digest="b1",
        content="<skill-content>B</skill-content>",
        invoked_at_turn=1,
    )
    assembler = PromptAssembler()

    result = assembler.build_active_skill_messages(state)

    assert len(result) == 1
    content = result[0]["content"]
    assert "<skill-content>A</skill-content>" in content
    assert "<skill-content>B</skill-content>" in content


# ── build_runtime_context ────────────────────────────────────


def test_build_runtime_context_basic(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.todo_state.items = [
        TodoItem(content="Do X", active_form="Doing X", status="in_progress"),
    ]
    assembler = PromptAssembler()

    result = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<runtime-context>" in result
    assert "</runtime-context>" in result
    assert "<environment>" in result
    assert "<todo-state>" in result


def test_build_runtime_context_empty_when_nothing_to_render(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    result = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    # Even with just environment info, it should render since we always have environment
    assert "<runtime-context>" in result


def test_build_runtime_context_includes_active_skills(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["my-skill"] = InvokedSkillRecord(
        skill_id="my-skill",
        skill_path="/skills/my-skill/SKILL.md",
        content_digest="d1",
        content="<skill-content>hello</skill-content>",
        invoked_at_turn=0,
    )
    assembler = PromptAssembler()

    result = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<active-skills>" in result
    assert "<skill-content>hello</skill-content>" in result


def test_build_runtime_context_includes_todo_items(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.todo_state.items = [
        TodoItem(content="Task A", active_form="Working on A", status="pending"),
        TodoItem(content="Task B", active_form="Working on B", status="in_progress"),
    ]
    assembler = PromptAssembler()

    result = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "Working on A" in result
    assert "Working on B" in result


# ── build_query_overlay ──────────────────────────────────────


def test_build_query_overlay_empty_when_no_flags(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    run_state = RunState()
    assembler = PromptAssembler()

    result = assembler.build_query_overlay(state, run_state)

    assert result == ""


def test_build_query_overlay_with_replan_required(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    run_state = RunState(todo_replan_required=True, todo_replan_reason="tasks changed")
    assembler = PromptAssembler()

    result = assembler.build_query_overlay(state, run_state)

    assert "<query-overlay>" in result
    assert "<todo-replan>" in result
    assert "tasks changed" in result


def test_build_query_overlay_with_barrier_reason(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    run_state = RunState(barrier_reason="awaiting user input")
    assembler = PromptAssembler()

    result = assembler.build_query_overlay(state, run_state)

    assert "<query-overlay>" in result
    assert "<barrier>" in result
    assert "awaiting user input" in result


def test_build_query_overlay_with_both(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    run_state = RunState(
        todo_replan_required=True,
        todo_replan_reason="new task",
        barrier_reason="blocked",
    )
    assembler = PromptAssembler()

    result = assembler.build_query_overlay(state, run_state)

    assert "<query-overlay>" in result
    assert "<todo-replan>" in result
    assert "<barrier>" in result


# ── build_internal_runtime_view ──────────────────────────────


def test_build_internal_runtime_view_basic(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    run_state = RunState()
    assembler = PromptAssembler()

    result = assembler.build_internal_runtime_view(state, run_state)

    assert "invoked_skills" in result
    assert "todo_items" in result
    assert "barrier_reason" in result
    assert result["invoked_skills"] == []
    assert result["todo_items"] == []
    assert result["barrier_reason"] is None


def test_build_internal_runtime_view_with_data(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["skill-a"] = InvokedSkillRecord(
        skill_id="skill-a",
        skill_path="/a/SKILL.md",
        content_digest="d1",
        content="content",
        invoked_at_turn=0,
    )
    state.todo_state.items = [
        TodoItem(content="Task", active_form="Doing Task", status="in_progress"),
    ]
    run_state = RunState(barrier_reason="blocked")
    assembler = PromptAssembler()

    result = assembler.build_internal_runtime_view(state, run_state)

    assert result["invoked_skills"] == ["skill-a"]
    assert result["todo_items"] == ["Doing Task"]
    assert result["barrier_reason"] == "blocked"


# ── build_stable_context (alias) ─────────────────────────────


def test_build_stable_context_matches_build_stable(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))
    context = assembler.build_stable_context(state, project_root=str(tmp_path))

    assert stable == context


# ── file-runtime rendering ──────────────────────────────────


def test_build_runtime_context_includes_recent_file_runtime(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.read_file_state[str(tmp_path / "a.txt")] = FileState(
        content="alpha\nbeta\ngamma",
        timestamp=10.0,
        offset=None,
        limit=None,
    )
    assembler = PromptAssembler()

    runtime = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<file-runtime>" in runtime
    assert "a.txt" in runtime
    assert "alpha" in runtime


def test_build_runtime_context_omits_file_runtime_when_empty(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    runtime = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<file-runtime>" not in runtime


def test_build_internal_runtime_view_exposes_read_file_state(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.read_file_state[str(tmp_path / "a.txt")] = FileState(
        content="alpha",
        timestamp=10.0,
        offset=None,
        limit=None,
    )
    state.todo_state = TodoState(
        items=[TodoItem(content="Draft", active_form="Drafting", status="in_progress")]
    )
    run_state = RunState(barrier_reason="skill_expanded")
    assembler = PromptAssembler()

    internal = assembler.build_internal_runtime_view(state, run_state)

    assert str(tmp_path / "a.txt") in internal["read_file_state"]
    assert internal["todo_items"] == ["Drafting"]
    assert internal["barrier_reason"] == "skill_expanded"
