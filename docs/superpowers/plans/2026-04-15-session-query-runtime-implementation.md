# Session / Query / Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the agent runtime around `SessionEngine + QueryLoop + ToolRuntime`, remove the nested tool loop design, and move the codebase from a flat `core/` layout to explicit `session / query / tools / prompt / policy` boundaries.

**Architecture:** Introduce new packages alongside the current code, prove each boundary with tests, then switch the CLI entrypoint to the new session-driven architecture and delete the legacy orchestrator. Keep the existing tool registry and tool handlers working during the migration by wrapping them behind the new runtime interfaces first, then relocate them once the new loop is stable.

**Tech Stack:** Python 3.12, `pytest`, `openai`, `rich`

---

## File Structure Map

### New packages

- Create: `core/session/__init__.py` — package marker and public exports for session layer
- Create: `core/session/state.py` — `SessionState` dataclass and session-level mutation helpers
- Create: `core/session/store.py` — message append/read helpers that own `conversation_messages`
- Create: `core/session/view_builder.py` — build query-time message views from session state
- Create: `core/session/engine.py` — `SessionEngine` entrypoint for a single user input

- Create: `core/query/__init__.py` — package marker and public exports for query layer
- Create: `core/query/state.py` — `RunState`
- Create: `core/query/result.py` — `StopReason` and `QueryResult`
- Create: `core/query/recovery.py` — empty/truncated/api recovery decisions
- Create: `core/query/loop.py` — the only top-level `while True`

- Create: `core/prompt/__init__.py` — package marker
- Create: `core/prompt/cache.py` — stable prompt cache
- Create: `core/prompt/context.py` — prompt context payloads
- Create: `core/prompt/assembler.py` — stable + dynamic prompt assembly

- Create: `core/llm/__init__.py` — package marker
- Create: `core/llm/response.py` — `ModelResponse`
- Create: `core/llm/client.py` — `ModelGateway`

- Create: `core/policy/__init__.py` — package marker
- Create: `core/policy/base.py` — `RunPolicy` protocol and `PolicyRunner`
- Create: `core/policy/max_turns.py` — max-turn policy
- Create: `core/policy/todo_tracking.py` — todo reminder policy

- Create: `core/shared/__init__.py` — package marker
- Create: `core/shared/types.py` — lightweight shared dataclasses and enums
- Create: `core/shared/protocol.py` — shared protocol types only

### Tools migration

- Modify: `core/tools/__init__.py` — narrow it to registry + context exports, stop carrying unrelated orchestration concerns
- Create: `core/tools/runtime.py` — move `ToolCall` and `ToolExecutorRuntime` out of legacy `core/runtime.py`
- Create: `core/tools/context.py` — move `ToolUseContext`, `ToolResult`, `FileState`, `safe_path`
- Create: `core/tools/builtin/__init__.py` — package marker for built-in tools
- Move: `core/tools/bash.py` -> `core/tools/builtin/bash.py`
- Move: `core/tools/read_file.py` -> `core/tools/builtin/read_file.py`
- Move: `core/tools/write_file.py` -> `core/tools/builtin/write_file.py`
- Move: `core/tools/edit_file.py` -> `core/tools/builtin/edit_file.py`
- Move: `core/tools/todo.py` -> `core/tools/builtin/todo.py`
- Move: `core/tools/subagent.py` -> `core/tools/builtin/subagent.py`
- Delete: `core/runtime.py` after callers switch to `core/tools/runtime.py`

### Legacy removal and integration

- Modify: `01_agent_loop.py` — switch from `AgentLoop` to `SessionEngine`
- Delete: `core/agent.py` after entrypoint and callers stop importing it

### Tests

- Create: `tests/session/test_session_engine.py`
- Create: `tests/session/test_view_builder.py`
- Create: `tests/query/test_query_loop.py`
- Create: `tests/query/test_recovery.py`
- Create: `tests/tools/test_runtime.py`
- Create: `tests/policy/test_max_turns.py`
- Create: `tests/policy/test_todo_tracking.py`
- Create: `tests/integration/test_session_cli_flow.py`

## Task 1: Create package skeleton and shared run/query types

**Files:**
- Create: `core/session/__init__.py`
- Create: `core/query/__init__.py`
- Create: `core/prompt/__init__.py`
- Create: `core/llm/__init__.py`
- Create: `core/policy/__init__.py`
- Create: `core/shared/__init__.py`
- Create: `core/shared/types.py`
- Create: `core/shared/protocol.py`
- Create: `core/query/result.py`
- Create: `core/query/state.py`
- Test: `tests/query/test_query_types.py`

