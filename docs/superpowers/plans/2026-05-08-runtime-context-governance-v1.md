# Runtime Context Governance V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current `ContextManager`-centric compaction flow with a V1 runtime governor that prevents prompt blowups by offloading large tool results, enforcing per-message budgets, protecting `read_file` working context, and applying layered context governance before model calls.

**Architecture:** V1 introduces four focused runtime modules around the existing session pipeline: `ToolResultOffloader` handles file persistence and frozen replacement decisions, `PreviewStrip` shrinks previously offloaded previews under pressure, `microcompact.py` owns transcript clearing for low-density ephemeral outputs, and `ContextGovernor` orchestrates waterline-based policy selection before `MessageViewBuilder` assembles the request. `compact_service.py` remains only for summary compaction and runtime restore payload generation.

**Tech Stack:** Python 3.11, pytest, dataclasses, pathlib, existing `SessionEngine` / `QueryLoop` / `PromptAssembler` session runtime

---

## File Structure

### Create

- `core/session/content_replacement.py` — frozen replacement state and transcript records for persisted tool results
- `core/session/offloader.py` — immediate tool-result persistence and per-message aggregate budget enforcement
- `core/session/preview_strip.py` — preview-only shrinking for `<persisted-output>` messages
- `core/session/read_working_set.py` — `read_file` protection and post-compact restore helpers
- `core/session/microcompact.py` — extracted time-based microcompact logic for compactable ephemeral outputs
- `core/session/governor.py` — waterline-based runtime governor and blocking gate
- `tests/session/test_content_replacement.py`
- `tests/session/test_offloader.py`
- `tests/session/test_preview_strip.py`
- `tests/session/test_read_working_set.py`
- `tests/session/test_microcompact.py`
- `tests/session/test_governor.py`

### Modify

- `core/session/state.py` — add `session_id` and typed replacement state storage
- `core/session/store.py` — provision session-scoped `.harness/sessions/<session_id>/tool-results/`
- `core/session/token_budget.py` — replace single summary threshold helper with waterline calculation helpers
- `core/session/compact_service.py` — keep only summary compaction and runtime restore support, delegate `read_file` restore to `read_working_set.py`
- `core/query/loop.py` — inject offloader/governor flow around tool execution and query preparation
- `core/session/engine.py` — default-wire governor and offloader instead of `ContextManager`
- `core/session/view_builder.py` — update module docs to the governor-based pipeline
- `core/session/__init__.py` — export governor/offloader/query-context symbols
- `tests/session/test_compact_service.py` — trim to summary/runtime-restore behavior only
- `tests/session/test_token_budget.py` — assert waterline math and calibrated token usage
- `tests/test_query_display.py` — replace fake context manager with fake governor/offloader wiring
- `tests/test_query_logging.py` — replace fake context manager with fake governor/offloader wiring
- `tests/test_todo_planning_integration.py` — assert default `SessionEngine` path still wires correctly

### Delete

- `core/session/context_manager.py`
- `tests/session/test_context_manager.py`

---

### Task 1: Session Primitives And Replacement State

**Files:**
- Create: `core/session/content_replacement.py`
- Modify: `core/session/state.py`
- Modify: `core/session/store.py`
- Test: `tests/session/test_content_replacement.py`

- [ ] **Step 1: Write the failing tests for replacement state and session tool-result directories**

```python
from pathlib import Path

from core.session.content_replacement import (
    ContentReplacementRecord,
    ContentReplacementState,
    clone_content_replacement_state,
    reconstruct_content_replacement_state,
)
from core.session.state import SessionState
from core.session.store import SessionStore


def test_clone_content_replacement_state_copies_seen_ids_and_replacements(tmp_path: Path) -> None:
    source = ContentReplacementState(
        seen_ids={"toolu_1"},
        replacements={"toolu_1": "<persisted-output>saved</persisted-output>"},
    )

    cloned = clone_content_replacement_state(source)

    assert cloned is not source
    assert cloned.seen_ids == {"toolu_1"}
    assert cloned.replacements == {"toolu_1": "<persisted-output>saved</persisted-output>"}


def test_reconstruct_content_replacement_state_freezes_seen_results() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_keep", "name": "find", "args": {"pattern": "x"}}]},
        {"role": "tool", "tool_call_id": "toolu_keep", "content": "small result"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_big", "name": "bash", "args": {"command": "cat log"}}]},
        {"role": "tool", "tool_call_id": "toolu_big", "content": "<persisted-output>saved</persisted-output>"},
    ]
    records = [
        ContentReplacementRecord(
            kind="tool-result",
            tool_use_id="toolu_big",
            replacement="<persisted-output>saved</persisted-output>",
        )
    ]

    restored = reconstruct_content_replacement_state(messages, records)

    assert restored.seen_ids == {"toolu_keep", "toolu_big"}
    assert restored.replacements == {"toolu_big": "<persisted-output>saved</persisted-output>"}


def test_session_store_creates_session_scoped_tool_result_dir(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[], session_id="sess1234")
    store = SessionStore(state, working_dir=tmp_path)

    assert store.tool_result_dir == tmp_path / ".harness" / "sessions" / "sess1234" / "tool-results"
    assert store.tool_result_dir.is_dir()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_content_replacement.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.content_replacement'` and `TypeError: SessionStore.__init__() got an unexpected keyword argument 'working_dir'`

