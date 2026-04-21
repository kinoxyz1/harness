# Runtime Update Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current barrier/context-patch tool protocol with a unified runtime update protocol that keeps runtime truth in explicit state and lets `QueryLoop` own transition application.

**Architecture:** Keep `conversation_messages` as the append-only transcript, but move tool-visible truth into explicit `SessionState` and `RunState` updates. Tools return `ToolInvocationOutcome`, `ToolExecutorRuntime` applies updates through reducer callbacks in deterministic order, and `QueryLoop` advances only through query-owned `TransitionReason` values instead of tool-private barrier semantics.

**Tech Stack:** Python 3, dataclasses, existing `QueryLoop` / `ToolExecutorRuntime` / `PromptAssembler` stack, `pytest`

---

## Integration Note

Tasks 2 through 5 form one atomic migration chain.

- After Task 1: new protocol types exist, but most tools still return the old `ToolResult`.
- After Task 2: runtime expects `ToolInvocationOutcome`, but builtins and `QueryLoop` are not fully migrated yet.
- After Task 3 and Task 4: builtins return new outcomes, but `QueryLoop` may still expect the old batch shape until Task 5 lands.
- Only after Task 5 does the system become end-to-end runnable again.

Do not stop at Task 2, 3, or 4 and assume the branch is integration-safe. Treat Tasks 2 to 5 as one contiguous refactor batch on the same branch, with focused tests at each step and full end-to-end verification only after Task 5 or later.

---

## File Structure

- Create: `core/query/reducers.py`
  Responsibility: define `apply_session_update`, `apply_run_update`, `apply_transition`, and small reducer-owned helpers for runtime state maintenance.
- Modify: `core/tools/context.py`
  Responsibility: replace `ToolResult` / `ContextPatch` / `ExecutionBarrier` with `ToolInvocationOutcome`, `ToolOutcomeStatus`, `SessionUpdate`, `RunUpdate`; keep `ToolUseContext` as a read-oriented runtime handle and break direct state mutation aliases.
- Modify: `core/tools/runtime.py`
  Responsibility: consume `ToolInvocationOutcome`, preserve readonly parallelism, apply serial updates immediately, merge parallel updates in original call order, and reject serial calls that become disallowed after narrowing.
- Modify: `core/tools/__init__.py`
  Responsibility: export the new protocol types and make `ToolRegistry.execute` return `ToolInvocationOutcome`.
- Modify: `core/tools/builtin/skill.py`
  Responsibility: stop mutating `SessionState` directly; return skill activation message plus `SessionUpdate.INVOKE_SKILL` and `SessionUpdate.APPEND_SKILL_EVENT`.
- Modify: `core/tools/builtin/todo.py`
  Responsibility: stop mutating `state.todo_state` directly; return `SessionUpdate.SET_TODO_ITEMS` plus `RunUpdate.RESET_TODO_TURN_COUNTER`; remove the unused compatibility `get_state()` API.
- Modify: `core/tools/builtin/read_file.py`
  Responsibility: return `SessionUpdate.UPSERT_FILE_STATE` instead of writing through `ToolUseContext`.
- Modify: `core/tools/builtin/write_file.py`
  Responsibility: return `SessionUpdate.UPSERT_FILE_STATE` plus `RunUpdate.MARK_FILE_MODIFIED`.
- Modify: `core/tools/builtin/edit_file.py`
  Responsibility: keep read-before-write validation, but return updates instead of mutating file cache or run state directly.
- Modify: `core/tools/builtin/find.py`
  Responsibility: migrate to `ToolInvocationOutcome` with message-only success/failure results.
- Modify: `core/tools/builtin/bash.py`
  Responsibility: migrate to `ToolInvocationOutcome` with `ToolOutcomeStatus` for blocked / cancelled / failure cases.
- Modify: `core/skills/runtime.py`
  Responsibility: split direct state mutation helpers into pure helpers that build an `InvokedSkillRecord` or its payload so reducer code can own the actual write.
- Modify: `core/query/state.py`
  Responsibility: add `transition`; remove `barrier_reason`, `todo_replan_required`, and `todo_replan_reason`; keep only query-owned counters and overrides.
- Modify: `core/query/loop.py`
  Responsibility: consume `ToolBatchResult.messages`, call reducers, set `TransitionReason`, run reducer-owned maintenance, and remove barrier branches plus `_apply_batch_control_plane`.
- Modify: `core/query/recovery.py`
  Responsibility: keep recovery logic, but make transition reasons explicit for empty-response and max-tokens recovery paths.
- Modify: `core/policy/todo_tracking.py`
  Responsibility: remove post-skill replan behavior; rely only on explicit state and stale-plan heuristics.
- Modify: `core/prompt/assembler.py`
  Responsibility: shrink `build_query_overlay()` to an empty or future-only hook; expose `transition` in the internal runtime view instead of `barrier_reason`.
- Modify: `core/session/view_builder.py`
  Responsibility: keep overlay call, but treat it as mostly empty; continue filtering tools from `run_state.allowed_tools_override`.
- Modify: `tests/test_runtime_control_plane.py`
  Responsibility: replace barrier-era protocol assertions with outcome/update/reducer/runtime-ordering assertions.
- Modify: `tests/test_todo_planning_policy.py`
  Responsibility: remove barrier-driven todo replan assertions; keep stale-plan reminder assertions.
- Modify: `tests/session/test_skill_tool.py`
  Responsibility: assert skill tool returns updates instead of direct mutation + barrier.
- Modify: `tests/session/test_file_runtime_state.py`
  Responsibility: move file-runtime assertions to reducer-applied update behavior.
- Modify: `tests/session/test_prompt_assembler.py`
  Responsibility: remove overlay/barrier expectations; assert internal runtime view exposes `transition`.
- Modify: `tests/test_query_logging.py`
  Responsibility: update fakes to the new `ToolBatchResult` shape used by `QueryLoop`.
- Modify: `tests/test_runtime_logging.py`
  Responsibility: update fake tools to return `ToolInvocationOutcome` and keep runtime logging coverage valid.

## Task 1: Introduce Outcome Types And Reducers

**Files:**
- Create: `core/query/reducers.py`
- Modify: `core/tools/context.py`
- Modify: `core/tools/__init__.py`
- Modify: `core/query/state.py`
- Test: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: Write the failing reducer/protocol tests**

