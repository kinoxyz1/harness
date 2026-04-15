# Skill Progressive Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add progressive loading for local skills so the model sees `SKILL.md` plus a reference-file index, then reads reference files on demand via the existing `read_file` tool.

**Architecture:** Keep the current split between discover, load, and prompt assembly. `SkillRegistry` will parse and validate reference metadata during discover without reading reference bodies, `SessionEngine`/`/skills reload` will preserve the working directory needed to compute model-facing paths, and `PromptAssembler` will render `<instruction>` plus `<reference-files>` into the active-skill system message.

**Tech Stack:** Python 3.12, pytest, dataclasses, pathlib, existing `SessionEngine` / `PromptAssembler` / `SkillRegistry` runtime

---

## File Structure

### Modified Files

- `core/skills/models.py`
  Responsibility: add `SkillReference` and extend `SkillMeta` with declared reference metadata.
- `core/skills/registry.py`
  Responsibility: parse `references` frontmatter, validate reference paths stay under `skill_dir`, compute both internal absolute paths and prompt-facing relative paths, and keep `load()` limited to `SKILL.md`.
- `core/skills/__init__.py`
  Responsibility: export `SkillReference`.
- `core/session/engine.py`
  Responsibility: pass `working_dir` into initial skill discovery during bootstrap.
- `core/session/commands.py`
  Responsibility: reuse the stored working directory on `/skills reload` so refreshed metadata computes the same prompt paths.
- `core/prompt/assembler.py`
  Responsibility: render `<instruction>` and `<reference-files>` for each active skill.
- `tests/session/test_skills_registry.py`
  Responsibility: verify reference parsing, path validation, `prompt_path` generation, and that `load()` still returns only `SKILL.md`.
- `tests/session/test_prompt_assembler.py`
  Responsibility: verify active-skill rendering includes reference index and relative prompt paths.
- `tests/session/test_view_builder.py`
  Responsibility: verify reference index survives injection ordering in the model-facing view.
- `tests/session/test_engine_commands.py`
  Responsibility: verify bootstrap/reload preserve reference metadata and end-to-end skill activation exposes `<reference-files>`.

---

### Task 1: Add Reference Metadata To Skill Models And Registry

**Files:**
- Modify: `core/skills/models.py`
- Modify: `core/skills/registry.py`
- Modify: `core/skills/__init__.py`
- Test: `tests/session/test_skills_registry.py`

- [ ] **Step 1: Write the failing registry tests**

```python
# tests/session/test_skills_registry.py
from __future__ import annotations

from pathlib import Path

import pytest

from core.skills.registry import SkillRegistry


def write_skill(
    root: Path,
    skill_id: str,
    body: str,
    *,
    extra_files: dict[str, str] | None = None,
) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    for rel_path, content in (extra_files or {}).items():
        file_path = skill_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def test_discovers_declared_skill_references(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
references:
  - path: style-system.md
    purpose: CSS rules
  - path: cards/catalog.md
    purpose: Card catalog
---

Main workflow instructions.
""",
        extra_files={
            "style-system.md": "body",
            "cards/catalog.md": "cards",
        },
    )

    registry = SkillRegistry()
    catalog = registry.discover(
        tmp_path / ".harness" / "skills",
        working_dir=tmp_path,
    )

    refs = catalog["analysis-report"].references
    assert [ref.path for ref in refs] == ["style-system.md", "cards/catalog.md"]
    assert [ref.prompt_path for ref in refs] == [
        ".harness/skills/analysis-report/style-system.md",
        ".harness/skills/analysis-report/cards/catalog.md",
    ]
    assert refs[0].abs_path == (
        tmp_path / ".harness" / "skills" / "analysis-report" / "style-system.md"
    ).resolve()


def test_rejects_reference_that_escapes_skill_dir(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "bad-skill",
        """---
name: Bad Skill
description: Broken references
references:
  - path: ../secrets.md
    purpose: should fail
---

Body
""",
        extra_files={"../secrets.md": "secret"},
    )

    registry = SkillRegistry()
    catalog = registry.discover(
        tmp_path / ".harness" / "skills",
        working_dir=tmp_path,
    )

    assert "bad-skill" not in catalog
    assert "bad-skill" in registry.errors


def test_load_still_returns_only_skill_body(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
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
        extra_files={"style-system.md": "css rules"},
    )

    registry = SkillRegistry()
    registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    content = registry.load("analysis-report")

    assert content.body == "Step 1\nStep 2"
    assert "style-system.md" not in content.body
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: FAIL with `AttributeError` for missing `references` / `prompt_path`, or `TypeError` because `discover()` does not accept `working_dir`.

- [ ] **Step 3: Extend the skill dataclasses and exports**

```python
# core/skills/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(slots=True)
class SkillReference:
    path: str
    purpose: str | None
    abs_path: Path
    prompt_path: str


