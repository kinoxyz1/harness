from __future__ import annotations

from types import SimpleNamespace

from core.policy.base import PolicyRunner
from core.policy.todo_tracking import TodoPlanningPolicy
from core.llm.client import RequestCancelledError
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

    def build(self, prepared, *, run_state):
        self.last_messages = prepared.working_transcript
        return ModelInputView(system="SYSTEM", messages=list(prepared.working_transcript), tools=None)


class FakeGovernor:
    def __init__(self, prepared_messages=None, observability=None) -> None:
        self.prepared_messages = prepared_messages
        self.observability = observability or {
            "steps": ["estimate"],
            "before_tokens": 0,
            "after_tokens": 0,
            "strategies_run": [],
        }

    def assess(self, *, session_state, run_state, store, **kwargs):
        run_state.context_observability = dict(self.observability)
        messages = self.prepared_messages
        if messages is None:
            messages = list(session_state.conversation_messages)
        return SimpleNamespace(
            messages=messages,
            working_transcript=messages,
            observability=run_state.context_observability,
        )


class FakeOffloader:
    def maybe_persist(self, tool_use_id, content, *, tool_name):
        return content


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


class FakeCancelledModelGateway:
    def call_once(self, messages, *, system="", tools, request_options=None):
        raise RequestCancelledError("cancelled")


class FakeErroredModelGateway:
    def call_once(self, messages, *, system="", tools, request_options=None):
        raise RuntimeError("HTTP/1.1 504 Gateway Time-out")


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


class FakePromptAssembler:
    def build_stable_context(self, state, *, project_root=None):
        return "stable"

    def build_stable_tools(self, state, *, tools=None):
        return tools

    def build_runtime_blocks(self, state, *, working_dir):
        return []

    def build_query_overlay_blocks(self, state, run_state):
        return []


def test_query_loop_renders_reasoning_when_present() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
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
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=PolicyRunner([TodoPlanningPolicy()]),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
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
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGatewayWithToolTurn(),
        tool_runtime=FakeToolRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
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
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGatewayWithToolTurn(),
        tool_runtime=FakeToolRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert result.turns_used == 1
    assert session_state.conversation_messages[-2]["role"] == "tool"
    assert session_state.conversation_messages[-2]["content"] == "ok"


def test_query_loop_uses_governor_before_view_builder_and_surfaces_status() -> None:
    session_state = SessionState(conversation_messages=[{"role": "user", "content": "raw"}])
    store = SessionStore(session_state)
    builder = FakeViewBuilder()
    renderer = FakeRenderer()
    observability = {
        "steps": ["estimate", "per_message_budget", "microcompact"],
        "before_tokens": 1200,
        "after_tokens": 800,
        "strategies_run": ["microcompact"],
    }

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=builder,
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(
            prepared_messages=[{"role": "user", "content": "prepared"}],
            observability=observability,
        ),
        offloader=FakeOffloader(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert builder.last_messages == [{"role": "user", "content": "prepared"}]
    assert renderer.status_calls == ["上下文管理: microcompact 1k→0k (↓0k)"]


def test_query_loop_returns_aborted_when_model_request_is_cancelled() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeCancelledModelGateway(),
        tool_runtime=object(),
        tool_context=SimpleNamespace(cancelled=True),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=FakeRenderer(),
    )

    assert result.stop_reason == StopReason.ABORTED
    assert result.success is False
    assert result.final_output == "已取消当前运行。"


def test_query_loop_returns_api_error_when_model_request_fails() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeErroredModelGateway(),
        tool_runtime=object(),
        tool_context=SimpleNamespace(cancelled=False),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=FakeRenderer(),
    )

    assert result.stop_reason == StopReason.API_ERROR
    assert result.success is False
    assert "504 Gateway Time-out" in result.final_output


from core.shared.stream_events import make_event


class FakeStreamingRenderer(FakeRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.begin_calls: list[str] = []
        self.event_types: list[str] = []
        self.end_calls: list[str] = []

    def begin_stream(self, turn_id: str, meta: dict[str, object]) -> None:
        self.begin_calls.append(turn_id)

    def consume_event(self, event) -> None:
        self.event_types.append(event.type)

    def end_stream(self, turn_id: str, result_meta: dict[str, object]) -> None:
        self.end_calls.append(turn_id)


class FakeStreamingGateway:
    def stream_once(self, messages, *, system="", tools=None, request_options=None, turn_id: str):
        yield make_event("response_start", turn_id, 1, "model", "live")
        yield make_event("thinking_delta", turn_id, 2, "model", "live", {"text": "先分析"})
        yield make_event("content_delta", turn_id, 3, "model", "live", {"text": "最终回答"})
        yield make_event(
            "response_completed",
            turn_id,
            4,
            "model",
            "live",
            {
                "finish_reason": "end_turn",
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "reasoning_signature": "sig_1",
            },
        )


def test_query_loop_streams_main_agent_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr("core.query.loop.STREAMING_ENABLED", True)
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeStreamingRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeStreamingGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.begin_calls and renderer.end_calls
    assert renderer.event_types == ["response_start", "thinking_delta", "content_delta", "response_completed"]
    assert session_state.conversation_messages[-1]["role"] == "assistant"
    assert session_state.conversation_messages[-1]["content"] == "最终回答"


def test_query_loop_uses_batch_path_when_streaming_disabled(monkeypatch) -> None:
    monkeypatch.setattr("core.query.loop.STREAMING_ENABLED", False)
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=FakeRenderer(),
    )

    assert result.stop_reason == StopReason.COMPLETED