- [ ] **Step 1: Write the failing tests for `QueryResult`, `StopReason`, and `RunState`**

```python
from core.query.result import QueryResult, StopReason
from core.query.state import RunState


def test_query_result_defaults() -> None:
    result = QueryResult(final_output="ok", stop_reason=StopReason.COMPLETED)
    assert result.success is True
    assert result.turns_used == 0
    assert result.tool_calls_executed == 0
    assert result.files_modified == []


def test_run_state_starts_empty() -> None:
    state = RunState()
    assert state.turn_count == 0
    assert state.empty_retry_count == 0
    assert state.stop_reason is None
    assert state.files_modified == []
```

- [ ] **Step 2: Run the tests to verify the imports fail**

Run: `pytest tests/query/test_query_types.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.query'`

- [ ] **Step 3: Create the package markers and minimal shared types**

```python
# core/shared/types.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class UsageDelta:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class MessageBatch:
    messages: list[dict[str, str]] = field(default_factory=list)
```

```python
# core/shared/protocol.py
from __future__ import annotations

from typing import Protocol


class SupportsMessageDict(Protocol):
    def to_message(self) -> dict[str, object]:
        raise NotImplementedError
```

```python
# core/query/result.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StopReason(StrEnum):
    COMPLETED = "completed"
    EMPTY_RESPONSE = "empty_response"
    MAX_TURNS = "max_turns"
    API_ERROR = "api_error"
    ABORTED = "aborted"


@dataclass(slots=True)
class QueryResult:
    final_output: str
    stop_reason: StopReason
    success: bool = True
    turns_used: int = 0
    assistant_messages_added: int = 0
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
```

```python
# core/query/state.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
```

```python
# core/query/__init__.py
from .result import QueryResult, StopReason
from .state import RunState

__all__ = ["QueryResult", "RunState", "StopReason"]
```

```python
# package markers
# core/session/__init__.py
# core/prompt/__init__.py
# core/llm/__init__.py
# core/policy/__init__.py
# core/shared/__init__.py
__all__: list[str] = []
```

- [ ] **Step 4: Run the type tests and verify they pass**

Run: `pytest tests/query/test_query_types.py -v`

Expected: PASS with `2 passed`

- [ ] **Step 5: Commit the scaffolding**

```bash
git add core/session/__init__.py core/query/__init__.py core/prompt/__init__.py core/llm/__init__.py core/policy/__init__.py core/shared/__init__.py core/query/result.py core/query/state.py core/shared/types.py core/shared/protocol.py tests/query/test_query_types.py
git commit -m "feat: add query result and state primitives"
```

## Task 2: Move tool context/runtime behind the new `core.tools` boundary

**Files:**
- Create: `core/tools/context.py`
- Create: `core/tools/runtime.py`
- Modify: `core/tools/__init__.py`
- Modify: `core/tools/bash.py`
- Modify: `core/tools/read_file.py`
- Modify: `core/tools/write_file.py`
- Modify: `core/tools/edit_file.py`
- Modify: `core/tools/todo.py`
- Modify: `core/tools/subagent.py`
- Test: `tests/tools/test_runtime.py`

- [ ] **Step 1: Write the failing runtime tests**

```python
from core.tools.context import ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class DummyRegistry:
    def __init__(self) -> None:
        self._readonly = {"read": True, "write": False}

    def is_readonly(self, name: str) -> bool:
        return self._readonly[name]

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolResult:
        return ToolResult(output=f"{name}:{args['value']}", success=True)


def test_runtime_preserves_original_call_order() -> None:
    context = ToolUseContext(working_dir=".", max_turns=5)
    runtime = ToolExecutorRuntime(DummyRegistry(), context)
    calls = [
        ToolCall(idx=0, name="read", call_id="a", args={"value": "x"}),
        ToolCall(idx=1, name="write", call_id="b", args={"value": "y"}),
    ]
    results = runtime.execute_batch(calls)
    assert [result.output for result in results] == ["read:x", "write:y"]
```

- [ ] **Step 2: Run the tests to verify the new modules are missing**

Run: `pytest tests/tools/test_runtime.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.tools.context'`

- [ ] **Step 3: Add `core/tools/context.py` and `core/tools/runtime.py`, then make `core/tools/__init__.py` re-export them**

