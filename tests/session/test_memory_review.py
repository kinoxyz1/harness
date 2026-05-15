from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.memory.review import spawn_background_memory_review
from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.recovery import RecoveryManager
from core.session.engine import SessionEngine
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime


def response_with_tool(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[{"id": call_id, "name": name, "args": args}],
        content="",
        has_final_text=False,
        prompt_tokens=0,
        completion_tokens=0,
        to_message=lambda: {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": name, "args": args}],
        },
    )


def response_with_text(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[],
        content=text,
        has_final_text=True,
        prompt_tokens=0,
        completion_tokens=0,
        to_message=lambda: {"role": "assistant", "content": text},
    )


class StubModelGateway:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def call_once(self, messages, *, system="", tools=None, request_options=None):
        self.calls.append(
            {
                "messages": list(messages),
                "system": system,
                "tools": tools,
                "request_options": request_options,
            }
        )
        return self._responses.pop(0)


def make_engine(tmp_path: Path, gateway: StubModelGateway) -> SessionEngine:
    tool_context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    return SessionEngine(
        model_gateway=gateway,
        tool_runtime=ToolExecutorRuntime(registry, tool_context),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoPlanningPolicy()]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),
        renderer=None,
    )


def test_turn_completion_triggers_controlled_memory_review_and_persists_memory(tmp_path: Path) -> None:
    gateway = StubModelGateway(
        responses=[
            response_with_tool(
                "toolu_memory_review",
                "memory",
                {
                    "action": "add",
                    "target": "user",
                    "content": "User prefers concise answers in Chinese.",
                },
            ),
        ]
    )
    engine = make_engine(tmp_path, gateway)

    class InlineThread:
        def __init__(self, *, target, daemon=None, name=None):
            self._target = target

        def start(self) -> None:
            self._target()

    import core.memory.review as review_module
    original_thread = review_module.threading.Thread
    review_module.threading.Thread = InlineThread
    try:
        spawned = spawn_background_memory_review(
            session_state=engine.state,
            transcript=[
                {"role": "user", "content": "以后请用中文简洁回答。"},
                {"role": "assistant", "content": "Understood."},
            ],
            model_gateway=gateway,
            tool_runtime=engine._tool_runtime,
            tools=registry.schemas(),
        )
    finally:
        review_module.threading.Thread = original_thread

    assert spawned is True

    user_mem = tmp_path / ".harness" / "memories" / "USER.md"
    assert user_mem.exists()
    assert "Prefers concise answers in Chinese." in user_mem.read_text(encoding="utf-8")
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["tools"] == [schema for schema in registry.schemas() if schema["name"] == "memory"]
    assert "Review the conversation" in gateway.calls[0]["system"]
    assert gateway.calls[0]["request_options"].query_source == "memory_review"


def test_main_loop_does_not_expose_memory_tool_to_model(tmp_path: Path) -> None:
    gateway = StubModelGateway(responses=[response_with_text("ok")])
    engine = make_engine(tmp_path, gateway)
    engine.state.memory_review_interval = 0

    result = engine.submit_user_message("你好")

    assert result.final_output == "ok"
    assert len(gateway.calls) == 1
    tool_names = {schema["name"] for schema in gateway.calls[0]["tools"]}
    assert "memory" not in tool_names
    assert "bash" in tool_names