@dataclass(slots=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_file: Path
    references: list[SkillReference] = field(default_factory=list)
```

```python
# core/skills/__init__.py
from .models import ActiveSkillState, SkillContent, SkillEvent, SkillMeta, SkillReference
from .registry import SkillRegistry, compute_skills_revision

__all__ = [
    "ActiveSkillState",
    "SkillContent",
    "SkillEvent",
    "SkillMeta",
    "SkillReference",
    "SkillRegistry",
    "compute_skills_revision",
]
```

- [ ] **Step 4: Implement reference parsing and validation in the registry**

```python
# core/skills/registry.py
from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from .models import SkillContent, SkillMeta, SkillReference


def _resolve_reference(
    *,
    skill_dir: Path,
    working_dir: Path,
    raw_path: str,
    purpose: str | None,
) -> SkillReference:
    abs_path = (skill_dir / raw_path).resolve()
    try:
        abs_path.relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"reference escapes skill dir: {raw_path}") from exc
    if not abs_path.is_file():
        raise ValueError(f"reference file missing: {raw_path}")
    prompt_path = os.path.relpath(abs_path, working_dir.resolve())
    return SkillReference(
        path=raw_path,
        purpose=purpose,
        abs_path=abs_path,
        prompt_path=prompt_path,
    )


def _parse_references(
    meta_dict: dict[str, Any],
    *,
    skill_dir: Path,
    working_dir: Path,
) -> list[SkillReference]:
    raw_refs = meta_dict.get("references") or []
    if not isinstance(raw_refs, list):
        raise ValueError("references must be a list")
    refs: list[SkillReference] = []
    for entry in raw_refs:
        if not isinstance(entry, dict):
            raise ValueError("each reference must be a mapping")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("reference path must be a non-empty string")
        purpose = entry.get("purpose")
        if purpose is not None and not isinstance(purpose, str):
            raise ValueError("reference purpose must be a string")
        refs.append(
            _resolve_reference(
                skill_dir=skill_dir,
                working_dir=working_dir,
                raw_path=raw_path,
                purpose=purpose,
            )
        )
    return refs


class SkillRegistry:
    def __init__(self) -> None:
        self._catalog: dict[str, SkillMeta] = {}
        self._cache: dict[str, SkillContent] = {}
        self.errors: dict[str, str] = {}
        self.skills_dir: Path | None = None
        self.working_dir: Path | None = None

    def discover(
        self,
        skills_dir: Path,
        *,
        working_dir: Path | None = None,
    ) -> dict[str, SkillMeta]:
        self._catalog = {}
        self._cache = {}
        self.errors = {}
        self.skills_dir = skills_dir
        self.working_dir = (working_dir or Path.cwd()).resolve()

        if not skills_dir.is_dir():
            return {}

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                self.errors[skill_dir.name] = "SKILL.md missing"
                continue
            try:
                meta_dict, _ = _parse_skill_markdown(skill_file)
                references = _parse_references(
                    meta_dict,
                    skill_dir=skill_dir,
                    working_dir=self.working_dir,
                )
                self._catalog[skill_dir.name] = SkillMeta(
                    skill_id=skill_dir.name,
                    name=str(meta_dict["name"]),
                    description=str(meta_dict["description"]),
                    when_to_use=str(meta_dict["when-to-use"])
                    if meta_dict.get("when-to-use")
                    else None,
                    skill_dir=skill_dir,
                    skill_file=skill_file,
                    references=references,
                )
            except Exception as exc:
                self.errors[skill_dir.name] = str(exc)
        return dict(self._catalog)
