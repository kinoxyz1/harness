# Context Management Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace greedy tail slicing with a formal query-time context management pipeline that manages token pressure, compacts stale tool output, rewrites the working transcript, and performs one guarded overflow recovery without breaking the current think-act-observe loop.

**Architecture:** Add a `ContextManager` in front of `MessageViewBuilder`, keep `PromptAssembler` as the source of runtime truth, and split transcript compaction into focused helpers: token budgeting, provider-facing pairing repair, transcript rewrite, local tool-result compaction, summary compact, and overflow recovery. The first phase deliberately skips Claude Code’s cached microcompact, session-memory compact, context-collapse, and cross-process session migration.

**Tech Stack:** Python 3.12, pytest, existing `SessionState` / `RunState` / `QueryLoop` / `PromptAssembler` stack, Anthropic-style message normalization

---

## File Structure

### New Files

- `core/session/token_budget.py`
  Responsibility: rough token estimation, calibrated pressure decisions, and summary-compact trigger helpers.

- `core/session/pairing_repair.py`
  Responsibility: provider-facing tool-pair repair and duplicate/orphan cleanup for normalized messages.

- `core/session/transcript_rewriter.py`
  Responsibility: compact boundary/meta message factories and post-compact working-transcript assembly.

- `core/session/compact_service.py`
  Responsibility: tool-result budget replacement, time-based microcompact, runtime-restore message generation, and summary-compact execution.

- `core/session/context_manager.py`
  Responsibility: query-time orchestration, recursion guard, circuit breaker, observability, and reactive recovery entrypoint.

- `tests/session/test_token_budget.py`
  Responsibility: token-estimation and compact-trigger coverage.

- `tests/session/test_pairing_repair.py`
  Responsibility: pairing repair and orphan/duplicate cleanup coverage.

- `tests/session/test_transcript_rewriter.py`
  Responsibility: working-transcript replacement and compact rewrite shape coverage.

- `tests/session/test_compact_service.py`
  Responsibility: local compaction and summary-compact coverage.

- `tests/session/test_context_manager.py`
  Responsibility: pipeline order, recursion guard, circuit breaker, and reactive recovery coverage.

### Modified Files

- `core/session/state.py`
  Responsibility: hold compact-related session metadata.

- `core/query/state.py`
  Responsibility: hold per-run reactive-recovery state and context observability.

- `core/session/store.py`
  Responsibility: add formal working-transcript replacement and stamp messages with local metadata.

- `core/session/view_builder.py`
  Responsibility: stop owning transcript-retention policy and accept a pre-managed transcript from `ContextManager`.

- `core/query/loop.py`
  Responsibility: invoke context management before view assembly, persist prompt-usage observations, and perform one guarded retry on overflow.

- `core/llm/protocol.py`
  Responsibility: normalize internal compact meta messages and invoke provider-facing pairing repair before final payload emission.

- `core/llm/client.py`
  Responsibility: add request overrides for summary calls and export a dedicated overflow error type.

- `core/llm/anthropic_client.py`
  Responsibility: honor request overrides and reclassify prompt-too-long failures as a typed overflow error.

- `tests/session/test_view_builder.py`
  Responsibility: verify explicit transcript override support.

- `tests/session/test_state_assembled_runtime.py`
  Responsibility: prove runtime truth survives transcript rewrite.

- `tests/test_protocol.py`
  Responsibility: cover compact meta message normalization and repair integration.

- `tests/test_model_gateway.py`
  Responsibility: cover request-option forwarding for summary calls.

- `tests/test_anthropic_client.py`
  Responsibility: cover request overrides and prompt-too-long classification.

- `tests/test_query_logging.py`
  Responsibility: verify compact observability is surfaced during query execution.

---

### Task 1: Add Token Budget Primitives And Compact State Defaults

**Files:**
- Create: `core/session/token_budget.py`
- Modify: `core/session/state.py`
- Modify: `core/query/state.py`
- Test: `tests/session/test_token_budget.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.query.state import RunState
from core.session.state import SessionState
from core.session.token_budget import (
    estimate_message_tokens,
    estimate_messages_tokens,
    should_trigger_summary_compact,
)


def test_session_and_run_state_expose_compact_defaults() -> None:
    session = SessionState(conversation_messages=[])
    run = RunState()

    assert session.compact_state["tool_result_replacements"] == {}
    assert session.compact_state["consecutive_summary_failures"] == 0
    assert session.compact_state["last_prompt_tokens"] == 0
    assert run.reactive_recovery_attempted is False
    assert run.context_observability == {}


def test_estimate_message_tokens_counts_reasoning_and_tool_calls() -> None:
    message = {
        "role": "assistant",
        "content": "done",
        "reasoning": "step " * 40,
        "tool_calls": [
            {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}},
        ],
    }

    assert estimate_message_tokens(message) >= 25


def test_estimate_messages_tokens_sums_multiple_messages() -> None:
    messages = [
        {"role": "user", "content": "a" * 80},
        {"role": "assistant", "content": "b" * 40},
    ]

    assert estimate_messages_tokens(messages) >= 30


def test_should_trigger_summary_compact_reserves_output_headroom() -> None:
    should_compact = should_trigger_summary_compact(
        used_tokens=88_500,
        context_window_tokens=100_000,
        reserved_output_tokens=10_000,
        compact_buffer_tokens=1_000,
    )

    assert should_compact is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_token_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.token_budget'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/token_budget.py
from __future__ import annotations

from typing import Any


def _rough_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, len(value) // 4)
    return max(1, len(str(value)) // 4)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = 0
    total += _rough_text_tokens(message.get("content"))
    total += _rough_text_tokens(message.get("reasoning"))
    total += _rough_text_tokens(message.get("tool_calls"))
    return max(1, total)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def calibrated_input_tokens(
    *,
    estimated_tokens: int,
    observed_prompt_tokens: int,
) -> int:
    if observed_prompt_tokens <= 0:
        return estimated_tokens
    return max(estimated_tokens, observed_prompt_tokens)


def should_trigger_summary_compact(
    *,
    used_tokens: int,
    context_window_tokens: int,
    reserved_output_tokens: int,
    compact_buffer_tokens: int,
) -> bool:
    threshold = context_window_tokens - reserved_output_tokens - compact_buffer_tokens
    return used_tokens >= threshold
```