```python
from core.query.reducers import (
    apply_run_update,
    apply_session_update,
    apply_transition,
    collect_runtime_maintenance_updates,
    TransitionReason,
)
from core.query.state import RunState
from core.session.state import SessionState, TodoItem
from core.tools.context import (
    FileState,
    RunUpdate,
    RunUpdateKind,
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
)


def test_tool_invocation_outcome_defaults_to_empty_lists() -> None:
    outcome = ToolInvocationOutcome()
    assert outcome.messages == []
    assert outcome.session_updates == []
    assert outcome.run_updates == []
    assert outcome.status is ToolOutcomeStatus.SUCCESS


def test_apply_run_update_intersects_allowed_tools() -> None:
    state = RunState(allowed_tools_override={"skill", "todo", "bash"})
    update = RunUpdate(
        kind=RunUpdateKind.NARROW_ALLOWED_TOOLS,
        payload={"allowed_tools": {"skill", "todo"}},
    )

    apply_run_update(state, update)

    assert state.allowed_tools_override == {"skill", "todo"}


def test_apply_run_update_can_reset_todo_turn_counter() -> None:
    state = RunState(assistant_turns_since_todo=4)
    update = RunUpdate(
        kind=RunUpdateKind.RESET_TODO_TURN_COUNTER,
        payload={},
    )

    apply_run_update(state, update)

    assert state.assistant_turns_since_todo == 0


def test_apply_session_update_sets_todo_items() -> None:
    state = SessionState(conversation_messages=[])
    update = SessionUpdate(
        kind=SessionUpdateKind.SET_TODO_ITEMS,
        payload={
            "items": [
                TodoItem(content="Read spec", active_form="Reading spec", status="in_progress"),
            ],
            "last_write_turn": 3,
        },
    )

    apply_session_update(state, update)

    assert state.todo_state.items[0].content == "Read spec"
    assert state.todo_state.last_write_turn == 3


def test_apply_transition_resets_empty_retry_count_on_next_turn() -> None:
    state = RunState(empty_retry_count=2)

    apply_transition(state, TransitionReason.NEXT_TURN)

    assert state.transition is TransitionReason.NEXT_TURN
    assert state.empty_retry_count == 0


def test_collect_runtime_maintenance_updates_invalidates_stale_file_state(tmp_path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    state.read_file_state[str(file_path)] = FileState(
        content="alpha",
        timestamp=1.0,
        offset=None,
        limit=None,
    )

    updates = collect_runtime_maintenance_updates(state)

    assert updates[0].kind is SessionUpdateKind.INVALIDATE_FILE_STATE
    assert updates[0].payload["path"] == str(file_path)
```

- [ ] **Step 2: Run the focused tests and confirm they fail for missing symbols**

Run: `pytest tests/test_runtime_control_plane.py -v`

Expected: FAIL with import or attribute errors for `ToolInvocationOutcome`, `RunUpdate`, `SessionUpdate`, `TransitionReason`, or reducer helpers.

- [ ] **Step 3: Add the new protocol and reducer code**

```python
# core/tools/context.py
from enum import StrEnum


class SessionUpdateKind(StrEnum):
    INVOKE_SKILL = "invoke_skill"
    SET_TODO_ITEMS = "set_todo_items"
    UPSERT_FILE_STATE = "upsert_file_state"
    INVALIDATE_FILE_STATE = "invalidate_file_state"
    APPEND_SKILL_EVENT = "append_skill_event"


class RunUpdateKind(StrEnum):
    MARK_FILE_MODIFIED = "mark_file_modified"
    NARROW_ALLOWED_TOOLS = "narrow_allowed_tools"
    SET_MODEL_OVERRIDE = "set_model_override"
    SET_EFFORT_OVERRIDE = "set_effort_override"
    RESET_TODO_TURN_COUNTER = "reset_todo_turn_counter"


class ToolOutcomeStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class SessionUpdate:
    kind: SessionUpdateKind
    payload: dict[str, Any]


@dataclass(slots=True)
class RunUpdate:
    kind: RunUpdateKind
    payload: dict[str, Any]


@dataclass(slots=True)
class ToolInvocationOutcome:
    messages: list[dict[str, Any]] = field(default_factory=list)
    session_updates: list[SessionUpdate] = field(default_factory=list)
    run_updates: list[RunUpdate] = field(default_factory=list)
    status: ToolOutcomeStatus = ToolOutcomeStatus.SUCCESS
    error: str | None = None


def make_tool_message(context: "ToolUseContext", content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": context.tool_call_id, "content": content}
```