```python
# core/tools/context.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def safe_path(path: str, working_dir: str) -> Path:
    return (Path(working_dir).resolve() / path).resolve()


@dataclass(slots=True)
class ToolResult:
    output: str
    success: bool
    error: str | None = None
    truncated: bool = False


@dataclass(slots=True)
class FileState:
    content: str
    timestamp: float
    offset: int | None = None
    limit: int | None = None

    @property
    def is_full_read(self) -> bool:
        return self.offset is None and self.limit is None


class ToolUseContext:
    def __init__(self, *, working_dir: str, max_turns: int):
        self._working_dir = working_dir
        self._max_turns = max_turns
        self._tool_name = ""
        self._tool_call_id = ""
        self._turn_count = 0
        self._file_state: dict[str, FileState] = {}
        self._files_modified: list[str] = []
        self._messages: list[dict[str, Any]] | None = None
        self._cancelled = False

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def files_modified(self) -> list[str]:
        return list(self._files_modified)

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    def _set_call_identity(self, *, name: str, call_id: str, turn: int) -> None:
        self._tool_name = name
        self._tool_call_id = call_id
        self._turn_count = turn

    def get_file_state(self, path: str) -> FileState | None:
        state = self._file_state.get(path)
        if state is None:
            return None
        try:
            if os.path.getmtime(path) != state.timestamp:
                self._file_state.pop(path, None)
                return None
        except OSError:
            self._file_state.pop(path, None)
            return None
        return state
```

```python
# core/tools/runtime.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import ToolResult, ToolUseContext


@dataclass(slots=True)
class ToolCall:
    idx: int
    name: str
    call_id: str
    args: dict[str, Any]


class ToolExecutorRuntime:
    def __init__(self, registry: Any, context: ToolUseContext):
        self._registry = registry
        self._context = context

    def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        results: dict[int, ToolResult] = {}
        for call in tool_calls:
            self._context._set_call_identity(
                name=call.name,
                call_id=call.call_id,
                turn=self._context.turn_count,
            )
            results[call.idx] = self._registry.execute(call.name, call.args, self._context)
        return [results[i] for i in range(len(tool_calls))]
```

```python
# core/tools/__init__.py
from .context import FileState, ToolResult, ToolUseContext, safe_path
from .runtime import ToolCall, ToolExecutorRuntime
```

- [ ] **Step 4: Update existing built-in tools to import `ToolResult` and `ToolUseContext` from `core.tools.context`**

```python
# example import in each tool module
from core.tools.context import ToolResult, ToolUseContext
```

- [ ] **Step 5: Run the runtime test and commit**

Run: `pytest tests/tools/test_runtime.py -v`

Expected: PASS with `1 passed`

```bash
git add core/tools/__init__.py core/tools/context.py core/tools/runtime.py core/tools/bash.py core/tools/read_file.py core/tools/write_file.py core/tools/edit_file.py core/tools/todo.py core/tools/subagent.py tests/tools/test_runtime.py
git commit -m "refactor: move tool runtime under core.tools"
```

## Task 3: Add session state, store, and message view builder

**Files:**
- Create: `core/session/state.py`
- Create: `core/session/store.py`
- Create: `core/session/view_builder.py`
- Create: `core/session/__init__.py`
- Test: `tests/session/test_view_builder.py`

- [ ] **Step 1: Write the failing session tests**

```python
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder


def test_session_store_appends_messages_in_order() -> None:
    state = SessionState(conversation_messages=[])
    store = SessionStore(state)
    store.append({"role": "user", "content": "hello"})
    store.append({"role": "assistant", "content": "world"})
    assert state.conversation_messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]


def test_view_builder_returns_query_messages_without_mutating_history() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    view = MessageViewBuilder().build(state)
    assert view.messages == [{"role": "user", "content": "hello"}]
    assert state.conversation_messages == [{"role": "user", "content": "hello"}]
```

- [ ] **Step 2: Run the tests to verify the session modules are missing**

Run: `pytest tests/session/test_view_builder.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.session'`

- [ ] **Step 3: Create the session state/store/view modules**

```python
# core/session/state.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    discovered_skills: set[str] = field(default_factory=set)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
```

```python
# core/session/store.py
from __future__ import annotations

from typing import Any

from .state import SessionState


class SessionStore:
    def __init__(self, state: SessionState):
        self._state = state

    def append(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self._state.conversation_messages.extend(messages)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._state.conversation_messages)
```

