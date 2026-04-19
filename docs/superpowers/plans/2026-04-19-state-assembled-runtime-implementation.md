# State-Assembled Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `harness` from transcript-driven model input to state-assembled model input, so skills/todo/file runtime context are rendered from `SessionState` instead of surviving as history messages.

**Architecture:** Implement the cutover in dependency order. First make `PromptAssembler` able to render active skills from `invoked_skills`, then stop skill activation from writing `<skill-runtime>` into transcript, then promote `SessionState.read_file_state` and `todo_state` into runtime context, then switch `MessageViewBuilder` to produce `ModelInputView`, and finally wire `QueryLoop` / `ModelGateway` / `SessionEngine` to consume `system + transcript slice` instead of full `conversation_messages`.

**Tech Stack:** Python 3.12, `pytest`, existing `SessionState` / `RunState` / `PromptAssembler` / `MessageViewBuilder` / `QueryLoop` / `ModelGateway` / Anthropic client stack

---

## File Structure Map

### Modified Files

- `core/prompt/assembler.py`
  Responsibility: Add Phase 1 context-rendering APIs, render active skills from `invoked_skills`, render todo/file runtime summaries, and expose an internal runtime view.
- `core/skills/runtime.py`
  Responsibility: Keep `InvokedSkillRecord` as the skill truth source, move budget accounting off transcript, and provide runtime rendering helpers instead of transcript-only injection.
- `core/session/commands.py`
  Responsibility: Stop `/skills use` from appending `<skill-runtime>` messages into transcript; only record skill invocation metadata and events.
- `core/tools/builtin/skill.py`
  Responsibility: Stop skill tool from returning injected runtime transcript messages; rely on `state.invoked_skills` plus barrier.
- `core/tools/context.py`
  Responsibility: Make `ToolUseContext` share its file-state cache with `SessionState.read_file_state`.
- `core/session/view_builder.py`
  Responsibility: Introduce `ModelInputView`, select transcript slices by budget, assemble `system`, and expose `internal_runtime_view`.
- `core/session/__init__.py`
  Responsibility: Export `ModelInputView` instead of legacy `MessageView`.
- `core/session/engine.py`
  Responsibility: Stop bootstrapping stable/environment prompt into transcript and rely on view assembly at query time.
- `core/query/loop.py`
  Responsibility: Consume `ModelInputView` and pass `system` separately into the model gateway.
- `core/llm/client.py`
  Responsibility: Accept `system` separately from transcript messages and forward it to the underlying client.
- `core/llm/anthropic_client.py`
  Responsibility: Merge explicit `system` input with any normalized system text and call Anthropic with the top-level `system` parameter.

### Modified Tests

- `tests/session/test_prompt_assembler.py`
  Responsibility: Prove active skill rendering, runtime context rendering, internal runtime view, and query overlay rendering.
- `tests/session/test_engine_commands.py`
  Responsibility: Stop asserting transcript mutation for skill runtime; assert assembled view/system contains skill guidance instead.
- `tests/session/test_skill_tool.py`
  Responsibility: Stop expecting skill tool to inject runtime transcript messages; assert barrier + state mutation only.
- `tests/session/test_view_builder.py`
  Responsibility: Cover `ModelInputView`, `system` assembly, transcript slicing, and tool filtering.
- `tests/test_runtime_control_plane.py`
  Responsibility: Update runtime control plane assertions to the new skill-tool behavior.
- `tests/test_model_gateway.py`
  Responsibility: Assert explicit `system` is forwarded into the client.
- `tests/test_query_display.py`
  Responsibility: Update fake view builder/model gateway signatures to `ModelInputView` + `system`.
- `tests/test_query_logging.py`
  Responsibility: Same as display tests, plus reasoning/logging path.

### New Tests

- `tests/session/test_file_runtime_state.py`
  Responsibility: Prove `ToolUseContext` and `SessionState.read_file_state` share authority, and read/edit/write tools keep the session-level file cache fresh.
- `tests/session/test_state_assembled_runtime.py`
  Responsibility: Prove transcript independence: if assistant/tool transcript is removed, assembled `system` still contains skill/todo/file runtime context.

## Locked-In Implementation Decisions

