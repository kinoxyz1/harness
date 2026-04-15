# Skills System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build v1 local inline skills support with standard `SKILL.md` discovery, explicit `/skills` commands, session-managed active skills, and prompt/view injection that reliably affects model input.

**Architecture:** Skills stay out of `QueryLoop` control flow. `SessionEngine` owns skill state and command handling, `PromptAssembler` builds the stable catalog and active-skill system message, and `MessageViewBuilder` injects active skills into the actual model-facing message list. REPL parses `/skills` commands before normal user queries enter the engine.

**Tech Stack:** Python 3.12, pytest, dataclasses, existing `SessionEngine` / `PromptAssembler` / `MessageViewBuilder` runtime

---

## File Structure

### New Files

- `core/skills/__init__.py`
  Responsibility: export skill dataclasses and registry helpers.
- `core/skills/models.py`
  Responsibility: `SkillMeta`, `SkillContent`, `ActiveSkillState`, `SkillEvent`.
- `core/skills/registry.py`
  Responsibility: scan `.harness/skills/*/SKILL.md`, parse standard frontmatter subset, compute `skills_revision`, load `SKILL.md` body.
- `core/session/commands.py`
  Responsibility: parse and execute `/skills list|show|use|off|reload` against `SessionState`.
- `tests/session/test_skills_registry.py`
  Responsibility: discovery, metadata parsing, digest, revision behavior.
- `tests/session/test_prompt_assembler.py`
  Responsibility: stable prompt catalog injection and active-skill system message generation.
- `tests/session/test_view_builder.py`
  Responsibility: active-skill message ordering in model-facing view.
- `tests/session/test_engine_commands.py`
  Responsibility: `SessionEngine.handle_command()` behavior and state changes.
- `tests/test_agent_loop_cli.py`
  Responsibility: REPL-level `/skills` routing behavior in `01_agent_loop.py`.

### Modified Files

- `core/session/state.py`
  Responsibility: replace `discovered_skills` with `skill_catalog`, `active_skills`, `skill_events`, `skills_revision`.
- `core/prompt/assembler.py`
  Responsibility: include skill catalog in stable prompt cache key and render active skill system message.
- `core/session/view_builder.py`
  Responsibility: inject active-skill system message between leading system messages and conversation history.
- `core/session/engine.py`
  Responsibility: discover skills during bootstrap and expose `handle_command(...)`.
- `01_agent_loop.py`
  Responsibility: detect `/skills` commands and route them to `SessionEngine` instead of `submit_user_message(...)`.

---

### Task 1: Add Skill Models And Registry

**Files:**
- Create: `core/skills/__init__.py`
- Create: `core/skills/models.py`
- Create: `core/skills/registry.py`
- Test: `tests/session/test_skills_registry.py`

- [ ] **Step 1: Write the failing registry tests**

```python
# tests/session/test_skills_registry.py
from __future__ import annotations

from pathlib import Path

from core.skills.registry import SkillRegistry, compute_skills_revision


def write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_discovers_standard_skill_metadata(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
when-to-use: Use when the user wants a finished report
---

Skill body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    meta = catalog["analysis-report"]
    assert meta.skill_id == "analysis-report"
    assert meta.name == "Analysis Report"
    assert meta.description == "Generate HTML reports"
    assert meta.when_to_use == "Use when the user wants a finished report"


def test_rejects_skill_without_description(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "broken-skill",
        """---
name: Broken
---

Skill body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    assert "broken-skill" not in catalog
    assert "broken-skill" in registry.errors


def test_load_returns_body_and_digest(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
---

Body line 1
Body line 2
""",
    )

    registry = SkillRegistry()
    registry.discover(tmp_path)
    content = registry.load("analysis-report")

    assert content.body == "Body line 1\nBody line 2"
    assert content.content_digest


def test_compute_revision_changes_when_mtime_changes(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
---

Body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)
    first = compute_skills_revision(catalog)
    skill_file = tmp_path / "analysis-report" / "SKILL.md"
    skill_file.touch()
    catalog = registry.discover(tmp_path)
    second = compute_skills_revision(catalog)

    assert first != second
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.skills'`

- [ ] **Step 3: Write the skill models**

