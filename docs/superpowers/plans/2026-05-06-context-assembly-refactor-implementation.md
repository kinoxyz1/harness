# Context Assembly Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `MessageViewBuilder`'s transcript-budget-centric assembly with a block-based context pipeline built around `stable_system + stable_tools + runtime_blocks + working_transcript`.

**Architecture:** Introduce a first-class `PreparedQueryContext`, move stable tool packaging into `PromptAssembler`, let `ContextManager` own total input budgeting across stable/tools/runtime/transcript, and reduce `MessageViewBuilder` to final view assembly plus light normalization. The implementation keeps existing compact helpers but re-anchors them around the new prepared context object instead of ad hoc transcript slicing.

**Tech Stack:** Python 3.12, pytest, existing `SessionState` / `RunState` / `QueryLoop` / `SessionEngine` / `PromptAssembler` stack

---

## File Structure

### New Files

- `core/session/query_context.py`
  Responsibility: define `ContextBlock` and `PreparedQueryContext`.

- `tests/session/test_query_context.py`
  Responsibility: validate the new query-context dataclasses and export surface.

### Modified Files

- `core/session/__init__.py`
  Responsibility: export the new query-context types.

- `core/prompt/assembler.py`
  Responsibility: keep stable prompt rendering, add `build_stable_tools()`, replace monolithic runtime string assembly with block-oriented runtime rendering.

- `core/session/context_manager.py`
  Responsibility: accept stable/tools/runtime inputs, budget them together, prune optional runtime blocks, and return `PreparedQueryContext`.

- `core/session/view_builder.py`
  Responsibility: consume `PreparedQueryContext`, remove transcript slicing, and emit final `ModelInputView`.

- `core/session/engine.py`
  Responsibility: stop constructing `MessageViewBuilder(tools=...)`, persist raw tool schemas separately, and pass them into the query loop.

- `core/query/loop.py`
  Responsibility: explicitly build stable system, stable tools, runtime blocks, and overlay blocks before invoking `ContextManager`.

- `tests/session/test_prompt_assembler.py`
  Responsibility: cover `build_stable_tools()` and `build_runtime_blocks()`.

- `tests/session/test_context_manager.py`
  Responsibility: cover prepared-context output, stable tool budgeting, and runtime-block pruning.

- `tests/session/test_view_builder.py`
  Responsibility: cover new `build(prepared=...)` behavior and removal of transcript budget slicing.

- `tests/session/test_state_assembled_runtime.py`
  Responsibility: prove runtime truth and stable tools survive transcript rewrite under the new prepared-context flow.

- `tests/session/test_engine_commands.py`
  Responsibility: update direct `MessageViewBuilder.build(...)` call sites to the new prepared-context flow.

---

### Task 1: Introduce Query Context Types

**Files:**
- Create: `core/session/query_context.py`
- Modify: `core/session/__init__.py`
- Test: `tests/session/test_query_context.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.session import ContextBlock, PreparedQueryContext


def test_context_block_tracks_required_budget_metadata() -> None:
    block = ContextBlock(
        kind="file_runtime",
        content="<file-runtime>alpha</file-runtime>",
        required=False,
        token_estimate=42,
    )

    assert block.kind == "file_runtime"
    assert block.required is False
    assert block.token_estimate == 42


def test_prepared_query_context_groups_stable_and_mutable_inputs() -> None:
    prepared = PreparedQueryContext(
        stable_system="system text",
        stable_tools=[{"name": "todo"}],
        runtime_blocks=[
            ContextBlock(
                kind="todo_state",
                content="<todo-state />",
                required=True,
                token_estimate=7,
            )
        ],
        working_transcript=[{"role": "user", "content": "hi"}],
    )

    assert prepared.stable_system == "system text"
    assert prepared.stable_tools == [{"name": "todo"}]
    assert prepared.runtime_blocks[0].kind == "todo_state"
    assert prepared.working_transcript == [{"role": "user", "content": "hi"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_query_context.py -v`
Expected: FAIL with `ImportError` because `ContextBlock` and `PreparedQueryContext` do not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/query_context.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextBlock:
    kind: str
    content: str
    required: bool
    token_estimate: int


@dataclass(slots=True)
class PreparedQueryContext:
    stable_system: str
    stable_tools: list[dict[str, Any]] | None
    runtime_blocks: list[ContextBlock]
    working_transcript: list[dict[str, Any]]
    observability: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, int] = field(default_factory=dict)
```

```python
# core/session/__init__.py
from .query_context import ContextBlock, PreparedQueryContext
from .state import SessionState
from .store import SessionStore
from .view_builder import ModelInputView, MessageViewBuilder

