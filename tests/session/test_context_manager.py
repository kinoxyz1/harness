from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.llm.client import ContextWindowExceededError
from core.llm.protocol import normalize_messages
from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.query.state import RunState
from core.session.context_manager import ContextManager
from core.session.compact_service import summarize_and_compact
from core.session.state import SessionState
from core.session.store import SessionStore


class StubCompactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        return list(messages)

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return list(messages)

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        return [
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact"},
            {"role": "assistant", "content": "summary"},
        ]


class ShrinkingCompactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        compacted = []
        for message in messages:
            rewritten = dict(message)
            if rewritten.get("role") == "tool":
                rewritten["content"] = "small"
            compacted.append(rewritten)
        return compacted

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return list(messages)

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        return list(messages)


class FailingCompactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        return list(messages)

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return list(messages)

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        raise RuntimeError("summary failed")


class RecordingFailingCompactService(FailingCompactService):
    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        raise RuntimeError("summary failed")


class FailThriceThenSucceedCompactService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._failures_remaining = 3

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        return list(messages)

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return list(messages)

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise RuntimeError("summary failed")
        return [
            {"role": "meta_compact_boundary", "kind": "compact_boundary", "content": "reason=summary_compact"},
            {"role": "assistant", "content": "summary"},
        ]


class RealSummaryCompactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_tool_result_budget(self, messages, *, state, per_message_token_limit):
        self.calls.append("tool_result_budget")
        return list(messages)

    def apply_time_based_microcompact(self, messages, *, age_cutoff_seconds, keep_recent_trajectories):
        self.calls.append("microcompact")
        return list(messages)

    def summarize_and_compact(self, messages, *, state, summary_gateway, keep_last_messages):
        self.calls.append("summary_compact")
        return summarize_and_compact(
            messages,
            state=state,
            summary_gateway=summary_gateway,
            keep_last_messages=keep_last_messages,
        )


def test_context_manager_runs_estimate_budget_microcompact_then_summary() -> None:
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
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
    ]
    assert prepared.observability["before_tokens"] == 1500
    assert prepared.observability["after_tokens"] > 0
    assert prepared.messages[0]["role"] == "meta_compact_boundary"
    assert [message["role"] for message in state.conversation_messages] == [message["role"] for message in prepared.messages]
    assert [message["content"] for message in state.conversation_messages] == [message["content"] for message in prepared.messages]
    assert all("_meta" in message for message in state.conversation_messages)
    assert state.compact_state["last_compact_observability"] == prepared.observability
    assert run_state.context_observability == prepared.observability


def test_context_manager_skips_summary_when_query_source_is_compact() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    service = StubCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=SessionStore(state),
        query_source="compact",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
    ]
    assert service.calls == ["tool_result_budget", "microcompact"]
    assert state.conversation_messages == [{"role": "user", "content": "x" * 5000}]


def test_context_manager_uses_post_pruning_pressure_for_summary_decision() -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "toolu_1", "name": "read_file", "args": {"path": "a.txt"}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "x" * 5000},
        ]
    )
    service = ShrinkingCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=RunState(),
        store=SessionStore(state),
        query_source="main_loop",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
    ]
    assert service.calls == ["tool_result_budget", "microcompact"]


def test_context_manager_trips_summary_breaker_after_three_failures() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = FailingCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )
    store = SessionStore(state)
    run_state = RunState()

    for _ in range(3):
        prepared = manager.prepare_for_query(
            session_state=state,
            run_state=run_state,
            store=store,
            query_source="main_loop",
        )
        assert "summary_compact_failed" in prepared.observability["steps"]

    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=run_state,
        store=store,
        query_source="main_loop",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
        "summary_compact_skipped_breaker",
    ]
    assert service.calls == [
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
    ]
    assert state.compact_state["consecutive_summary_failures"] == 3
    assert state.compact_state["summary_compact_cooldown_until"] > 0.0


def test_context_manager_skips_summary_during_cooldown() -> None:
    clock = {"now": 0.0}

    def time_fn() -> float:
        return clock["now"]

    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = FailingCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
        time_fn=time_fn,
        summary_breaker_cooldown_seconds=10.0,
    )
    store = SessionStore(state)
    run_state = RunState()

    for _ in range(3):
        manager.prepare_for_query(
            session_state=state,
            run_state=run_state,
            store=store,
            query_source="main_loop",
        )

    clock["now"] = 5.0
    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=run_state,
        store=store,
        query_source="main_loop",
    )

    assert prepared.observability["steps"] == [
        "estimate",
        "tool_result_budget",
        "microcompact",
        "summary_compact_skipped_breaker",
    ]
    assert service.calls == [
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
    ]


def test_context_manager_allows_summary_after_cooldown_and_resets_breaker() -> None:
    clock = {"now": 0.0}

    def time_fn() -> float:
        return clock["now"]

    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = FailThriceThenSucceedCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
        time_fn=time_fn,
        summary_breaker_cooldown_seconds=10.0,
    )
    store = SessionStore(state)
    run_state = RunState()

    for _ in range(3):
        manager.prepare_for_query(
            session_state=state,
            run_state=run_state,
            store=store,
            query_source="main_loop",
        )

    clock["now"] = 11.0
    prepared = manager.prepare_for_query(
        session_state=state,
        run_state=run_state,
        store=store,
        query_source="main_loop",
    )

    assert prepared.observability["steps"][-1] == "summary_compact"
    assert state.compact_state["consecutive_summary_failures"] == 0
    assert state.compact_state["summary_compact_cooldown_until"] == 0.0
    assert prepared.messages[0]["role"] == "meta_compact_boundary"