```python
# core/skills/models.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_file: Path


@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str


@dataclass(slots=True)
class ActiveSkillState:
    skill_id: str
    activated_at_message_index: int
    source: str
    content_digest: str


@dataclass(slots=True)
class SkillEvent:
    skill_id: str
    action: str
    source: str
    conversation_index: int
```

- [ ] **Step 4: Write the registry implementation**

```python
# core/skills/registry.py
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from .models import SkillContent, SkillMeta


def _parse_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"SKILL.md missing frontmatter: {path}")
    end = text.find("---", 3)
    if end == -1:
        raise ValueError(f"SKILL.md frontmatter not closed: {path}")
    meta = yaml.safe_load(text[3:end]) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"frontmatter must be a mapping: {path}")
    body = text[end + 3 :].strip()
    return meta, body


def _digest_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def compute_skills_revision(catalog: dict[str, SkillMeta]) -> str:
    lines: list[str] = []
    for skill_id in sorted(catalog):
        meta = catalog[skill_id]
        mtime_ns = meta.skill_file.stat().st_mtime_ns
        lines.append(f"{skill_id}:{mtime_ns}")
    return _digest_text("\n".join(lines))


class SkillRegistry:
    def __init__(self) -> None:
        self._catalog: dict[str, SkillMeta] = {}
        self._cache: dict[str, SkillContent] = {}
        self.errors: dict[str, str] = {}

    def discover(self, skills_dir: Path) -> dict[str, SkillMeta]:
        self._catalog = {}
        self._cache = {}
        self.errors = {}

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
                name = str(meta_dict["name"])
                description = str(meta_dict["description"])
                when_to_use = meta_dict.get("when-to-use")
            except Exception as exc:
                self.errors[skill_dir.name] = str(exc)
                continue
            self._catalog[skill_dir.name] = SkillMeta(
                skill_id=skill_dir.name,
                name=name,
                description=description,
                when_to_use=str(when_to_use) if when_to_use else None,
                skill_dir=skill_dir,
                skill_file=skill_file,
            )
        return dict(self._catalog)

    def load(self, skill_id: str) -> SkillContent:
        if skill_id in self._cache:
            return self._cache[skill_id]
        meta = self._catalog[skill_id]
        _, body = _parse_skill_markdown(meta.skill_file)
        content = SkillContent(meta=meta, body=body, content_digest=_digest_text(body))
        self._cache[skill_id] = content
        return content
```

- [ ] **Step 5: Export the new skill API**

```python
# core/skills/__init__.py
from .models import ActiveSkillState, SkillContent, SkillEvent, SkillMeta
from .registry import SkillRegistry, compute_skills_revision

__all__ = [
    "ActiveSkillState",
    "SkillContent",
    "SkillEvent",
    "SkillMeta",
    "SkillRegistry",
    "compute_skills_revision",
]
```

- [ ] **Step 6: Run the registry tests to verify they pass**

Run: `pytest tests/session/test_skills_registry.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/skills/__init__.py core/skills/models.py core/skills/registry.py tests/session/test_skills_registry.py
git commit -m "feat: add local skill registry"
```

### Task 2: Extend Session State And Stable Prompt Catalog

**Files:**
- Modify: `core/session/state.py`
- Modify: `core/prompt/assembler.py`
- Test: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing prompt assembler tests**

```python
# tests/session/test_prompt_assembler.py
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
        active_skills={},
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
    assert "stable_system_prompt:rev-1" in state.prompt_cache
    assert "stable_system_prompt:rev-2" in state.prompt_cache
```

- [ ] **Step 2: Run the prompt assembler tests to verify they fail**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: FAIL with `TypeError` about unexpected `skill_catalog` or missing catalog in stable prompt

- [ ] **Step 3: Extend `SessionState`**

```python
# core/session/state.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.skills import ActiveSkillState, SkillEvent, SkillMeta


@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    active_skills: dict[str, ActiveSkillState] = field(default_factory=dict)
    skill_events: list[SkillEvent] = field(default_factory=list)
    skills_revision: str | None = None
    read_file_state: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 4: Render the available-skills catalog in `PromptAssembler.build_stable()`**

```python
# core/prompt/assembler.py
from __future__ import annotations

from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState


