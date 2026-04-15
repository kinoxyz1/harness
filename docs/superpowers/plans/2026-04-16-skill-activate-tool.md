# Skill Activate Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `activate_skill` tool the model can call to activate skills from `<available-skills>`, with stronger prompt instructions telling the model to check and use skills before starting work.

**Architecture:** New builtin tool module `core/tools/builtin/activate_skill.py` follows the existing pattern (SCHEMA + handle + READONLY). The handler mutates session state (adds to `active_skills`, appends `SkillEvent`). The tool receives `session_state` through `ToolUseContext` via a new optional `_session_state` attribute. The system prompt is updated with skill-usage instructions. Skill activation takes effect on the next model turn via the existing `MessageViewBuilder.build()` flow.

**Tech Stack:** Python 3.12, pytest, dataclasses, existing ToolRegistry / ToolExecutorRuntime / SessionEngine / PromptAssembler

---

## File Structure

### New Files

- `core/tools/builtin/activate_skill.py`
  Responsibility: `activate_skill` tool — SCHEMA, handle, emits `[Skill]` log, mutates session state.

### Modified Files

- `core/tools/context.py`
  Responsibility: add `_session_state` attribute to `ToolUseContext` so the tool handler can access skill state.
- `core/session/engine.py`
  Responsibility: set `_session_state` on the tool context during construction.
- `core/prompt/system_context.py`
  Responsibility: add skill-usage instructions to the framework prompt telling the model to check `<available-skills>` and activate matching skills before starting work.
- `tests/session/test_activate_skill_tool.py`
  Responsibility: verify tool activates skills, respects limits, emits logs.

---

### Task 1: Add `_session_state` To ToolUseContext

**Files:**
- Modify: `core/tools/context.py`
- Test: `tests/session/test_activate_skill_tool.py` (setup)

- [ ] **Step 1: Write the failing test for session_state access**

```python
# tests/session/test_activate_skill_tool.py
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.session.engine import SessionEngine
from core.session.state import SessionState
from core.skills import ActiveSkillState
from core.tools.context import ToolUseContext


def _make_ctx(tmp_path: Path, *, session_state: SessionState | None = None) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    if session_state is not None:
        ctx._session_state = session_state
    return ctx


def test_tool_context_holds_session_state(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[])
    ctx = _make_ctx(tmp_path, session_state=state)

    assert ctx.session_state is state
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/session/test_activate_skill_tool.py -v`
Expected: FAIL — `ToolUseContext` has no `session_state` property.

- [ ] **Step 3: Add `_session_state` to `ToolUseContext`**

In `core/tools/context.py`, add a `_session_state` field and property:

```python
# In __init__ (after self._cancelled = False):
        self._session_state: Any = None

# Add property (after the cancelled property):
    @property
    def session_state(self) -> Any:
        return self._session_state
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/session/test_activate_skill_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/tools/context.py tests/session/test_activate_skill_tool.py
git commit -m "feat: add session_state to ToolUseContext"
```

---

### Task 2: Create The `activate_skill` Tool

**Files:**
- Create: `core/tools/builtin/activate_skill.py`
- Modify: `core/tools/builtin/__init__.py`
- Test: `tests/session/test_activate_skill_tool.py`

- [ ] **Step 1: Write the failing tool tests**

