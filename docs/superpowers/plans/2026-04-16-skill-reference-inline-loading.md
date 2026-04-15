# Skill Reference Inline Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inline reference file contents into the active skill system message at activation time and add `[Skill]` lifecycle logs to the Runtime output stream.

**Architecture:** `SkillRegistry.load()` reads all declared reference file bodies into a new `reference_bodies` dict on `SkillContent`. `PromptAssembler.build_active_skill_messages()` renders reference file contents inline instead of just the index. `/skills use` budget check includes reference chars. `[Skill]` logs are emitted via `sys.stdout.write` with cyan ANSI in `commands.py`.

**Tech Stack:** Python 3.12, pytest, dataclasses, pathlib, existing SessionEngine / PromptAssembler / SkillRegistry runtime

---

## File Structure

### Modified Files

- `core/skills/models.py`
  Responsibility: add `reference_bodies: dict[str, str]` field to `SkillContent`.
- `core/skills/registry.py`
  Responsibility: `load()` reads all reference file bodies after loading SKILL.md; populates `reference_bodies` on the returned `SkillContent`.
- `core/prompt/assembler.py`
  Responsibility: `_render_reference_files()` renders full file content from `reference_bodies` instead of just purpose lines.
- `core/session/commands.py`
  Responsibility: budget check includes reference chars in `/skills use`; `[Skill]` logs on use/off/reload.
- `tests/session/test_skills_registry.py`
  Responsibility: verify `load()` returns `reference_bodies`; verify failed reads are silently skipped; update existing test that asserts body-only.
- `tests/session/test_prompt_assembler.py`
  Responsibility: verify active skill message contains reference body content inline.
- `tests/session/test_engine_commands.py`
  Responsibility: verify budget rejection includes reference chars; verify e2e reference content reaches model view.

---

### Task 1: Add `reference_bodies` To SkillContent And Inline In Registry Load

**Files:**
- Modify: `core/skills/models.py`
- Modify: `core/skills/registry.py`
- Test: `tests/session/test_skills_registry.py`

- [ ] **Step 1: Write the failing registry tests**

Add these tests to `tests/session/test_skills_registry.py`. The test at line 249 (`test_load_still_returns_only_skill_body`) needs updating — it currently asserts that `style-system.md` is NOT in the body, which remains true, but we also need to assert that reference bodies ARE returned via the new field.

```python
# tests/session/test_skills_registry.py
# UPDATE the existing test_load_still_returns_only_skill_body (line 249):
def test_load_still_returns_only_skill_body(tmp_path: Path) -> None:
    """load() body must NOT contain reference content, but reference_bodies should."""
    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate HTML reports
references:
  - path: style-system.md
    purpose: CSS rules
---

Step 1
Step 2
""",
        encoding="utf-8",
    )
    (skill_dir / "style-system.md").write_text("css rules body", encoding="utf-8")

    registry = SkillRegistry()
    registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    content = registry.load("analysis-report")

    assert content.body == "Step 1\nStep 2"
    assert "style-system.md" not in content.body
    # NEW assertions:
    assert content.reference_bodies == {
        ".harness/skills/analysis-report/style-system.md": "css rules body",
    }


# ADD new test:
def test_load_skips_missing_reference_files(tmp_path: Path) -> None:
    """load() silently skips reference files that cannot be read."""
    skill_dir = tmp_path / ".harness" / "skills" / "fragile-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Fragile
description: Has a missing reference
references:
  - path: exists.md
    purpose: exists
  - path: missing.md
    purpose: gone
---

Body
""",
        encoding="utf-8",
    )
    (skill_dir / "exists.md").write_text("present content", encoding="utf-8")
    # missing.md is intentionally NOT created

    registry = SkillRegistry()
    registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    content = registry.load("fragile-skill")

    assert content.reference_bodies == {
        ".harness/skills/fragile-skill/exists.md": "present content",
    }


# ADD new test:
def test_load_returns_empty_reference_bodies_for_skill_without_refs(tmp_path: Path) -> None:
    """load() returns empty dict for skills with no references."""
    write_skill(
        tmp_path,
        "plain-skill",
        """---
name: Plain
description: No references
---

Plain body
""",
    )

    registry = SkillRegistry()
    registry.discover(tmp_path)
    content = registry.load("plain-skill")

    assert content.reference_bodies == {}
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: FAIL — `test_load_still_returns_only_skill_body` fails because `content.reference_bodies` does not exist. `test_load_skips_missing_reference_files` fails because the missing reference causes discover to reject the skill (since `_resolve_reference` checks `abs_path.is_file()`). `test_load_returns_empty_reference_bodies_for_skill_without_refs` fails because `SkillContent` has no `reference_bodies` attribute.

- [ ] **Step 3: Add `reference_bodies` to `SkillContent` dataclass**

```python
# core/skills/models.py
# UPDATE SkillContent (line 27):
@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str
    reference_bodies: dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 4: Update `SkillRegistry.load()` to read reference file bodies**