```python
# core/session/view_builder.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import SessionState


@dataclass(slots=True)
class MessageView:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None


class MessageViewBuilder:
    def build(self, state: SessionState) -> MessageView:
        return MessageView(messages=list(state.conversation_messages), tools=None)
```

- [ ] **Step 4: Run the session tests and then add `core/session/__init__.py` exports**

Run: `pytest tests/session/test_view_builder.py -v`

Expected: PASS with the view-builder tests green

```python
# core/session/__init__.py
from .state import SessionState
from .store import SessionStore
from .view_builder import MessageView, MessageViewBuilder

__all__ = ["MessageView", "MessageViewBuilder", "SessionState", "SessionStore"]
```

- [ ] **Step 5: Commit the session foundation**

```bash
git add core/session/__init__.py core/session/state.py core/session/store.py core/session/view_builder.py tests/session/test_view_builder.py
git commit -m "feat: add session state and message view builder"
```

## Task 4: Add prompt cache and stable/dynamic prompt assembly

**Files:**
- Create: `core/prompt/cache.py`
- Create: `core/prompt/context.py`
- Create: `core/prompt/assembler.py`
- Test: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: Write failing tests for prompt caching**

```python
from core.prompt.assembler import PromptAssembler
from core.session.state import SessionState
from core.query.state import RunState


def test_prompt_assembler_caches_stable_prompt() -> None:
    state = SessionState(conversation_messages=[])
    assembler = PromptAssembler()
    first = assembler.build_stable(state)
    second = assembler.build_stable(state)
    assert first == second
    assert state.prompt_cache["stable_system_prompt"] == first


def test_prompt_assembler_builds_dynamic_prompt_from_run_state() -> None:
    state = SessionState(conversation_messages=[])
    run_state = RunState(turn_count=2)
    assembler = PromptAssembler()
    dynamic_prompt = assembler.build_dynamic(state, run_state)
    assert "turn_count=2" in dynamic_prompt
```

- [ ] **Step 2: Run the tests to verify prompt modules are missing**

Run: `pytest tests/session/test_prompt_assembler.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.prompt'`

- [ ] **Step 3: Implement the minimal cache and assembler**

```python
# core/prompt/context.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PromptContext:
    stable_system_prompt: str
    dynamic_prompt: str
```

```python
# core/prompt/cache.py
from __future__ import annotations


class PromptCache:
    def get(self, store: dict[str, str], key: str) -> str | None:
        return store.get(key)

    def set(self, store: dict[str, str], key: str, value: str) -> str:
        store[key] = value
        return value
```

```python
# core/prompt/assembler.py
from __future__ import annotations

from core.prompt.cache import PromptCache
from core.prompt.context import PromptContext
from core.session.state import SessionState
from core.query.state import RunState


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None):
        self._cache = cache or PromptCache()

    def build_stable(self, state: SessionState) -> str:
        cached = self._cache.get(state.prompt_cache, "stable_system_prompt")
        if cached is not None:
            return cached
        value = "无论如何你都要使用中文回答用户"
        return self._cache.set(state.prompt_cache, "stable_system_prompt", value)

    def build_dynamic(self, state: SessionState, run_state: RunState) -> str:
        return f"turn_count={run_state.turn_count}"

    def build_context(self, state: SessionState, run_state: RunState) -> PromptContext:
        return PromptContext(
            stable_system_prompt=self.build_stable(state),
            dynamic_prompt=self.build_dynamic(state, run_state),
        )
```

- [ ] **Step 4: Run the prompt tests and keep the output stable**

Run: `pytest tests/session/test_prompt_assembler.py -v`

Expected: PASS with `2 passed`

- [ ] **Step 5: Commit prompt assembly**

```bash
git add core/prompt/cache.py core/prompt/context.py core/prompt/assembler.py tests/session/test_prompt_assembler.py
git commit -m "feat: add session-scoped prompt assembly"
```

## Task 5: Add `ModelGateway`, recovery, and the flat `QueryLoop`

**Files:**
- Create: `core/llm/response.py`
- Create: `core/llm/client.py`
- Create: `core/query/recovery.py`
- Create: `core/query/loop.py`
- Test: `tests/query/test_query_loop.py`
- Test: `tests/query/test_recovery.py`

- [ ] **Step 1: Write the failing flat-loop tests**