- [ ] **Step 3: Implement `ContentReplacementState`, transcript records, and session-scoped store directories**

```python
# core/session/content_replacement.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ContentReplacementRecord:
    kind: Literal["tool-result"]
    tool_use_id: str
    replacement: str


def clone_content_replacement_state(source: ContentReplacementState) -> ContentReplacementState:
    return ContentReplacementState(
        seen_ids=set(source.seen_ids),
        replacements=dict(source.replacements),
    )


def reconstruct_content_replacement_state(
    messages: list[dict[str, object]],
    records: list[ContentReplacementRecord],
) -> ContentReplacementState:
    state = ContentReplacementState()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id"):
                state.seen_ids.add(str(tool_call["id"]))
    for record in records:
        state.seen_ids.add(record.tool_use_id)
        state.replacements[record.tool_use_id] = record.replacement
    return state
```

```python
# core/session/state.py
from uuid import uuid4

from .content_replacement import ContentReplacementState


@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    session_id: str = field(default_factory=lambda: uuid4().hex[:16])
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    skill_events: list[SkillEvent] = field(default_factory=list)
    invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
    skills_revision: str | None = None
    read_file_state: dict[str, Any] = field(default_factory=dict)
    system_prompt_override: str | None = None
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
    todo_state: TodoState = field(default_factory=TodoState)
    compact_state: dict[str, Any] = field(default_factory=_default_compact_state)
    content_replacement_state: ContentReplacementState = field(
        default_factory=ContentReplacementState
    )
```

```python
# core/session/store.py
from pathlib import Path


class SessionStore:
    def __init__(self, state: SessionState, *, working_dir: str | Path = "."):
        self._state = state
        self._working_dir = Path(working_dir)
        self._tool_result_dir = (
            self._working_dir
            / ".harness"
            / "sessions"
            / state.session_id
            / "tool-results"
        )
        self._tool_result_dir.mkdir(parents=True, exist_ok=True)

    @property
    def tool_result_dir(self) -> Path:
        return self._tool_result_dir
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_content_replacement.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/content_replacement.py core/session/state.py core/session/store.py tests/session/test_content_replacement.py
git commit -m "feat: add session replacement state primitives"
```

### Task 2: ToolResultOffloader And Per-Message Aggregate Budget

**Files:**
- Create: `core/session/offloader.py`
- Modify: `core/session/store.py`
- Test: `tests/session/test_offloader.py`

- [ ] **Step 1: Write the failing tests for persistence and aggregate budget enforcement**

```python
from pathlib import Path

from core.session.content_replacement import ContentReplacementState
from core.session.offloader import ToolResultOffloader


def test_maybe_persist_writes_large_tool_results_and_freezes_replacement(tmp_path: Path) -> None:
    state = ContentReplacementState()
    offloader = ToolResultOffloader(tool_result_dir=tmp_path, replacement_state=state)

    replacement = offloader.maybe_persist(
        "toolu_big",
        "header\n" + ("x" * 6000),
        tool_name="web_fetch",
    )

    saved = tmp_path / "toolu_big.txt"
    assert saved.exists()
    assert state.seen_ids == {"toolu_big"}
    assert state.replacements["toolu_big"] == replacement
    assert "<persisted-output>" in replacement
    assert str(saved) in replacement


def test_enforce_per_message_budget_reapplies_frozen_replacements_and_skips_read_file(tmp_path: Path) -> None:
    state = ContentReplacementState(
        seen_ids={"toolu_old"},
        replacements={"toolu_old": "<persisted-output>old</persisted-output>"},
    )
    offloader = ToolResultOffloader(tool_result_dir=tmp_path, replacement_state=state)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_old", "name": "bash", "args": {"command": "cat log"}},
                {"id": "toolu_read", "name": "read_file", "args": {"path": "alpha.py"}},
                {"id": "toolu_new", "name": "web_fetch", "args": {"url": "https://example.com"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_old", "content": "small"},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "r" * 120000},
        {"role": "tool", "tool_call_id": "toolu_new", "content": "n" * 120000},
    ]

    rewritten = offloader.enforce_per_message_budget(messages)

    assert rewritten[1]["content"] == "<persisted-output>old</persisted-output>"
    assert rewritten[2]["content"] == "r" * 120000
    assert "<persisted-output>" in rewritten[3]["content"]
    assert "toolu_new" in state.seen_ids
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_offloader.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.offloader'`

- [ ] **Step 3: Implement immediate persistence and aggregate budget enforcement**