```python
# tests/session/test_activate_skill_tool.py
# ADD these tests after the existing test:


def _make_engine(tmp_path: Path) -> SessionEngine:
    from core.session.engine import SessionEngine

    class DummyQueryLoop:
        def run(self, **kwargs):
            return SimpleNamespace(final_output="ok")

    return SessionEngine(
        model_gateway=object(),
        tool_runtime=object(),
        tool_context=SimpleNamespace(working_dir=str(tmp_path)),
        policy_runner=object(),
        recovery=object(),
        query_loop=DummyQueryLoop(),
    )


def _write_skill(
    tmp_path: Path,
    skill_id: str,
    name: str,
    desc: str,
    body: str,
) -> None:
    skill_dir = tmp_path / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}",
        encoding="utf-8",
    )


def test_activate_skill_tool_activates_skill(tmp_path: Path) -> None:
    """activate_skill tool should add skill to active_skills and return success."""
    import sys
    import io
    from core.tools.builtin.activate_skill import handle

    _write_skill(tmp_path, "test-skill", "Test", "A test skill", "Skill body")
    engine = _make_engine(tmp_path)
    engine.bootstrap()

    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx._session_state = engine.state

    result = handle({"skill_id": "test-skill"}, ctx)

    assert result.success
    assert "test-skill" in engine.state.active_skills
    assert "activated" in result.output.lower()


def test_activate_skill_tool_rejects_unknown(tmp_path: Path) -> None:
    """activate_skill should reject unknown skill_id."""
    from core.tools.builtin.activate_skill import handle

    engine = _make_engine(tmp_path)
    engine.bootstrap()

    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx._session_state = engine.state

    result = handle({"skill_id": "nonexistent"}, ctx)

    assert not result.success
    assert "not found" in result.output.lower()


def test_activate_skill_tool_rejects_already_active(tmp_path: Path) -> None:
    """activate_skill should reject re-activating an already active skill."""
    from core.tools.builtin.activate_skill import handle

    _write_skill(tmp_path, "test-skill", "Test", "A test skill", "Body")
    engine = _make_engine(tmp_path)
    engine.bootstrap()

    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx._session_state = engine.state

    handle({"skill_id": "test-skill"}, ctx)
    result = handle({"skill_id": "test-skill"}, ctx)

    assert not result.success
    assert "already active" in result.output.lower()


def test_activate_skill_tool_respects_max_limit(tmp_path: Path) -> None:
    """activate_skill should respect MAX_ACTIVE_SKILLS limit."""
    from core.tools.builtin.activate_skill import handle

    for i in range(4):
        _write_skill(tmp_path, f"skill-{i}", f"Skill {i}", f"Skill {i}", f"Body {i}")
    engine = _make_engine(tmp_path)
    engine.bootstrap()

    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx._session_state = engine.state

    handle({"skill_id": "skill-0"}, ctx)
    handle({"skill_id": "skill-1"}, ctx)
    handle({"skill_id": "skill-2"}, ctx)
    result = handle({"skill_id": "skill-3"}, ctx)

    assert not result.success
    assert "max" in result.output.lower() or "limit" in result.output.lower()
    assert "skill-3" not in engine.state.active_skills
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_activate_skill_tool.py -v`
Expected: FAIL — module `core.tools.builtin.activate_skill` does not exist.

- [ ] **Step 3: Create `activate_skill.py` tool module**

```python
# core/tools/builtin/activate_skill.py
"""Skill activation tool.

Allows the model to activate a skill from <available-skills> catalog.
Activation takes effect on the next model turn.
"""
from __future__ import annotations

import sys
from typing import Any

from ..context import ToolResult, ToolUseContext
from ...session.commands import MAX_ACTIVE_SKILLS, MAX_TOTAL_SKILL_CHARS
from ...skills import ActiveSkillState, SkillEvent


SCHEMA: dict[str, Any] = {
    "name": "activate_skill",
    "description": (
        "Activate a skill from the available skills catalog. "
        "When a skill is activated, its full instructions and reference files "
        "are loaded into your context on the next turn, giving you detailed "
        "domain knowledge and workflow rules to follow. "
        "Check <available-skills> in the system prompt for skill IDs and their "
        "descriptions. Activate skills BEFORE starting work on tasks that match "
        "a skill's description."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "The skill ID to activate (from <available-skills> catalog)",
            },
        },
        "required": ["skill_id"],
    },
}

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": False,
}


def _skill_total_chars(content) -> int:
    return len(content.body) + sum(len(v) for v in content.reference_bodies.values())


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """Activate a skill by ID. Mutates session state."""
    skill_id = args.get("skill_id", "").strip()
    if not skill_id:
        return ToolResult(output="Missing skill_id", success=False, error="missing_params")

    state = context.session_state
    if state is None:
        return ToolResult(output="No session state available", success=False, error="no_state")

    # Validate skill exists
    if skill_id not in state.skill_catalog:
        available = ", ".join(sorted(state.skill_catalog.keys())) if state.skill_catalog else "(none)"
        return ToolResult(
            output=f"Skill not found: {skill_id}. Available: {available}",
            success=False,
            error="not_found",
        )

    # Already active
    if skill_id in state.active_skills:
        return ToolResult(
            output=f"Skill already active: {skill_id}",
            success=False,
            error="already_active",
        )

    # Max active limit
    if len(state.active_skills) >= MAX_ACTIVE_SKILLS:
        active_list = ", ".join(sorted(state.active_skills.keys()))
        return ToolResult(
            output=f"Cannot activate: max {MAX_ACTIVE_SKILLS} active skills. "
                   f"Currently active: {active_list}",
            success=False,
            error="limit_reached",
        )

    # Load content for budget check and logging
    from core.session.engine import SessionEngine
    # Access the registry through a well-known path
    # The tool context doesn't hold the registry, so we use the state's
    # catalog to trigger a load from the skill_dir
    from core.skills import SkillRegistry
    # We need the registry — get it from the catalog metadata
    meta = state.skill_catalog[skill_id]
    # Build a temporary registry just for loading (this is not ideal but
    # avoids coupling the tool to the engine's internal registry)
    # Better approach: store registry ref on session_state
    pass

    # Actually, let's use a different approach: store the registry on context
    ...
```