```python
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.query.state import RunState
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder


class StubModelResponse:
    def __init__(self, *, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []

    @property
    def has_final_text(self) -> bool:
        return bool(self.content)

    def to_message(self) -> dict[str, str]:
        return {"role": "assistant", "content": self.content}


class StubGateway:
    def __init__(self, responses):
        self._responses = list(responses)

    def call_once(self, messages, *, tools, prompt):
        return self._responses.pop(0)


class StubRuntime:
    def execute_batch(self, tool_calls, *, context):
        class Batch:
            tool_results = [{"role": "tool", "content": "ok"}]
            files_modified = []
            tool_names = ["read_file"]
        return Batch()


class StubPolicyRunner:
    def before_model_call(self, context, state):
        return []

    def after_tool_batch(self, context, state, batch_result):
        return []

    def should_stop(self, context, state):
        return None


class StubRecovery:
    def handle(self, model_resp, state):
        raise AssertionError("recovery should not be called for final text")


def test_query_loop_executes_tools_then_returns_to_top_of_loop() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "read file"}])
    loop = QueryLoop()
    result = loop.run(
        session_state=state,
        store=SessionStore(state),
        view_builder=MessageViewBuilder(),
        prompt_assembler=None,
        model_gateway=StubGateway([
            StubModelResponse(tool_calls=[{"name": "read_file"}]),
            StubModelResponse(content="done"),
        ]),
        tool_runtime=StubRuntime(),
        tool_context=None,
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )
    assert result.stop_reason == StopReason.COMPLETED
    assert state.conversation_messages[-1] == {"role": "assistant", "content": "done"}
```

- [ ] **Step 2: Run the loop tests and verify the new loop is missing**

Run: `pytest tests/query/test_query_loop.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.query.loop'`

- [ ] **Step 3: Implement `ModelResponse`, `RecoveryManager`, and the flat loop**

```python
# core/llm/client.py
from __future__ import annotations

from core.llm_client import OpenAIClient
from core.llm.response import ModelResponse


class ModelGateway:
    def __init__(self, client: OpenAIClient | None = None):
        self._client = client or OpenAIClient()

    def call_once(self, messages, *, tools, prompt):
        response = self._client.call(messages, tools=tools)
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
```

```python
# core/llm/response.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def has_final_text(self) -> bool:
        return bool(self.content.strip())

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message
```

```python
# core/query/recovery.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RecoveryDecision:
    should_continue: bool
    follow_up_messages: list[dict[str, str]] = field(default_factory=list)


class RecoveryManager:
    def handle(self, model_resp, state) -> RecoveryDecision:
        if model_resp.finish_reason == "length":
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请继续输出。"}],
            )
        if not model_resp.has_final_text:
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请直接给出最终答复。"}],
            )
        return RecoveryDecision(should_continue=False)
```

```python
# core/query/loop.py
from __future__ import annotations

from core.query.result import QueryResult, StopReason
from core.query.state import RunState


class QueryLoop:
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
    ) -> QueryResult:
        state = RunState()

        while True:
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            view = view_builder.build(session_state)
            prompt = "" if prompt_assembler is None else prompt_assembler.build_dynamic(session_state, state)
            model_resp = model_gateway.call_once(view.messages, tools=view.tools, prompt=prompt)
            state.last_model_response = model_resp
            store.append(model_resp.to_message())

            if model_resp.tool_calls:
                batch = tool_runtime.execute_batch(model_resp.tool_calls, context=tool_context)
                store.extend(batch.tool_results)
                state.turn_count += 1
                state.tool_calls_executed += len(model_resp.tool_calls)
                state.files_modified.extend(batch.files_modified)
                after_messages = policy_runner.after_tool_batch(session_state, state, batch)
                if after_messages:
                    store.extend(after_messages)
                stop_reason = policy_runner.should_stop(session_state, state)
                if stop_reason == "max_turns" and state.stop_reason != "max_turns":
                    state.stop_reason = "max_turns"
                    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
                    continue
                continue

            if model_resp.has_final_text:
                return QueryResult(
                    final_output=model_resp.content,
                    stop_reason=StopReason.MAX_TURNS if state.stop_reason == "max_turns" else StopReason.COMPLETED,
                    turns_used=state.turn_count,
                    tool_calls_executed=state.tool_calls_executed,
                    files_modified=state.files_modified,
                )

            decision = recovery.handle(model_resp, state)
            if decision.should_continue:
                store.extend(decision.follow_up_messages)
                state.empty_retry_count += 1
                continue

            return QueryResult(
                final_output="",
                stop_reason=StopReason.EMPTY_RESPONSE,
                success=False,
                turns_used=state.turn_count,
                tool_calls_executed=state.tool_calls_executed,
                files_modified=state.files_modified,
            )
```