- Phase 1 keeps `SessionState` flat. The plan does not introduce `state.runtime.*` nested dataclasses.
- `build_stable_context(...)`, `build_runtime_context(...)`, `build_query_overlay(...)`, and `build_internal_runtime_view(...)` are the new PromptAssembler interfaces. `build_stable(...)` may remain as a thin compatibility wrapper during the task sequence, but the main path must switch to the new methods by the end.
- `build_active_skill_messages(...)` must stop returning `[]`. It renders skill runtime from `state.invoked_skills`.
- `ToolUseContext.bind_runtime(...)` becomes the handoff point that aliases the tool-level file cache to `SessionState.read_file_state`.
- `ModelInputView.messages` is the final API payload. `transcript_slice` remains a builder-internal concept and, if needed for debugging, lives in `internal_runtime_view["transcript_slice"]`.
- `SessionEngine.bootstrap()` keeps skill discovery and revision calculation, but no longer writes stable prompt or `<environment>` messages into `conversation_messages`.
- Phase 1 intentionally renders the environment baseline via `build_runtime_context(...)` instead of `build_stable_context(...)`. This is a conscious deviation from the spec split because the payload depends on `working_dir` and replaces the old per-session environment transcript message.
- The initial cutover order is fixed: skill runtime rendering -> skill transcript removal -> todo/file runtime rendering -> view builder cutover -> query/model gateway cutover -> integration proof.

---

### Task 1: Add PromptAssembler Phase 1 APIs and render active skills from `invoked_skills`

**Files:**
- Modify: `core/prompt/assembler.py`
- Modify: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing tests for active skill rendering, runtime context, and query overlay**

```python
# tests/session/test_prompt_assembler.py
from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState
from core.skills.models import InvokedSkillRecord


def make_state(tmp_path: Path) -> SessionState:
    return SessionState(conversation_messages=[], skills_revision="rev-1")


def test_build_active_skill_messages_renders_invoked_skills(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path=str(tmp_path / ".harness" / "skills" / "analysis-report" / "SKILL.md"),
        content_digest="digest-1",
        content=(
            '  <skill id="analysis-report" source="local-inline">\n'
            "    <instruction>\nUse a fixed HTML structure.\n    </instruction>\n"
            "  </skill>"
        ),
        invoked_at_turn=1,
    )
    assembler = PromptAssembler()

    rendered = assembler.build_active_skill_messages(state)

    assert len(rendered) == 1
    assert rendered[0]["role"] == "system"
    assert "<active-skills>" in rendered[0]["content"]
    assert 'id="analysis-report"' in rendered[0]["content"]
    assert "Use a fixed HTML structure." in rendered[0]["content"]


def test_build_runtime_context_includes_skills_and_todo_snapshot(tmp_path: Path) -> None:
    state = make_state(tmp_path)
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
                content="Draft the report",
                active_form="Drafting the report",
                status="in_progress",
                workflow_ref="3",
            )
        ]
    )
    assembler = PromptAssembler()

    runtime = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<runtime-context>" in runtime
    assert "<active-skills>" in runtime
    assert "<todo-state>" in runtime
    assert "Drafting the report" in runtime


def test_build_query_overlay_renders_replan_flags() -> None:
    state = SessionState(conversation_messages=[])
    run_state = RunState(todo_replan_required=True, todo_replan_reason="skill_expanded")
    assembler = PromptAssembler()

    overlay = assembler.build_query_overlay(state, run_state)

    assert "<query-overlay>" in overlay
    assert "skill_expanded" in overlay
```

- [ ] **Step 2: Run the PromptAssembler tests to verify the new API is missing**

Run: `pytest tests/session/test_prompt_assembler.py -q`

Expected:
- `AttributeError: 'PromptAssembler' object has no attribute 'build_runtime_context'`
- `AssertionError` because `build_active_skill_messages()` still returns `[]`

- [ ] **Step 3: Implement the new PromptAssembler API with active-skill rendering**