```python
# core/session/offloader.py
from __future__ import annotations

from pathlib import Path

from .content_replacement import ContentReplacementState

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000
PREVIEW_SIZE_BYTES = 2_000
STRUCTURED_TOOLS = {"todo", "skill"}
WORKING_CONTEXT_TOOLS = {"read_file"}


class ToolResultOffloader:
    def __init__(self, *, tool_result_dir: Path, replacement_state: ContentReplacementState) -> None:
        self._tool_result_dir = tool_result_dir
        self._state = replacement_state

    def maybe_persist(self, tool_use_id: str, content: str, *, tool_name: str) -> str:
        threshold = self._get_persistence_threshold(tool_name)
        if len(content) <= threshold:
            return content
        filepath = self._tool_result_dir / f"{tool_use_id}.txt"
        filepath.write_text(content, encoding="utf-8")
        preview = self._truncate_preview(content)
        replacement = (
            f"{PERSISTED_OUTPUT_TAG}\n"
            f"Output too large ({len(content)} chars). Full output saved to: {filepath}\n\n"
            f"Preview (first {len(preview)} bytes):\n"
            f"{preview}\n"
            f"{PERSISTED_OUTPUT_CLOSING_TAG}"
        )
        self._state.seen_ids.add(tool_use_id)
        self._state.replacements[tool_use_id] = replacement
        return replacement

    def enforce_per_message_budget(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        tool_name_by_id = self._build_tool_name_map(messages)
        total_chars = 0
        rewritten: list[dict[str, object]] = []
        candidates: list[tuple[int, str, str]] = []

        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                rewritten.append(dict(message))
                continue
            message_copy = dict(message)
            tool_use_id = str(message_copy.get("tool_call_id", ""))
            tool_name = tool_name_by_id.get(tool_use_id, "")
            frozen = self._state.replacements.get(tool_use_id)
            if frozen is not None:
                message_copy["content"] = frozen
            content = str(message_copy.get("content", ""))
            if tool_name not in WORKING_CONTEXT_TOOLS | STRUCTURED_TOOLS:
                total_chars += len(content)
                candidates.append((idx, tool_use_id, tool_name))
            rewritten.append(message_copy)

        if total_chars <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
            return rewritten

        for idx, tool_use_id, tool_name in sorted(
            candidates,
            key=lambda item: len(str(rewritten[item[0]].get("content", ""))),
            reverse=True,
        ):
            if total_chars <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
                break
            content = str(rewritten[idx]["content"])
            replacement = self._state.replacements.get(tool_use_id) or self.maybe_persist(
                tool_use_id,
                content,
                tool_name=tool_name,
            )
            total_chars -= len(content)
            total_chars += len(replacement)
            rewritten[idx]["content"] = replacement
        return rewritten

    def _get_persistence_threshold(self, tool_name: str) -> int:
        if tool_name in WORKING_CONTEXT_TOOLS | STRUCTURED_TOOLS:
            return 10**18
        if tool_name == "bash":
            return min(30_000, DEFAULT_MAX_RESULT_SIZE_CHARS)
        return DEFAULT_MAX_RESULT_SIZE_CHARS

    def _truncate_preview(self, content: str) -> str:
        preview = content[:PREVIEW_SIZE_BYTES]
        last_newline = preview.rfind("\n")
        if last_newline > PREVIEW_SIZE_BYTES * 0.5:
            preview = preview[:last_newline]
        return preview

    def _build_tool_name_map(self, messages: list[dict[str, object]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for tool_call in message.get("tool_calls") or []:
                if isinstance(tool_call, dict) and tool_call.get("id") and tool_call.get("name"):
                    result[str(tool_call["id"])] = str(tool_call["name"])
        return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_offloader.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/offloader.py tests/session/test_offloader.py
git commit -m "feat: add tool result offloader"
```

### Task 3: PreviewStrip For Already-Offloaded Results

**Files:**
- Create: `core/session/preview_strip.py`
- Test: `tests/session/test_preview_strip.py`

- [ ] **Step 1: Write the failing tests for preview shrinking**

```python
from core.session.preview_strip import strip_persisted_output_previews


def test_strip_persisted_output_previews_rewrites_only_persisted_outputs() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "toolu_1", "content": "<persisted-output>\nOutput too large. Full output saved to: /tmp/toolu_1.txt\n\nPreview (first 20 bytes):\nabcdef\n</persisted-output>"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "keep me"},
    ]
    replacements = {"toolu_1": messages[0]["content"]}

    rewritten, changed = strip_persisted_output_previews(messages, replacements)

    assert changed is True
    assert rewritten[0]["content"] == "[Tool result offloaded to: /tmp/toolu_1.txt]"
    assert replacements["toolu_1"] == "[Tool result offloaded to: /tmp/toolu_1.txt]"
    assert rewritten[1]["content"] == "keep me"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_preview_strip.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.preview_strip'`

- [ ] **Step 3: Implement preview-only shrinking**

```python
# core/session/preview_strip.py
from __future__ import annotations

PERSISTED_OUTPUT_PREFIX = "Full output saved to: "


def strip_persisted_output_previews(
    messages: list[dict[str, object]],
    replacements: dict[str, str],
) -> tuple[list[dict[str, object]], bool]:
    changed = False
    rewritten: list[dict[str, object]] = []
    for message in messages:
        message_copy = dict(message)
        content = message_copy.get("content")
        if not isinstance(content, str) or "<persisted-output>" not in content:
            rewritten.append(message_copy)
            continue
        filepath = _extract_filepath(content)
        collapsed = f"[Tool result offloaded to: {filepath}]"
        message_copy["content"] = collapsed
        tool_use_id = message_copy.get("tool_call_id")
        if isinstance(tool_use_id, str):
            replacements[tool_use_id] = collapsed
        rewritten.append(message_copy)
        changed = True
    return rewritten, changed


def _extract_filepath(content: str) -> str:
    for line in content.splitlines():
        if "Full output saved to:" in line:
            return line.split("Full output saved to:", 1)[1].strip()
    raise ValueError("persisted-output message missing saved path")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_preview_strip.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/preview_strip.py tests/session/test_preview_strip.py
git commit -m "feat: add preview strip strategy"
```