def _stable_cache_key(state: SessionState) -> str:
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}"


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


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None):
        self._cache = cache or PromptCache()

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        cache_key = _stable_cache_key(state)
        cached = self._cache.get(state.prompt_cache, cache_key)
        if cached is not None:
            return cached
        parts = [get_system_context(project_root=project_root)]
        catalog = _render_skill_catalog(state)
        if catalog:
            parts.append(catalog)
        stable_prompt = "\n\n".join(parts)
        return self._cache.set(state.prompt_cache, cache_key, stable_prompt)
```

- [ ] **Step 5: Run the prompt assembler tests to verify they pass**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/session/state.py core/prompt/assembler.py tests/session/test_prompt_assembler.py
git commit -m "feat: add skill catalog to session prompt state"
```

### Task 3: Inject Active Skill Messages Into The Model View

**Files:**
- Modify: `core/prompt/assembler.py`
- Modify: `core/session/view_builder.py`
- Test: `tests/session/test_view_builder.py`

- [ ] **Step 1: Write the failing view-builder tests**

```python
# tests/session/test_view_builder.py
from __future__ import annotations

from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.session.state import SessionState
from core.session.view_builder import MessageViewBuilder
from core.skills import ActiveSkillState, SkillMeta


def test_inserts_active_skill_system_message_after_leading_system_messages(tmp_path: Path) -> None:
    skill_file = tmp_path / "analysis-report" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("Skill body", encoding="utf-8")

    state = SessionState(
        conversation_messages=[
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "hello"},
        ],
        skill_catalog={
            "analysis-report": SkillMeta(
                skill_id="analysis-report",
                name="Analysis Report",
                description="Generate reports",
                when_to_use=None,
                skill_dir=skill_file.parent,
                skill_file=skill_file,
            )
        },
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

    view = MessageViewBuilder(prompt_assembler=PromptAssembler()).build(state)

    assert view.messages[0]["content"] == "stable prompt"
    assert view.messages[1]["role"] == "system"
    assert "<active-skills>" in view.messages[1]["content"]
    assert "Skill body" in view.messages[1]["content"]
    assert view.messages[2] == {"role": "user", "content": "hello"}
```

- [ ] **Step 2: Run the view-builder tests to verify they fail**

Run: `pytest tests/session/test_view_builder.py -v`
Expected: FAIL with `TypeError` for unexpected `prompt_assembler` or missing active-skills injection

- [ ] **Step 3: Add active-skill message rendering to `PromptAssembler`**

```python
# core/prompt/assembler.py
from core.skills import SkillRegistry


class PromptAssembler:
    def __init__(
        self,
        cache: PromptCache | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self._cache = cache or PromptCache()
        self._skill_registry = skill_registry or SkillRegistry()

    def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
        if not state.active_skills:
            return []
        lines = ["<active-skills>"]
        for skill_id in sorted(state.active_skills):
            content = self._skill_registry.load(skill_id)
            lines.append(f'  <active-skill id="{skill_id}">')
            lines.append(content.body)
            lines.append("  </active-skill>")
        lines.append("</active-skills>")
        return [{"role": "system", "content": "\n".join(lines)}]
```

- [ ] **Step 4: Update `MessageViewBuilder` to inject active skills**

```python
# core/session/view_builder.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.prompt.assembler import PromptAssembler

from .state import SessionState


@dataclass(slots=True)
class MessageView:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None


class MessageViewBuilder:
    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        prompt_assembler: PromptAssembler | None = None,
    ):
        self._tools = tools
        self._prompt_assembler = prompt_assembler or PromptAssembler()

    def build(self, state: SessionState) -> MessageView:
        messages = list(state.conversation_messages)
        system_prefix: list[dict[str, Any]] = []
        while messages and messages[0].get("role") == "system":
            system_prefix.append(messages.pop(0))
        active_skill_messages = self._prompt_assembler.build_active_skill_messages(state)
        return MessageView(
            messages=[*system_prefix, *active_skill_messages, *messages],
            tools=self._tools,
        )
```

- [ ] **Step 5: Run the view-builder tests to verify they pass**

Run: `pytest tests/session/test_view_builder.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/prompt/assembler.py core/session/view_builder.py tests/session/test_view_builder.py
git commit -m "feat: inject active skills into message view"
```