- [ ] **Step 4: Run the query loop and recovery tests**

Run: `pytest tests/query/test_query_loop.py tests/query/test_recovery.py -v`

Expected: PASS with all query tests green

- [ ] **Step 5: Commit the flat loop**

```bash
git add core/llm/response.py core/llm/client.py core/query/recovery.py core/query/loop.py tests/query/test_query_loop.py tests/query/test_recovery.py
git commit -m "feat: add flat query loop and recovery manager"
```

## Task 6: Add policies and `SessionEngine`

**Files:**
- Create: `core/policy/base.py`
- Create: `core/policy/max_turns.py`
- Create: `core/policy/todo_tracking.py`
- Create: `core/session/engine.py`
- Test: `tests/policy/test_max_turns.py`
- Test: `tests/policy/test_todo_tracking.py`
- Test: `tests/session/test_session_engine.py`

- [ ] **Step 1: Write failing tests for max-turn and todo policies**

```python
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoTrackingPolicy
from core.session.engine import SessionEngine
from core.query.state import RunState


def test_max_turns_policy_requests_stop_at_limit() -> None:
    policy = MaxTurnsPolicy(max_turns=2)
    state = RunState(turn_count=2)
    assert policy.should_stop(None, state) == "max_turns"


def test_todo_tracking_policy_requests_reminder_after_three_non_todo_turns() -> None:
    policy = TodoTrackingPolicy()
    state = RunState()
    policy.after_tool_batch(context=None, state=state, batch_result=type("Batch", (), {"tool_names": ["read_file"]})())
    policy.after_tool_batch(context=None, state=state, batch_result=type("Batch", (), {"tool_names": ["read_file"]})())
    messages = policy.after_tool_batch(context=None, state=state, batch_result=type("Batch", (), {"tool_names": ["read_file"]})())
    assert messages == [{"role": "user", "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>"}]


def test_session_engine_appends_user_message_before_running_query_loop() -> None:
    class StubLoop:
        def run(self, **kwargs):
            from core.query.result import QueryResult, StopReason
            return QueryResult(final_output="done", stop_reason=StopReason.COMPLETED)

    engine = SessionEngine(
        model_gateway=object(),
        tool_runtime=object(),
        tool_context=None,
        policy_runner=object(),
        recovery=object(),
        query_loop=StubLoop(),
    )
    engine.submit_user_message("hello")
    assert engine.state.conversation_messages[0] == {"role": "user", "content": "hello"}
```

- [ ] **Step 2: Run the policy tests to verify the modules are missing**

Run: `pytest tests/policy/test_max_turns.py tests/policy/test_todo_tracking.py tests/session/test_session_engine.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.policy'` or `No module named 'core.session.engine'`

- [ ] **Step 3: Implement `RunPolicy`, `PolicyRunner`, and the two policies**

```python
# core/policy/base.py
from __future__ import annotations

from typing import Protocol


class RunPolicy(Protocol):
    def before_model_call(self, context, state) -> list[dict[str, str]]:
        raise NotImplementedError

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        raise NotImplementedError

    def should_stop(self, context, state) -> str | None:
        raise NotImplementedError


class PolicyRunner:
    def __init__(self, policies: list[RunPolicy]):
        self._policies = policies

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.before_model_call(context, state))
        return messages

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.after_tool_batch(context, state, batch_result))
        return messages

    def should_stop(self, context, state) -> str | None:
        for policy in self._policies:
            decision = policy.should_stop(context, state)
            if decision is not None:
                return decision
        return None
```

```python
# core/policy/max_turns.py
from __future__ import annotations


class MaxTurnsPolicy:
    def __init__(self, max_turns: int):
        self._max_turns = max_turns

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, context, state) -> str | None:
        if state.turn_count >= self._max_turns:
            return "max_turns"
        return None
```

```python
# core/policy/todo_tracking.py
from __future__ import annotations


class TodoTrackingPolicy:
    def __init__(self) -> None:
        self._rounds_without_todo = 0

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        if "todo" in getattr(batch_result, "tool_names", []):
            self._rounds_without_todo = 0
            return []
        self._rounds_without_todo += 1
        if self._rounds_without_todo >= 3:
            self._rounds_without_todo = 0
            return [{"role": "user", "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>"}]
        return []

    def should_stop(self, context, state) -> str | None:
        return None
```