### Task 4: Read Working Set Protection And Runtime Restore

**Files:**
- Create: `core/session/read_working_set.py`
- Modify: `core/session/compact_service.py`
- Test: `tests/session/test_read_working_set.py`
- Test: `tests/session/test_compact_service.py`

- [ ] **Step 1: Write the failing tests for `read_file` protection and restore deduplication**

```python
from core.session.compact_service import build_runtime_restore_messages
from core.session.read_working_set import (
    collect_recent_read_restore_messages,
    read_tool_ids_to_protect,
)
from core.session.state import SessionState
from core.tools.context import FileState


def test_read_tool_ids_to_protect_tracks_recent_read_results() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_read_old", "name": "read_file", "args": {"path": "a.py"}}]},
        {"role": "tool", "tool_call_id": "toolu_read_old", "content": "alpha"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_bash", "name": "bash", "args": {"command": "pwd"}}]},
        {"role": "tool", "tool_call_id": "toolu_bash", "content": "pwd"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_read_new", "name": "read_file", "args": {"path": "b.py"}}]},
        {"role": "tool", "tool_call_id": "toolu_read_new", "content": "beta"},
    ]

    protected = read_tool_ids_to_protect(messages, keep_last_reads=2)

    assert protected == {"toolu_read_old", "toolu_read_new"}


def test_collect_recent_read_restore_messages_skips_files_already_kept_in_tail() -> None:
    state = SessionState(conversation_messages=[])
    state.read_file_state = {
        "/tmp/a.py": FileState(content="alpha", timestamp=10.0),
        "/tmp/b.py": FileState(content="beta", timestamp=20.0),
    }
    kept = [{"role": "tool", "tool_call_id": "toolu_read_b", "content": "beta"}]

    restored = collect_recent_read_restore_messages(state, kept_messages=kept, limit=2)

    assert len(restored) == 1
    assert restored[0]["kind"] == "file_runtime"
    assert "/tmp/a.py" in restored[0]["content"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_read_working_set.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.read_working_set'`

- [ ] **Step 3: Implement recent read tracking and runtime restore helpers**

```python
# core/session/read_working_set.py
from __future__ import annotations


def read_tool_ids_to_protect(
    messages: list[dict[str, object]],
    *,
    keep_last_reads: int,
) -> set[str]:
    read_ids: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("name") == "read_file":
                read_ids.append(str(tool_call["id"]))
    if keep_last_reads <= 0:
        return set()
    return set(read_ids[-keep_last_reads:])


def collect_recent_read_restore_messages(
    state,
    *,
    kept_messages: list[dict[str, object]],
    limit: int,
) -> list[dict[str, str]]:
    kept_contents = {
        str(message.get("content", ""))
        for message in kept_messages
        if message.get("role") == "tool"
    }
    restored: list[dict[str, str]] = []
    for path, file_state in sorted(
        state.read_file_state.items(),
        key=lambda item: getattr(item[1], "timestamp", 0.0),
        reverse=True,
    ):
        excerpt = getattr(file_state, "content", "")[:200]
        if excerpt in kept_contents:
            continue
        restored.append(
            {
                "role": "meta_runtime_restore",
                "kind": "file_runtime",
                "content": f"path={path};full_read={str(getattr(file_state, 'is_full_read', True)).lower()}\n{excerpt}",
            }
        )
        if len(restored) >= limit:
            break
    return restored
```

```python
# core/session/compact_service.py
from .read_working_set import collect_recent_read_restore_messages


def build_runtime_restore_messages(state: SessionState, *, kept_messages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    restored: list[dict[str, Any]] = []
    kept_messages = kept_messages or []
    # existing todo_restore and skills_restore blocks stay unchanged
    restored.extend(
        collect_recent_read_restore_messages(
            state,
            kept_messages=kept_messages,
            limit=3,
        )
    )
    return restored


def summarize_and_compact(
    messages: list[dict[str, Any]],
    *,
    state: SessionState,
    summary_gateway: Any,
    keep_last_messages: int,
) -> list[dict[str, Any]]:
    base_messages = _strip_trailing_runtime_restore(messages)
    keep_from_index = max(0, len(base_messages) - keep_last_messages)
    keep_from_index = _align_keep_start_to_complete_tool_batch(
        base_messages,
        keep_from_index,
    )
    summary_response = summary_gateway.call_once(
        base_messages[:keep_from_index],
        system=SUMMARY_SYSTEM_PROMPT,
        tools=None,
        request_options=ModelRequestOptions(
            query_source="compact",
            max_output_tokens=1200,
            thinking_mode="disabled",
        ),
    )
    boundary = create_compact_boundary(
        reason="summary_compact",
        summarized_messages=keep_from_index,
    )
    summary = create_compact_summary(summary_response.content.strip())
    kept = base_messages[keep_from_index:]
    runtime_restore = build_runtime_restore_messages(state, kept_messages=kept)
    return build_post_compact_messages(
        boundary=boundary,
        summary=summary,
        kept=kept,
        runtime_restore=runtime_restore,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_read_working_set.py tests/session/test_compact_service.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/read_working_set.py core/session/compact_service.py tests/session/test_read_working_set.py tests/session/test_compact_service.py
git commit -m "feat: protect read working set during compaction"
```