```python
# core/session/state.py
    compact_state: dict[str, Any] = field(default_factory=lambda: {
        "tool_result_replacements": {},
        "consecutive_summary_failures": 0,
        "last_prompt_tokens": 0,
        "last_compact_observability": {},
    })
```

```python
# core/query/state.py
    reactive_recovery_attempted: bool = False
    context_observability: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/session/test_token_budget.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/token_budget.py core/session/state.py core/query/state.py tests/session/test_token_budget.py
git commit -m "feat: add token budget primitives"
```

---

### Task 2: Normalize Compact Meta Messages And Extract Pairing Repair

**Files:**
- Create: `core/session/pairing_repair.py`
- Modify: `core/llm/protocol.py`
- Test: `tests/session/test_pairing_repair.py`
- Test: `tests/test_protocol.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.llm.protocol import normalize_messages
from core.session.pairing_repair import repair_tool_result_pairs


def test_repair_inserts_placeholder_for_missing_tool_result() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        }
    ]

    repaired = repair_tool_result_pairs(messages)

    assert repaired[-1]["role"] == "tool"
    assert repaired[-1]["tool_call_id"] == "toolu_1"


def test_repair_drops_orphaned_tool_result() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "toolu_missing", "content": "orphaned"},
        {"role": "assistant", "content": [{"type": "text", "text": "keep"}]},
    ]

    repaired = repair_tool_result_pairs(messages)

    assert repaired == [{"role": "assistant", "content": [{"type": "text", "text": "keep"}]}]


def test_normalize_messages_converts_compact_meta_roles_to_provider_safe_roles() -> None:
    messages = [
        {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact"},
        {"role": "meta_compact_summary", "kind": "compact_summary", "content": "Primary Request and Intent: inspect loop"},
        {"role": "meta_runtime_restore", "kind": "file_runtime", "content": "core/query/loop.py"},
    ]

    system, normalized = normalize_messages(messages)

    assert system == ""
    assert [message["role"] for message in normalized] == ["user", "assistant", "user"]
    assert "compact_boundary" in normalized[0]["content"]
    assert normalized[1]["content"][0]["text"].startswith("<compact_summary>")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_pairing_repair.py tests/test_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.pairing_repair'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/pairing_repair.py
from __future__ import annotations

from typing import Any


SYNTHETIC_TOOL_RESULT = "[Tool result missing due to transcript repair]"


def repair_tool_result_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    seen_tool_use_ids: set[str] = set()
    paired_tool_result_ids: set[str] = set()

    for message in messages:
        if message.get("role") == "assistant":
            content = message.get("content", [])
            if not isinstance(content, list):
                repaired.append(message)
                continue

            deduped_blocks: list[dict[str, Any]] = []
            assistant_tool_use_ids: list[str] = []
            for block in content:
                if block.get("type") != "tool_use":
                    deduped_blocks.append(block)
                    continue
                tool_use_id = block["id"]
                if tool_use_id in seen_tool_use_ids:
                    continue
                seen_tool_use_ids.add(tool_use_id)
                assistant_tool_use_ids.append(tool_use_id)
                deduped_blocks.append(block)

            repaired.append({**message, "content": deduped_blocks})

            for tool_use_id in assistant_tool_use_ids:
                has_result = any(
                    candidate.get("role") == "tool" and candidate.get("tool_call_id") == tool_use_id
                    for candidate in messages
                )
                if not has_result:
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": SYNTHETIC_TOOL_RESULT,
                    })
            continue

        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id not in seen_tool_use_ids:
                continue
            if tool_call_id in paired_tool_result_ids:
                continue
            paired_tool_result_ids.add(tool_call_id)

        repaired.append(message)

    return repaired
```

```python
# core/llm/protocol.py
from core.session.pairing_repair import repair_tool_result_pairs


def _convert_meta_message(msg: dict[str, Any]) -> dict[str, Any]:
    kind = msg.get("kind", "meta")
    content = msg.get("content", "")

    if msg.get("role") == "meta_compact_summary":
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": f"<{kind}>\n{content}\n</{kind}>"}],
        }

    return {
        "role": "user",
        "content": f"<{kind}>\n{content}\n</{kind}>",
    }


def normalize_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    non_system: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
            continue
        non_system.append(msg)

    converted: list[dict[str, Any]] = []
    for msg in non_system:
        role = msg.get("role")
        if role in {"meta_compact_boundary", "meta_compact_summary", "meta_runtime_restore"}:
            converted.append(_convert_meta_message(msg))
        elif role == "user":
            converted.append(_convert_user(msg))
        elif role == "assistant":
            converted.append(_convert_assistant(msg))
        elif role == "tool":
            converted.append(msg)

    converted = repair_tool_result_pairs(converted)
    converted = _merge_tool_results(converted)
    converted = _merge_consecutive_roles(converted)
    return "\n\n".join(system_parts), converted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/session/test_pairing_repair.py tests/test_protocol.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/pairing_repair.py core/llm/protocol.py tests/session/test_pairing_repair.py tests/test_protocol.py
git commit -m "feat: add compact-safe protocol normalization"
```