def test_context_manager_reactive_recover_honors_cooldown_and_can_retry_after_expiry() -> None:
    clock = {"now": 0.0}

    def time_fn() -> float:
        return clock["now"]

    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = FailThriceThenSucceedCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
        time_fn=time_fn,
        summary_breaker_cooldown_seconds=10.0,
    )
    store = SessionStore(state)
    run_state = RunState()

    for _ in range(3):
        manager.prepare_for_query(
            session_state=state,
            run_state=run_state,
            store=store,
            query_source="main_loop",
        )

    clock["now"] = 5.0
    skipped = manager.reactive_recover(
        session_state=state,
        run_state=run_state,
        store=store,
    )
    assert skipped.observability["steps"] == [
        "reactive_recover",
        "summary_compact_skipped_breaker",
    ]

    clock["now"] = 11.0
    retried = manager.reactive_recover(
        session_state=state,
        run_state=run_state,
        store=store,
    )

    assert retried.observability["steps"][-1] == "summary_compact"
    assert state.compact_state["consecutive_summary_failures"] == 0
    assert state.compact_state["summary_compact_cooldown_until"] == 0.0


def test_context_manager_reactive_recover_honors_summary_breaker_after_failures() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = RecordingFailingCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )
    store = SessionStore(state)
    run_state = RunState()

    for _ in range(3):
        manager.prepare_for_query(
            session_state=state,
            run_state=run_state,
            store=store,
            query_source="main_loop",
        )

    prepared = manager.reactive_recover(
        session_state=state,
        run_state=run_state,
        store=store,
    )

    assert prepared.observability["steps"] == [
        "reactive_recover",
        "summary_compact_skipped_breaker",
    ]
    assert service.calls == [
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
        "tool_result_budget",
        "microcompact",
        "summary_compact",
    ]
    assert state.compact_state["consecutive_summary_failures"] == 3


def test_context_manager_reactive_recover_counts_summary_failures() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "x" * 5000}])
    state.compact_state["last_prompt_tokens"] = 1500
    service = FailingCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )
    store = SessionStore(state)
    run_state = RunState()

    prepared = manager.reactive_recover(
        session_state=state,
        run_state=run_state,
        store=store,
    )

    assert prepared.observability["steps"] == [
        "reactive_recover",
        "summary_compact_failed",
    ]
    assert state.compact_state["consecutive_summary_failures"] == 1


def test_context_manager_reactive_recover_preserves_latest_tool_results_through_normalization() -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "m0"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "toolu_old", "name": "read_file", "args": {"path": "old.txt"}},
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_old", "content": "old result"},
            {"role": "user", "content": "m1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "toolu_latest", "name": "read_file", "args": {"path": "latest.txt"}},
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_latest", "content": "latest result"},
            {"role": "user", "content": "tail"},
        ]
    )
    service = RealSummaryCompactService()
    manager = ContextManager(
        compact_service=service,
        summary_gateway=object(),
        context_window_tokens=12_000,
    )
    store = SessionStore(state)

    prepared = manager.reactive_recover(
        session_state=state,
        run_state=RunState(),
        store=store,
    )
    _, normalized = normalize_messages(prepared.messages)

    assert any(
        block.get("type") == "tool_result"
        and block.get("tool_use_id") == "toolu_latest"
        and block.get("content") == "latest result"
        for message in normalized
        if message["role"] == "user"
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
    )


class _OverflowingModelGateway:
    def __init__(self) -> None:
        self.calls = 0

    def call_once(self, messages, *, system="", tools):
        self.calls += 1
        if self.calls == 1:
            raise ContextWindowExceededError("prompt is too long: 210000 tokens > 200000 maximum")
        return ModelResponse(content="final")


class _NoOpPolicyRunner:
    def before_model_call(self, session_state, state):
        return []

    def after_tool_batch(self, session_state, state, batch):
        return []

    def should_stop(self, session_state, state):
        return None


class _NoOpRecovery:
    def handle(self, model_resp, state):
        raise AssertionError("recovery should not be used in overflow retry test")


class _StaticViewBuilder:
    def build(self, *args, **kwargs):
        transcript_messages = kwargs.get("transcript_messages", [])
        return SimpleNamespace(system="runtime", messages=list(transcript_messages), tools=[])


class _ReactiveContextManager:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.reactive_calls = 0

    def prepare_for_query(self, *, session_state, run_state, store, query_source):
        self.prepare_calls += 1
        return SimpleNamespace(
            messages=list(session_state.conversation_messages),
            observability={"steps": ["estimate"], "before_tokens": 1, "after_tokens": 1},
        )

    def reactive_recover(self, *, session_state, run_state, store):
        self.reactive_calls += 1
        return SimpleNamespace(messages=list(session_state.conversation_messages), observability={})


def test_query_loop_retries_once_after_context_window_exceeded() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    store = SessionStore(session_state)
    loop = QueryLoop()
    model_gateway = _OverflowingModelGateway()
    context_manager = _ReactiveContextManager()

    result = loop.run(
        session_state=session_state,
        store=store,
        view_builder=_StaticViewBuilder(),
        prompt_assembler=object(),
        model_gateway=model_gateway,
        tool_runtime=SimpleNamespace(execute_batch=pytest.fail),
        tool_context=SimpleNamespace(working_dir="."),
        policy_runner=_NoOpPolicyRunner(),
        recovery=_NoOpRecovery(),
        context_manager=context_manager,
        renderer=None,
    )

    assert result.final_output == "final"
    assert model_gateway.calls == 2
    assert context_manager.prepare_calls == 2
    assert context_manager.reactive_calls == 1