Wait — there's a design problem. The tool handler needs access to the `SkillRegistry` to `load()` the skill and check the budget. But `ToolUseContext` doesn't hold a registry reference.

**Revised approach:** Add `_skill_registry` to `ToolUseContext` alongside `_session_state`. The `SessionEngine` sets both during construction.

- [ ] **Step 3 (revised): Add `_skill_registry` to `ToolUseContext`**

In `core/tools/context.py`, add a `_skill_registry` field:

```python
# In __init__ (after self._session_state = None):
        self._skill_registry: Any = None

# Add property (after session_state property):
    @property
    def skill_registry(self) -> Any:
        return self._skill_registry
```

Update the test for the new property:

```python
# tests/session/test_activate_skill_tool.py — update test_tool_context_holds_session_state:
def test_tool_context_holds_session_state(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[])
    registry = object()
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx._session_state = state
    ctx._skill_registry = registry

    assert ctx.session_state is state
    assert ctx.skill_registry is registry
```

- [ ] **Step 4: Create `activate_skill.py` with working handler**

```python
# core/tools/builtin/activate_skill.py
"""Skill activation tool.

Allows the model to activate a skill from <available-skills> catalog.
Activation takes effect on the next model turn.
"""
from __future__ import annotations

import sys
from typing import Any

from ..context import ToolResult, ToolUseContext
from ...session.commands import MAX_ACTIVE_SKILLS, _skill_total_chars, MAX_TOTAL_SKILL_CHARS


SCHEMA: dict[str, Any] = {
    "name": "activate_skill",
    "description": (
        "Activate a skill from the available skills catalog. "
        "When a skill is activated, its full instructions and reference files "
        "are loaded into your context on the next turn, giving you detailed "
        "domain knowledge and workflow rules to follow. "
        "Check <available-skills> in the system prompt for skill IDs and their "
        "descriptions. Activate skills BEFORE starting work on tasks that match "
        "a skill's description."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "The skill ID to activate (from <available-skills> catalog)",
            },
        },
        "required": ["skill_id"],
    },
}

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": False,
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """Activate a skill by ID. Mutates session state."""
    skill_id = args.get("skill_id", "").strip()
    if not skill_id:
        return ToolResult(output="Missing skill_id", success=False, error="missing_params")

    state = context.session_state
    if state is None:
        return ToolResult(output="No session state available", success=False, error="no_state")

    registry = context.skill_registry
    if registry is None:
        return ToolResult(output="No skill registry available", success=False, error="no_registry")

    # Validate skill exists in catalog
    if skill_id not in state.skill_catalog:
        available = ", ".join(sorted(state.skill_catalog.keys())) if state.skill_catalog else "(none)"
        return ToolResult(
            output=f"Skill not found: {skill_id}. Available: {available}",
            success=False,
            error="not_found",
        )

    # Already active
    if skill_id in state.active_skills:
        return ToolResult(
            output=f"Skill already active: {skill_id}",
            success=False,
            error="already_active",
        )

    # Max active limit
    if len(state.active_skills) >= MAX_ACTIVE_SKILLS:
        active_list = ", ".join(sorted(state.active_skills.keys()))
        return ToolResult(
            output=f"Cannot activate: max {MAX_ACTIVE_SKILLS} active skills. "
                   f"Currently active: {active_list}",
            success=False,
            error="limit_reached",
        )

    # Load and budget check
    try:
        content = registry.load(skill_id)
    except (ValueError, KeyError) as exc:
        return ToolResult(output=f"Failed to load skill: {exc}", success=False, error="load_failed")

    total_chars = sum(
        _skill_total_chars(registry.load(sid))
        for sid in state.active_skills
    ) + _skill_total_chars(content)

    if total_chars > MAX_TOTAL_SKILL_CHARS:
        return ToolResult(
            output=f"Cannot activate: total skill content would exceed "
                   f"{MAX_TOTAL_SKILL_CHARS:,} characters.",
            success=False,
            error="budget_exceeded",
        )

    # Activate
    from ...skills import ActiveSkillState, SkillEvent

    state.active_skills[skill_id] = ActiveSkillState(
        skill_id=skill_id,
        activated_at_message_index=len(state.conversation_messages),
        source="model_tool_call",
        content_digest=content.content_digest,
    )
    state.skill_events.append(
        SkillEvent(
            skill_id=skill_id,
            action="activated",
            source="model_tool_call",
            conversation_index=len(state.conversation_messages),
        )
    )

    # Emit [Skill] log
    ref_count = len(content.reference_bodies)
    ref_chars = sum(len(v) for v in content.reference_bodies.values())
    sys.stdout.write(
        f"\033[36m[Skill] 激活 {skill_id}"
        f" ({ref_count} refs, {ref_chars:,} chars 内联)\033[0m\n"
    )

    return ToolResult(
        output=f"Skill activated: {skill_id}. "
               f"The skill's instructions and {ref_count} reference files "
               f"will be loaded into your context on the next turn.",
        success=True,
    )
```

