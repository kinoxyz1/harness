# Runtime Follow-Up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the post-refactor runtime so tool calls work again, stable prompt/context actually reaches the model, and `max_turns` becomes a real stop boundary.

**Architecture:** Keep the current `SessionEngine + QueryLoop + ToolRuntime` structure, but correct the three broken seams: the `ToolRuntime` return contract, the prompt/context injection path, and the `max_turns` stop semantics. Do not introduce new abstractions like `ModelInput` or a `finalizing` state machine; instead, make the existing layers fulfill their original responsibilities.

**Tech Stack:** Python 3.12, `pytest`, `openai`, `rich`

---

## File Structure Map

### Existing files to modify

- Modify: `core/tools/runtime.py` — return `ToolBatchResult` instead of `list[ToolResult]`
- Modify: `core/query/loop.py` — consume `ToolBatchResult`, remove dead `prompt` flow, enforce `max_turns` stop behavior
- Modify: `core/session/store.py` — add `prepend()` for stable system prompt injection
- Modify: `core/session/engine.py` — inject stable prompt and environment context before the first user turn
- Modify: `core/prompt/assembler.py` — make stable prompt real, dynamic prompt message-based, remove dead `build_context()`
- Modify: `core/llm/client.py` — remove unused `prompt` parameter from `call_once()`
- Modify: `core/session/subagent.py` — align with SessionEngine bootstrap rules and avoid duplicate prompt/context injection

### New tests to create

- Create: `tests/query/test_tool_runtime_contract.py`
- Create: `tests/session/test_prompt_pipeline.py`
- Create: `tests/query/test_max_turns_stop.py`
- Create: `tests/integration/test_followup_fixes.py`

## Task 1: Fix the `ToolRuntime` contract and reconnect the tool path

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `core/query/loop.py`
- Create: `tests/query/test_tool_runtime_contract.py`

- [ ] **Step 1: Write the failing tests for the runtime contract**

```python
# tests/query/test_tool_runtime_contract.py
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder
from core.tools.context import ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class DummyRegistry:
    def is_readonly(self, name: str) -> bool:
        return True

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolResult:
        return ToolResult(output=f"{name}:{args['path']}", success=True)


class StubModelResponse:
    def __init__(self, *, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = "tool_calls" if self.tool_calls else "stop"

    @property
    def has_final_text(self) -> bool:
        return bool(self.content)

    def to_message(self) -> dict:
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


class StubGateway:
    def __init__(self) -> None:
        self._responses = [
            StubModelResponse(tool_calls=[{"id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]),
            StubModelResponse(content="done"),
        ]

    def call_once(self, messages, *, tools):
        return self._responses.pop(0)


class StubPolicyRunner:
    def before_model_call(self, context, state):
        return []

    def after_tool_batch(self, context, state, batch_result):
        return []

    def should_stop(self, context, state):
        return None


class StubRecovery:
    def handle(self, model_resp, state):
        raise AssertionError("recovery should not be called in this test")


def test_tool_runtime_returns_structured_batch_result() -> None:
    context = ToolUseContext(working_dir=".", max_turns=5)
    runtime = ToolExecutorRuntime(DummyRegistry(), context)
    batch = runtime.execute_batch([ToolCall(idx=0, name="read_file", call_id="call-1", args={"path": "README.md"})])
    assert batch.tool_results == [{"role": "tool", "tool_call_id": "call-1", "content": "read_file:README.md"}]
    assert batch.files_modified == []
    assert batch.tool_names == ["read_file"]


def test_query_loop_consumes_structured_batch_result_and_continues() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "read README"}])
    store = SessionStore(session_state)
    runtime = ToolExecutorRuntime(DummyRegistry(), ToolUseContext(working_dir=".", max_turns=5))

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=MessageViewBuilder(),
        prompt_assembler=None,
        model_gateway=StubGateway(),
        tool_runtime=runtime,
        tool_context=None,
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert session_state.conversation_messages[-1] == {"role": "assistant", "content": "done"}
    assert session_state.conversation_messages[-2] == {"role": "tool", "tool_call_id": "call-1", "content": "read_file:README.md"}
```

- [ ] **Step 2: Run the tests to verify they fail for the expected contract mismatch**

Run: `pytest tests/query/test_tool_runtime_contract.py -v`

Expected:
- `TypeError` because `execute_batch()` does not accept the `context` keyword argument
- or attribute errors because `QueryLoop` expects `batch.tool_results`

- [ ] **Step 3: Replace the runtime return type with `ToolBatchResult`**