```python
# core/prompt/assembler.py
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState, TodoItem

if TYPE_CHECKING:
    from core.skills.models import SkillMeta
    from core.skills.registry import SkillRegistry


def _stable_cache_key(state: SessionState, *, project_root: str | None = None) -> str:
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}:{digest}"


def _render_skill_catalog(state: SessionState) -> str:
    if not state.skill_catalog:
        return ""
    lines = ["<available-skills>"]
    for skill_id, meta in sorted(state.skill_catalog.items()):
        lines.append(f'  <skill id="{skill_id}">')
        lines.append(f"    名称：{meta.name}")
        lines.append(f"    描述：{meta.description}")
        if meta.when_to_use:
            lines.append(f"    适用：{meta.when_to_use}")
        lines.append("  </skill>")
    lines.append("</available-skills>")
    return "\n".join(lines)


def _render_todo_state(items: list[TodoItem]) -> str:
    if not items:
        return ""
    lines = ["<todo-state>"]
    for item in items:
        lines.append(
            f'  <item status="{item.status}"'
            + (f' workflow_ref="{item.workflow_ref}">' if item.workflow_ref else ">")
        )
        lines.append(f"    {item.active_form}")
        lines.append("  </item>")
    lines.append("</todo-state>")
    return "\n".join(lines)


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None, skill_registry: SkillRegistry | None = None):
        self._cache = cache or PromptCache()
        self._skill_registry = skill_registry

    def build_stable_context(self, state: SessionState, *, project_root: str | None = None) -> str:
        cache_key = _stable_cache_key(state, project_root=project_root)
        cached = self._cache.get(state.prompt_cache, cache_key)
        if cached is not None:
            return cached
        parts = [get_system_context(project_root=project_root)]
        catalog = _render_skill_catalog(state)
        if catalog:
            parts.append(catalog)
        stable_prompt = "\n\n".join(parts)
        return self._cache.set(state.prompt_cache, cache_key, stable_prompt)

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        return self.build_stable_context(state, project_root=project_root)

    def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
        if not state.invoked_skills:
            return []
        lines = ["<active-skills>"]
        for skill_id, record in sorted(state.invoked_skills.items()):
            lines.append(f'  <active-skill id="{skill_id}">')
            lines.append(record.content)
            lines.append("  </active-skill>")
        lines.append("</active-skills>")
        return [{"role": "system", "content": "\n".join(lines)}]

    def build_runtime_context(
        self,
        state: SessionState,
        *,
        working_dir: str,
        char_budget: int | None = None,
    ) -> str:
        parts = [get_user_context(working_dir)]
        parts.extend(message["content"] for message in self.build_active_skill_messages(state))
        todo_block = _render_todo_state(state.todo_state.items)
        if todo_block:
            parts.append(todo_block)
        body = "\n\n".join(part for part in parts if part).strip()
        if not body:
            return ""
        return f"<runtime-context>\n{body}\n</runtime-context>"

    def build_query_overlay(self, state: SessionState, run_state: RunState) -> str:
        if not run_state.todo_replan_required and not run_state.barrier_reason:
            return ""
        lines = ["<query-overlay>"]
        if run_state.todo_replan_required:
            lines.append(f'  <todo-replan reason="{run_state.todo_replan_reason or "unknown"}" />')
        if run_state.barrier_reason:
            lines.append(f'  <barrier reason="{run_state.barrier_reason}" />')
        lines.append("</query-overlay>")
        return "\n".join(lines)

    def build_internal_runtime_view(self, state: SessionState, run_state: RunState) -> dict[str, Any]:
        return {
            "invoked_skills": list(state.invoked_skills.keys()),
            "todo_items": [item.active_form for item in state.todo_state.items],
            "barrier_reason": run_state.barrier_reason,
        }
```

- [ ] **Step 4: Re-run PromptAssembler tests**

Run: `pytest tests/session/test_prompt_assembler.py -q`

Expected: PASS for the new PromptAssembler tests

- [ ] **Step 5: Commit the PromptAssembler Phase 1 API**

```bash
git add core/prompt/assembler.py tests/session/test_prompt_assembler.py
git commit -m "feat: add phase 1 runtime context assembler APIs"
```

---

### Task 2: Remove transcript-based skill runtime injection from `/skills use` and the `skill` tool

**Files:**
- Modify: `core/skills/runtime.py`
- Modify: `core/session/commands.py`
- Modify: `core/tools/builtin/skill.py`
- Modify: `tests/session/test_engine_commands.py`
- Modify: `tests/session/test_skill_tool.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: Rewrite the failing skill tests to target runtime authority instead of transcript mutation**

```python
# tests/session/test_engine_commands.py
from core.query.state import RunState


def test_handle_command_use_tracks_invoked_skill_without_writing_runtime_message(tmp_path: Path) -> None:
    write_skill(tmp_path, "analysis-report", "Analysis Report", "Generate reports", "Skill body")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills use analysis-report")

    assert "loaded" in result.lower() or "activated" in result.lower()
    assert "analysis-report" in engine.state.invoked_skills
    assert not any(
        "<skill-runtime>" in m.get("content", "")
        for m in engine.state.conversation_messages
    )