```python
# core/query/reducers.py
from enum import StrEnum

from core.query.state import RunState
from core.session.state import SessionState
from core.tools.context import RunUpdate, RunUpdateKind, SessionUpdate, SessionUpdateKind


class TransitionReason(StrEnum):
    NEXT_TURN = "next_turn"
    MAX_TURNS_RECOVERY = "max_turns_recovery"
    EMPTY_RESPONSE_RETRY = "empty_response_retry"
    MAX_TOKENS_RECOVERY = "max_tokens_recovery"


def apply_session_update(session_state: SessionState, update: SessionUpdate) -> None:
    if update.kind is SessionUpdateKind.SET_TODO_ITEMS:
        items = list(update.payload["items"])
        session_state.todo_state.items = [] if items and all(item.status == "completed" for item in items) else items
        session_state.todo_state.last_completed_items = items if items and all(item.status == "completed" for item in items) else []
        session_state.todo_state.last_write_turn = update.payload["last_write_turn"]
        return
    if update.kind is SessionUpdateKind.UPSERT_FILE_STATE:
        session_state.read_file_state[update.payload["path"]] = update.payload["file_state"]
        return
    if update.kind is SessionUpdateKind.INVALIDATE_FILE_STATE:
        session_state.read_file_state.pop(update.payload["path"], None)
        return
    if update.kind is SessionUpdateKind.INVOKE_SKILL:
        session_state.invoked_skills[update.payload["skill_id"]] = update.payload["record"]
        return
    if update.kind is SessionUpdateKind.APPEND_SKILL_EVENT:
        session_state.skill_events.append(update.payload["event"])
        return
    raise ValueError(f"Unsupported session update: {update.kind}")


def apply_run_update(run_state: RunState, update: RunUpdate) -> None:
    if update.kind is RunUpdateKind.MARK_FILE_MODIFIED:
        path = update.payload["path"]
        if path not in run_state.files_modified:
            run_state.files_modified.append(path)
        return
    if update.kind is RunUpdateKind.NARROW_ALLOWED_TOOLS:
        allowed = set(update.payload["allowed_tools"])
        run_state.allowed_tools_override = allowed if run_state.allowed_tools_override is None else run_state.allowed_tools_override & allowed
        return
    if update.kind is RunUpdateKind.SET_MODEL_OVERRIDE:
        run_state.model_override = update.payload["model"]
        return
    if update.kind is RunUpdateKind.SET_EFFORT_OVERRIDE:
        run_state.effort_override = update.payload["effort"]
        return
    if update.kind is RunUpdateKind.RESET_TODO_TURN_COUNTER:
        run_state.assistant_turns_since_todo = 0
        return
    raise ValueError(f"Unsupported run update: {update.kind}")


def apply_transition(run_state: RunState, reason: TransitionReason) -> None:
    run_state.transition = reason
    if reason is TransitionReason.NEXT_TURN:
        run_state.empty_retry_count = 0
    elif reason is TransitionReason.EMPTY_RESPONSE_RETRY:
        run_state.empty_retry_count += 1


def collect_runtime_maintenance_updates(session_state: SessionState) -> list[SessionUpdate]:
    updates: list[SessionUpdate] = []
    for path, file_state in list(session_state.read_file_state.items()):
        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            current_mtime = None
        if current_mtime != file_state.timestamp:
            updates.append(
                SessionUpdate(
                    kind=SessionUpdateKind.INVALIDATE_FILE_STATE,
                    payload={"path": path},
                )
            )
    return updates
```

```python
# core/query/state.py
from core.query.reducers import TransitionReason


@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
    allowed_tools_override: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
    assistant_turns_since_todo: int = 0
    transition: TransitionReason | None = None
    last_displayed_todo_items: list["TodoItem"] | None = None
```

```python
# core/tools/__init__.py
from .context import (
    FileState,
    RunUpdate,
    RunUpdateKind,
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
    safe_path,
)
```

- [ ] **Step 4: Run the protocol tests again**

Run: `pytest tests/test_runtime_control_plane.py -v`

Expected: PASS for the new reducer/protocol tests; older barrier-era tests may still fail until later tasks migrate them.

- [ ] **Step 5: Commit the protocol scaffold**

```bash
git add core/query/reducers.py core/query/state.py core/tools/context.py core/tools/__init__.py tests/test_runtime_control_plane.py
git commit -m "refactor: add runtime update protocol scaffolding"
```

## Task 2: Refactor Tool Runtime To Apply Updates Deterministically

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `tests/test_runtime_control_plane.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: Write failing runtime-ordering tests**

```python
from core.query.reducers import apply_run_update
from core.query.state import RunState
from core.tools import ToolRegistry
from core.tools.context import RunUpdate, RunUpdateKind, ToolInvocationOutcome, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class _NarrowTool:
    SCHEMA = {"name": "narrow", "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            messages=[{"role": "tool", "tool_call_id": context.tool_call_id, "content": "narrowed"}],
            run_updates=[RunUpdate(kind=RunUpdateKind.NARROW_ALLOWED_TOOLS, payload={"allowed_tools": {"todo"}})],
        )


