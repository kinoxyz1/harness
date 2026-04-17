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
from core.tools.runtime import ToolBatchResult


class FakeRenderer:
    def __init__(self) -> None:
        self.thinking_calls: list[tuple[str, str]] = []
        self.assistant_calls: list[str | None] = []
        self.status_calls: list[str] = []

    def show_thinking(self, title: str, reasoning: str) -> None:
        self.thinking_calls.append((title, reasoning))

    def show_assistant(self, content: str | None) -> None:
        self.assistant_calls.append(content)

    def show_status(self, message: str) -> None:
        self.status_calls.append(message)


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


class FakeModelGatewayWithToolTurn:
    def __init__(self) -> None:
        self._responses = [
            ModelResponse(
                content="我先读取配置文件。",
                tool_calls=[{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}],
                finish_reason="tool_use",
            ),
            ModelResponse(
                content="final answer",
                finish_reason="end_turn",
            ),
        ]

    def call_once(self, messages, *, tools):
        return self._responses.pop(0)


class FakeToolRuntime:
    def execute_batch(self, tool_calls):
        return ToolBatchResult(
            tool_results=[
                {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
            ],
            files_modified=[],
            tool_names=["read_file"],
            injected_messages=[],
            context_patches=[],
            barrier=None,
            tool_successes=[True],
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


def test_query_loop_renders_assistant_content_when_tool_calls_are_present() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=FakeModelGatewayWithToolTurn(),
        tool_runtime=FakeToolRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert renderer.assistant_calls == ["我先读取配置文件。"]
    assert result.stop_reason == StopReason.COMPLETED