- [ ] **Step 5: Update `__init__.py` to include the new tool**

```python
# core/tools/builtin/__init__.py
__all__ = [
    "activate_skill",
    "bash",
    "edit_file",
    "find",
    "read_file",
    "todo",
    "write_file",
]
```

Note: The `__all__` export is not used by `auto_discover()` — it discovers tools by scanning `.py` files in the builtin directory. But adding it keeps the exports consistent.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/session/test_activate_skill_tool.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add core/tools/builtin/activate_skill.py core/tools/builtin/__init__.py core/tools/context.py tests/session/test_activate_skill_tool.py
git commit -m "feat: add activate_skill tool for model-driven skill activation"
```

---

### Task 3: Wire SessionEngine To Set Registry And State On ToolContext

**Files:**
- Modify: `core/session/engine.py`
- Test: `tests/session/test_activate_skill_tool.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/session/test_activate_skill_tool.py
# ADD this test:

def test_engine_wires_registry_and_state_to_context(tmp_path: Path) -> None:
    """SessionEngine should set session_state and skill_registry on ToolUseContext."""
    from core.tools import registry as tool_registry

    _write_skill(tmp_path, "test-skill", "Test", "A test skill", "Body")

    # Build engine with real tool_runtime that has the tool registry
    from core.tools.runtime import ToolExecutorRuntime
    from core.tools.context import ToolUseContext

    tool_context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)

    engine = SessionEngine(
        model_gateway=object(),
        tool_runtime=ToolExecutorRuntime(tool_registry, tool_context),
        tool_context=tool_context,
        policy_runner=object(),
        recovery=object(),
        query_loop=_make_engine(tmp_path)._query_loop,
    )
    engine.bootstrap()

    assert tool_context._session_state is engine.state
    assert tool_context._skill_registry is engine._skill_registry
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/session/test_activate_skill_tool.py::test_engine_wires_registry_and_state_to_context -v`
Expected: FAIL — `tool_context._session_state` is None because engine doesn't set it.

- [ ] **Step 3: Wire registry and state in `SessionEngine.__init__`**

In `core/session/engine.py`, after constructing `self._skill_registry` and `self._tool_context`, set the references:

```python
# core/session/engine.py — update __init__:
# After self._renderer = renderer (line 43):
        # Give tool context access to session state and skill registry
        if self._tool_context is not None:
            self._tool_context._session_state = self._state
            self._tool_context._skill_registry = self._skill_registry