---

### Task 3: Add Working-Transcript Replacement And Rewrite Primitives

**Files:**
- Create: `core/session/transcript_rewriter.py`
- Modify: `core/session/store.py`
- Test: `tests/session/test_transcript_rewriter.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.transcript_rewriter import (
    build_post_compact_messages,
    create_compact_boundary,
    create_compact_summary,
)


def test_store_replace_working_transcript_replaces_messages() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "before"}])
    store = SessionStore(state)

    store.replace_working_transcript([{"role": "user", "content": "after"}])

    assert state.conversation_messages == [{"role": "user", "content": "after", "_meta": state.conversation_messages[0]["_meta"]}]
    assert "created_at" in state.conversation_messages[0]["_meta"]


def test_build_post_compact_messages_orders_boundary_summary_kept_restore() -> None:
    messages = build_post_compact_messages(
        boundary=create_compact_boundary(reason="summary_compact", summarized_messages=3),
        summary=create_compact_summary("Primary Request and Intent: inspect query loop"),
        kept=[{"role": "assistant", "content": "recent working set"}],
        runtime_restore=[{"role": "meta_runtime_restore", "kind": "file_runtime", "content": "core/query/loop.py"}],
    )

    assert [message["role"] for message in messages] == [
        "meta_compact_boundary",
        "meta_compact_summary",
        "assistant",
        "meta_runtime_restore",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_transcript_rewriter.py -v`
Expected: FAIL because `replace_working_transcript` and `core.session.transcript_rewriter` do not exist

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/store.py
from __future__ import annotations

import time
from typing import Any


class SessionStore:
    def _stamp(self, message: dict[str, Any]) -> dict[str, Any]:
        meta = dict(message.get("_meta", {}))
        meta.setdefault("created_at", time.time())
        return {**message, "_meta": meta}

    def prepend(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.insert(0, self._stamp(message))

    def append(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.append(self._stamp(message))

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self._state.conversation_messages.extend(self._stamp(message) for message in messages)

    def replace_working_transcript(self, messages: list[dict[str, Any]]) -> None:
        self._state.conversation_messages[:] = [self._stamp(message) for message in messages]
```

```python
# core/session/transcript_rewriter.py
from __future__ import annotations

from typing import Any


def create_compact_boundary(*, reason: str, summarized_messages: int) -> dict[str, Any]:
    return {
        "role": "meta_compact_boundary",
        "kind": "compact_boundary",
        "content": f"reason={reason};summarized_messages={summarized_messages}",
    }


def create_compact_summary(summary_text: str) -> dict[str, Any]:
    return {
        "role": "meta_compact_summary",
        "kind": "compact_summary",
        "content": summary_text,
    }


def build_post_compact_messages(
    *,
    boundary: dict[str, Any],
    summary: dict[str, Any],
    kept: list[dict[str, Any]],
    runtime_restore: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [boundary, summary, *kept, *runtime_restore]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/session/test_transcript_rewriter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/store.py core/session/transcript_rewriter.py tests/session/test_transcript_rewriter.py
git commit -m "feat: add working transcript replacement"
```

---

### Task 4: Implement Tool-Result Budget And Time-Based Microcompact

**Files:**
- Create: `core/session/compact_service.py`
- Test: `tests/session/test_compact_service.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.session.compact_service import (
    MICROCOMPACT_PLACEHOLDER,
    TOOL_RESULT_PLACEHOLDER,
    apply_time_based_microcompact,
    apply_tool_result_budget,
)
from core.session.state import SessionState


def test_apply_tool_result_budget_reuses_stable_placeholder_per_tool_call() -> None:
    state = SessionState(conversation_messages=[])
    messages = [
        {
            "role": "tool",
            "tool_call_id": "toolu_read_1",
            "content": "x" * 6000,
            "_meta": {"created_at": 10.0},
        }
    ]

    compacted = apply_tool_result_budget(messages, state=state, per_message_token_limit=100)

    assert compacted[0]["content"] == TOOL_RESULT_PLACEHOLDER
    assert state.compact_state["tool_result_replacements"]["toolu_read_1"] == TOOL_RESULT_PLACEHOLDER


def test_time_based_microcompact_clears_old_read_file_results_but_keeps_recent_one() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_read_1", "name": "read_file", "args": {"path": "a.py"}}],
            "_meta": {"created_at": 10.0},
        },
        {"role": "tool", "tool_call_id": "toolu_read_1", "content": "A" * 500, "_meta": {"created_at": 11.0}},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_read_2", "name": "read_file", "args": {"path": "b.py"}}],
            "_meta": {"created_at": 7200.0},
        },
        {"role": "tool", "tool_call_id": "toolu_read_2", "content": "B" * 500, "_meta": {"created_at": 7201.0}},
    ]

    compacted = apply_time_based_microcompact(
        messages,
        age_cutoff_seconds=1800,
        keep_recent_trajectories=1,
    )

    assert compacted[1]["content"] == MICROCOMPACT_PLACEHOLDER
    assert compacted[3]["content"] == "B" * 500
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_compact_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.session.compact_service'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/compact_service.py
from __future__ import annotations

from typing import Any

from .token_budget import estimate_message_tokens


TOOL_RESULT_PLACEHOLDER = "[Tool result compacted to stay within budget]"
MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"
COMPACTABLE_TOOLS = {"read_file", "find", "grep", "glob"}