class _TodoTool:
    SCHEMA = {"name": "todo", "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(messages=[{"role": "tool", "tool_call_id": context.tool_call_id, "content": "todo ok"}])


class _WriteTool:
    SCHEMA = {"name": "write_file", "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        raise AssertionError("write_file should be rejected after narrowing")


def test_runtime_rejects_later_serial_call_after_narrowing(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_NarrowTool)
    reg.register(_TodoTool)
    reg.register(_WriteTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    run_state = RunState()

    batch = runtime.execute_batch(
        [
            ToolCall(idx=0, name="narrow", call_id="toolu_1", args={}),
            ToolCall(idx=1, name="write_file", call_id="toolu_2", args={}),
            ToolCall(idx=2, name="todo", call_id="toolu_3", args={}),
        ],
        run_state=run_state,
        apply_session_update=lambda update: None,
        apply_run_update=lambda update: apply_run_update(run_state, update),
    )

    assert batch.messages[1]["content"] == "Tool 'write_file' rejected: no longer allowed in this run."
    assert batch.messages[2]["content"] == "todo ok"
    assert run_state.allowed_tools_override == {"todo"}
```

- [ ] **Step 2: Run the runtime tests and confirm the current runtime still fails**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_logging.py -v`

Expected: FAIL because `ToolExecutorRuntime.execute_batch` still expects the old `ToolResult` and barrier-aware flow.

- [ ] **Step 3: Replace barrier-aware execution with outcome-aware execution**

```python
# core/tools/runtime.py
@dataclass(slots=True)
class ToolBatchResult:
    messages: list[dict[str, Any]]
    tool_names: list[str]
    tool_statuses: list[ToolOutcomeStatus]
    session_updates: list[SessionUpdate]
    run_updates: list[RunUpdate]


def execute_batch(
    self,
    tool_calls: list[ToolCall],
    *,
    run_state,
    apply_session_update,
    apply_run_update,
) -> ToolBatchResult:
    ordered_outcomes: dict[int, ToolInvocationOutcome] = {}
    applied_session_updates: list[SessionUpdate] = []
    applied_run_updates: list[RunUpdate] = []

    for batch in self._partition(tool_calls):
        if batch.parallel:
            results = self._execute_parallel(batch)
            for call in sorted(batch.calls, key=lambda value: value.idx):
                outcome = results[call.idx]
                ordered_outcomes[call.idx] = outcome
                for update in outcome.session_updates:
                    apply_session_update(update)
                    applied_session_updates.append(update)
                for update in outcome.run_updates:
                    apply_run_update(update)
                    applied_run_updates.append(update)
            continue

        for call in batch.calls:
            if run_state.allowed_tools_override is not None and call.name not in run_state.allowed_tools_override:
                ordered_outcomes[call.idx] = ToolInvocationOutcome(
                    messages=[{"role": "tool", "tool_call_id": call.call_id, "content": f"Tool '{call.name}' rejected: no longer allowed in this run."}],
                    status=ToolOutcomeStatus.BLOCKED,
                    error="tool_not_allowed",
                )
                continue

            outcome = self._run_single(call)
            ordered_outcomes[call.idx] = outcome
            for update in outcome.session_updates:
                apply_session_update(update)
                applied_session_updates.append(update)
            for update in outcome.run_updates:
                apply_run_update(update)
                applied_run_updates.append(update)

    ordered_calls = sorted(tool_calls, key=lambda call: call.idx)
    return ToolBatchResult(
        messages=[message for call in ordered_calls for message in ordered_outcomes[call.idx].messages],
        tool_names=[call.name for call in ordered_calls],
        tool_statuses=[ordered_outcomes[call.idx].status for call in ordered_calls],
        session_updates=applied_session_updates,
        run_updates=applied_run_updates,
    )
```

```python
# core/tools/runtime.py
def _run_single(self, call: ToolCall) -> ToolInvocationOutcome:
    self._context._set_call_identity(name=call.name, call_id=call.call_id, turn=self._context.turn_count)
    start = time.time()
    outcome_holder: list[ToolInvocationOutcome] = []
    error_holder: list[Exception] = []

    def run() -> None:
        try:
            outcome = self._registry.execute(call.name, call.args, self._context)
            if outcome.messages:
                first = outcome.messages[0]
                content = first.get("content", "")
                if isinstance(content, str) and len(content) > MAX_OUTPUT_CHARS:
                    truncated = content[:MAX_OUTPUT_CHARS] + f"\n\n... (输出已截断，原始 {len(content)} 字符，显示前 {MAX_OUTPUT_CHARS} 字符)"
                    first = dict(first)
                    first["content"] = truncated
                    outcome = ToolInvocationOutcome(
                        messages=[first, *outcome.messages[1:]],
                        session_updates=outcome.session_updates,
                        run_updates=outcome.run_updates,
                        status=outcome.status,
                        error=outcome.error,
                    )
            outcome_holder.append(outcome)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=run)
    thread.start()

    shown_trace_progress = False
    shown_compact_status = False
    while thread.is_alive():
        thread.join(timeout=1.0)
        if thread.is_alive():
            elapsed = int(time.time() - start)
            if elapsed >= 2:
                if self._trace_enabled():
                    sys.stdout.write(f"\r\033[K\033[36m[Runtime]   ⏳ {call.name} 执行中... {elapsed}s\033[0m")
                    sys.stdout.flush()
                    shown_trace_progress = True
                elif self._renderer is not None and not self._display.quiet and not shown_compact_status:
                    self._renderer.show_status(f"{call.name} 执行中... {elapsed}s")
                    shown_compact_status = True

    if shown_trace_progress and not self._display.quiet:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    if error_holder:
        return ToolInvocationOutcome(
            messages=[{"role": "tool", "tool_call_id": call.call_id, "content": f"Internal error: {error_holder[0]}"}],
            status=ToolOutcomeStatus.FAILURE,
            error="internal_error",
        )

    return outcome_holder[0]
```

```python
# core/tools/runtime.py
def _execute_parallel(self, batch: _Batch) -> dict[int, ToolInvocationOutcome]:
    results: dict[int, ToolInvocationOutcome] = {}
    calls_by_idx = {call.idx: call for call in batch.calls}
    with ThreadPoolExecutor(max_workers=len(batch.calls)) as pool:
        futures = {pool.submit(self._run_single, call): call.idx for call in batch.calls}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = ToolInvocationOutcome(
                    messages=[{"role": "tool", "tool_call_id": calls_by_idx[idx].call_id, "content": f"Internal error: {exc}"}],
                    status=ToolOutcomeStatus.FAILURE,
                    error="internal_error",
                )
    return results
```

```python
# core/tools/runtime.py
# delete the old barrier-only path entirely:
# - remove: if any(call.name == "skill"): return self._execute_with_barrier(tool_calls)
# - delete: _execute_with_barrier(...)
# - delete: ContextPatch / ExecutionBarrier imports
# - delete: skipped-by-barrier tool result generation
```

- [ ] **Step 4: Re-run the runtime-focused tests**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_logging.py -v`

Expected: PASS for the new runtime-ordering tests and any updated logging tests that no longer depend on barrier fields.

- [ ] **Step 5: Commit the runtime refactor**

```bash
git add core/tools/runtime.py tests/test_runtime_control_plane.py tests/test_runtime_logging.py
git commit -m "refactor: make tool runtime consume structured outcomes"
```

## Task 3: Convert Skill And Todo To Explicit Session Updates

**Files:**
- Modify: `core/tools/builtin/skill.py`
- Modify: `core/tools/builtin/todo.py`
- Modify: `core/skills/runtime.py`
- Modify: `tests/session/test_skill_tool.py`
- Modify: `tests/test_todo_planning_policy.py`

- [ ] **Step 1: Write failing tests for skill/todo outcome behavior**

```python
from pathlib import Path

from core.query.reducers import apply_session_update
from core.session.state import SessionState
from core.skills.registry import SkillRegistry
from core.tools.context import RunUpdateKind, SessionUpdateKind, ToolOutcomeStatus, ToolUseContext


def test_skill_tool_returns_invoke_skill_update_without_barrier(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the workflow.\n", encoding="utf-8")

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    state = SessionState(conversation_messages=[], skill_catalog=catalog)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=registry)
    ctx._set_call_identity(name="skill", call_id="toolu_skill", turn=1)

    outcome = handle({"skill": "analysis-report"}, ctx)

    assert outcome.status is ToolOutcomeStatus.SUCCESS
    assert outcome.messages[0]["content"].startswith("Skill loaded: analysis-report")
    assert [update.kind for update in outcome.session_updates] == [
        SessionUpdateKind.INVOKE_SKILL,
        SessionUpdateKind.APPEND_SKILL_EVENT,
    ]

    apply_session_update(state, outcome.session_updates[0])
    assert "analysis-report" in state.invoked_skills
    apply_session_update(state, outcome.session_updates[1])
    assert state.skill_events[-1].skill_id == "analysis-report"


def test_todo_tool_returns_set_todo_items_update_without_direct_state_write(tmp_path: Path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)
    ctx._set_call_identity(name="todo", call_id="toolu_todo", turn=2)

    outcome = handle(
        {"items": [{"content": "Read spec", "active_form": "Reading spec", "status": "in_progress"}]},
        ctx,
    )

    assert state.todo_state.items == []
    assert outcome.session_updates[0].kind is SessionUpdateKind.SET_TODO_ITEMS
    assert outcome.run_updates[0].kind is RunUpdateKind.RESET_TODO_TURN_COUNTER
```

- [ ] **Step 2: Run the skill/todo tests**

Run: `pytest tests/session/test_skill_tool.py tests/test_todo_planning_policy.py -v`

Expected: FAIL because `skill` still returns a barrier and `todo` still mutates `SessionState` directly.

- [ ] **Step 3: Refactor skill and todo to return updates instead of direct writes**

```python
# core/skills/runtime.py
def build_invoked_skill_record(*, state, skill_id: str, content: SkillContent, turn: int) -> InvokedSkillRecord:
    body = build_skill_runtime_body(skill_id, content)
    ensure_inline_skill_budget(state=state, new_content=body)
    return InvokedSkillRecord(
        skill_id=skill_id,
        skill_path=str(content.meta.skill_file),
        content_digest=content.content_digest,
        content=body,
        invoked_at_turn=turn,
    )
```

```python
# core/tools/builtin/skill.py
from core.skills.models import SkillEvent
from core.tools.context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    skill_id = args.get("skill", "").strip()
    if not skill_id:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, "Missing skill parameter")],
            status=ToolOutcomeStatus.FAILURE,
            error="missing_params",
        )

    state = context.session_state
    registry = context.skill_registry
    if state is None or registry is None:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, "Skill runtime unavailable")],
            status=ToolOutcomeStatus.FAILURE,
            error="runtime_unavailable",
        )

    if skill_id not in state.skill_catalog:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, f"Skill not found: {skill_id}")],
            status=ToolOutcomeStatus.FAILURE,
            error="not_found",
        )

    content = registry.load(skill_id)
    record = build_invoked_skill_record(state=state, skill_id=skill_id, content=content, turn=context.turn_count)
    return ToolInvocationOutcome(
        messages=[make_tool_message(context, f"Skill loaded: {skill_id}. Re-evaluate your next action using the injected skill guidance.")],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.INVOKE_SKILL,
                payload={"skill_id": skill_id, "record": record},
            ),
            SessionUpdate(
                kind=SessionUpdateKind.APPEND_SKILL_EVENT,
                payload={
                    "event": SkillEvent(
                        skill_id=skill_id,
                        action="activated",
                        source="model_tool_call",
                        conversation_index=-1,
                    ),
                },
            ),
        ],
        status=ToolOutcomeStatus.SUCCESS,
    )