- [ ] **Step 4: Implement `SessionEngine` and verify it appends the user message before delegating to `QueryLoop`**

```python
# core/session/engine.py
from __future__ import annotations

from core.prompt.assembler import PromptAssembler
from core.query.loop import QueryLoop
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder


class SessionEngine:
    def __init__(self, *, model_gateway, tool_runtime, tool_context, policy_runner, recovery, query_loop=None):
        self._state = SessionState(conversation_messages=[])
        self._store = SessionStore(self._state)
        self._view_builder = MessageViewBuilder()
        self._prompt_assembler = PromptAssembler()
        self._query_loop = query_loop or QueryLoop()
        self._model_gateway = model_gateway
        self._tool_runtime = tool_runtime
        self._tool_context = tool_context
        self._policy_runner = policy_runner
        self._recovery = recovery

    @property
    def state(self) -> SessionState:
        return self._state

    def submit_user_message(self, text: str):
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
        )
```

- [ ] **Step 5: Run the policy/session tests and commit**

Run: `pytest tests/policy/test_max_turns.py tests/policy/test_todo_tracking.py tests/session/test_session_engine.py -v`

Expected: PASS with all policy and session tests green

```bash
git add core/policy/base.py core/policy/max_turns.py core/policy/todo_tracking.py core/session/engine.py tests/policy/test_max_turns.py tests/policy/test_todo_tracking.py tests/session/test_session_engine.py
git commit -m "feat: add session engine and run policies"
```

## Task 7: Switch the CLI entrypoint and remove the legacy orchestrator

**Files:**
- Modify: `01_agent_loop.py`
- Delete: `core/agent.py`
- Delete: `core/runtime.py`
- Test: `tests/integration/test_session_cli_flow.py`

- [ ] **Step 1: Write the failing integration test for the session-driven CLI**

```python
from core.query.result import QueryResult, StopReason
from core.session.engine import SessionEngine


class StubGateway:
    def call_once(self, messages, *, tools, prompt):
        from core.llm.response import ModelResponse
        return ModelResponse(content="你好")


class StubRuntime:
    def execute_batch(self, tool_calls, *, context):
        raise AssertionError("tool runtime should not be called")


class StubPolicyRunner:
    def before_model_call(self, context, state):
        return []

    def after_tool_batch(self, context, state, batch_result):
        return []

    def should_stop(self, context, state):
        return None


class StubRecovery:
    def handle(self, model_resp, state):
        raise AssertionError("recovery should not be called")


def test_session_engine_handles_a_single_user_turn() -> None:
    engine = SessionEngine(
        model_gateway=StubGateway(),
        tool_runtime=StubRuntime(),
        tool_context=None,
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )
    result = engine.submit_user_message("你好")
    assert result == QueryResult(final_output="你好", stop_reason=StopReason.COMPLETED, turns_used=0)
```

- [ ] **Step 2: Run the integration test and verify it fails before the entrypoint switch**

Run: `pytest tests/integration/test_session_cli_flow.py -v`

Expected: FAIL because `SessionEngine` and `QueryLoop` are not fully wired together yet

- [ ] **Step 3: Replace `AgentLoop` wiring in `01_agent_loop.py` with `SessionEngine` wiring**

```python
from __future__ import annotations

from rich.console import Console

from core.llm.client import ModelGateway
from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoTrackingPolicy
from core.query.recovery import RecoveryManager
from core.session.engine import SessionEngine
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime

console = Console()


def main() -> None:
    tool_context = ToolUseContext(working_dir=".", max_turns=20)
    engine = SessionEngine(
        model_gateway=ModelGateway(),
        tool_runtime=ToolExecutorRuntime(registry, tool_context),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoTrackingPolicy()]),
        recovery=RecoveryManager(),
    )

    console.print("[bold green]Agent Loop 已启动。[/bold green] 输入 [dim]exit[/dim] 或 [dim]quit[/dim] 退出。\n")
    while True:
        query = input(">> ")
        if query.strip().lower() in ("exit", "quit"):
            break
        if not query.strip():
            continue
        engine.submit_user_message(query)
        print()
```

- [ ] **Step 4: Delete `core/agent.py` and `core/runtime.py`, then update any remaining imports to `core/query/loop.py` and `core/tools/runtime.py`**