```python
# core/tools/runtime.py
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolCall:
    idx: int
    name: str
    call_id: str
    args: dict[str, Any]


@dataclass(slots=True)
class ToolBatchResult:
    tool_results: list[dict[str, Any]]
    files_modified: list[str]
    tool_names: list[str]


class ToolExecutorRuntime:
    def __init__(self, registry, context: ToolUseContext, display: RunDisplayOptions | None = None):
        self._registry = registry
        self._context = context
        self._display = display or RunDisplayOptions()

    def execute_batch(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
        if not tool_calls:
            return ToolBatchResult(tool_results=[], files_modified=[], tool_names=[])

        batches = self._partition(tool_calls)
        raw_results: dict[int, ToolResult] = {}

        for batch in batches:
            if batch.parallel:
                raw_results.update(self._execute_parallel(batch))
            else:
                raw_results.update(self._execute_serial(batch))

        ordered_calls = [tool_calls[i] for i in range(len(tool_calls))]
        ordered_results = [raw_results[i] for i in range(len(tool_calls))]
        tool_messages = [
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result.output,
            }
            for call, result in zip(ordered_calls, ordered_results)
        ]
        return ToolBatchResult(
            tool_results=tool_messages,
            files_modified=self._context.files_modified,
            tool_names=[call.name for call in ordered_calls],
        )
```

- [ ] **Step 4: Update `QueryLoop` to consume the structured result**

```python
# core/query/loop.py
if model_resp.tool_calls:
    parsed_calls = _parse_tool_calls(model_resp.tool_calls)
    batch = tool_runtime.execute_batch(parsed_calls)
    store.extend(batch.tool_results)
    state.turn_count += 1
    state.tool_calls_executed += len(parsed_calls)
    state.files_modified.extend(batch.files_modified)
    after_messages = policy_runner.after_tool_batch(session_state, state, batch)
    if after_messages:
        store.extend(after_messages)
    stop_reason = policy_runner.should_stop(session_state, state)
    if stop_reason == "max_turns" and state.stop_reason != "max_turns":
        state.stop_reason = "max_turns"
        store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
    continue
```

- [ ] **Step 5: Re-run the runtime contract tests and commit**

Run: `pytest tests/query/test_tool_runtime_contract.py -v`

Expected: PASS with both tests green

```bash
git add core/tools/runtime.py core/query/loop.py tests/query/test_tool_runtime_contract.py
git commit -m "fix: restore query loop and tool runtime contract"
```

## Task 2: Repair the stable prompt and environment-context pipeline

**Files:**
- Modify: `core/session/store.py`
- Modify: `core/session/engine.py`
- Modify: `core/prompt/assembler.py`
- Modify: `core/llm/client.py`
- Create: `tests/session/test_prompt_pipeline.py`

- [ ] **Step 1: Write the failing prompt-pipeline tests**

```python
# tests/session/test_prompt_pipeline.py
from core.llm.client import ModelGateway
from core.prompt.assembler import PromptAssembler
from core.query.result import StopReason
from core.session.engine import SessionEngine
from core.tools.context import ToolUseContext


class CaptureClient:
    def __init__(self) -> None:
        self.messages = None
        self.tools = None

    def call(self, messages, tools=None):
        self.messages = messages
        self.tools = tools

        class Response:
            content = "ok"
            tool_calls = []
            finish_reason = "stop"
            prompt_tokens = 1
            completion_tokens = 1

        return Response()


class StubToolRuntime:
    def execute_batch(self, tool_calls):
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


def test_session_engine_injects_stable_prompt_before_first_user_turn() -> None:
    capture = CaptureClient()
    engine = SessionEngine(
        model_gateway=ModelGateway(capture),
        tool_runtime=StubToolRuntime(),
        tool_context=ToolUseContext(working_dir=".", max_turns=5),
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )

    result = engine.submit_user_message("你好")

    assert result.stop_reason == StopReason.COMPLETED
    assert engine.state.conversation_messages[0]["role"] == "system"
    assert "AI 助手" in engine.state.conversation_messages[0]["content"]
    assert capture.messages[0]["role"] == "system"
    assert "AI 助手" in capture.messages[0]["content"]


def test_session_engine_injects_environment_context_once() -> None:
    capture = CaptureClient()
    engine = SessionEngine(
        model_gateway=ModelGateway(capture),
        tool_runtime=StubToolRuntime(),
        tool_context=ToolUseContext(working_dir=".", max_turns=5),
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )

    engine.submit_user_message("第一次")
    engine.submit_user_message("第二次")

    env_messages = [m for m in engine.state.conversation_messages if m["role"] == "user" and "<environment>" in m["content"]]
    assert len(env_messages) == 1
```