```

```python
# core/tools/builtin/todo.py
from core.tools.context import RunUpdate, RunUpdateKind, SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, make_tool_message


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    if context.session_state is None:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, "No session state available")],
            status=ToolOutcomeStatus.FAILURE,
            error="no_state",
        )
    if not isinstance(args, dict):
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, "参数错误: args 必须是对象")],
            status=ToolOutcomeStatus.FAILURE,
            error="validation_failed",
        )

    items_data = args.get("items")
    valid, error = _validate_items(items_data)
    if not valid:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, f"参数错误: {error}")],
            status=ToolOutcomeStatus.FAILURE,
            error="validation_failed",
        )

    items = [
        TodoItem(
            content=item["content"].strip(),
            active_form=item["active_form"].strip(),
            status=item["status"],
            workflow_ref=(item.get("workflow_ref") or None),
        )
        for item in items_data
    ]
    rendered_items = [] if items and all(item.status == "completed" for item in items) else items
    return ToolInvocationOutcome(
        messages=[make_tool_message(context, _render_progress(rendered_items))],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.SET_TODO_ITEMS,
                payload={"items": items, "last_write_turn": context.turn_count},
            ),
        ],
        run_updates=[
            RunUpdate(
                kind=RunUpdateKind.RESET_TODO_TURN_COUNTER,
                payload={},
            ),
        ],
        status=ToolOutcomeStatus.SUCCESS,
    )
```

- [ ] **Step 4: Re-run skill/todo tests**

Run: `pytest tests/session/test_skill_tool.py tests/test_todo_planning_policy.py -v`

Expected: PASS for skill/todo outcome behavior; stale-reminder tests may still need one more update after QueryLoop changes in Task 5.

- [ ] **Step 5: Commit the skill/todo migration**

```bash
git add core/tools/builtin/skill.py core/tools/builtin/todo.py core/skills/runtime.py tests/session/test_skill_tool.py tests/test_todo_planning_policy.py
git commit -m "refactor: move skill and todo onto session updates"
```

## Task 4: Convert File Tools And Generic Builtins To Outcome Messages

**Files:**
- Modify: `core/tools/builtin/read_file.py`
- Modify: `core/tools/builtin/write_file.py`
- Modify: `core/tools/builtin/edit_file.py`
- Modify: `core/tools/builtin/find.py`
- Modify: `core/tools/builtin/bash.py`
- Modify: `core/tools/context.py`
- Modify: `tests/session/test_file_runtime_state.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: Write failing tests for file-state updates**

```python
from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState
from core.tools.context import FileState, RunUpdateKind, SessionUpdateKind, ToolUseContext


def test_read_file_returns_upsert_file_state_update(tmp_path) -> None:
    from core.tools.builtin.read_file import handle

    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)
    ctx._set_call_identity(name="read_file", call_id="toolu_read", turn=1)

    outcome = handle({"path": str(file_path)}, ctx)

    assert outcome.session_updates[0].kind is SessionUpdateKind.UPSERT_FILE_STATE
    apply_session_update(state, outcome.session_updates[0])
    saved = state.read_file_state[str(file_path)]
    assert isinstance(saved, FileState)
    assert saved.content == "alpha\nbeta"


def test_write_file_returns_file_state_and_mark_modified_updates(tmp_path) -> None:
    from core.tools.builtin.write_file import handle

    state = SessionState(conversation_messages=[])
    run_state = RunState()
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)
    ctx._set_call_identity(name="write_file", call_id="toolu_write", turn=1)

    outcome = handle({"path": "out.txt", "content": "hello"}, ctx)

    assert [update.kind for update in outcome.session_updates] == [SessionUpdateKind.UPSERT_FILE_STATE]
    assert [update.kind for update in outcome.run_updates] == [RunUpdateKind.MARK_FILE_MODIFIED]

    apply_session_update(state, outcome.session_updates[0])
    apply_run_update(run_state, outcome.run_updates[0])

    assert str(tmp_path / "out.txt") in state.read_file_state
    assert str(tmp_path / "out.txt") in run_state.files_modified
```