def apply_tool_result_budget(
    messages: list[dict[str, Any]],
    *,
    state,
    per_message_token_limit: int,
) -> list[dict[str, Any]]:
    replacements: dict[str, str] = state.compact_state["tool_result_replacements"]
    result: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") != "tool":
            result.append(message)
            continue

        tool_call_id = message.get("tool_call_id")
        if tool_call_id in replacements:
            result.append({**message, "content": replacements[tool_call_id]})
            continue

        if estimate_message_tokens(message) > per_message_token_limit:
            replacements[tool_call_id] = TOOL_RESULT_PLACEHOLDER
            result.append({**message, "content": TOOL_RESULT_PLACEHOLDER})
            continue

        result.append(message)

    return result


def apply_time_based_microcompact(
    messages: list[dict[str, Any]],
    *,
    age_cutoff_seconds: int,
    keep_recent_trajectories: int,
) -> list[dict[str, Any]]:
    compactable_ids: list[str] = []
    newest_timestamp = 0.0

    for message in messages:
        newest_timestamp = max(newest_timestamp, float(message.get("_meta", {}).get("created_at", 0.0)))
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if call.get("name") in COMPACTABLE_TOOLS:
                compactable_ids.append(call["id"])

    keep_ids = set(compactable_ids[-keep_recent_trajectories:])
    compactable_set = set(compactable_ids)
    result: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") != "tool":
            result.append(message)
            continue

        tool_call_id = message.get("tool_call_id")
        created_at = float(message.get("_meta", {}).get("created_at", newest_timestamp))
        is_old = newest_timestamp - created_at >= age_cutoff_seconds

        if tool_call_id in compactable_set and tool_call_id not in keep_ids and is_old:
            result.append({**message, "content": MICROCOMPACT_PLACEHOLDER})
            continue

        result.append(message)

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/session/test_compact_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/compact_service.py tests/session/test_compact_service.py
git commit -m "feat: add local transcript compaction"
```

---

### Task 5: Add Summary-Call Request Overrides To The LLM Stack

**Files:**
- Modify: `core/llm/client.py`
- Modify: `core/llm/anthropic_client.py`
- Test: `tests/test_model_gateway.py`
- Test: `tests/test_anthropic_client.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.llm.client import ModelGateway, ModelRequestOptions


class FakeClient:
    def __init__(self):
        self.last_call = None

    def call(self, messages, *, system="", tools=None, request_options=None):
        self.last_call = {
            "messages": messages,
            "system": system,
            "tools": tools,
            "request_options": request_options,
        }
        return type("Resp", (), {
            "content": "answer",
            "tool_calls": [],
            "finish_reason": "end_turn",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "reasoning": "",
            "reasoning_signature": "",
        })()


def test_model_gateway_forwards_request_options() -> None:
    client = FakeClient()
    gateway = ModelGateway(client)
    request_options = ModelRequestOptions(
        query_source="compact",
        max_output_tokens=1200,
        thinking_mode="disabled",
    )

    gateway.call_once(
        [{"role": "user", "content": "summarize"}],
        system="TEXT ONLY",
        tools=None,
        request_options=request_options,
    )

    assert client.last_call["request_options"] == request_options
```

```python
from unittest.mock import MagicMock, patch

from core.llm.anthropic_client import AnthropicClient
from core.llm.client import ModelRequestOptions


@patch("core.llm.anthropic_client.create_llm_client")
def test_anthropic_client_applies_max_tokens_override_and_disabled_thinking(mock_create) -> None:
    fake_sdk = MagicMock()
    fake_sdk.messages.create.return_value = MagicMock(
        content=[MagicMock(type="text", text="ok")],
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=10, output_tokens=20),
    )
    mock_create.return_value = fake_sdk

    client = AnthropicClient()
    client.call(
        [{"role": "user", "content": "hi"}],
        request_options=ModelRequestOptions(
            query_source="compact",
            max_output_tokens=321,
            thinking_mode="disabled",
        ),
    )

    kwargs = fake_sdk.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 321
    assert "thinking" not in kwargs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_model_gateway.py tests/test_anthropic_client.py -v`
Expected: FAIL because `ModelRequestOptions` and `request_options` support do not exist

- [ ] **Step 3: Write minimal implementation**

```python
# core/llm/client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ModelRequestOptions:
    query_source: str = "main_loop"
    max_output_tokens: int | None = None
    thinking_mode: Literal["default", "disabled"] = "default"


class ContextWindowExceededError(RuntimeError):
    pass


class ModelGateway:
    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None,
        request_options: ModelRequestOptions | None = None,
    ) -> ModelResponse:
        if self._client is None:
            raise RuntimeError("No LLM client configured")

        response = self._client.call(
            messages,
            system=system,
            tools=tools,
            request_options=request_options,
        )
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            reasoning=response.reasoning or "",
            reasoning_signature=response.reasoning_signature or "",
        )
```

```python
# core/llm/anthropic_client.py
from core.llm.client import ModelRequestOptions


