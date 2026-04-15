from __future__ import annotations

from types import SimpleNamespace

from core.policy.base import PolicyRunner
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.llm.response import ModelResponse
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageView


class FakeRenderer:
    def __init__(self) -> None:
        self.thinking_calls: list[tuple[str, str]] = []

    def show_thinking(self, title: str, reasoning: str) -> None:
        self.thinking_calls.append((title, reasoning))


class FakeViewBuilder:
    def build(self, state: SessionState, *, run_state=None) -> MessageView:
        return MessageView(messages=list(state.conversation_messages), tools=None)


class FakeModelGateway:
    def call_once(self, messages, *, tools):
        return ModelResponse(
            content="final answer",
            reasoning="reasoning trace",
            finish_reason="end_turn",
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


def test_query_loop_renders_reasoning_when_present() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.thinking_calls == [("思考过程", "reasoning trace")]


def test_query_loop_renders_reasoning_with_todo_planning_policy() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=PolicyRunner([TodoPlanningPolicy()]),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.thinking_calls == [("思考过程", "reasoning trace")]