- [ ] **Step 2: Run the file/runtime tests**

Run: `pytest tests/session/test_file_runtime_state.py tests/test_runtime_logging.py -v`

Expected: FAIL because file tools still write through `ToolUseContext.set_file_state`, `update_file_state`, and `mark_file_modified`, and `ToolUseContext.bind_runtime()` still aliases file cache directly into `SessionState`.

- [ ] **Step 3: Return explicit file updates and remove direct mutation helpers from tool code**

```python
# core/tools/builtin/read_file.py
from core.tools.context import FileState, SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, make_tool_message


return ToolInvocationOutcome(
    messages=[make_tool_message(context, output or "(空文件)")],
    session_updates=[
        SessionUpdate(
            kind=SessionUpdateKind.UPSERT_FILE_STATE,
            payload={
                "path": abs_path,
                "file_state": FileState(
                    content="\n".join(lines),
                    timestamp=file_path.stat().st_mtime,
                    offset=offset if offset > 1 else None,
                    limit=limit if limit < total_lines else None,
                ),
            },
        ),
    ],
    status=ToolOutcomeStatus.SUCCESS,
)
```

```python
# core/tools/builtin/write_file.py
from core.tools.context import RunUpdate, RunUpdateKind, SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, make_tool_message


return ToolInvocationOutcome(
    messages=[make_tool_message(context, f"{action} {file_path} ({lines} 行)")],
    session_updates=[
        SessionUpdate(
            kind=SessionUpdateKind.UPSERT_FILE_STATE,
            payload={"path": abs_path, "file_state": FileState(content=final_content, timestamp=file_path.stat().st_mtime)},
        ),
    ],
    run_updates=[RunUpdate(kind=RunUpdateKind.MARK_FILE_MODIFIED, payload={"path": abs_path})],
)
```

```python
# core/tools/builtin/edit_file.py
return ToolInvocationOutcome(
    messages=[make_tool_message(context, f"已替换 {replaced} 处匹配")],
    session_updates=[
        SessionUpdate(
            kind=SessionUpdateKind.UPSERT_FILE_STATE,
            payload={"path": abs_path, "file_state": FileState(content=new_content, timestamp=file_path.stat().st_mtime)},
        ),
    ],
    run_updates=[RunUpdate(kind=RunUpdateKind.MARK_FILE_MODIFIED, payload={"path": abs_path})],
)
```

```python
# core/tools/builtin/find.py and core/tools/builtin/bash.py
return ToolInvocationOutcome(
    messages=[make_tool_message(context, output)],
    status=ToolOutcomeStatus.SUCCESS,
)
```

```python
# core/tools/context.py
class ToolUseContext:
    def bind_runtime(self, *, session_state: Any | None = None, skill_registry: Any | None = None) -> None:
        if session_state is not None:
            self._session_state = session_state
        if skill_registry is not None:
            self._skill_registry = skill_registry

    def get_file_state(self, path: str) -> FileState | None:
        source = self._session_state.read_file_state if self._session_state is not None else self._file_state
        state = source.get(path)
        if state is None:
            return None
        try:
            if os.path.getmtime(path) != state.timestamp:
                return None
        except OSError:
            return None
        return state

    # delete: set_file_state, update_file_state, invalidate_file_state, mark_file_modified
```

- [ ] **Step 4: Re-run the file/runtime tests**

Run: `pytest tests/session/test_file_runtime_state.py tests/test_runtime_logging.py -v`

Expected: PASS with reducer-applied file-state assertions; no test should rely on `ToolUseContext` mutation helpers anymore.

- [ ] **Step 5: Commit the file-tool migration**

```bash
git add core/tools/builtin/read_file.py core/tools/builtin/write_file.py core/tools/builtin/edit_file.py core/tools/builtin/find.py core/tools/builtin/bash.py core/tools/context.py tests/session/test_file_runtime_state.py tests/test_runtime_logging.py
git commit -m "refactor: move file and shell tools onto explicit outcomes"
```

## Task 5: Rewrite QueryLoop Around Reducers And Transition Reasons

**Files:**
- Modify: `core/query/loop.py`
- Modify: `core/query/state.py`
- Modify: `core/query/recovery.py`
- Modify: `core/policy/todo_tracking.py`
- Modify: `tests/test_query_logging.py`
- Modify: `tests/test_runtime_control_plane.py`
- Modify: `tests/test_todo_planning_policy.py`

- [ ] **Step 1: Write failing tests for transition-owned control flow**

```python
from types import SimpleNamespace

from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.query.state import RunState
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import ModelInputView
from core.llm.response import ModelResponse
from core.query.reducers import TransitionReason
from core.tools.context import ToolInvocationOutcome
from core.tools.runtime import ToolBatchResult


class _FakeToolRuntime:
    def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
        return ToolBatchResult(
            messages=[{"role": "tool", "tool_call_id": "toolu_1", "content": "ok"}],
            tool_names=["read_file"],
            tool_statuses=[],
            session_updates=[],
            run_updates=[],
        )


def test_query_loop_marks_next_turn_after_tool_batch() -> None:
    class FakeViewBuilder:
        def build(self, state, **kwargs):
            return ModelInputView(system="SYSTEM", messages=list(state.conversation_messages), tools=None)

    class FakeModelGateway:
        def __init__(self) -> None:
            self._responses = [
                ModelResponse(content="", tool_calls=[{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}], finish_reason="tool_use"),
                ModelResponse(content="final answer", finish_reason="end_turn"),
            ]

        def call_once(self, messages, *, system="", tools=None):
            return self._responses.pop(0)

    class FakePolicyRunner:
        def before_model_call(self, session_state, run_state):
            return []

        def after_tool_batch(self, session_state, run_state, batch_result):
            return []

        def should_stop(self, session_state, run_state):
            return None

    class FakeRecovery:
        def handle(self, model_resp, run_state):
            return SimpleNamespace(should_continue=False, follow_up_messages=[])

    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=FakeModelGateway(),
        tool_runtime=_FakeToolRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert result.turns_used == 1
```