def test_active_skill_body_reaches_assembled_system_view(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Use a fixed HTML structure for all reports.",
    )
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")
    engine.append_message({"role": "user", "content": "Generate a report"})

    view = engine._view_builder.build(
        engine.state,
        run_state=RunState(),
        prompt_assembler=engine._prompt_assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert "<active-skills>" in view.system
    assert "Use a fixed HTML structure for all reports." in view.system
    assert any("Generate a report" in m["content"] for m in view.messages if m["role"] == "user")
```

```python
# tests/session/test_skill_tool.py
from core.tools.context import ExecutionBarrier, ToolUseContext


def test_skill_tool_records_invoked_skill_and_returns_barrier_without_injected_message(tmp_path: Path) -> None:
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
    assert result.injected_messages == []
    assert "analysis-report" in state.invoked_skills
```

```python
# tests/test_runtime_control_plane.py
def test_runtime_returns_empty_injected_messages_for_real_skill_tool(tmp_path) -> None:
    from core.tools.builtin.skill import handle as skill_handle
    from core.skills.registry import SkillRegistry

    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the workflow.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    state = SessionState(conversation_messages=[], skill_catalog=catalog)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=registry)
    ctx._set_call_identity(name="skill", call_id="toolu_skill", turn=1)

    result = skill_handle({"skill": "analysis-report"}, ctx)

    assert result.injected_messages == []
    assert result.barrier == ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
```

- [ ] **Step 2: Run the skill-related tests and confirm they fail against the transcript-based implementation**

Run: `pytest tests/session/test_engine_commands.py tests/session/test_skill_tool.py tests/test_runtime_control_plane.py -q`

Expected:
- failures asserting `<skill-runtime>` should not be present in transcript
- failures asserting `result.injected_messages == []`

- [ ] **Step 3: Move skill runtime authority fully onto `state.invoked_skills`**

```python
# core/skills/runtime.py
from __future__ import annotations

from core.skills.models import InvokedSkillRecord, SkillContent


def build_skill_runtime_body(skill_id: str, content: SkillContent) -> str:
    lines = [
        f'  <skill id="{skill_id}" source="local-inline">',
        "    <instruction>",
        content.body,
        "    </instruction>",
    ]
    if content.reference_bodies:
        lines.append("    <reference-files>")
        for path, body in content.reference_bodies.items():
            lines.append(f'      <file path="{path}">')
            lines.append(body)
            lines.append("      </file>")
        lines.append("    </reference-files>")
    lines.append("  </skill>")
    return "\n".join(lines)


def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 24_000) -> None:
    used_chars = sum(len(record.content) for record in state.invoked_skills.values())
    if used_chars + len(new_content) > max_chars:
        raise ValueError(f"Inline skill budget exceeded: {used_chars + len(new_content)} > {max_chars}")


def apply_skill_invocation(*, state, skill_id: str, content: SkillContent, turn: int) -> InvokedSkillRecord:
    rendered = build_skill_runtime_body(skill_id, content)
    ensure_inline_skill_budget(state=state, new_content=rendered)
    record = InvokedSkillRecord(
        skill_id=skill_id,
        skill_path=str(content.meta.skill_file),
        content_digest=content.content_digest,
        content=rendered,
        invoked_at_turn=turn,
    )
    state.invoked_skills[skill_id] = record
    return record
```

```python
# core/session/commands.py
    if subcmd == "use" and len(parts) == 3:
        skill_id = parts[2]
        if skill_id not in state.skill_catalog:
            return CommandResult(True, f"Skill not found: {skill_id}")
        content = registry.load(skill_id)
        try:
            apply_skill_invocation(
                state=state,
                skill_id=skill_id,
                content=content,
                turn=0,
            )
        except ValueError as exc:
            return CommandResult(True, str(exc))
        state.skill_events.append(
            SkillEvent(
                skill_id=skill_id,
                action="activated",
                source="user_command",
                conversation_index=len(state.conversation_messages),
            )
        )
        return CommandResult(True, f"Loaded skill runtime: {skill_id}")
```

```python
# core/tools/builtin/skill.py
    try:
        apply_skill_invocation(
            state=state,
            skill_id=skill_id,
            content=content,
            turn=context.turn_count,
        )
    except ValueError as exc:
        return ToolResult(output=str(exc), success=False, error="budget_exceeded")

    return ToolResult(
        output=f"Skill loaded: {skill_id}. Re-evaluate your next action using the active skill guidance.",
        success=True,
        injected_messages=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )
```

- [ ] **Step 4: Re-run the skill-related tests**

Run: `pytest tests/session/test_engine_commands.py tests/session/test_skill_tool.py tests/test_runtime_control_plane.py -q`

Expected: PASS for the updated skill-runtime assertions

- [ ] **Step 5: Commit the skill runtime cutover**

```bash
git add core/skills/runtime.py core/session/commands.py core/tools/builtin/skill.py tests/session/test_engine_commands.py tests/session/test_skill_tool.py tests/test_runtime_control_plane.py
git commit -m "feat: move skill runtime authority off transcript"
```

---

### Task 3: Promote session-backed file runtime and render todo/file summaries into runtime context

**Files:**
- Modify: `core/tools/context.py`
- Modify: `core/prompt/assembler.py`
- Create: `tests/session/test_file_runtime_state.py`
- Modify: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing tests for session-backed file state and runtime-context rendering**

```python
# tests/session/test_file_runtime_state.py
from core.session.state import SessionState
from core.tools.context import FileState, ToolUseContext