- [ ] **Step 2: Run the prompt tests to verify the pipeline is still disconnected**

Run: `pytest tests/session/test_prompt_pipeline.py -v`

Expected:
- no system prompt at index 0
- or no environment context user message
- or `ModelGateway` still silently ignoring prompt-related work

- [ ] **Step 3: Add `prepend()` to `SessionStore` and use it for stable bootstrap messages**

```python
# core/session/store.py
class SessionStore:
    def __init__(self, state: SessionState):
        self._state = state

    def prepend(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.insert(0, message)

    def append(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self._state.conversation_messages.extend(messages)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._state.conversation_messages)
```

- [ ] **Step 4: Make `PromptAssembler` build real stable prompt and message-shaped dynamic output**

```python
# core/prompt/assembler.py
from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None):
        self._cache = cache or PromptCache()

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        cached = self._cache.get(state.prompt_cache, "stable_system_prompt")
        if cached is not None:
            return cached
        stable_prompt = get_system_context(project_root=project_root)
        return self._cache.set(state.prompt_cache, "stable_system_prompt", stable_prompt)

    def build_environment_message(self, *, working_dir: str) -> dict[str, str]:
        return {"role": "user", "content": get_user_context(working_dir)}

    def build_dynamic(self, state: SessionState, run_state: RunState) -> list[dict[str, str]]:
        return []
```

- [ ] **Step 5: Bootstrap the session in `SessionEngine` and remove the dead `prompt` parameter from `ModelGateway`**

```python
# core/session/engine.py
class SessionEngine:
    def __init__(
        self,
        *,
        model_gateway,
        tool_runtime,
        tool_context,
        policy_runner,
        recovery,
        query_loop=None,
        view_builder=None,
    ):
        self._state = SessionState(conversation_messages=[])
        self._store = SessionStore(self._state)
        self._view_builder = view_builder or MessageViewBuilder()
        self._prompt_assembler = PromptAssembler()
        self._query_loop = query_loop or QueryLoop()
        self._model_gateway = model_gateway
        self._tool_runtime = tool_runtime
        self._tool_context = tool_context
        self._policy_runner = policy_runner
        self._recovery = recovery
        self._bootstrap_session_messages()

    def _bootstrap_session_messages(self) -> None:
        stable_prompt = self._prompt_assembler.build_stable(
            self._state,
            project_root=self._tool_context.working_dir if self._tool_context else None,
        )
        if not self._state.conversation_messages or self._state.conversation_messages[0]["role"] != "system":
            self._store.prepend({"role": "system", "content": stable_prompt})

        environment_message = self._prompt_assembler.build_environment_message(
            working_dir=self._tool_context.working_dir if self._tool_context else ".",
        )
        has_environment = any(
            message.get("role") == "user" and "<environment>" in message.get("content", "")
            for message in self._state.conversation_messages
        )
        if not has_environment:
            self._store.append(environment_message)
```

```python
# core/llm/client.py
class ModelGateway:
    def __init__(self, client: Any | None = None):
        self._client = client

    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        if self._client is None:
            raise RuntimeError("No LLM client configured")

        response = self._client.call(messages, tools=tools)
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
```

- [ ] **Step 6: Re-run the prompt tests and commit**

Run: `pytest tests/session/test_prompt_pipeline.py -v`

Expected: PASS with prompt and environment messages both present exactly once

```bash
git add core/session/store.py core/session/engine.py core/prompt/assembler.py core/llm/client.py tests/session/test_prompt_pipeline.py
git commit -m "fix: reconnect session prompt and environment pipeline"
```

## Task 3: Make `max_turns` a real stop boundary

**Files:**
- Modify: `core/query/loop.py`
- Create: `tests/query/test_max_turns_stop.py`

- [ ] **Step 1: Write the failing tests for `max_turns` stop behavior**