### Task 5: Extract Microcompact Into Its Own Module

**Files:**
- Create: `core/session/microcompact.py`
- Modify: `core/session/compact_service.py`
- Test: `tests/session/test_microcompact.py`
- Test: `tests/session/test_compact_service.py`

- [ ] **Step 1: Write the failing tests for compactable tool filtering**

```python
from core.session.microcompact import MICROCOMPACT_PLACEHOLDER, apply_time_based_microcompact


def test_apply_time_based_microcompact_skips_read_file_and_only_clears_old_ephemeral_outputs() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_read", "name": "read_file", "args": {"path": "a.py"}}], "_meta": {"created_at": 10.0}},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "alpha", "_meta": {"created_at": 20.0}},
        {"role": "assistant", "tool_calls": [{"id": "toolu_find", "name": "find", "args": {"pattern": "needle"}}], "_meta": {"created_at": 30.0}},
        {"role": "tool", "tool_call_id": "toolu_find", "content": "old matches", "_meta": {"created_at": 40.0}},
        {"role": "assistant", "tool_calls": [{"id": "toolu_web", "name": "web_fetch", "args": {"url": "https://example.com"}}], "_meta": {"created_at": 200.0}},
        {"role": "tool", "tool_call_id": "toolu_web", "content": "fresh html", "_meta": {"created_at": 220.0}},
    ]

    compacted = apply_time_based_microcompact(
        messages,
        age_cutoff_seconds=100,
        keep_recent_trajectories=1,
    )

    assert compacted[1]["content"] == "alpha"
    assert compacted[3]["content"] == MICROCOMPACT_PLACEHOLDER
    assert compacted[5]["content"] == "fresh html"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_microcompact.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.microcompact'`

- [ ] **Step 3: Move the current time-based logic into `microcompact.py` and narrow the whitelist**

```python
# core/session/microcompact.py
from __future__ import annotations

MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"
COMPACTABLE_TOOLS = {"bash", "find", "grep", "glob", "web_fetch", "web_search", "write_file"}


def apply_time_based_microcompact(
    messages: list[dict[str, object]],
    *,
    age_cutoff_seconds: float,
    keep_recent_trajectories: int,
) -> list[dict[str, object]]:
    newest_timestamp = max(
        (
            message["_meta"]["created_at"]
            for message in messages
            if isinstance(message.get("_meta"), dict) and isinstance(message["_meta"].get("created_at"), (int, float))
        ),
        default=None,
    )
    if newest_timestamp is None:
        return [dict(message) for message in messages]

    tool_name_by_id = _build_tool_name_map(messages)
    compactable_ids = [tool_id for tool_id, tool_name in tool_name_by_id.items() if tool_name in COMPACTABLE_TOOLS]
    keep_ids = set(compactable_ids[-keep_recent_trajectories:]) if keep_recent_trajectories > 0 else set()

    compacted: list[dict[str, object]] = []
    for message in messages:
        message_copy = dict(message)
        if message_copy.get("role") != "tool":
            compacted.append(message_copy)
            continue
        tool_use_id = str(message_copy.get("tool_call_id", ""))
        created_at = _message_created_at(message_copy)
        if (
            tool_use_id in compactable_ids
            and tool_use_id not in keep_ids
            and created_at is not None
            and newest_timestamp - created_at >= age_cutoff_seconds
        ):
            message_copy["content"] = MICROCOMPACT_PLACEHOLDER
        compacted.append(message_copy)
    return compacted


def _build_tool_name_map(messages: list[dict[str, object]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id") and tool_call.get("name"):
                mapping[str(tool_call["id"])] = str(tool_call["name"])
    return mapping


def _message_created_at(message: dict[str, object]) -> float | None:
    meta = message.get("_meta")
    if not isinstance(meta, dict):
        return None
    created_at = meta.get("created_at")
    return created_at if isinstance(created_at, (int, float)) else None
```

```python
# core/session/compact_service.py
from .microcompact import MICROCOMPACT_PLACEHOLDER

# remove apply_time_based_microcompact and COMPACTABLE_TOOLS from this file
# keep summarize_and_compact and build_runtime_restore_messages only
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_microcompact.py tests/session/test_compact_service.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/microcompact.py core/session/compact_service.py tests/session/test_microcompact.py tests/session/test_compact_service.py
git commit -m "refactor: extract microcompact module"
```

### Task 6: ContextGovernor And Waterline Math

**Files:**
- Create: `core/session/governor.py`
- Modify: `core/session/token_budget.py`
- Modify: `core/session/__init__.py`
- Test: `tests/session/test_governor.py`
- Test: `tests/session/test_token_budget.py`