```python
# core/skills/registry.py
# UPDATE the load() method (line 149):
def load(self, skill_id: str) -> SkillContent:
    if skill_id in self._cache:
        return self._cache[skill_id]
    if skill_id not in self._catalog:
        raise ValueError(f"unknown skill: {skill_id!r}")
    meta = self._catalog[skill_id]
    _, body = _parse_skill_markdown(meta.skill_file)

    ref_bodies: dict[str, str] = {}
    for ref in meta.references:
        try:
            ref_bodies[ref.prompt_path] = ref.abs_path.read_text(encoding="utf-8")
        except Exception:
            pass

    content = SkillContent(
        meta=meta,
        body=body,
        content_digest=_digest_text(body),
        reference_bodies=ref_bodies,
    )
    self._cache[skill_id] = content
    return content
```

- [ ] **Step 5: Run the registry tests to verify they pass**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: ALL PASS — `test_load_still_returns_only_skill_body` passes with updated assertions, `test_load_skips_missing_reference_files` passes, `test_load_returns_empty_reference_bodies_for_skill_without_refs` passes, and all existing tests continue to pass.

- [ ] **Step 6: Commit**

```bash
git add core/skills/models.py core/skills/registry.py tests/session/test_skills_registry.py
git commit -m "feat: inline reference file bodies into SkillContent on load"
```

---

### Task 2: Render Reference Bodies Inline In Active Skill Prompt

**Files:**
- Modify: `core/prompt/assembler.py`
- Test: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing prompt assembler test**

Update `tests/session/test_prompt_assembler.py` — the existing `test_build_active_skill_messages_includes_reference_files` (line 115) currently asserts that `"CSS rules"` (the purpose text) appears in the output. It needs to also assert that the actual file content appears.

```python
# tests/session/test_prompt_assembler.py
# UPDATE test_build_active_skill_messages_includes_reference_files (line 115):
def test_build_active_skill_messages_includes_reference_files(tmp_path: Path) -> None:
    """Active skill message should inline reference file content."""
    from core.skills import ActiveSkillState

    skills_dir = tmp_path / ".harness" / "skills"
    skill_dir = skills_dir / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate HTML reports
references:
  - path: style-system.md
    purpose: CSS rules
---

Follow the main workflow.
""",
        encoding="utf-8",
    )
    (skill_dir / "style-system.md").write_text("body { color: red; }", encoding="utf-8")

    registry = SkillRegistry()
    catalog = registry.discover(skills_dir, working_dir=tmp_path)

    state = SessionState(
        conversation_messages=[],
        skill_catalog=catalog,
        active_skills={
            "analysis-report": ActiveSkillState(
                skill_id="analysis-report",
                activated_at_message_index=0,
                source="user_command",
                content_digest="digest",
            )
        },
        skill_events=[],
        skills_revision="rev-1",
    )
    assembler = PromptAssembler(skill_registry=registry)

    messages = assembler.build_active_skill_messages(state)

    content = messages[0]["content"]
    assert "<instruction>" in content
    assert "Follow the main workflow." in content
    assert "<reference-files>" in content
    assert ".harness/skills/analysis-report/style-system.md" in content
    # NEW: the actual file content must be inlined
    assert "body { color: red; }" in content


# ADD new test:
def test_build_active_skill_messages_without_references(tmp_path: Path) -> None:
    """Active skill with no references should not include <reference-files>."""
    from core.skills import ActiveSkillState

    skills_dir = tmp_path / ".harness" / "skills"
    skill_dir = skills_dir / "plain-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Plain
description: No references
---

Just instructions.
""",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    catalog = registry.discover(skills_dir, working_dir=tmp_path)

    state = SessionState(
        conversation_messages=[],
        skill_catalog=catalog,
        active_skills={
            "plain-skill": ActiveSkillState(
                skill_id="plain-skill",
                activated_at_message_index=0,
                source="user_command",
                content_digest="digest",
            )
        },
        skill_events=[],
        skills_revision="rev-1",
    )
    assembler = PromptAssembler(skill_registry=registry)

    messages = assembler.build_active_skill_messages(state)

    content = messages[0]["content"]
    assert "<instruction>" in content
    assert "Just instructions." in content
    assert "<reference-files>" not in content
```

- [ ] **Step 2: Run the prompt assembler tests to verify they fail**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: FAIL — `test_build_active_skill_messages_includes_reference_files` fails because `"body { color: red; }"` is not in the output (current implementation renders only the purpose text "CSS rules").