### Task 4: Add `/skills` Command Routing To SessionEngine And REPL

**Files:**
- Create: `core/session/commands.py`
- Modify: `core/session/engine.py`
- Modify: `01_agent_loop.py`
- Test: `tests/session/test_engine_commands.py`
- Test: `tests/test_agent_loop_cli.py`

- [ ] **Step 1: Write the failing command tests**

```python
# tests/session/test_engine_commands.py
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.session.engine import SessionEngine


class DummyQueryLoop:
    def run(self, **kwargs):
        return SimpleNamespace(final_output="ok")


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


def test_handle_command_use_activates_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate reports
---

Skill body
""",
        encoding="utf-8",
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills use analysis-report")

    assert "analysis-report" in engine.state.active_skills
    assert "activated" in result.lower()


def test_handle_command_off_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills off analysis-report")

    assert "not active" in result.lower()
```

```python
# tests/test_agent_loop_cli.py
from __future__ import annotations

from types import SimpleNamespace

import 01_agent_loop as agent_loop


def test_cli_routes_skills_command_without_calling_submit(monkeypatch) -> None:
    calls: list[str] = []

    class FakeEngine:
        def handle_command(self, raw: str) -> str:
            calls.append(f"cmd:{raw}")
            return "listed"

        def submit_user_message(self, text: str):
            calls.append(f"msg:{text}")
            return SimpleNamespace(final_output="reply")

    outputs: list[str] = []
    monkeypatch.setattr(agent_loop, "console", SimpleNamespace(print=outputs.append))

    result = agent_loop.handle_input("/skills list", FakeEngine())

    assert result is True
    assert calls == ["cmd:/skills list"]
```

- [ ] **Step 2: Run the command tests to verify they fail**

Run: `pytest tests/session/test_engine_commands.py tests/test_agent_loop_cli.py -v`
Expected: FAIL with missing `handle_command`, missing `bootstrap`, or missing CLI helper

- [ ] **Step 3: Add command parsing and execution helpers**

```python
# core/session/commands.py
from __future__ import annotations

from dataclasses import dataclass

from core.skills import ActiveSkillState, SkillEvent, SkillRegistry, compute_skills_revision


@dataclass(slots=True)
class CommandResult:
    handled: bool
    output: str = ""


def is_skills_command(raw: str) -> bool:
    return raw.strip().startswith("/skills")


def execute_skills_command(raw: str, *, state, registry: SkillRegistry) -> CommandResult:
    parts = raw.strip().split()
    if parts[:2] == ["/skills", "list"]:
        lines = [f"- {skill_id}: {meta.description}" for skill_id, meta in sorted(state.skill_catalog.items())]
        return CommandResult(True, "\n".join(lines) if lines else "(no skills)")
    if len(parts) == 3 and parts[:2] == ["/skills", "show"]:
        content = registry.load(parts[2])
        return CommandResult(True, content.body)
    if len(parts) == 3 and parts[:2] == ["/skills", "use"]:
        content = registry.load(parts[2])
        state.active_skills[parts[2]] = ActiveSkillState(
            skill_id=parts[2],
            activated_at_message_index=len(state.conversation_messages),
            source="user_command",
            content_digest=content.content_digest,
        )
        state.skill_events.append(
            SkillEvent(
                skill_id=parts[2],
                action="activated",
                source="user_command",
                conversation_index=len(state.conversation_messages),
            )
        )
        return CommandResult(True, f"Activated skill: {parts[2]}")
    if len(parts) == 3 and parts[:2] == ["/skills", "off"]:
        removed = state.active_skills.pop(parts[2], None)
        if removed is None:
            return CommandResult(True, f"Skill not active: {parts[2]}")
        state.skill_events.append(
            SkillEvent(
                skill_id=parts[2],
                action="deactivated",
                source="user_command",
                conversation_index=len(state.conversation_messages),
            )
        )
        return CommandResult(True, f"Deactivated skill: {parts[2]}")
    if parts[:2] == ["/skills", "reload"]:
        state.skill_catalog = registry.discover(registry.skills_dir)
        state.skills_revision = compute_skills_revision(state.skill_catalog)
        state.skill_events.append(
            SkillEvent(
                skill_id="*",
                action="reload",
                source="user_command",
                conversation_index=len(state.conversation_messages),
            )
        )
        return CommandResult(True, "Reloaded skills")
    return CommandResult(True, "Usage: /skills list|show <id>|use <id>|off <id>|reload")
```