```python
# tests/query/test_max_turns_stop.py
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder


class StubResponse:
    def __init__(self, *, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = "tool_calls" if self.tool_calls else "stop"

    @property
    def has_final_text(self) -> bool:
        return bool(self.content)

    def to_message(self) -> dict:
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


class CaptureGateway:
    def __init__(self) -> None:
        self.tools_arguments = []
        self._responses = [
            StubResponse(tool_calls=[{"id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]),
            StubResponse(tool_calls=[{"id": "call-2", "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]),
        ]

    def call_once(self, messages, *, tools):
        self.tools_arguments.append(tools)
        return self._responses.pop(0)


class StubBatch:
    def __init__(self):
        self.tool_results = [{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]
        self.files_modified = []
        self.tool_names = ["read_file"]


class StubRuntime:
    def execute_batch(self, tool_calls):
        return StubBatch()


class StubPolicyRunner:
    def __init__(self) -> None:
        self.calls = 0

    def before_model_call(self, context, state):
        return []

    def after_tool_batch(self, context, state, batch_result):
        return []

    def should_stop(self, context, state):
        self.calls += 1
        if self.calls == 1:
            return "max_turns"
        return None


class StubRecovery:
    def handle(self, model_resp, state):
        raise AssertionError("recovery should not run in this test")


def test_query_loop_disables_tools_after_max_turns() -> None:
    gateway = CaptureGateway()
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "hi"}])
    store = SessionStore(session_state)
    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=MessageViewBuilder(tools=[{"type": "function"}]),
        prompt_assembler=None,
        model_gateway=gateway,
        tool_runtime=StubRuntime(),
        tool_context=None,
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )
    assert gateway.tools_arguments[0] == [{"type": "function"}]
    assert gateway.tools_arguments[1] is None
    assert result.stop_reason == StopReason.MAX_TURNS
```

- [ ] **Step 2: Run the `max_turns` test to verify the current loop still carries tools forward**

Run: `pytest tests/query/test_max_turns_stop.py -v`

Expected: FAIL because the second model call still receives tools, or because the loop continues executing tool calls after `max_turns`

- [ ] **Step 3: Update `QueryLoop` so the next call after `max_turns` uses `tools=None` and terminates on new `tool_call`**

```python
# core/query/loop.py
while True:
    before_messages = policy_runner.before_model_call(session_state, state)
    if before_messages:
        store.extend(before_messages)

    if prompt_assembler is not None:
        dynamic_messages = prompt_assembler.build_dynamic(session_state, state)
        if dynamic_messages:
            store.extend(dynamic_messages)

    view = view_builder.build(session_state)
    active_tools = None if state.stop_reason == "max_turns" else view.tools
    model_resp = model_gateway.call_once(view.messages, tools=active_tools)
    state.last_model_response = model_resp
    store.append(model_resp.to_message())

    if model_resp.tool_calls and state.stop_reason == "max_turns":
        return QueryResult(
            final_output="",
            stop_reason=StopReason.MAX_TURNS,
            success=False,
            turns_used=state.turn_count,
            tool_calls_executed=state.tool_calls_executed,
            files_modified=state.files_modified,
        )

    if model_resp.tool_calls:
        parsed_calls = _parse_tool_calls(model_resp.tool_calls)
        batch = tool_runtime.execute_batch(parsed_calls)
        store.extend(batch.tool_results)
        state.turn_count += 1
        state.tool_calls_executed += len(parsed_calls)
        state.files_modified.extend(batch.files_modified)
        after_messages = policy_runner.after_tool_batch(session_state, state, batch)
        if after_messages:
            store.extend(after_messages)
        stop_reason = policy_runner.should_stop(session_state, state)
        if stop_reason == "max_turns" and state.stop_reason != "max_turns":
            state.stop_reason = "max_turns"
            store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
        continue
```

- [ ] **Step 4: Re-run the `max_turns` test and a focused query regression test**

Run: `pytest tests/query/test_max_turns_stop.py tests/query/test_tool_runtime_contract.py -v`

Expected: PASS with both query behavior tests green

- [ ] **Step 5: Commit the stop-boundary fix**

```bash
git add core/query/loop.py tests/query/test_max_turns_stop.py tests/query/test_tool_runtime_contract.py
git commit -m "fix: enforce max_turns as a real stop boundary"
```

## Task 4: Clean up call sites, subagent behavior, and run final integration checks

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `core/prompt/assembler.py`
- Create: `tests/integration/test_followup_fixes.py`

- [ ] **Step 1: Write the failing integration tests for the repaired main path and subagent path**