- [ ] **Step 1: Write the failing tests for waterlines, preview strip, blocking gate, and summary breaker**

```python
from types import SimpleNamespace

from core.query.state import RunState
from core.session.governor import ContextGovernor
from core.session.offloader import ToolResultOffloader
from core.session.query_context import ContextBlock
from core.session.state import SessionState
from core.session.store import SessionStore


def test_context_governor_runs_budget_preview_microcompact_then_summary(tmp_path) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "assistant", "tool_calls": [{"id": "toolu_big", "name": "web_fetch", "args": {"url": "https://example.com"}}]},
            {"role": "tool", "tool_call_id": "toolu_big", "content": "<persisted-output>\nOutput too large. Full output saved to: /tmp/toolu_big.txt\n\nPreview (first 20 bytes):\nabcdef\n</persisted-output>"},
            {"role": "user", "content": "x" * 70000},
        ]
    )
    store = SessionStore(state, working_dir=tmp_path)
    offloader = ToolResultOffloader(
        tool_result_dir=store.tool_result_dir,
        replacement_state=state.content_replacement_state,
    )
    governor = ContextGovernor(
        offloader=offloader,
        compact_service=SimpleNamespace(summarize_and_compact=lambda messages, **_: [{"role": "assistant", "content": "summary"}]),
        summary_gateway=object(),
        context_window_tokens=90000,
        max_output_tokens=10000,
    )

    prepared = governor.assess(
        session_state=state,
        run_state=RunState(),
        store=store,
        stable_system="stable",
        stable_tools=None,
        runtime_blocks=[ContextBlock(kind="environment", content="env", required=True, token_estimate=10)],
        overlay_blocks=[],
        query_source="main_loop",
    )

    assert prepared.observability["strategies_run"] == ["preview_strip", "microcompact", "auto_compact"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_governor.py tests/session/test_token_budget.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.governor'`

- [ ] **Step 3: Implement waterline helpers and `ContextGovernor`**

```python
# core/session/token_budget.py
def calc_effective_context_window(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
) -> int:
    return context_window_tokens - max_output_tokens


def calc_waterlines(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
) -> dict[str, int]:
    effective = calc_effective_context_window(
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
    )
    return {
        "preview_strip": effective - 40_000,
        "microcompact": effective - 20_000,
        "auto_compact": effective - 13_000,
        "blocking": effective - 3_000,
    }
```

```python
# core/session/governor.py
from __future__ import annotations

import time
from typing import Any

from .microcompact import apply_time_based_microcompact
from .preview_strip import strip_persisted_output_previews
from .query_context import ContextBlock, PreparedQueryContext
from .token_budget import calc_effective_context_window, calc_waterlines, calibrated_input_tokens, estimate_messages_tokens


class ContextGovernor:
    def __init__(
        self,
        *,
        offloader,
        compact_service,
        summary_gateway,
        context_window_tokens: int = 100_000,
        max_output_tokens: int = 10_000,
        summary_breaker_cooldown_seconds: float = 60.0,
        time_fn=time.monotonic,
    ) -> None:
        self._offloader = offloader
        self._compact_service = compact_service
        self._summary_gateway = summary_gateway
        self._context_window_tokens = context_window_tokens
        self._max_output_tokens = max_output_tokens
        self._summary_breaker_cooldown_seconds = summary_breaker_cooldown_seconds
        self._time_fn = time_fn

    def assess(
        self,
        *,
        session_state,
        run_state,
        store,
        stable_system: str,
        stable_tools: list[dict[str, Any]] | None,
        runtime_blocks: list[ContextBlock],
        overlay_blocks: list[ContextBlock],
        query_source: str | None = None,
    ) -> PreparedQueryContext:
        messages = list(session_state.conversation_messages)
        waterlines = calc_waterlines(
            context_window_tokens=self._context_window_tokens,
            max_output_tokens=self._max_output_tokens,
        )
        water_level = self._calc_water_level(session_state, stable_system, stable_tools, runtime_blocks)
        messages = self._offloader.enforce_per_message_budget(messages)
        strategies_run: list[str] = []
        if water_level >= waterlines["preview_strip"]:
            messages, changed = strip_persisted_output_previews(
                messages,
                session_state.content_replacement_state.replacements,
            )
            if changed:
                strategies_run.append("preview_strip")
        messages = apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )
        strategies_run.append("microcompact")
        if water_level >= waterlines["auto_compact"]:
            messages = self._summarize_with_breaker(
                messages=messages,
                session_state=session_state,
                keep_last_messages=4,
            )
            strategies_run.append("auto_compact")
        if estimate_messages_tokens(messages) >= waterlines["blocking"]:
            messages = self._run_blocking_recover(
                messages=messages,
                session_state=session_state,
                store=store,
            )
            strategies_run.append("blocking_gate")
        observability = {
            "water_level": water_level,
            "water_line": self._water_line_name(water_level, waterlines),
            "strategies_run": strategies_run,
            "steps": ["estimate", "per_message_budget", *strategies_run],
            "before_tokens": water_level,
            "after_tokens": estimate_messages_tokens(messages),
        }
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=list(runtime_blocks) + list(overlay_blocks),
            working_transcript=messages,
            observability=observability,
        )

    def _calc_water_level(self, session_state, stable_system: str, stable_tools, runtime_blocks) -> int:
        estimated = estimate_messages_tokens(session_state.conversation_messages)
        used = calibrated_input_tokens(
            estimated_tokens=estimated,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )
        stable_system_tokens = max(1, len(stable_system) // 4)
        stable_tools_tokens = 0 if not stable_tools else max(1, len(str(stable_tools)) // 4)
        required_runtime_tokens = sum(block.token_estimate for block in runtime_blocks if block.required)
        return used + stable_system_tokens + stable_tools_tokens + required_runtime_tokens

    def _summarize_with_breaker(self, *, messages, session_state, keep_last_messages):
        return self._compact_service.summarize_and_compact(
            messages,
            state=session_state,
            summary_gateway=self._summary_gateway,
            keep_last_messages=keep_last_messages,
        )

    def _run_blocking_recover(self, *, messages, session_state, store):
        compacted = self._compact_service.summarize_and_compact(
            messages,
            state=session_state,
            summary_gateway=self._summary_gateway,
            keep_last_messages=2,
        )
        if store is not None:
            store.replace_working_transcript(compacted)
        return compacted

    def _water_line_name(self, water_level: int, waterlines: dict[str, int]) -> str:
        if water_level >= waterlines["blocking"]:
            return "blocking"
        if water_level >= waterlines["auto_compact"]:
            return "autocompact"
        if water_level >= waterlines["microcompact"]:
            return "microcompact"
        if water_level >= waterlines["preview_strip"]:
            return "preview_strip"
        return "normal"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/session/test_governor.py tests/session/test_token_budget.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/governor.py core/session/token_budget.py core/session/__init__.py tests/session/test_governor.py tests/session/test_token_budget.py
git commit -m "feat: add context governor and waterline math"
```