- [ ] **Step 4: Wire command handling into `SessionEngine`**

```python
# core/session/engine.py
from pathlib import Path

from core.session.commands import execute_skills_command
from core.skills import SkillRegistry, compute_skills_revision


class SessionEngine:
    def __init__(..., skill_registry: SkillRegistry | None = None):
        ...
        self._skill_registry = skill_registry or SkillRegistry()

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        skills_dir = Path(self._tool_context.working_dir) / ".harness" / "skills"
        self._state.skill_catalog = self._skill_registry.discover(skills_dir)
        self._state.skills_revision = compute_skills_revision(self._state.skill_catalog)
        self._bootstrap_session_messages()
        self._bootstrapped = True

    def handle_command(self, raw: str) -> str:
        self.bootstrap()
        result = execute_skills_command(raw, state=self._state, registry=self._skill_registry)
        return result.output

    def submit_user_message(self, text: str):
        self.bootstrap()
        ...
```

- [ ] **Step 5: Add REPL command routing helper**

```python
# 01_agent_loop.py
def handle_input(raw: str, engine: SessionEngine) -> bool:
    text = raw.strip()
    if not text:
        return True
    if text.startswith("/skills"):
        output = engine.handle_command(text)
        if output:
            console.print(output)
        return True
    result = engine.submit_user_message(text)
    if result.final_output:
        console.print(result.final_output)
    return True


def main() -> None:
    ...
    while True:
        ...
        if query.strip().lower() in ("exit", "quit"):
            ...
        handle_input(query, engine)
        print()
```

- [ ] **Step 6: Run the command tests to verify they pass**

Run: `pytest tests/session/test_engine_commands.py tests/test_agent_loop_cli.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/session/commands.py core/session/engine.py 01_agent_loop.py tests/session/test_engine_commands.py tests/test_agent_loop_cli.py
git commit -m "feat: add session skills commands"
```

### Task 5: Verify End-To-End Skills Prompt Injection

**Files:**
- Modify: `tests/session/test_engine_commands.py`
- Modify: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Add an integration-style test that the model-facing view contains active skill body**

```python
# tests/session/test_engine_commands.py
from core.session.view_builder import MessageViewBuilder


def test_active_skill_body_reaches_model_view(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate reports
---

Use a fixed HTML structure.
""",
        encoding="utf-8",
    )

    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")
    engine.append_message({"role": "user", "content": "Generate a report"})

    view = MessageViewBuilder(prompt_assembler=engine._prompt_assembler).build(engine.state)

    assert any("Use a fixed HTML structure." in msg["content"] for msg in view.messages if msg["role"] == "system")
```

- [ ] **Step 2: Run the targeted integration tests**

Run: `pytest tests/session/test_engine_commands.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py -v`
Expected: PASS

- [ ] **Step 3: Run the broader regression set**

Run: `pytest tests/test_loop.py tests/test_protocol.py tests/test_tool_registry.py tests/session/test_skills_registry.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py tests/session/test_engine_commands.py tests/test_agent_loop_cli.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/session/test_engine_commands.py tests/session/test_prompt_assembler.py
git commit -m "test: verify skills prompt injection end to end"
```

## Self-Review

### Spec coverage

- Local standard skill discovery: Task 1
- Session state and `skills_revision`: Task 2
- Active skill message injection: Task 3
- Explicit `/skills` commands: Task 4
- Real model-view verification: Task 5

No spec requirement is left without a task in this v1 scope.

### Placeholder scan

- No `TODO` / `TBD`
- Each task contains exact files, commands, and concrete code
- No step says “similar to previous task”

### Type consistency

- Runtime identity uses `skill_id` consistently
- Session state uses `skill_catalog`, `active_skills`, `skill_events`, `skills_revision`
- Command surface consistently uses `/skills list|show|use|off|reload`

Plan complete and saved to `docs/superpowers/plans/2026-04-15-skills-system-implementation.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration

2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