__all__ = [
    "ContextBlock",
    "PreparedQueryContext",
    "ModelInputView",
    "MessageViewBuilder",
    "SessionState",
    "SessionStore",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/session/test_query_context.py -v`
Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add core/session/query_context.py core/session/__init__.py tests/session/test_query_context.py
git commit -m "feat: add prepared query context models"
```

### Task 2: Refactor PromptAssembler Into Stable Tools And Runtime Blocks

**Files:**
- Modify: `core/prompt/assembler.py`
- Test: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_stable_tools_returns_schema_copy(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()
    tools = [{"name": "todo", "description": "todo", "input_schema": {"type": "object"}}]

    result = assembler.build_stable_tools(state, tools=tools)

    assert result == tools
    assert result is not tools


def test_build_runtime_blocks_marks_file_runtime_as_optional(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    file_path = tmp_path / "report.md"
    state.read_file_state[str(file_path)] = FileState(
        content="# Report\nalpha\nbeta",
        timestamp=10.0,
        offset=None,
        limit=None,
    )
    assembler = PromptAssembler()

    blocks = assembler.build_runtime_blocks(state, working_dir=str(tmp_path))

    assert [block.kind for block in blocks[:3]] == ["environment", "active_skills", "todo_state"]
    assert any(block.kind == "file_runtime" and block.required is False for block in blocks)


def test_build_runtime_blocks_keeps_active_skill_required(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.invoked_skills["my-skill"] = InvokedSkillRecord(
        skill_id="my-skill",
        skill_path="/skills/my-skill/SKILL.md",
        content_digest="d1",
        content="<skill-content>hello</skill-content>",
        invoked_at_turn=0,
    )
    assembler = PromptAssembler()

    blocks = assembler.build_runtime_blocks(state, working_dir=str(tmp_path))

    active_skill_block = next(block for block in blocks if block.kind == "active_skills")
    assert active_skill_block.required is True
    assert "<skill-content>hello</skill-content>" in active_skill_block.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: FAIL with `AttributeError: 'PromptAssembler' object has no attribute 'build_stable_tools'`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/prompt/assembler.py
from core.session.query_context import ContextBlock


def _estimate_block_tokens(content: str) -> int:
    return max(1, len(content) // 4)


class PromptAssembler:
    def build_stable_tools(
        self,
        state: SessionState,
        *,
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if tools is None:
            return None
        return [dict(tool) for tool in tools]

    def build_runtime_blocks(
        self,
        state: SessionState,
        *,
        working_dir: str,
    ) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []

        environment = get_user_context(working_dir)
        blocks.append(
            ContextBlock(
                kind="environment",
                content=environment,
                required=True,
                token_estimate=_estimate_block_tokens(environment),
            )
        )

        active_msgs = self.build_active_skill_messages(state)
        active_content = active_msgs[0]["content"] if active_msgs else ""
        blocks.append(
            ContextBlock(
                kind="active_skills",
                content=active_content,
                required=True,
                token_estimate=_estimate_block_tokens(active_content),
            )
        )

        todo_content = _render_todo_state(state.todo_state.items)
        blocks.append(
            ContextBlock(
                kind="todo_state",
                content=todo_content,
                required=True,
                token_estimate=_estimate_block_tokens(todo_content),
            )
        )

        file_block = _render_file_runtime(state.read_file_state, char_budget=12_000)
        if file_block:
            blocks.append(
                ContextBlock(
                    kind="file_runtime",
                    content=file_block,
                    required=False,
                    token_estimate=_estimate_block_tokens(file_block),
                )
            )

        return blocks

    def build_query_overlay_blocks(
        self,
        state: SessionState,
        run_state: RunState,
    ) -> list[ContextBlock]:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/session/test_prompt_assembler.py -v`
Expected: PASS including the new `build_stable_tools` and `build_runtime_blocks` tests.

- [ ] **Step 5: Commit**

```bash
git add core/prompt/assembler.py tests/session/test_prompt_assembler.py
git commit -m "refactor: assemble stable tools and runtime blocks"
```

### Task 3: Make ContextManager Return PreparedQueryContext

**Files:**
- Modify: `core/session/context_manager.py`
- Test: `tests/session/test_context_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.session.query_context import ContextBlock


def test_context_manager_returns_prepared_query_context() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    store = SessionStore(state)
    manager = ContextManager(
        compact_service=StubCompactService(),
        summary_gateway=object(),
        context_window_tokens=12_000,
    )
    run_state = RunState()

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=run_state,
        store=store,
        query_source="main_loop",
        stable_system="system",
        stable_tools=[{"name": "todo"}],
        runtime_blocks=[
            ContextBlock(kind="environment", content="<environment />", required=True, token_estimate=5),
            ContextBlock(kind="file_runtime", content="<file-runtime>big</file-runtime>", required=False, token_estimate=20),
        ],
        overlay_blocks=[],
    )

    assert prepared.stable_system == "system"
    assert prepared.stable_tools == [{"name": "todo"}]
    assert prepared.working_transcript[0]["role"] == "meta_compact_boundary"


def test_context_manager_drops_optional_runtime_blocks_before_summary() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 2000}])
    manager = ContextManager(
        compact_service=ShrinkingCompactService(),
        summary_gateway=object(),
        context_window_tokens=2_500,
    )

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=SessionStore(state),
        query_source="main_loop",
        stable_system="s" * 2000,
        stable_tools=[{"name": "todo", "input_schema": {"type": "object"}}],
        runtime_blocks=[
            ContextBlock(kind="environment", content="<environment />", required=True, token_estimate=10),
            ContextBlock(kind="file_runtime", content="<file-runtime>drop-me</file-runtime>", required=False, token_estimate=800),
        ],
        overlay_blocks=[],
    )

    assert [block.kind for block in prepared.runtime_blocks] == ["environment"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_context_manager.py -v`
Expected: FAIL because `prepare_for_query()` does not accept `stable_system`, `stable_tools`, or `runtime_blocks`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/context_manager.py
from core.session.query_context import ContextBlock, PreparedQueryContext


def _estimate_tools_tokens(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return max(1, len(str(tools)) // 4)


def _prune_optional_runtime_blocks(
    blocks: list[ContextBlock],
    *,
    optional_budget: int,
) -> list[ContextBlock]:
    kept: list[ContextBlock] = []
    optional_used = 0
    for block in blocks:
        if block.required:
            kept.append(block)
            continue
        if optional_used + block.token_estimate > optional_budget:
            continue
        kept.append(block)
        optional_used += block.token_estimate
    return kept


class ContextManager:
    def prepare_for_query(..., stable_system, stable_tools, runtime_blocks, overlay_blocks) -> PreparedQueryContext:
        messages = list(session_state.conversation_messages)
        estimated_tokens = estimate_messages_tokens(messages)
        used_tokens = calibrated_input_tokens(
            estimated_tokens=estimated_tokens,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )

        stable_system_tokens = max(1, len(stable_system) // 4)
        stable_tools_tokens = _estimate_tools_tokens(stable_tools)
        required_runtime_tokens = sum(block.token_estimate for block in runtime_blocks if block.required)
        optional_budget = 12_000
        kept_runtime_blocks = _prune_optional_runtime_blocks(runtime_blocks + overlay_blocks, optional_budget=optional_budget)

        messages = self._compact_service.apply_tool_result_budget(
            messages,
            state=session_state,
            per_message_token_limit=1200,
        )
        messages = self._compact_service.apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )

        total_used_tokens = used_tokens + stable_system_tokens + stable_tools_tokens + required_runtime_tokens
        if query_source != "compact" and should_trigger_summary_compact(
            used_tokens=total_used_tokens,
            context_window_tokens=self._context_window_tokens,
            reserved_output_tokens=10_000,
            compact_buffer_tokens=1_000,
        ):
            messages = self._summarize_with_breaker(
                messages=messages,
                session_state=session_state,
                keep_last_messages=4,
                observability=observability,
            )
            if observability["steps"][-1] == "summary_compact" and store is not None:
                store.replace_working_transcript(messages)

        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=kept_runtime_blocks,
            working_transcript=messages,
            observability=observability,
            budget={
                "stable_system_tokens": stable_system_tokens,
                "stable_tools_tokens": stable_tools_tokens,
                "required_runtime_tokens": required_runtime_tokens,
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/session/test_context_manager.py -v`
Expected: PASS with the new prepared-context assertions and existing compact pipeline tests updated to read `prepared.working_transcript`.

- [ ] **Step 5: Commit**

```bash
git add core/session/context_manager.py tests/session/test_context_manager.py
git commit -m "refactor: return prepared query context from context manager"
```

### Task 4: Reduce MessageViewBuilder To Final Assembly

**Files:**
- Modify: `core/session/view_builder.py`
- Test: `tests/session/test_view_builder.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.session.query_context import ContextBlock, PreparedQueryContext


def test_build_assembles_system_from_prepared_context() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="stable",
        stable_tools=[{"name": "todo"}],
        runtime_blocks=[
            ContextBlock(kind="environment", content="<environment />", required=True, token_estimate=5),
            ContextBlock(kind="todo_state", content="<todo-state />", required=True, token_estimate=5),
        ],
        working_transcript=[{"role": "user", "content": "hello"}],
    )

    view = builder.build(prepared, run_state=RunState())

    assert view.system == "stable\n\n<environment />\n\n<todo-state />"
    assert view.messages == [{"role": "user", "content": "hello"}]
    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_does_not_slice_prepared_transcript() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="stable",
        stable_tools=None,
        runtime_blocks=[],
        working_transcript=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    )

    view = builder.build(prepared, run_state=RunState())

    assert view.messages == prepared.working_transcript
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_view_builder.py -v`
Expected: FAIL because `MessageViewBuilder.build()` still expects `SessionState`, `prompt_assembler`, and transcript budget arguments.

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/view_builder.py
class MessageViewBuilder:
    def build(
        self,
        prepared: PreparedQueryContext,
        *,
        run_state,
    ) -> ModelInputView:
        transcript = self._strip_old_thinking(prepared.working_transcript)
        system_parts = [prepared.stable_system] + [
            block.content for block in prepared.runtime_blocks if block.content
        ]
        tools = prepared.stable_tools
        if run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        return ModelInputView(
            system="\n\n".join(part for part in system_parts if part),
            messages=transcript,
            tools=tools,
            internal_runtime_view={
                "runtime_blocks": [block.kind for block in prepared.runtime_blocks],
                "working_transcript": list(transcript),
                "budget": dict(prepared.budget),
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/session/test_view_builder.py -v`
Expected: PASS, and the old transcript-budget tests should be rewritten or removed because slicing is no longer part of this class.

- [ ] **Step 5: Commit**

```bash
git add core/session/view_builder.py tests/session/test_view_builder.py
git commit -m "refactor: make view builder consume prepared query context"
```

### Task 5: Thread Stable Tools And Prepared Context Through QueryLoop And Engine

**Files:**
- Modify: `core/session/engine.py`
- Modify: `core/query/loop.py`
- Modify: `tests/session/test_state_assembled_runtime.py`
- Modify: `tests/session/test_engine_commands.py`

- [ ] **Step 1: Write the failing integration tests**

```python
def test_runtime_view_survives_after_transcript_rewrite_with_stable_tools(
    tmp_path: Path,
) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact;summarized_messages=1"},
            {"role": "meta_compact_summary", "kind": "compact_summary", "content": "summary"},
            {"role": "user", "content": "follow-up"},
        ],
    )
    state.invoked_skills["rewrite-skill"] = InvokedSkillRecord(
        skill_id="rewrite-skill",
        skill_path="/skills/rewrite-skill/SKILL.md",
        content_digest="digest-2",
        content="<skill-runtime>Rewrite-safe skill instructions</skill-runtime>",
        invoked_at_turn=4,
    )

    assembler = PromptAssembler()
    prepared = PreparedQueryContext(
        stable_system=assembler.build_stable_context(state, project_root=str(tmp_path)),
        stable_tools=assembler.build_stable_tools(
            state,
            tools=[{"name": "todo", "description": "todo", "input_schema": {"type": "object"}}],
        ),
        runtime_blocks=assembler.build_runtime_blocks(state, working_dir=str(tmp_path)),
        working_transcript=state.conversation_messages,
    )

    view = MessageViewBuilder().build(prepared, run_state=RunState())

    assert [tool["name"] for tool in view.tools] == ["todo"]
    assert "Rewrite-safe skill instructions" in view.system
    assert view.messages[-1]["content"] == "follow-up"
```

```python
def test_active_skill_body_reaches_model_view(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")

    assembler = engine._prompt_assembler
    prepared = engine._context_manager.prepare_for_query(
        session_state=engine.state,
        run_state=RunState(),
        store=engine._store,
        query_source="main_loop",
        stable_system=assembler.build_stable_context(engine.state, project_root=str(tmp_path)),
        stable_tools=assembler.build_stable_tools(engine.state, tools=engine._tools),
        runtime_blocks=assembler.build_runtime_blocks(engine.state, working_dir=str(tmp_path)),
        overlay_blocks=assembler.build_query_overlay_blocks(engine.state, RunState()),
    )

    view = engine._view_builder.build(prepared, run_state=RunState())

    assert "Use a fixed HTML structure" in view.system
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py -v`
Expected: FAIL because `SessionEngine` does not keep `_tools`, `QueryLoop` does not build stable tools, and the integration tests still use the old builder signature.

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/engine.py
class SessionEngine:
    def __init__(..., tools=None, ...):
        self._tools = tools
        self._view_builder = view_builder or MessageViewBuilder()

    def submit_user_message(self, text):
        self.bootstrap()
        self._store.append({"role": "user", "content": text})
        return self._query_loop.run(
            session_state=self._state,
            store=self._store,
            view_builder=self._view_builder,
            prompt_assembler=self._prompt_assembler,
            model_gateway=self._model_gateway,
            tool_runtime=self._tool_runtime,
            tool_context=self._tool_context,
            policy_runner=self._policy_runner,
            recovery=self._recovery,
            context_manager=self._context_manager,
            tools=self._tools,
            renderer=self._renderer,
        )
```

```python
# core/query/loop.py
def run(..., context_manager, tools=None, renderer=None) -> QueryResult:
    ...

stable_system = prompt_assembler.build_stable_context(
    session_state,
    project_root=tool_context.working_dir if tool_context is not None else None,
)
stable_tools = prompt_assembler.build_stable_tools(
    session_state,
    tools=tools,
)
runtime_blocks = prompt_assembler.build_runtime_blocks(
    session_state,
    working_dir=tool_context.working_dir,
)
overlay_blocks = prompt_assembler.build_query_overlay_blocks(session_state, state)

prepared = context_manager.prepare_for_query(
    session_state=session_state,
    run_state=state,
    store=store,
    query_source="main_loop",
    stable_system=stable_system,
    stable_tools=stable_tools,
    runtime_blocks=runtime_blocks,
    overlay_blocks=overlay_blocks,
)

view = view_builder.build(
    prepared,
    run_state=state,
)
```

- [ ] **Step 4: Run focused integration tests**

Run: `pytest tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py -v`
Expected: PASS, proving the new prepared-context flow works end-to-end for runtime truth and stable tools.

- [ ] **Step 5: Commit**

```bash
git add core/session/engine.py core/query/loop.py tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py
git commit -m "refactor: thread prepared context through query loop"
```

### Task 6: Run Full Session Refactor Regression

**Files:**
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/session/test_context_manager.py`
- Modify: `tests/session/test_view_builder.py`
- Modify: `tests/session/test_state_assembled_runtime.py`
- Modify: `tests/session/test_engine_commands.py`

- [ ] **Step 1: Run the focused session suite**

Run: `pytest tests/session/test_query_context.py tests/session/test_prompt_assembler.py tests/session/test_context_manager.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py -v`
Expected: PASS with all session-context refactor tests green.

- [ ] **Step 2: Run the broader regression suite**

Run: `pytest tests/test_query_display.py tests/test_query_logging.py tests/test_protocol.py tests/test_model_gateway.py -v`
Expected: PASS, or if any fail, only because they still reference the removed view-builder signature or old transcript-slice assumptions.

- [ ] **Step 3: Fix any remaining assertion drift**

```python
# Typical final cleanups should look like this:
assert "working_transcript" in view.internal_runtime_view
assert prepared.budget["stable_tools_tokens"] >= 0
assert "transcript_slice" not in view.internal_runtime_view
```

- [ ] **Step 4: Run the final combined command**

Run: `pytest tests/session/test_query_context.py tests/session/test_prompt_assembler.py tests/session/test_context_manager.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py tests/test_query_display.py tests/test_query_logging.py tests/test_protocol.py tests/test_model_gateway.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/session/test_query_context.py tests/session/test_prompt_assembler.py tests/session/test_context_manager.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/session/test_engine_commands.py tests/test_query_display.py tests/test_query_logging.py tests/test_protocol.py tests/test_model_gateway.py
git commit -m "test: cover context assembly refactor"
```

---

## Self-Review

### Spec Coverage

- `PreparedQueryContext` and `ContextBlock`: Task 1
- `build_stable_tools()` and runtime block assembly: Task 2
- `ContextManager` owning total budgeting across stable/tools/runtime/transcript: Task 3
- `MessageViewBuilder` no longer slicing transcript: Task 4
- `QueryLoop` and `SessionEngine` adopting the new prepared-context flow: Task 5
- Regression proof for runtime truth and stable tools after transcript rewrite: Tasks 5-6

### Placeholder Scan

- No `TODO` / `TBD` implementation steps remain inside tasks.
- Every code-edit step contains concrete code or method signatures.
- Every execution step names an exact `pytest` command and expected result.

### Type Consistency

- `PreparedQueryContext` always uses `stable_tools`, never `tools`.
- `MessageViewBuilder.build()` always accepts `prepared` and `run_state`.
- `ContextManager.prepare_for_query()` and `reactive_recover()` both take `stable_system`, `stable_tools`, `runtime_blocks`, and `overlay_blocks`.

---

Plan complete and saved to `docs/superpowers/plans/2026-05-06-context-assembly-refactor-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