### Task 7: QueryLoop And SessionEngine Wiring

**Files:**
- Modify: `core/query/loop.py`
- Modify: `core/session/engine.py`
- Modify: `core/session/view_builder.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/test_query_logging.py`
- Modify: `tests/test_todo_planning_integration.py`

- [ ] **Step 1: Write the failing tests for governor/offloader wiring**

```python
from types import SimpleNamespace

from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.session.state import SessionState
from core.session.store import SessionStore


def test_query_loop_uses_governor_and_offloader_before_model_call() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    events: list[str] = []

    class FakeOffloader:
        def maybe_persist(self, tool_use_id, content, *, tool_name):
            events.append(f"persist:{tool_use_id}:{tool_name}")
            return content

    class FakeGovernor:
        def assess(self, **kwargs):
            events.append("assess")
            return SimpleNamespace(
                working_transcript=list(session_state.conversation_messages),
                observability={"steps": ["estimate"], "before_tokens": 0, "after_tokens": 0},
                stable_system="SYSTEM",
                stable_tools=None,
                runtime_blocks=[],
            )

    class FakeViewBuilder:
        def build(self, prepared, *, run_state):
            return SimpleNamespace(system="SYSTEM", messages=list(prepared.working_transcript), tools=None)

    class FakeModelGateway:
        def __init__(self) -> None:
            self._responses = [
                ModelResponse(
                    content="",
                    tool_calls=[{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}],
                    finish_reason="tool_use",
                ),
                ModelResponse(content="done", finish_reason="end_turn"),
            ]

        def call_once(self, messages, *, system="", tools=None):
            return self._responses.pop(0)

    class FakeToolRuntime:
        def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
            return SimpleNamespace(
                messages=[{"role": "tool", "tool_call_id": "toolu_1", "content": "alpha"}],
                tool_names=["read_file"],
                tool_statuses=[],
                session_updates=[],
                run_updates=[],
            )

    class FakePolicyRunner:
        def before_model_call(self, session_state, state):
            return []

        def after_tool_batch(self, session_state, state, batch):
            return []

        def should_stop(self, session_state, state):
            return None

    class FakeRecovery:
        def handle(self, model_resp, state):
            return SimpleNamespace(should_continue=False, follow_up_messages=[])

    QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=SimpleNamespace(
            build_stable_context=lambda *args, **kwargs: "stable",
            build_stable_tools=lambda *args, **kwargs: None,
            build_runtime_blocks=lambda *args, **kwargs: [],
            build_query_overlay_blocks=lambda *args, **kwargs: [],
        ),
        model_gateway=FakeModelGateway(),
        tool_runtime=FakeToolRuntime(),
        tool_context=SimpleNamespace(working_dir="."),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=None,
    )
    assert events == ["persist:toolu_1:read_file", "assess"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_integration.py -v`

Expected: FAIL with missing `governor` / `offloader` constructor arguments and stale `FakeContextManager` expectations

- [ ] **Step 3: Replace `ContextManager` wiring with governor and offloader**

```python
# core/session/engine.py
from core.session.governor import ContextGovernor
from core.session.offloader import ToolResultOffloader


# inside SessionEngine.__init__
self._state = SessionState(conversation_messages=[])
working_dir = Path(getattr(tool_context, "working_dir", "."))
self._store = SessionStore(self._state, working_dir=working_dir)
self._offloader = ToolResultOffloader(
    tool_result_dir=self._store.tool_result_dir,
    replacement_state=self._state.content_replacement_state,
)
self._governor = governor or ContextGovernor(
    offloader=self._offloader,
    compact_service=compact_service,
    summary_gateway=model_gateway,
)
```