- [ ] **Step 2: Run the query-loop and policy tests**

Run: `pytest tests/test_query_logging.py tests/test_todo_planning_policy.py tests/test_runtime_control_plane.py -v`

Expected: FAIL because `QueryLoop` still expects `batch.tool_results`, still calls `_apply_batch_control_plane`, and `TodoPlanningPolicy` still depends on `todo_replan_required`.

- [ ] **Step 3: Remove barrier/todo bridge logic and drive the loop with reducers**

```python
# core/query/state.py
@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
    allowed_tools_override: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
    assistant_turns_since_todo: int = 0
    transition: TransitionReason | None = None
    last_displayed_todo_items: list["TodoItem"] | None = None
```

```python
# core/query/loop.py
from core.query.reducers import (
    TransitionReason,
    apply_run_update,
    apply_session_update,
    apply_transition,
    collect_runtime_maintenance_updates,
)


for update in collect_runtime_maintenance_updates(session_state):
    apply_session_update(session_state, update)

batch = tool_runtime.execute_batch(
    parsed_calls,
    run_state=state,
    apply_session_update=lambda update: apply_session_update(session_state, update),
    apply_run_update=lambda update: apply_run_update(state, update),
)
store.extend(batch.messages)
state.turn_count += 1
state.tool_calls_executed += len(parsed_calls)
apply_transition(state, TransitionReason.NEXT_TURN)
_render_todo_state_update(renderer, session_state, state, batch)

# files_modified now accumulate through RunUpdateKind.MARK_FILE_MODIFIED inside apply_run_update.
# usage_delta remains model-response-owned and is not part of ToolBatchResult in this refactor.

stop_reason = policy_runner.should_stop(session_state, state)
if stop_reason == "max_turns" and state.stop_reason != "max_turns":
    state.stop_reason = "max_turns"
    apply_transition(state, TransitionReason.MAX_TURNS_RECOVERY)
    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
    continue
```

```python
# core/query/loop.py
def _todo_write_succeeded(batch: ToolBatchResult) -> bool:
    return any(
        update.kind is SessionUpdateKind.SET_TODO_ITEMS
        for update in batch.session_updates
    )
```

```python
# core/query/recovery.py
from core.query.reducers import TransitionReason


@dataclass(slots=True)
class RecoveryDecision:
    should_continue: bool
    follow_up_messages: list[dict[str, str]] = field(default_factory=list)
    transition_reason: TransitionReason | None = None


class RecoveryManager:
    def handle(self, model_resp, state) -> RecoveryDecision:
        if model_resp.finish_reason == "length":
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请继续输出。"}],
                transition_reason=TransitionReason.MAX_TOKENS_RECOVERY,
            )
        if not model_resp.has_final_text:
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请直接给出最终答复。"}],
                transition_reason=TransitionReason.EMPTY_RESPONSE_RETRY,
            )
        return RecoveryDecision(should_continue=False)
```

```python
# core/policy/todo_tracking.py
class TodoPlanningPolicy:
    STALE_ASSISTANT_TURNS = 4

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        todo_state = session_state.todo_state
        if todo_state.items and run_state.assistant_turns_since_todo >= self.STALE_ASSISTANT_TURNS:
            snapshot = "\n".join(
                f"- [{item.status}] {item.content}" + (f" ({item.workflow_ref})" if item.workflow_ref else "")
                for item in todo_state.items
            )
            return [{
                "role": "user",
                "content": (
                    "<system-reminder type=\"todo_stale\">\n"
                    "当前计划可能已过时，请先刷新 todo。\n"
                    f"{snapshot}\n"
                    "</system-reminder>"
                ),
            }]
        return []
```

```python
# core/query/loop.py
decision = recovery.handle(model_resp, state)
if decision.should_continue:
    if decision.transition_reason is not None:
        apply_transition(state, decision.transition_reason)
    store.extend(decision.follow_up_messages)
    continue
```

- [ ] **Step 4: Re-run the query-loop and policy tests**

Run: `pytest tests/test_query_logging.py tests/test_todo_planning_policy.py tests/test_runtime_control_plane.py -v`

Expected: PASS with no references to `barrier_reason`, `todo_replan_required`, or `_apply_batch_control_plane`.

- [ ] **Step 5: Commit the QueryLoop control-plane rewrite**

```bash
git add core/query/loop.py core/query/state.py core/query/recovery.py core/policy/todo_tracking.py tests/test_query_logging.py tests/test_todo_planning_policy.py tests/test_runtime_control_plane.py
git commit -m "refactor: move query loop to reducer owned transitions"
```

## Task 6: Shrink Overlay, Update View Assembly, And Remove Dead APIs

**Files:**
- Modify: `core/prompt/assembler.py`
- Modify: `core/session/view_builder.py`
- Modify: `core/tools/builtin/todo.py`
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/session/test_file_runtime_state.py`

- [ ] **Step 1: Write failing prompt/view tests for the new runtime view**

```python
from core.prompt.assembler import PromptAssembler
from core.query.reducers import TransitionReason
from core.query.state import RunState
from core.session.state import SessionState


def test_build_query_overlay_is_empty_after_runtime_update_refactor(tmp_path) -> None:
    state = SessionState(conversation_messages=[])
    assembler = PromptAssembler()

    assert assembler.build_query_overlay(state, RunState()) == ""
    assert assembler.build_query_overlay(state, RunState(transition=TransitionReason.NEXT_TURN)) == ""


def test_build_internal_runtime_view_exposes_transition(tmp_path) -> None:
    state = SessionState(conversation_messages=[])
    assembler = PromptAssembler()

    result = assembler.build_internal_runtime_view(state, RunState(transition=TransitionReason.EMPTY_RESPONSE_RETRY))

    assert result["transition"] == "empty_response_retry"
    assert "barrier_reason" not in result
```

- [ ] **Step 2: Run the prompt/view tests**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_file_runtime_state.py -v`