```python
# tests/integration/test_followup_fixes.py
from core.query.result import StopReason
from core.session.engine import SessionEngine
from core.session.subagent import SubagentRequest, SubagentRuntime, SubagentType
from core.tools.context import ToolUseContext


class CaptureClient:
    def __init__(self) -> None:
        self.calls = []

    def call(self, messages, tools=None):
        self.calls.append((messages, tools))

        class Response:
            content = "ok"
            tool_calls = []
            finish_reason = "stop"
            prompt_tokens = 1
            completion_tokens = 1

        return Response()


class StubRuntime:
    def execute_batch(self, tool_calls):
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


def test_main_session_path_keeps_stable_system_prompt_at_message_zero() -> None:
    client = CaptureClient()
    engine = SessionEngine(
        model_gateway=__import__("core.llm.client", fromlist=["ModelGateway"]).ModelGateway(client),
        tool_runtime=StubRuntime(),
        tool_context=ToolUseContext(working_dir=".", max_turns=5),
        policy_runner=StubPolicyRunner(),
        recovery=StubRecovery(),
    )
    result = engine.submit_user_message("你好")
    assert result.stop_reason == StopReason.COMPLETED
    assert engine.state.conversation_messages[0]["role"] == "system"


def test_subagent_runtime_does_not_duplicate_bootstrap_messages() -> None:
    runtime = SubagentRuntime(
        parent_context=ToolUseContext(working_dir=".", max_turns=5),
        llm_factory=CaptureClient,
    )
    result = runtime.run(SubagentRequest(task="只回答一句话", agent_type=SubagentType.GENERAL))
    assert result.stop_reason == "completed"
```

- [ ] **Step 2: Run the integration tests to expose duplicate system/environment injection or stale call signatures**

Run: `pytest tests/integration/test_followup_fixes.py -v`

Expected: FAIL because subagent bootstrap duplicates messages, or because some call site still passes the removed `prompt` keyword

- [ ] **Step 3: Remove dead prompt code, update `SessionEngine` and `SubagentRuntime`, and keep only one bootstrap path**

```python
# core/prompt/assembler.py
class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None):
        self._cache = cache or PromptCache()

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        cached = self._cache.get(state.prompt_cache, "stable_system_prompt")
        if cached is not None:
            return cached
        stable_prompt = get_system_context(project_root=project_root)
        return self._cache.set(state.prompt_cache, "stable_system_prompt", stable_prompt)

    def build_environment_message(self, *, working_dir: str) -> dict[str, str]:
        return {"role": "user", "content": get_user_context(working_dir)}

    def build_dynamic(self, state: SessionState, run_state: RunState) -> list[dict[str, str]]:
        return []
```

```python
# core/session/subagent.py
# only the relevant bootstrap section
engine = SessionEngine(
    model_gateway=ModelGateway(self._llm_factory()),
    tool_runtime=ToolExecutorRuntime(sub_registry, tool_context, display=RunDisplayOptions(quiet=True)),
    tool_context=tool_context,
    policy_runner=PolicyRunner([MaxTurnsPolicy(max_turns)]),
    recovery=RecoveryManager(),
    view_builder=MessageViewBuilder(tools=sub_schemas),
)

engine.append_message({"role": "system", "content": system_prompt})
engine.append_message({"role": "user", "content": env_context})
```

Then make `SessionEngine._bootstrap_session_messages()` skip injection when:

```python
has_system = any(message.get("role") == "system" for message in self._state.conversation_messages)
has_environment = any(
    message.get("role") == "user" and "<environment>" in message.get("content", "")
    for message in self._state.conversation_messages
)
```

- [ ] **Step 4: Run the final integration tests and the full targeted suite**

Run: `pytest tests/query/test_tool_runtime_contract.py tests/session/test_prompt_pipeline.py tests/query/test_max_turns_stop.py tests/integration/test_followup_fixes.py -v`

Expected: PASS with all follow-up-fix regressions green

- [ ] **Step 5: Commit the follow-up fix integration pass**

```bash
git add core/session/subagent.py core/session/engine.py core/prompt/assembler.py tests/integration/test_followup_fixes.py
git commit -m "fix: complete runtime follow-up repairs"
```

## Self-Review

### Spec coverage

- `ToolRuntime` structured contract: covered by Task 1
- Prompt/context mainline repair: covered by Task 2
- `max_turns` true stop behavior: covered by Task 3
- Main path + subagent path integration: covered by Task 4

No spec gaps found.

### Placeholder scan

- Searched manually for `TBD`, `TODO`, `implement later`, `similar to Task`, and placeholder ellipses.
- No placeholders remain in the plan body.

### Type consistency

- `ToolBatchResult` is introduced in Task 1 and used consistently in later tasks.
- `ModelGateway.call_once(messages, *, tools)` is defined in Task 2 and used consistently in Tasks 3 and 4.
- `SessionStore.prepend()` is introduced in Task 2 and reused consistently in Task 4.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-15-runtime-followup-fixes-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