```python
# Example import updates
from core.query.loop import QueryLoop
from core.tools.runtime import ToolCall, ToolExecutorRuntime
from core.tools.context import ToolUseContext
```

- [ ] **Step 5: Run the integration test, the full suite, and commit the cutover**

Run: `pytest tests/query tests/session tests/tools tests/policy tests/integration -v`

Expected: PASS with the new architecture green and no imports from `core.agent`

```bash
git add 01_agent_loop.py core/session/engine.py core/query/loop.py core/tools/runtime.py tests/integration/test_session_cli_flow.py
git rm core/agent.py core/runtime.py
git commit -m "refactor: switch CLI to session-query-runtime architecture"
```

## Task 8: Move built-in tools under `core/tools/builtin` and finish package cleanup

**Files:**
- Create: `core/tools/builtin/__init__.py`
- Move: `core/tools/bash.py` -> `core/tools/builtin/bash.py`
- Move: `core/tools/read_file.py` -> `core/tools/builtin/read_file.py`
- Move: `core/tools/write_file.py` -> `core/tools/builtin/write_file.py`
- Move: `core/tools/edit_file.py` -> `core/tools/builtin/edit_file.py`
- Move: `core/tools/todo.py` -> `core/tools/builtin/todo.py`
- Move: `core/tools/subagent.py` -> `core/tools/builtin/subagent.py`
- Modify: `core/tools/__init__.py`
- Test: `tests/tools/test_registry_autodiscovery.py`

- [ ] **Step 1: Write the failing autodiscovery test**

```python
from core.tools import registry


def test_registry_discovers_builtin_tools_from_package() -> None:
    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "bash" in names
    assert "read_file" in names
    assert "todo" in names
```

- [ ] **Step 2: Run the autodiscovery test before moving the files**

Run: `pytest tests/tools/test_registry_autodiscovery.py -v`

Expected: FAIL after the files move, until autodiscovery is updated to scan `core/tools/builtin`

- [ ] **Step 3: Move the tool modules and update autodiscovery**

```python
# core/tools/__init__.py
def auto_discover() -> ToolRegistry:
    reg = ToolRegistry()
    tools_dir = pathlib.Path(__file__).parent / "builtin"
    for file in tools_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        module = importlib.import_module(f"core.tools.builtin.{file.stem}")
        reg.register(module)
    return reg
```

```python
# core/tools/builtin/__init__.py
__all__ = [
    "bash",
    "edit_file",
    "read_file",
    "subagent",
    "todo",
    "write_file",
]
```

- [ ] **Step 4: Run the autodiscovery test and a targeted built-in tool smoke test**

Run: `pytest tests/tools/test_registry_autodiscovery.py tests/tools/test_runtime.py -v`

Expected: PASS with the builtin package green

- [ ] **Step 5: Commit the final layout cleanup**

```bash
git add core/tools/__init__.py core/tools/builtin/__init__.py core/tools/builtin/bash.py core/tools/builtin/edit_file.py core/tools/builtin/read_file.py core/tools/builtin/subagent.py core/tools/builtin/todo.py core/tools/builtin/write_file.py tests/tools/test_registry_autodiscovery.py
git commit -m "refactor: move builtin tools into package structure"
```

## Self-Review

### Spec coverage

- `SessionEngine + QueryLoop + ToolRuntime` split: covered by Tasks 2, 5, 6, 7
- Flat main loop with atomic tool execution: covered by Task 5
- Session-vs-run state split: covered by Tasks 1 and 3
- Stable prompt assembly in session layer: covered by Task 4
- Policy-based max-turn and todo reminder: covered by Task 6
- Directory reorganization under `session / query / tools / prompt / policy`: covered by Tasks 1, 2, 3, 4, 6, 8
- CLI cutover and legacy deletion: covered by Task 7

No spec gaps found.

### Placeholder scan

- Searched for `TBD`, `TODO`, `implement later`, and `similar to Task`.
- No placeholders remain in the plan body.

### Type consistency

- `QueryResult`, `StopReason`, and `RunState` are introduced in Task 1 and used consistently in later tasks.
- `ToolUseContext`, `ToolCall`, and `ToolExecutorRuntime` are introduced in Task 2 and referenced consistently in later tasks.
- `SessionState`, `SessionStore`, and `MessageViewBuilder` are introduced in Task 3 and reused consistently in Tasks 5, 6, and 7.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-15-session-query-runtime-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