- [ ] **Step 3: Update `_render_reference_files()` to inline reference bodies**

```python
# core/prompt/assembler.py
# UPDATE _render_reference_files (line 35) to accept content and inline bodies:
def _render_reference_files(meta, reference_bodies: dict[str, str]) -> list[str]:
    if not meta.references:
        return []
    lines = ["    <reference-files>"]
    for ref in meta.references:
        lines.append(f'      <file path="{ref.prompt_path}">')
        body_text = reference_bodies.get(ref.prompt_path)
        if body_text:
            lines.append(body_text)
        elif ref.purpose:
            lines.append(f"        {ref.purpose}")
        lines.append("      </file>")
    lines.append("    </reference-files>")
    return lines
```

```python
# core/prompt/assembler.py
# UPDATE build_active_skill_messages (line 65) to pass reference_bodies:
def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
    if not state.active_skills:
        return []
    if self._skill_registry is None:
        return []
    lines = ["<active-skills>"]
    for skill_id in sorted(state.active_skills):
        try:
            content = self._skill_registry.load(skill_id)
        except (ValueError, KeyError):
            lines.append(f'  <!-- skill "{skill_id}" failed to load -->')
            continue
        lines.append(f'  <active-skill id="{skill_id}">')
        lines.append("    <instruction>")
        lines.append(content.body)
        lines.append("    </instruction>")
        lines.extend(_render_reference_files(content.meta, content.reference_bodies))
        lines.append("  </active-skill>")
    lines.append("</active-skills>")
    return [{"role": "system", "content": "\n".join(lines)}]
```

- [ ] **Step 4: Run the prompt assembler tests to verify they pass**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: ALL PASS — `test_build_active_skill_messages_includes_reference_files` now finds `"body { color: red; }"` in the output. `test_build_active_skill_messages_without_references` passes because `<reference-files>` is omitted when there are no references.

- [ ] **Step 5: Commit**

```bash
git add core/prompt/assembler.py tests/session/test_prompt_assembler.py
git commit -m "feat: inline reference file contents in active skill prompt"
```

---

### Task 3: Update Budget Check And Add `[Skill]` Lifecycle Logs

**Files:**
- Modify: `core/session/commands.py`
- Test: `tests/session/test_engine_commands.py`

- [ ] **Step 1: Write the failing engine command tests**

```python
# tests/session/test_engine_commands.py
# ADD new tests:

def test_use_rejects_when_reference_chars_exceed_budget(tmp_path: Path) -> None:
    """Budget check must include reference body chars."""
    # SKILL.md body = ~50 chars, reference file = 12000 chars
    big_ref = "y" * 12_000
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

    assert "cannot activate" in result.lower() or "exceed" in result.lower()
    assert "heavy-skill-1" not in engine.state.active_skills


def test_active_skill_inlines_reference_content_in_model_view(tmp_path: Path) -> None:
    """End-to-end: reference body content reaches model view."""
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

    view = MessageViewBuilder(prompt_assembler=engine._prompt_assembler).build(engine.state)

    skill_msg = next(
        msg for msg in view.messages
        if msg["role"] == "system" and "<active-skills>" in msg.get("content", "")
    )
    assert "<instruction>" in skill_msg["content"]
    assert "Follow the main workflow." in skill_msg["content"]
    assert "<reference-files>" in skill_msg["content"]
    assert "h1 { font-size: 2rem; }" in skill_msg["content"]
```

Also update the existing `test_active_skill_reference_index_reaches_model_view` (line 378) to assert that the actual file content (not just the purpose text) appears:

```python
# tests/session/test_engine_commands.py
# UPDATE test_active_skill_reference_index_reaches_model_view (line 378):
def test_active_skill_reference_index_reaches_model_view(tmp_path: Path) -> None:
    """End-to-end: reference content survives from bootstrap through to model view."""
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

    view = MessageViewBuilder(prompt_assembler=engine._prompt_assembler).build(engine.state)

    skill_msg = next(
        msg for msg in view.messages
        if msg["role"] == "system" and "<active-skills>" in msg.get("content", "")
    )
    assert "<instruction>" in skill_msg["content"]
    assert "<reference-files>" in skill_msg["content"]
    assert ".harness/skills/analysis-report/style-system.md" in skill_msg["content"]
    # The actual file content must be present:
    assert "css-variable-definitions" in skill_msg["content"]
```

- [ ] **Step 2: Run the engine command tests to verify they fail**

Run: `pytest tests/session/test_engine_commands.py -v`
Expected: FAIL — `test_use_rejects_when_reference_chars_exceed_budget` fails because budget check does not include reference chars (the second skill activates successfully). `test_active_skill_inlines_reference_content_in_model_view` may pass or fail depending on whether Task 2 has already been applied — if applied, it passes; the budget test is the critical failure.