def test_bind_runtime_aliases_tool_file_cache_to_session_state(tmp_path) -> None:
    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)

    ctx.bind_runtime(session_state=state)
    ctx.set_file_state(
        str(tmp_path / "a.txt"),
        FileState(content="alpha", timestamp=1.0, offset=None, limit=None),
    )

    assert str(tmp_path / "a.txt") in state.read_file_state


def test_read_file_updates_session_read_file_state(tmp_path) -> None:
    from core.tools.builtin.read_file import handle
    from core.tools.context import FileState

    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)

    result = handle({"path": "a.txt"}, ctx)

    assert result.success is True
    saved = state.read_file_state[str(file_path)]
    assert isinstance(saved, FileState)
    assert saved.content == "alpha\nbeta"
```

```python
# tests/session/test_prompt_assembler.py
from core.tools.context import FileState


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


def test_build_internal_runtime_view_exposes_runtime_authority(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run the file-runtime and assembler tests to verify session-backed file state is still disconnected**

Run: `pytest tests/session/test_file_runtime_state.py tests/session/test_prompt_assembler.py -q`

Expected:
- `state.read_file_state` stays empty because `ToolUseContext` still uses a private `_file_state`
- runtime context lacks `<file-runtime>`

- [ ] **Step 3: Alias the tool file cache to `SessionState.read_file_state` and render file/todo summaries**

```python
# core/tools/context.py
    def bind_runtime(self, *, session_state: Any | None = None, skill_registry: Any | None = None) -> None:
        if session_state is not None:
            self._session_state = session_state
            if hasattr(session_state, "read_file_state") and isinstance(session_state.read_file_state, dict):
                self._file_state = session_state.read_file_state
        if skill_registry is not None:
            self._skill_registry = skill_registry
```

```python
# core/prompt/assembler.py
from pathlib import Path
from typing import cast
from core.tools.context import FileState


def _render_file_runtime(read_file_state: dict[str, FileState], *, char_budget: int) -> str:
    if not read_file_state:
        return ""
    lines = ["<file-runtime>"]
    budget_used = len("<file-runtime>\n</file-runtime>")
    for path, value in sorted(read_file_state.items(), key=lambda item: cast(FileState, item[1]).timestamp, reverse=True):
        state = cast(FileState, value)
        excerpt = state.content[:400]
        block = [
            f'  <file path="{Path(path).name}" full_read="{str(state.is_full_read).lower()}">',
            excerpt,
            "  </file>",
        ]
        rendered = "\n".join(block)
        if budget_used + len(rendered) > char_budget:
            break
        lines.extend(block)
        budget_used += len(rendered)
    lines.append("</file-runtime>")
    return "\n".join(lines) if len(lines) > 2 else ""


    def build_runtime_context(
        self,
        state: SessionState,
        *,
        working_dir: str,
        char_budget: int | None = None,
    ) -> str:
        total_budget = char_budget or 36_000
        parts = [get_user_context(working_dir)]
        parts.extend(message["content"] for message in self.build_active_skill_messages(state))
        todo_block = _render_todo_state(state.todo_state.items)
        if todo_block:
            parts.append(todo_block)
        file_block = _render_file_runtime(state.read_file_state, char_budget=12_000)
        if file_block:
            parts.append(file_block)
        body = "\n\n".join(part for part in parts if part)[:total_budget].strip()
        if not body:
            return ""
        return f"<runtime-context>\n{body}\n</runtime-context>"

    def build_internal_runtime_view(self, state: SessionState, run_state: RunState) -> dict[str, Any]:
        return {
            "invoked_skills": list(state.invoked_skills.keys()),
            "todo_items": [item.active_form for item in state.todo_state.items],
            "read_file_state": dict(state.read_file_state),
            "barrier_reason": run_state.barrier_reason,
        }
```

Replace `build_runtime_context(...)` from Task 1 with this version; Task 3 is a replacement of the earlier minimal renderer, not an additive second implementation.

- [ ] **Step 4: Re-run the file-runtime and assembler tests**

Run: `pytest tests/session/test_file_runtime_state.py tests/session/test_prompt_assembler.py -q`

Expected: PASS, including file-runtime rendering assertions

- [ ] **Step 5: Commit session-backed file runtime and runtime-context rendering**

```bash
git add core/tools/context.py core/prompt/assembler.py tests/session/test_file_runtime_state.py tests/session/test_prompt_assembler.py
git commit -m "feat: render session-backed file and todo runtime context"
```

---

### Task 4: Introduce `ModelInputView` and make `MessageViewBuilder` assemble `system + transcript slice + tools`

**Files:**
- Modify: `core/session/view_builder.py`
- Modify: `core/session/__init__.py`
- Modify: `tests/session/test_view_builder.py`

- [ ] **Step 1: Replace the view-builder tests with `ModelInputView` assertions**

```python
# tests/session/test_view_builder.py
from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.state import SessionState
from core.session.view_builder import MessageViewBuilder, ModelInputView


def test_build_returns_system_and_transcript_slice_separately(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
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

    assert isinstance(view, ModelInputView)
    assert isinstance(view.system, str)
    assert view.messages == state.conversation_messages
    assert "transcript_slice" in view.internal_runtime_view


def test_build_with_run_state_filters_tools(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    run_state = RunState(allowed_tools_override={"todo"})
    builder = MessageViewBuilder(
        tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ]
    )
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=run_state,
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_respects_transcript_char_budget(tmp_path: Path) -> None:
    long_text = "x" * 300
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": long_text},
            {"role": "user", "content": "u2"},
        ],
    )
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
        transcript_char_budget=50,
    )

    assert view.messages[-1] == {"role": "user", "content": "u2"}
    assert sum(len(m.get("content", "")) for m in view.messages if isinstance(m.get("content"), str)) <= 50
```

- [ ] **Step 2: Run the view-builder tests to verify the current builder still returns the old `MessageView`**

Run: `pytest tests/session/test_view_builder.py -q`

Expected:
- import failure for `ModelInputView`
- `TypeError` because `build()` does not accept `prompt_assembler`, `working_dir`, or `project_root`

- [ ] **Step 3: Implement `ModelInputView` and transcript slicing**

```python
# core/session/view_builder.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.prompt.assembler import PromptAssembler
from .state import SessionState


@dataclass(slots=True)
class ModelInputView:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    internal_runtime_view: dict[str, Any] = field(default_factory=dict)


class MessageViewBuilder:
    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools = tools

    def _content_char_cost(self, content: Any) -> int:
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return len(str(content)[:6_000])
        if isinstance(content, dict):
            return len(str(content)[:6_000])
        return 0

    def _select_transcript_slice(
        self,
        messages: list[dict[str, Any]],
        *,
        char_budget: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            content = message.get("content", "")
            cost = self._content_char_cost(content)
            if selected and used + cost > char_budget:
                continue
            selected.append(message)
            used += cost
            if used >= char_budget:
                break
        return list(reversed(selected))

    def build(
        self,
        state: SessionState,
        *,
        run_state,
        prompt_assembler: PromptAssembler,
        working_dir: str,
        project_root: str | None = None,
        transcript_char_budget: int | None = None,
    ) -> ModelInputView:
        budget = transcript_char_budget or 24_000
        transcript_slice = self._select_transcript_slice(state.conversation_messages, char_budget=budget)
        system_parts = [
            prompt_assembler.build_stable_context(state, project_root=project_root),
            prompt_assembler.build_runtime_context(state, working_dir=working_dir),
            prompt_assembler.build_query_overlay(state, run_state),
        ]
        tools = self._tools
        if run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        internal_runtime_view = prompt_assembler.build_internal_runtime_view(state, run_state)
        internal_runtime_view["transcript_slice"] = list(transcript_slice)
        return ModelInputView(
            system="\n\n".join(part for part in system_parts if part),
            messages=transcript_slice,
            tools=tools,
            internal_runtime_view=internal_runtime_view,
        )
```

```python
# core/session/__init__.py
from .state import SessionState
from .store import SessionStore
from .view_builder import ModelInputView, MessageViewBuilder

__all__ = ["ModelInputView", "MessageViewBuilder", "SessionState", "SessionStore"]
```

- [ ] **Step 4: Re-run the view-builder tests**

Run: `pytest tests/session/test_view_builder.py -q`

Expected: PASS with the new `ModelInputView` semantics. `tests/test_query_display.py` and `tests/test_query_logging.py` are expected to stay broken until Task 5 updates the query-path callers and fake builders to the new signature.

- [ ] **Step 5: Commit the new view-builder cutover**

```bash
git add core/session/view_builder.py core/session/__init__.py tests/session/test_view_builder.py
git commit -m "feat: add model input view assembly"
```

---

### Task 5: Wire `ModelInputView.system` through `SessionEngine`, `QueryLoop`, and `ModelGateway`, and stop bootstrapping prompt/environment into transcript

**Files:**
- Modify: `core/session/engine.py`
- Modify: `core/query/loop.py`
- Modify: `core/llm/client.py`
- Modify: `core/llm/anthropic_client.py`
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/test_model_gateway.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/test_query_logging.py`

- [ ] **Step 1: Write the failing tests for explicit `system` forwarding and transcript-clean bootstrap**

```python
# tests/test_model_gateway.py
class FakeClient:
    def __init__(self) -> None:
        self.last_call = None

    def call(self, messages, *, system="", tools=None):
        self.last_call = {"messages": messages, "system": system, "tools": tools}
        return SimpleNamespace(
            content="answer",
            tool_calls=[],
            finish_reason="end_turn",
            prompt_tokens=10,
            completion_tokens=20,
            reasoning="step by step",
        )


def test_model_gateway_forwards_explicit_system() -> None:
    client = FakeClient()
    gateway = ModelGateway(client)

    response = gateway.call_once([{"role": "user", "content": "hi"}], system="SYSTEM", tools=None)

    assert response.reasoning == "step by step"
    assert client.last_call["system"] == "SYSTEM"
```

```python
# tests/session/test_engine_commands.py
def test_bootstrap_discovers_skills_without_writing_prompt_messages(tmp_path: Path) -> None:
    write_skill(tmp_path, "discovered-skill", "Discovered", "A discovered skill", "Body")
    engine = make_engine(tmp_path)

    engine.bootstrap()

    assert "discovered-skill" in engine.state.skill_catalog
    assert engine.state.skills_revision is not None
    assert engine.state.conversation_messages == []
```

```python
# tests/session/test_prompt_assembler.py
def test_build_runtime_context_includes_environment_baseline(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    runtime = assembler.build_runtime_context(state, working_dir=str(tmp_path))

    assert "<environment>" in runtime
    assert str(tmp_path) in runtime
```

```python
# tests/test_query_display.py
from core.session.view_builder import ModelInputView


class FakeViewBuilder:
    def build(self, state: SessionState, *, run_state, prompt_assembler, working_dir, project_root=None, transcript_char_budget=None) -> ModelInputView:
        return ModelInputView(system="SYSTEM", messages=list(state.conversation_messages), tools=None)


class FakeModelGateway:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.system_inputs: list[str] = []

    def call_once(self, messages, *, system, tools):
        self.system_inputs.append(system)
        return self._responses.pop(0)
```

- [ ] **Step 2: Run the gateway, engine, and query tests to verify the signatures still use transcript-only messages**

Run: `pytest tests/test_model_gateway.py tests/session/test_engine_commands.py tests/test_query_display.py tests/test_query_logging.py -q`

Expected:
- `TypeError` because `call_once()` does not accept `system`
- bootstrap test fails because `conversation_messages` still contains prompt/environment messages
- fake view builder signature mismatch

- [ ] **Step 3: Update the main path to consume `ModelInputView` and explicit `system`**

```python
# core/llm/client.py
class ModelGateway:
    def __init__(self, client: Any | None = None):
        self._client = client

    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        if self._client is None:
            raise RuntimeError("No LLM client configured")
        response = self._client.call(messages, system=system, tools=tools)
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            reasoning=response.reasoning or "",
        )
```

```python
# core/llm/anthropic_client.py
    def call(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        display: RunDisplayOptions | None = None,
    ) -> LLMResponse:
        ...
        normalized_system, api_messages = normalize_messages(messages)
        full_system = "\n\n".join(part for part in [system, normalized_system] if part)
        params: dict[str, Any] = {
            "model": MODEL,
            "system": full_system,
            "messages": api_messages,
            "max_tokens": MAX_TOKENS,
        }
```

```python
# core/query/loop.py
            view = view_builder.build(
                session_state,
                run_state=state,
                prompt_assembler=prompt_assembler,
                working_dir=tool_context.working_dir if tool_context is not None else ".",
                project_root=tool_context.working_dir if tool_context is not None else None,
            )
            active_tools = None if state.stop_reason == "max_turns" else view.tools
            model_resp = model_gateway.call_once(view.messages, system=view.system, tools=active_tools)
```

```python
# core/session/engine.py
    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        working_dir = Path(self._tool_context.working_dir) if self._tool_context else Path(".")
        skills_dir = working_dir / ".harness" / "skills"
        self._state.skill_catalog = self._skill_registry.discover(
            skills_dir,
            working_dir=working_dir,
        )
        self._state.skills_revision = compute_skills_revision(self._state.skill_catalog)
        self._bootstrapped = True
```

```python
# core/prompt/assembler.py
class PromptAssembler:
    ...

    # Delete build_environment_message(). Environment baseline is now rendered
    # through build_runtime_context(...), so the old transcript-oriented helper is dead code.
```

- [ ] **Step 4: Re-run the gateway, engine, and query tests**

Run: `pytest tests/session/test_prompt_assembler.py tests/test_model_gateway.py tests/session/test_engine_commands.py tests/test_query_display.py tests/test_query_logging.py -q`

Expected: PASS with explicit `system` forwarding and clean transcript bootstrap

- [ ] **Step 5: Commit the query/model-gateway cutover**

```bash
git add core/prompt/assembler.py core/session/engine.py core/query/loop.py core/llm/client.py core/llm/anthropic_client.py tests/session/test_prompt_assembler.py tests/test_model_gateway.py tests/session/test_engine_commands.py tests/test_query_display.py tests/test_query_logging.py
git commit -m "feat: consume state-assembled model input view"
```

---

### Task 6: Add transcript-independence proof tests and clean up the remaining transcript-centric assertions

**Files:**
- Create: `tests/session/test_state_assembled_runtime.py`
- Modify: `tests/session/test_engine_commands.py`
- Modify: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing integration test for transcript independence**

```python
# tests/session/test_state_assembled_runtime.py
from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState
from core.session.view_builder import MessageViewBuilder
from core.skills.models import InvokedSkillRecord
from core.tools.context import FileState


def test_runtime_view_survives_when_assistant_and_tool_transcript_is_removed(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run the integration proof test and confirm it fails before the final cleanup**

Run: `pytest tests/session/test_state_assembled_runtime.py -q`

Expected:
- failure if assembled `system` still misses skill/todo/file runtime context

- [ ] **Step 3: Clean up the remaining transcript-centric assertions in existing tests**

```python
# tests/session/test_engine_commands.py
def test_active_skill_persists_across_turns_in_assembled_system(tmp_path: Path) -> None:
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
    view1 = engine._view_builder.build(
        engine.state,
        run_state=RunState(),
        prompt_assembler=engine._prompt_assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    engine.append_message({"role": "assistant", "content": "reply 1"})
    engine.append_message({"role": "user", "content": "turn 2"})
    view2 = engine._view_builder.build(
        engine.state,
        run_state=RunState(),
        prompt_assembler=engine._prompt_assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert "analysis-report" in engine.state.invoked_skills
    assert "Persistent skill content." in view1.system
    assert "Persistent skill content." in view2.system
```

- [ ] **Step 4: Run the full targeted regression suite for the cutover**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_engine_commands.py tests/session/test_skill_tool.py tests/session/test_file_runtime_state.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/test_runtime_control_plane.py tests/test_model_gateway.py tests/test_query_display.py tests/test_query_logging.py -q`

Expected: all targeted tests pass

- [ ] **Step 5: Commit the transcript-independence proof and test cleanup**

```bash
git add tests/session/test_prompt_assembler.py tests/session/test_engine_commands.py tests/session/test_skill_tool.py tests/session/test_file_runtime_state.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/test_runtime_control_plane.py tests/test_model_gateway.py tests/test_query_display.py tests/test_query_logging.py
git commit -m "test: prove state-assembled runtime no longer depends on transcript"
```

---

## Self-Review

### Spec coverage

- `build_active_skill_messages()` 不再为空：Task 1。
- skill truth 不再依赖 transcript：Task 2。
- `SessionState.read_file_state` 成为 runtime authority：Task 3。
- `build_runtime_context()` 渲染 todo/file runtime：Task 3。
- `ModelInputView` / `MessageViewBuilder` 切换：Task 4。
- `system + messages` 分通道进入模型：Task 5。
- `SessionEngine.bootstrap()` 不再污染 transcript：Task 5。
- `build_environment_message()` 清理，并将环境基线迁入 runtime context：Task 5。
- transcript independence 集成证明：Task 6。

### Placeholder scan

- 没有 `TODO` / `TBD` / “later” / “similar to Task N” 之类占位语句。
- 每个代码步骤都给了具体的测试或实现片段。
- 每个任务都包含运行命令和提交命令。

### Type consistency

- `ModelInputView.system/messages/tools/internal_runtime_view` 在 Task 4-5 一致。
- `build_stable_context` / `build_runtime_context` / `build_query_overlay` / `build_internal_runtime_view` 在 Task 1-5 一致。
- `ModelGateway.call_once(..., system=..., tools=...)` 与 Task 5 的 fake gateways 一致。