def call(
    self,
    messages: list[dict[str, Any]],
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    stream: bool = False,
    display: RunDisplayOptions | None = None,
    request_options: ModelRequestOptions | None = None,
) -> LLMResponse:
    request_options = request_options or ModelRequestOptions()
    normalized_system, api_messages = normalize_messages(messages)
    full_system = "\n\n".join(part for part in [system, normalized_system] if part)

    params: dict[str, Any] = {
        "model": MODEL,
        "system": full_system,
        "messages": api_messages,
        "max_tokens": request_options.max_output_tokens or MAX_TOKENS,
    }
    if tools:
        params["tools"] = tools
    if request_options.thinking_mode != "disabled":
        self._apply_thinking(params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_model_gateway.py tests/test_anthropic_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/llm/client.py core/llm/anthropic_client.py tests/test_model_gateway.py tests/test_anthropic_client.py
git commit -m "feat: add summary request overrides"
```

---

### Task 6: Implement Summary Compact And Runtime-Restore Messages

**Files:**
- Modify: `core/session/compact_service.py`
- Modify: `core/session/transcript_rewriter.py`
- Test: `tests/session/test_compact_service.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from core.session.compact_service import build_runtime_restore_messages, summarize_and_compact
from core.session.state import SessionState, TodoItem
from core.skills.models import InvokedSkillRecord


class StubSummaryGateway:
    def __init__(self) -> None:
        self.calls = []

    def call_once(self, messages, *, system="", tools=None, request_options=None):
        self.calls.append({
            "messages": messages,
            "system": system,
            "tools": tools,
            "request_options": request_options,
        })
        return type("Resp", (), {
            "content": "Primary Request and Intent: inspect query loop\nCurrent Work: wiring context manager",
            "tool_calls": [],
            "finish_reason": "end_turn",
            "prompt_tokens": 30,
            "completion_tokens": 60,
            "reasoning": "",
            "reasoning_signature": "",
        })()


def test_build_runtime_restore_messages_uses_runtime_truth(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[])
    state.todo_state.items = [TodoItem(content="Inspect loop", active_form="Inspecting loop", status="in_progress")]
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path="/skills/analysis-report/SKILL.md",
        content_digest="digest-1",
        content="<skill-content>report workflow</skill-content>",
        invoked_at_turn=1,
    )
    state.read_file_state[str(tmp_path / "loop.py")] = type("FileState", (), {"content": "class QueryLoop", "timestamp": 1.0})()

    restored = build_runtime_restore_messages(state)

    assert {message["kind"] for message in restored} == {"todo_restore", "skills_restore", "file_runtime"}


def test_summarize_and_compact_rewrites_to_boundary_summary_kept_restore(tmp_path: Path) -> None:
    gateway = StubSummaryGateway()
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "Inspect context management"},
            {"role": "assistant", "content": "I will read the loop."},
            {"role": "assistant", "content": "Recent working set"},
        ]
    )

    compacted = summarize_and_compact(
        state.conversation_messages,
        state=state,
        summary_gateway=gateway,
        keep_last_messages=1,
    )

    assert compacted[0]["role"] == "meta_compact_boundary"
    assert compacted[1]["role"] == "meta_compact_summary"
    assert compacted[2]["content"] == "Recent working set"
    assert gateway.calls[0]["tools"] is None
    assert gateway.calls[0]["request_options"].query_source == "compact"
    assert gateway.calls[0]["request_options"].thinking_mode == "disabled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_compact_service.py -v`
Expected: FAIL because `build_runtime_restore_messages` and `summarize_and_compact` do not exist

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/compact_service.py
from core.llm.client import ModelRequestOptions
from .transcript_rewriter import (
    build_post_compact_messages,
    create_compact_boundary,
    create_compact_summary,
)