- [ ] **Step 3: Update budget check in `/skills use` to include reference chars**

```python
# core/session/commands.py
# ADD helper function after MAX_TOTAL_SKILL_CHARS:
def _skill_total_chars(content) -> int:
    return len(content.body) + sum(len(v) for v in content.reference_bodies.values())
```

```python
# core/session/commands.py
# UPDATE the /skills use subcmd budget check (line 58):
# REPLACE:
#   total_chars = sum(
#       registry.load(sid).body.__len__()
#       for sid in state.active_skills
#   ) + len(content.body)
# WITH:
    total_chars = sum(
        _skill_total_chars(registry.load(sid))
        for sid in state.active_skills
    ) + _skill_total_chars(content)
```

- [ ] **Step 4: Add `[Skill]` lifecycle logs**

Add `import sys` at the top of `core/session/commands.py` if not already present, then add logs to each subcommand:

```python
# core/session/commands.py
# ADD at top of file (after existing imports):
import sys
```

```python
# core/session/commands.py
# UPDATE the /skills use success path (after line 82, before return):
# ADD before "return CommandResult(True, f"Activated skill: {skill_id}")":
        ref_count = len(content.reference_bodies)
        ref_chars = sum(len(v) for v in content.reference_bodies.values())
        sys.stdout.write(
            f"\033[36m[Skill] 激活 {skill_id}"
            f" ({ref_count} refs, {ref_chars:,} chars 内联)\033[0m\n"
        )
        return CommandResult(True, f"Activated skill: {skill_id}")
```

```python
# core/session/commands.py
# UPDATE the /skills off success path (after line 96, before return):
# ADD before "return CommandResult(True, f"Deactivated skill: {skill_id}")":
        sys.stdout.write(
            f"\033[36m[Skill] 停用 {skill_id}\033[0m\n"
        )
        return CommandResult(True, f"Deactivated skill: {skill_id}")
```

```python
# core/session/commands.py
# UPDATE the /skills reload success path (after line 117, before return):
# ADD before "return CommandResult(True, "Reloaded skills")":
        skill_count = len(state.skill_catalog)
        sys.stdout.write(
            f"\033[36m[Skill] 重新加载 skills 目录 ({skill_count} skills discovered)\033[0m\n"
        )
        return CommandResult(True, "Reloaded skills")
```

- [ ] **Step 5: Run the engine command tests to verify they pass**

Run: `pytest tests/session/test_engine_commands.py -v`
Expected: ALL PASS — `test_use_rejects_when_reference_chars_exceed_budget` now correctly rejects the second heavy skill. `test_active_skill_inlines_reference_content_in_model_view` passes. Updated `test_active_skill_reference_index_reaches_model_view` passes.

- [ ] **Step 6: Commit**

```bash
git add core/session/commands.py tests/session/test_engine_commands.py
git commit -m "feat: budget check includes reference chars; add [Skill] lifecycle logs"
```

---

### Task 4: Run Full Regression Suite

**Files:**
- No new files

- [ ] **Step 1: Run all skill-related tests**

Run: `pytest tests/session/test_skills_registry.py tests/session/test_prompt_assembler.py tests/session/test_engine_commands.py -v`
Expected: ALL PASS across all three test files.

- [ ] **Step 2: Run broader CLI regression suite**

Run: `pytest tests/test_agent_loop_cli.py -v`
Expected: PASS — no regressions from the model/registry/assembler/commands changes.

- [ ] **Step 3: Commit any test cleanup if needed**

Only commit if test adjustments were needed.

---

## Implementation Notes

- The `field(default_factory=dict)` default on `reference_bodies` ensures existing code that constructs `SkillContent` without the new field continues to work.
- Failed reference file reads are silently skipped via `try/except` in `load()`. A missing reference file does NOT prevent skill activation. This is intentional — the skill body should contain standalone instructions per the SKILL.md authoring constraint.
- The `/skills reload` command calls `discover()` which resets `self._cache`, so stale reference bodies are naturally cleared.
- `_render_reference_files()` falls back to the purpose text if a reference body is empty or missing, maintaining backward compatibility.

## Self-Review

- **Spec coverage**: Part 1 (inline loading) covered by Tasks 1-2. Part 2 (observability) covered by Task 3. Budget control covered by Task 3. Test coverage covered by all tasks.
- **Placeholder scan**: No TBD/TODO/fill-in-later text. All steps contain complete code.
- **Type consistency**: `reference_bodies: dict[str, str]` used consistently across `models.py`, `registry.py`, `assembler.py`, and `commands.py`. Key is always `prompt_path` (relative to working dir). `_skill_total_chars()` used consistently in budget check.