```python
# core/query/loop.py
def run(
    self,
    *,
    session_state,
    store,
    view_builder,
    prompt_assembler,
    model_gateway,
    tool_runtime,
    tool_context,
    policy_runner,
    recovery,
    governor,
    offloader,
    tools=None,
    renderer=None,
):
    batch = tool_runtime.execute_batch(
        parsed_calls,
        run_state=state,
        apply_session_update=lambda update: apply_session_update(session_state, update),
        apply_run_update=apply_run_update,
    )
    tool_name_by_id = {call.call_id: call.name for call in parsed_calls}
    persisted_messages = []
    for message in batch.messages:
        rewritten = dict(message)
        if rewritten.get("role") == "tool" and rewritten.get("tool_call_id"):
            tool_name = tool_name_by_id.get(rewritten["tool_call_id"], "")
            if tool_name != "read_file":
                rewritten["content"] = offloader.maybe_persist(
                    rewritten["tool_call_id"],
                    str(rewritten.get("content", "")),
                    tool_name=tool_name,
                )
        persisted_messages.append(rewritten)
    store.extend(persisted_messages)
    prepared = governor.assess(
        session_state=session_state,
        run_state=state,
        store=store,
        query_source="main_loop",
        stable_system=stable_system,
        stable_tools=stable_tools,
        runtime_blocks=runtime_blocks,
        overlay_blocks=overlay_blocks,
    )
```

```python
# core/session/view_builder.py
"""QueryLoop.run()
  → ContextGovernor.assess()          预算治理、blocking gate、产出 PreparedQueryContext
  → MessageViewBuilder.build()        仅做最终模型输入装配
"""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_integration.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/query/loop.py core/session/engine.py core/session/view_builder.py tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_integration.py
git commit -m "refactor: wire query loop through governor and offloader"
```

### Task 8: Remove Legacy ContextManager And Finalize Exports

**Files:**
- Delete: `core/session/context_manager.py`
- Delete: `tests/session/test_context_manager.py`
- Modify: `core/session/__init__.py`
- Modify: `tests/session/test_compact_service.py`
- Test: `tests/session/test_governor.py`
- Test: `tests/session/test_offloader.py`
- Test: `tests/session/test_preview_strip.py`
- Test: `tests/session/test_read_working_set.py`
- Test: `tests/session/test_microcompact.py`
- Test: `tests/session/test_compact_service.py`
- Test: `tests/session/test_token_budget.py`
- Test: `tests/test_query_display.py`
- Test: `tests/test_query_logging.py`
- Test: `tests/test_todo_planning_integration.py`

- [ ] **Step 1: Write the failing cleanup assertions**

```python
from core.session import ContextGovernor, SessionState, SessionStore, ToolResultOffloader


def test_core_session_exports_governor_and_offloader() -> None:
    assert ContextGovernor.__name__ == "ContextGovernor"
    assert ToolResultOffloader.__name__ == "ToolResultOffloader"
    assert SessionState.__name__ == "SessionState"
    assert SessionStore.__name__ == "SessionStore"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/session/test_governor.py::test_core_session_exports_governor_and_offloader -v`

Expected: FAIL with `ImportError: cannot import name 'ContextGovernor' from 'core.session'`

- [ ] **Step 3: Delete the legacy manager and finish public exports**

```python
# core/session/__init__.py
from .governor import ContextGovernor
from .offloader import ToolResultOffloader
from .query_context import ContextBlock, PreparedQueryContext
from .state import SessionState
from .store import SessionStore
from .view_builder import MessageViewBuilder, ModelInputView

__all__ = [
    "ContextBlock",
    "ContextGovernor",
    "MessageViewBuilder",
    "ModelInputView",
    "PreparedQueryContext",
    "SessionState",
    "SessionStore",
    "ToolResultOffloader",
]
```

```bash
rm core/session/context_manager.py tests/session/test_context_manager.py
```

- [ ] **Step 4: Run the focused and broad verification suites**

Run: `pytest tests/session/test_governor.py tests/session/test_offloader.py tests/session/test_preview_strip.py tests/session/test_read_working_set.py tests/session/test_microcompact.py tests/session/test_compact_service.py tests/session/test_token_budget.py tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_integration.py -v`

Expected: PASS

Run: `pytest tests/session -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/__init__.py core/session/offloader.py core/session/governor.py core/session/preview_strip.py core/session/read_working_set.py core/session/microcompact.py core/session/token_budget.py core/session/state.py core/session/store.py core/session/compact_service.py core/session/view_builder.py core/query/loop.py core/session/engine.py tests/session/test_content_replacement.py tests/session/test_offloader.py tests/session/test_preview_strip.py tests/session/test_read_working_set.py tests/session/test_microcompact.py tests/session/test_governor.py tests/session/test_compact_service.py tests/session/test_token_budget.py tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_integration.py
git commit -m "feat: ship runtime context governance v1"
```