SUMMARY_SYSTEM_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages
7. Pending Tasks
8. Current Work
9. Optional Next Step
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."""


def build_runtime_restore_messages(state) -> list[dict[str, Any]]:
    restored: list[dict[str, Any]] = []

    if state.todo_state.items:
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "todo_restore",
            "content": "\n".join(item.active_form for item in state.todo_state.items),
        })

    if state.invoked_skills:
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "skills_restore",
            "content": "\n".join(sorted(state.invoked_skills.keys())),
        })

    for path, file_state in sorted(
        state.read_file_state.items(),
        key=lambda item: getattr(item[1], "timestamp", 0.0),
        reverse=True,
    )[:3]:
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "file_runtime",
            "content": f"{path}: {getattr(file_state, 'content', '')[:200]}",
        })

    return restored


def summarize_and_compact(
    messages: list[dict[str, Any]],
    *,
    state,
    summary_gateway,
    keep_last_messages: int,
) -> list[dict[str, Any]]:
    keep_from_index = max(0, len(messages) - keep_last_messages)
    summary_response = summary_gateway.call_once(
        messages[:keep_from_index],
        system=SUMMARY_SYSTEM_PROMPT,
        tools=None,
        request_options=ModelRequestOptions(
            query_source="compact",
            max_output_tokens=1200,
            thinking_mode="disabled",
        ),
    )
    summary_text = summary_response.content.strip()
    boundary = create_compact_boundary(
        reason="summary_compact",
        summarized_messages=keep_from_index,
    )
    summary = create_compact_summary(summary_text)
    kept = messages[keep_from_index:]
    runtime_restore = build_runtime_restore_messages(state)
    return build_post_compact_messages(
        boundary=boundary,
        summary=summary,
        kept=kept,
        runtime_restore=runtime_restore,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/session/test_compact_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/compact_service.py core/session/transcript_rewriter.py tests/session/test_compact_service.py
git commit -m "feat: add summary compact path"
```

---

### Task 7: Add ContextManager And Integrate It Into QueryLoop

**Files:**
- Create: `core/session/context_manager.py`
- Modify: `core/session/view_builder.py`
- Modify: `core/query/loop.py`
- Create: `tests/session/test_context_manager.py`
- Modify: `tests/session/test_view_builder.py`
- Modify: `tests/test_query_logging.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.context_manager import ContextManager
from core.session.state import SessionState
from core.session.view_builder import MessageViewBuilder


class StubCompactService:
    def __init__(self) -> None:
        self.calls = []

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        return messages

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return messages

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        return [{"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact"}]


def test_context_manager_runs_budget_then_microcompact_then_summary() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    manager = ContextManager(compact_service=StubCompactService(), summary_gateway=object())

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=None,
        query_source="main_loop",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
    ]
    assert prepared.messages[0]["role"] == "meta_compact_boundary"


def test_context_manager_skips_summary_when_query_source_is_compact() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    service = StubCompactService()
    manager = ContextManager(compact_service=service, summary_gateway=object())

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=None,
        query_source="compact",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
    ]
    assert service.calls == ["tool_result_budget", "microcompact"]


def test_view_builder_can_use_prepared_transcript_messages(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "original"}])
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
        transcript_messages=[{"role": "user", "content": "prepared"}],
    )

    assert view.messages == [{"role": "user", "content": "prepared"}]
```

```python
from types import SimpleNamespace

from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.session.store import SessionStore
from core.session.view_builder import ModelInputView


class FakeRenderer:
    def __init__(self) -> None:
        self.status_calls = []

    def show_thinking(self, title, reasoning) -> None:
        return None

    def show_assistant(self, content) -> None:
        return None

    def show_status(self, message: str) -> None:
        self.status_calls.append(message)


class FakeViewBuilder:
    def __init__(self) -> None:
        self.last_messages = None

    def build(self, state, *, run_state=None, prompt_assembler=None, working_dir=".", project_root=None, transcript_char_budget=None, transcript_messages=None):
        self.last_messages = transcript_messages
        return ModelInputView(system="SYSTEM", messages=list(transcript_messages or state.conversation_messages), tools=None)


class FakeContextManager:
    def prepare_for_query(self, *, session_state, run_state, store, query_source):
        run_state.context_observability = {"steps": ["tool_result_budget", "microcompact"], "before_tokens": 1200, "after_tokens": 800}
        return SimpleNamespace(
            messages=[{"role": "user", "content": "prepared"}],
            observability=run_state.context_observability,
        )


class FakeModelGateway:
    def call_once(self, messages, *, system="", tools=None, request_options=None):
        return ModelResponse(content="final answer", finish_reason="end_turn")


def test_query_loop_uses_context_manager_before_view_builder() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "raw"}])
    store = SessionStore(session_state)
    builder = FakeViewBuilder()
    renderer = FakeRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=builder,
        prompt_assembler=object(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=type("Policy", (), {
            "before_model_call": lambda self, session_state, state: [],
            "after_tool_batch": lambda self, session_state, state, batch: [],
            "should_stop": lambda self, session_state, state: None,
        })(),
        recovery=type("Recovery", (), {"handle": lambda self, model_resp, state: SimpleNamespace(should_continue=False, follow_up_messages=[])})(),
        context_manager=FakeContextManager(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert builder.last_messages == [{"role": "user", "content": "prepared"}]
    assert renderer.status_calls == ["上下文管理: tool_result_budget,microcompact 1200->800"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_context_manager.py tests/session/test_view_builder.py tests/test_query_logging.py -v`
Expected: FAIL because `ContextManager`, `transcript_messages`, and query-loop integration do not exist

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/context_manager.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .token_budget import (
    calibrated_input_tokens,
    estimate_messages_tokens,
    should_trigger_summary_compact,
)


@dataclass(slots=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    observability: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    def __init__(self, *, compact_service, summary_gateway, context_window_tokens: int = 100_000):
        self._compact_service = compact_service
        self._summary_gateway = summary_gateway
        self._context_window_tokens = context_window_tokens

    def prepare_for_query(self, *, session_state, run_state, store, query_source: str) -> PreparedContext:
        messages = list(session_state.conversation_messages)
        estimated_tokens = estimate_messages_tokens(messages)
        used_tokens = calibrated_input_tokens(
            estimated_tokens=estimated_tokens,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )
        observability = {
            "steps": ["estimate"],
            "before_tokens": used_tokens,
            "after_tokens": used_tokens,
        }

        messages = self._compact_service.apply_tool_result_budget(
            messages,
            state=session_state,
            per_message_token_limit=1200,
        )
        observability["steps"].append("tool_result_budget")

        messages = self._compact_service.apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )
        observability["steps"].append("microcompact")

        if query_source != "compact" and should_trigger_summary_compact(
            used_tokens=used_tokens,
            context_window_tokens=self._context_window_tokens,
            reserved_output_tokens=10_000,
            compact_buffer_tokens=1_000,
        ):
            messages = self._compact_service.summarize_and_compact(
                messages,
                state=session_state,
                summary_gateway=self._summary_gateway,
                keep_last_messages=4,
            )
            observability["steps"].append("summary_compact")
            if store is not None:
                store.replace_working_transcript(messages)

        observability["after_tokens"] = estimate_messages_tokens(messages)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedContext(messages=messages, observability=observability)
```

```python
# core/session/view_builder.py
    def build(
        self,
        state: SessionState,
        *,
        run_state,
        prompt_assembler: PromptAssembler,
        working_dir: str,
        project_root: str | None = None,
        transcript_char_budget: int | None = None,
        transcript_messages: list[dict[str, Any]] | None = None,
    ) -> ModelInputView:
        budget = transcript_char_budget or 24_000
        source_messages = transcript_messages if transcript_messages is not None else state.conversation_messages
        transcript_slice = self._select_transcript_slice(source_messages, char_budget=budget)
        transcript_slice = self._strip_old_thinking(transcript_slice, keep_last=2)
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
        context_manager,
        renderer=None,
    ) -> QueryResult:
            prepared = context_manager.prepare_for_query(
                session_state=session_state,
                run_state=state,
                store=store,
                query_source="main_loop",
            )
            if renderer and prepared.observability.get("steps") != ["estimate"]:
                renderer.show_status(
                    "上下文管理: "
                    + ",".join(step for step in prepared.observability["steps"] if step != "estimate")
                    + f" {prepared.observability['before_tokens']}->{prepared.observability['after_tokens']}"
                )

            view = view_builder.build(
                session_state,
                run_state=state,
                prompt_assembler=prompt_assembler,
                working_dir=working_dir,
                project_root=getattr(tool_context, "working_dir", None),
                transcript_messages=prepared.messages,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/session/test_context_manager.py tests/session/test_view_builder.py tests/test_query_logging.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/context_manager.py core/session/view_builder.py core/query/loop.py tests/session/test_context_manager.py tests/session/test_view_builder.py tests/test_query_logging.py
git commit -m "feat: integrate context manager into query loop"
```

---

### Task 8: Add Circuit Breaker, Typed Overflow Recovery, And Final Regression Coverage

**Files:**
- Modify: `core/session/context_manager.py`
- Modify: `core/query/loop.py`
- Modify: `core/llm/anthropic_client.py`
- Modify: `tests/session/test_context_manager.py`
- Modify: `tests/test_anthropic_client.py`
- Modify: `tests/session/test_state_assembled_runtime.py`

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace

import pytest

from core.llm.client import ContextWindowExceededError
from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.query.state import RunState
from core.session.context_manager import ContextManager
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import ModelInputView


class AlwaysFailingCompactService:
    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        return messages

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        return messages

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        raise RuntimeError("summary failed")

    def reactive_recover(self, messages, *, state):
        return [{"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=reactive"}]


def test_context_manager_trips_circuit_breaker_after_three_summary_failures() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    manager = ContextManager(compact_service=AlwaysFailingCompactService(), summary_gateway=object(), context_window_tokens=100)

    for _ in range(3):
        manager.prepare_for_query(
            session_state=state,
            run_state=RunState(),
            store=None,
            query_source="main_loop",
        )

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=None,
        query_source="main_loop",
    )

    assert state.compact_state["consecutive_summary_failures"] == 3
    assert "summary_compact_skipped_breaker" in prepared.observability["steps"]


class OverflowThenSuccessGateway:
    def __init__(self) -> None:
        self.calls = 0

    def call_once(self, messages, *, system="", tools=None, request_options=None):
        self.calls += 1
        if self.calls == 1:
            raise ContextWindowExceededError("prompt is too long")
        return ModelResponse(content="final answer", finish_reason="end_turn", prompt_tokens=700, completion_tokens=50)


class MinimalViewBuilder:
    def build(self, state, *, run_state=None, prompt_assembler=None, working_dir=".", project_root=None, transcript_char_budget=None, transcript_messages=None):
        return ModelInputView(system="SYSTEM", messages=list(transcript_messages or state.conversation_messages), tools=None)


class RecoveryContextManager:
    def __init__(self) -> None:
        self.reactive_calls = 0

    def prepare_for_query(self, *, session_state, run_state, store, query_source):
        return SimpleNamespace(messages=list(session_state.conversation_messages), observability={"steps": ["estimate"], "before_tokens": 10, "after_tokens": 10})

    def reactive_recover(self, *, session_state, run_state, store):
        self.reactive_calls += 1
        compacted = [{"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=reactive"}]
        store.replace_working_transcript(compacted)
        return compacted


def test_query_loop_runs_single_guarded_retry_on_prompt_too_long() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    store = SessionStore(session_state)
    context_manager = RecoveryContextManager()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=MinimalViewBuilder(),
        prompt_assembler=object(),
        model_gateway=OverflowThenSuccessGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=type("Policy", (), {
            "before_model_call": lambda self, session_state, state: [],
            "after_tool_batch": lambda self, session_state, state, batch: [],
            "should_stop": lambda self, session_state, state: None,
        })(),
        recovery=type("Recovery", (), {"handle": lambda self, model_resp, state: SimpleNamespace(should_continue=False, follow_up_messages=[])})(),
        context_manager=context_manager,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert context_manager.reactive_calls == 1


def test_runtime_truth_survives_after_summary_rewrite(tmp_path) -> None:
    from core.prompt.assembler import PromptAssembler
    from core.skills.models import InvokedSkillRecord
    from core.tools.context import FileState
    from core.session.view_builder import MessageViewBuilder
    from core.session.state import TodoItem, TodoState

    state = SessionState(
        conversation_messages=[
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact"},
            {"role": "meta_compact_summary", "kind": "compact_summary", "content": "Current Work: inspect loop"},
            {"role": "assistant", "content": "Recent working set"},
        ]
    )
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path="/skills/analysis-report/SKILL.md",
        content_digest="digest-1",
        content="<skill-content>report workflow</skill-content>",
        invoked_at_turn=1,
    )
    state.todo_state = TodoState(items=[TodoItem(content="Inspect loop", active_form="Inspecting loop", status="in_progress")])
    state.read_file_state[str(tmp_path / "loop.py")] = FileState(content="class QueryLoop", timestamp=1.0, offset=None, limit=None)

    view = MessageViewBuilder().build(
        state,
        run_state=RunState(),
        prompt_assembler=PromptAssembler(),
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
        transcript_messages=state.conversation_messages,
    )

    assert "report workflow" in view.system
    assert "Inspecting loop" in view.system
    assert "loop.py" in view.system
```

```python
from unittest.mock import MagicMock, patch

import pytest

from core.llm.anthropic_client import AnthropicClient
from core.llm.client import ContextWindowExceededError


@patch("core.llm.anthropic_client.create_llm_client")
def test_anthropic_client_reclassifies_prompt_too_long(mock_create) -> None:
    fake_sdk = MagicMock()
    fake_sdk.messages.create.side_effect = RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")
    mock_create.return_value = fake_sdk

    client = AnthropicClient()

    with pytest.raises(ContextWindowExceededError):
        client.call([{"role": "user", "content": "hi"}])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session/test_context_manager.py tests/test_anthropic_client.py tests/session/test_state_assembled_runtime.py -v`
Expected: FAIL because circuit breaker, typed overflow recovery, and runtime-proof regression coverage are incomplete

- [ ] **Step 3: Write minimal implementation**

```python
# core/session/context_manager.py
    def _breaker_open(self, session_state) -> bool:
        return session_state.compact_state["consecutive_summary_failures"] >= 3

    def reactive_recover(self, *, session_state, run_state, store) -> list[dict[str, Any]]:
        compacted = self._compact_service.summarize_and_compact(
            list(session_state.conversation_messages),
            state=session_state,
            summary_gateway=self._summary_gateway,
            keep_last_messages=2,
        )
        store.replace_working_transcript(compacted)
        return compacted

    def prepare_for_query(self, *, session_state, run_state, store, query_source: str) -> PreparedContext:
        messages = list(session_state.conversation_messages)
        estimated_tokens = estimate_messages_tokens(messages)
        used_tokens = calibrated_input_tokens(
            estimated_tokens=estimated_tokens,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )
        observability = {"steps": ["estimate"], "before_tokens": used_tokens, "after_tokens": used_tokens}

        messages = self._compact_service.apply_tool_result_budget(
            messages,
            state=session_state,
            per_message_token_limit=1200,
        )
        observability["steps"].append("tool_result_budget")
        messages = self._compact_service.apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )
        observability["steps"].append("microcompact")

        should_summarize = (
            query_source != "compact"
            and not self._breaker_open(session_state)
            and should_trigger_summary_compact(
                used_tokens=used_tokens,
                context_window_tokens=self._context_window_tokens,
                reserved_output_tokens=10_000,
                compact_buffer_tokens=1_000,
            )
        )

        if self._breaker_open(session_state):
            observability["steps"].append("summary_compact_skipped_breaker")
        elif should_summarize:
            try:
                messages = self._compact_service.summarize_and_compact(
                    messages,
                    state=session_state,
                    summary_gateway=self._summary_gateway,
                    keep_last_messages=4,
                )
            except Exception:
                session_state.compact_state["consecutive_summary_failures"] += 1
                observability["steps"].append("summary_compact_failed")
            else:
                session_state.compact_state["consecutive_summary_failures"] = 0
                observability["steps"].append("summary_compact")
                if store is not None:
                    store.replace_working_transcript(messages)

        observability["after_tokens"] = estimate_messages_tokens(messages)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedContext(messages=messages, observability=observability)
```

```python
# core/llm/anthropic_client.py
from core.llm.client import ContextWindowExceededError


        if error.get("data"):
            err = error["data"]
            if "prompt is too long" in str(err).lower() or "context length" in str(err).lower():
                raise ContextWindowExceededError(str(err))
            if (
                self._adaptive_supported is None
                and isinstance(err, Exception)
                and "adaptive" in str(err).lower()
            ):
                self._adaptive_supported = False
                params["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
```

```python
# core/query/loop.py
from core.llm.client import ContextWindowExceededError


            try:
                model_resp = model_gateway.call_once(view.messages, system=view.system, tools=active_tools)
            except ContextWindowExceededError:
                if state.reactive_recovery_attempted:
                    raise
                context_manager.reactive_recover(
                    session_state=session_state,
                    run_state=state,
                    store=store,
                )
                state.reactive_recovery_attempted = True
                continue

            session_state.compact_state["last_prompt_tokens"] = model_resp.prompt_tokens
```

- [ ] **Step 4: Run the focused regression suite**

Run: `pytest tests/session/test_token_budget.py tests/session/test_pairing_repair.py tests/session/test_transcript_rewriter.py tests/session/test_compact_service.py tests/session/test_context_manager.py tests/session/test_view_builder.py tests/session/test_state_assembled_runtime.py tests/test_protocol.py tests/test_model_gateway.py tests/test_anthropic_client.py tests/test_query_logging.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/session/context_manager.py core/query/loop.py core/llm/anthropic_client.py tests/session/test_context_manager.py tests/test_anthropic_client.py tests/session/test_state_assembled_runtime.py
git commit -m "feat: add guarded overflow recovery"
```

---

## Spec Coverage Check

- Token-based pressure estimation and calibrated usage fallback: Task 1 and Task 7
- Provider-facing invariant repair and compact-safe normalization: Task 2
- Working transcript rewrite and formal replacement entrypoint: Task 3
- Tool-result budget and time-based microcompact: Task 4
- Same-model summary compact with disabled thinking and no tools: Task 5 and Task 6
- Query-time context pipeline before view assembly: Task 7
- Recursion guard, circuit breaker, and single guarded overflow retry: Task 7 and Task 8
- Runtime truth surviving transcript rewrite: Task 6 and Task 8
- Compact observability: Task 7 and Task 8

## Plan Review Notes

- The first-phase compactable tool set is `read_file`, `find`, `grep`, and `glob`. `bash` is intentionally excluded until the runtime can distinguish read-only commands from mutating shell operations.
- Compact boundary and runtime-restore markers stay out of transcript `system` messages. The plan converts them to provider-safe `user` or `assistant` content during protocol normalization.
- This plan keeps `PromptAssembler` as the durable renderer of todo, skill, and file runtime. Runtime-restore transcript messages are continuity hints, not the only source of truth.
- Existing in-memory sessions do not need a migration layer in phase 1. The plan intentionally avoids `schema_version`, resume migration, and cross-process transcript persistence.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-29-context-management-architecture-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