```

- [ ] **Step 5: Run the registry tests to verify they pass**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: PASS for the new reference parsing tests and the existing discovery/load tests.

- [ ] **Step 6: Commit the registry work**

```bash
git add core/skills/models.py core/skills/registry.py core/skills/__init__.py tests/session/test_skills_registry.py
git commit -m "feat: add declared skill references metadata"
```

---

### Task 2: Preserve Working Directory Through Bootstrap And Reload

**Files:**
- Modify: `core/session/engine.py`
- Modify: `core/session/commands.py`
- Test: `tests/session/test_engine_commands.py`

- [ ] **Step 1: Write the failing engine command tests**

```python
# tests/session/test_engine_commands.py
def write_skill(
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
    write_skill(
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
    write_skill(
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

    write_skill(
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

    refs = engine.state.skill_catalog["analysis-report"].references
    assert "reloaded" in result.lower()
    assert [ref.prompt_path for ref in refs] == [
        ".harness/skills/analysis-report/style-system.md",
        ".harness/skills/analysis-report/quality-checklist.md",
    ]
```

- [ ] **Step 2: Run the engine command tests to verify they fail**

Run: `pytest tests/session/test_engine_commands.py -v`
Expected: FAIL because `engine.bootstrap()` and `/skills reload` do not yet populate `references` / `prompt_path`.

- [ ] **Step 3: Pass `working_dir` into bootstrap discovery and preserve it on reload**

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
    self._bootstrap_session_messages()
    self._bootstrapped = True
```

```python
# core/session/commands.py
if subcmd == "reload":
    if registry.skills_dir is None:
        return CommandResult(True, "No skills directory configured")
    state.skill_catalog = registry.discover(
        registry.skills_dir,
        working_dir=registry.working_dir,
    )
    state.skills_revision = compute_skills_revision(state.skill_catalog)
    ...
```

- [ ] **Step 4: Run the engine command tests to verify they pass**

Run: `pytest tests/session/test_engine_commands.py -v`
Expected: PASS for the new bootstrap/reload reference tests and the existing `/skills` command tests.

- [ ] **Step 5: Commit the session wiring work**

```bash
git add core/session/engine.py core/session/commands.py tests/session/test_engine_commands.py
git commit -m "feat: preserve skill reference paths across bootstrap and reload"
```

---

### Task 3: Render Reference Index In The Active Skill System Message

**Files:**
- Modify: `core/prompt/assembler.py`
- Test: `tests/session/test_prompt_assembler.py`
- Test: `tests/session/test_view_builder.py`

- [ ] **Step 1: Write the failing prompt/view tests**

```python
# tests/session/test_prompt_assembler.py
from core.skills import SkillMeta, SkillReference


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
                references=[
                    SkillReference(
                        path="style-system.md",
                        purpose="CSS rules",
                        abs_path=skill_file.parent / "style-system.md",
                        prompt_path=".harness/skills/analysis-report/style-system.md",
                    )
                ],
            )
        },
        active_skills={},
        skill_events=[],
        skills_revision="rev-1",
    )


def test_build_active_skill_messages_includes_reference_files(tmp_path: Path) -> None:
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
    (skill_dir / "style-system.md").write_text("css", encoding="utf-8")

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
    assert '.harness/skills/analysis-report/style-system.md' in content
    assert "CSS rules" in content
```

```python
# tests/session/test_view_builder.py
def test_injected_active_skill_message_contains_reference_index(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".harness" / "skills"
    skill_dir = skills_dir / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate reports
references:
  - path: style-system.md
    purpose: CSS rules
---

Skill body content here.
""",
        encoding="utf-8",
    )
    (skill_dir / "style-system.md").write_text("css", encoding="utf-8")

    registry = SkillRegistry()
    catalog = registry.discover(skills_dir, working_dir=tmp_path)

    state = SessionState(
        conversation_messages=[
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "hello"},
        ],
        skill_catalog=catalog,
        active_skills={
            "analysis-report": ActiveSkillState(
                skill_id="analysis-report",
                activated_at_message_index=1,
                source="user_command",
                content_digest="digest",
            )
        },
        skill_events=[],
        skills_revision="rev-1",
    )
    assembler = PromptAssembler(skill_registry=registry)
    view = MessageViewBuilder(prompt_assembler=assembler).build(state)

    assert "<reference-files>" in view.messages[1]["content"]
    assert ".harness/skills/analysis-report/style-system.md" in view.messages[1]["content"]
```

- [ ] **Step 2: Run the prompt and view tests to verify they fail**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_view_builder.py -v`
Expected: FAIL because `SkillMeta` does not yet render `<instruction>` / `<reference-files>` into active skill messages.

- [ ] **Step 3: Render `<instruction>` and `<reference-files>` in the prompt assembler**

```python
# core/prompt/assembler.py
def _render_reference_files(meta) -> list[str]:
    if not meta.references:
        return []
    lines = ["    <reference-files>"]
    for ref in meta.references:
        lines.append(f'      <file path="{ref.prompt_path}">')
        if ref.purpose:
            lines.append(f"        {ref.purpose}")
        lines.append("      </file>")
    lines.append("    </reference-files>")
    return lines


def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
    if not state.active_skills or self._skill_registry is None:
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
        lines.extend(_render_reference_files(content.meta))
        lines.append("  </active-skill>")
    lines.append("</active-skills>")
    return [{"role": "system", "content": "\n".join(lines)}]
```

- [ ] **Step 4: Run the prompt and view tests to verify they pass**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_view_builder.py -v`
Expected: PASS, including the new assertions for `<instruction>` and `<reference-files>`.

- [ ] **Step 5: Commit the prompt rendering work**

```bash
git add core/prompt/assembler.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py
git commit -m "feat: render skill reference index in active prompt"
```

---

### Task 4: Add End-To-End Activation Coverage And Run Final Verification

**Files:**
- Modify: `tests/session/test_engine_commands.py`

- [ ] **Step 1: Write the failing end-to-end activation test**

```python
# tests/session/test_engine_commands.py
def test_active_skill_reference_index_reaches_model_view(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        "Analysis Report",
        "Generate reports",
        "Follow the main workflow.",
        references=[("style-system.md", "CSS rules")],
        extra_files={"style-system.md": "css"},
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
    assert "CSS rules" in skill_msg["content"]
```

- [ ] **Step 2: Run the end-to-end activation test to verify it fails**

Run: `pytest tests/session/test_engine_commands.py::test_active_skill_reference_index_reaches_model_view -v`
Expected: FAIL because the current model-facing message does not include `<reference-files>`.

- [ ] **Step 3: Update the existing helper/test data to support reference-aware skills**

```python
# tests/session/test_engine_commands.py
# Reuse the Task 2 helper shape everywhere in this file:
write_skill(
    tmp_path,
    "analysis-report",
    "Analysis Report",
    "Generate reports",
    "Use a fixed HTML structure for all reports.",
    references=[("style-system.md", "CSS rules")],
    extra_files={"style-system.md": "css"},
)
```

- [ ] **Step 4: Run the focused verification suite**

Run: `pytest tests/session/test_skills_registry.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py tests/session/test_engine_commands.py -v`
Expected: PASS with all new reference metadata, prompt rendering, and end-to-end activation coverage green.

- [ ] **Step 5: Run the broader session/CLI regression suite**

Run: `pytest tests/test_agent_loop_cli.py tests/session/test_skills_registry.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py tests/session/test_engine_commands.py -v`
Expected: PASS, confirming the progressive loading changes do not break `/skills` routing or session bootstrap behavior.

- [ ] **Step 6: Commit the integration coverage**

```bash
git add tests/session/test_engine_commands.py
git commit -m "test: cover skill progressive loading end to end"
```

---

## Implementation Notes

- Keep `SkillRegistry.load()` scoped to `SKILL.md` only. Do not read reference file bodies during activation.
- Keep `MAX_TOTAL_SKILL_CHARS` based on `SKILL.md` body only in this implementation. The spec explicitly defers long-term reference budgeting.
- Do not change `/skills show` to include references. It should continue returning only `SKILL.md` body.
- Prefer relative `prompt_path` values such as `.harness/skills/analysis-report/style-system.md`; never inject machine-specific absolute paths into the prompt.
- Reject invalid references early during discover so a broken skill never reaches the catalog.

## Self-Review

- Spec coverage: registry parsing, working-dir wiring, prompt rendering, and end-to-end activation all map directly to Tasks 1-4.
- Placeholder scan: no `TODO` / `TBD` / “implement later” text remains; each code-bearing step includes concrete snippets.
- Type consistency: plan uses `SkillReference`, `SkillMeta.references`, `SkillRegistry.discover(..., working_dir=...)`, and `prompt_path` consistently across all tasks.
