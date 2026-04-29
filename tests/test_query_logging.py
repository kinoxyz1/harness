from __future__ import annotations

from types import SimpleNamespace

from core.policy.base import PolicyRunner
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.loop import QueryLoop
from core.query.reducers import TransitionReason
from core.query.result import StopReason
from core.llm.response import ModelResponse
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import ModelInputView
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
    def __init__(self) -> None:
        self.last_messages = None

    def build(
        self,
        state: SessionState,
        *,
        run_state=None,
        prompt_assembler=None,
        working_dir=".",
        project_root=None,
        transcript_char_budget=None,
        transcript_messages=None,
    ) -> ModelInputView:
        self.last_messages = transcript_messages
        source = transcript_messages if transcript_messages is not None else state.conversation_messages
        return ModelInputView(system="SYSTEM", messages=list(source), tools=None)


class FakeContextManager:
    def __init__(self, prepared_messages=None, observability=None) -> None:
        self.prepared_messages = prepared_messages
        self.observability = observability or {
            "steps": ["estimate"],
            "before_tokens": 0,
            "after_tokens": 0,
        }

    def prepare_for_query(self, *, session_state, run_state, store, query_source):
        run_state.context_observability = dict(self.observability)
        messages = self.prepared_messages
        if messages is None:
            messages = list(session_state.conversation_messages)
        return SimpleNamespace(messages=messages, observability=run_state.context_observability)


class FakeModelGateway:
    def call_once(self, messages, *, system="", tools):
        return ModelResponse(
            content="final answer",
            reasoning="reasoning trace",
            finish_reason="end_turn",
            prompt_tokens=321,
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

    def call_once(self, messages, *, system="", tools):
        return self._responses.pop(0)


class FakeToolRuntime:
    def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
        return ToolBatchResult(
            messages=[
                {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
            ],
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
        context_manager=FakeContextManager(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.thinking_calls == [("思考过程", "reasoning trace")]
    assert session_state.compact_state["last_prompt_tokens"] == 321


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
        context_manager=FakeContextManager(),
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
        context_manager=FakeContextManager(),
        renderer=renderer,
    )

    assert renderer.assistant_calls == ["我先读取配置文件。"]
    assert result.stop_reason == StopReason.COMPLETED


def test_query_loop_marks_next_turn_after_tool_batch() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

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
        context_manager=FakeContextManager(),
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert result.turns_used == 1
    assert session_state.conversation_messages[-2]["role"] == "tool"
    assert session_state.conversation_messages[-2]["content"] == "ok"


def test_query_loop_uses_context_manager_before_view_builder_and_surfaces_status() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "raw"}])
    store = SessionStore(session_state)
    builder = FakeViewBuilder()
    renderer = FakeRenderer()
    observability = {
        "steps": ["estimate", "tool_result_budget", "microcompact"],
        "before_tokens": 1200,
        "after_tokens": 800,
    }

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=builder,
        prompt_assembler=object(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        context_manager=FakeContextManager(
            prepared_messages=[{"role": "user", "content": "prepared"}],
            observability=observability,
        ),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert builder.last_messages == [{"role": "user", "content": "prepared"}]
    assert renderer.status_calls == ["上下文管理: tool_result_budget,microcompact 1200->800"]