Expected: FAIL because prompt assembler still renders `<todo-replan>` / `<barrier>` overlay and internal runtime view still exposes `barrier_reason`.

- [ ] **Step 3: Collapse overlay to a future-only hook and delete dead compatibility APIs**

```python
# core/prompt/assembler.py
def build_query_overlay(self, state: SessionState, run_state: RunState) -> str:
    return ""


def build_internal_runtime_view(self, state: SessionState, run_state: RunState) -> dict[str, object]:
    return {
        "invoked_skills": list(state.invoked_skills.keys()),
        "todo_items": [item.active_form for item in state.todo_state.items],
        "read_file_state": dict(state.read_file_state),
        "transition": run_state.transition.value if run_state.transition is not None else None,
    }
```

```python
# core/session/view_builder.py
system_parts = [
    prompt_assembler.build_stable_context(state, project_root=project_root),
    prompt_assembler.build_runtime_context(state, working_dir=working_dir),
    prompt_assembler.build_query_overlay(state, run_state),
]
```

```python
# core/tools/builtin/todo.py
# delete the unused compatibility layer:
# _latest_todo_state
# get_state()
```

- [ ] **Step 4: Re-run prompt/view tests**

Run: `pytest tests/session/test_prompt_assembler.py tests/session/test_file_runtime_state.py -v`

Expected: PASS with an empty overlay and `transition` visible in the internal runtime view.

- [ ] **Step 5: Commit the view-assembly cleanup**

```bash
git add core/prompt/assembler.py core/session/view_builder.py core/tools/builtin/todo.py tests/session/test_prompt_assembler.py tests/session/test_file_runtime_state.py
git commit -m "refactor: shrink overlay and remove barrier era compatibility"
```

## Task 7: Migrate Remaining Tests, Sweep Dead References, And Verify

**Files:**
- Modify: `tests/test_runtime_control_plane.py`
- Modify: `tests/test_runtime_logging.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/test_query_logging.py`
- Modify: `tests/test_todo_planning_policy.py`
- Modify: `core/session/state.py`
- Modify: `core/tools/context.py`

- [ ] **Step 1: Add a final safety net test sweep**

```python
def test_run_state_no_longer_exposes_barrier_bridge_fields() -> None:
    state = RunState()
    assert not hasattr(state, "barrier_reason")
    assert not hasattr(state, "todo_replan_required")
    assert not hasattr(state, "todo_replan_reason")


def test_runtime_control_plane_no_longer_skips_after_skill(tmp_path) -> None:
    from core.query.reducers import apply_session_update, apply_run_update
    from core.query.state import RunState
    from core.tools import ToolRegistry
    from core.tools.context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolUseContext, make_tool_message
    from core.tools.runtime import ToolCall, ToolExecutorRuntime

    class _SkillLikeTool:
        SCHEMA = {"name": "skill", "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}
        READONLY = False
        ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(
                messages=[make_tool_message(context, "skill loaded")],
                session_updates=[
                    SessionUpdate(
                        kind=SessionUpdateKind.APPEND_SKILL_EVENT,
                        payload={"event": {"skill_id": "analysis-report", "action": "activated"}},
                    ),
                ],
            )

    class _TodoLikeTool:
        SCHEMA = {"name": "todo", "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}
        READONLY = False
        ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(messages=[make_tool_message(context, "todo updated")])

    reg = ToolRegistry()
    reg.register(_SkillLikeTool)
    reg.register(_TodoLikeTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)
    session_state = SessionState(conversation_messages=[])
    run_state = RunState()

    batch = runtime.execute_batch(
        [
            ToolCall(idx=0, name="skill", call_id="toolu_skill", args={}),
            ToolCall(idx=1, name="todo", call_id="toolu_todo", args={}),
        ],
        run_state=run_state,
        apply_session_update=lambda update: apply_session_update(session_state, update),
        apply_run_update=lambda update: apply_run_update(run_state, update),
    )

    assert [message["content"] for message in batch.messages] == ["skill loaded", "todo updated"]
```

- [ ] **Step 2: Run the focused compatibility sweep**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_logging.py tests/test_query_display.py tests/test_query_logging.py tests/test_todo_planning_policy.py tests/session/test_skill_tool.py tests/session/test_file_runtime_state.py tests/session/test_prompt_assembler.py -v`

Expected: PASS. Any failure at this point should indicate a missed old field name or an unported fake object.

- [ ] **Step 3: Sweep dead code and dead symbols**

```bash
rg -n "ExecutionBarrier|ContextPatch|ToolResult|barrier_reason|todo_replan_required|todo_replan_reason|get_state\\(" core tests
```

Expected: no matches in `core/` or `tests/`, except historical docs outside the implementation surface.

- [ ] **Step 4: Run the broader verification pass**

Run: `pytest -q`

Expected: full suite PASS. If the suite is too slow in CI-less local execution, at minimum keep the focused command from Step 2 green before proceeding.

- [ ] **Step 5: Commit the completed migration**

```bash
git add core tests
git commit -m "refactor: complete runtime update protocol migration"
```

## Self-Review

### Spec Coverage

- Unified tool outcome protocol: covered in Task 1 and Task 2.
- Structured `SessionUpdate` / `RunUpdate`: covered in Task 1 and exercised again in Task 3 and Task 4.
- Serial immediate application and parallel ordered merge: covered in Task 2.
- Skill no longer uses barrier and no skipped results remain: covered in Task 3 and Task 7.
- `allowed_tools_override` narrowing remains intersect-only and security-relevant: covered in Task 1 and Task 2.
- Query-owned transitions and preservation/reset semantics: covered in Task 5.
- Overlay shrink and transcript/runtime-truth separation: covered in Task 6.
- Direct replacement and test migration with dead-code removal: covered in Task 6 and Task 7.

### Placeholder Scan

- No placeholder markers or code ellipsis remain in the tasks.
- Every test step now contains a runnable body or a concrete shell command.

### Type Consistency

- `ToolInvocationOutcome`, `SessionUpdate`, `RunUpdate`, and `TransitionReason` are introduced once in Task 1 and reused consistently in later tasks.
- `ToolBatchResult.messages` is the single batch message channel used from Task 2 onward.
- `RunState.transition` is added in Task 1 and becomes the only query transition field in Task 5 and Task 6.