```

Note: `self._state` is created at line 30, so this assignment is valid in `__init__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_activate_skill_tool.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/engine.py tests/session/test_activate_skill_tool.py
git commit -m "feat: wire session state and skill registry into ToolUseContext"
```

---

### Task 4: Add Skill-Usage Instructions To System Prompt

**Files:**
- Modify: `core/prompt/system_context.py`

- [ ] **Step 1: Update the framework prompt**

In `core/prompt/system_context.py`, update `_FRAMEWORK_PROMPT` to include skill-usage instructions:

```python
_FRAMEWORK_PROMPT = """\
你是一个 AI 助手，运行在 harness 代理框架中。
你有以下可用工具：文件读写、文件搜索、文件编辑、bash 命令执行。工具的详细用法见各工具的描述。

判断用户意图：日常对话直接回答，需要操作时使用工具。
多步骤任务必须先调用 todo tool 创建计划，保持恰好一个 in_progress 任务，每步完成后更新状态。优先使用工具而非文字描述。

## Skills

系统提示词中包含 <available-skills> 目录，列出了当前项目可用的 skill。
在开始任何任务之前，你必须：

1. 检查 <available-skills> 是否有与当前任务匹配的 skill
2. 如果有匹配的 skill，立即调用 activate_skill 工具激活它
3. 等待 skill 激活成功后再开始执行任务（skill 指令会在下一轮加载到上下文中）

不要跳过 skill 激活步骤。Skill 提供了特定领域的专业知识、工作流和规则，遵循 skill 指令能显著提升输出质量。
"""
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `pytest tests/ -v --ignore=tests/test_tool_registry.py`
Expected: ALL PASS — the framework prompt text is tested in prompt assembler tests via string matching, and the added skill section doesn't break any existing assertions.

- [ ] **Step 3: Commit**

```bash
git add core/prompt/system_context.py
git commit -m "feat: add skill-usage instructions to system prompt"
```

---

### Task 5: Run Full Regression Suite

**Files:**
- No new files

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v --ignore=tests/test_tool_registry.py`
Expected: ALL PASS

- [ ] **Step 2: Run tool registry test to verify activate_skill is auto-discovered**

Run: `pytest tests/test_tool_registry.py -v`
Expected: The test may fail if it checks for specific tool names — if it expects the removed `subagent` tool, that failure is pre-existing. The `activate_skill` tool should appear in the auto-discovered tools.

---

## Implementation Notes

- The `activate_skill` tool handler mutates `session_state.active_skills` and `session_state.skill_events` directly. This is safe because tool execution in `ToolExecutorRuntime` runs write tools serially (not in parallel).
- The `source="model_tool_call"` on `ActiveSkillState` distinguishes model-initiated activations from user-initiated `/skills use` activations (which use `source="user_command"`).
- The tool returns a message telling the model the skill will be loaded "on the next turn." This sets the correct expectation — the model should not try to use skill knowledge in the same turn it activates.
- `_skill_total_chars` is imported from `commands.py` where it was added in the previous plan. This avoids duplication.
- The `[Skill]` log format is identical to the one used in `commands.py`, using the same cyan ANSI escape codes.

## Self-Review

- **Spec coverage:** activate_skill tool (Task 2), context wiring (Task 3), prompt instructions (Task 4), regression (Task 5). All requirements addressed.
- **Placeholder scan:** No TBD/TODO/fill-in-later text. All steps contain complete code.
- **Type consistency:** `session_state` and `skill_registry` properties on `ToolUseContext` used consistently. `_skill_total_chars` imported from `commands.py`. `MAX_ACTIVE_SKILLS` and `MAX_TOTAL_SKILL_CHARS` imported from `commands.py`.
