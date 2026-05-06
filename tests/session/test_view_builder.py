from pathlib import Path

from core.llm.protocol import normalize_messages
from core.query.state import RunState
from core.session.query_context import ContextBlock, PreparedQueryContext
from core.session.view_builder import MessageViewBuilder, ModelInputView


def test_build_assembles_system_from_prepared_context() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="stable",
        stable_tools=[{"name": "todo"}],
        runtime_blocks=[
            ContextBlock(kind="environment", content="<environment />", required=True, token_estimate=5),
            ContextBlock(kind="todo_state", content="<todo-state />", required=True, token_estimate=5),
        ],
        working_transcript=[{"role": "user", "content": "hello"}],
    )

    view = builder.build(prepared, run_state=RunState())

    assert isinstance(view, ModelInputView)
    assert view.system == "stable\n\n<environment />\n\n<todo-state />"
    assert view.messages == [{"role": "user", "content": "hello"}]
    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_does_not_slice_prepared_transcript() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="stable",
        stable_tools=None,
        runtime_blocks=[],
        working_transcript=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    )

    view = builder.build(prepared, run_state=RunState())

    assert view.messages == prepared.working_transcript


def test_build_filters_tools_by_allowed_override() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="system",
        stable_tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object"}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object"}},
        ],
        runtime_blocks=[],
        working_transcript=[{"role": "user", "content": "hello"}],
    )

    view = builder.build(prepared, run_state=RunState(allowed_tools_override={"todo"}))

    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_strips_signature_but_preserves_reasoning() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="system",
        stable_tools=None,
        runtime_blocks=[],
        working_transcript=[
            {"role": "user", "content": "Make slides"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_skill", "name": "skill", "args": {"skill": "ppt-master"}}],
                "reasoning": "Need to load the skill first.",
                "reasoning_signature": "sig-skill",
            },
            {"role": "tool", "tool_call_id": "toolu_skill", "content": "skill ok"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_todo", "name": "todo", "args": {"items": []}}],
                "reasoning": "Need a plan before proceeding.",
                "reasoning_signature": "sig-todo",
            },
            {"role": "tool", "tool_call_id": "toolu_todo", "content": "todo ok"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_read", "name": "read_file", "args": {"path": "slides.md"}}],
                "reasoning": "Read the source document next.",
                "reasoning_signature": "sig-read",
            },
            {"role": "tool", "tool_call_id": "toolu_read", "content": "read ok"},
        ],
    )

    view = builder.build(prepared, run_state=RunState())
    _, normalized = normalize_messages(view.messages)

    assistant_tool_messages = [
        message
        for message in normalized
        if message["role"] == "assistant"
    ]

    for idx, msg in enumerate(assistant_tool_messages):
        thinking_blocks = [b for b in msg.get("content", []) if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1, f"第 {idx} 条 assistant 消息缺少 thinking block: {msg}"
        assert "signature" not in thinking_blocks[0], f"第 {idx} 条 assistant 消息的 thinking block 不应包含 signature"


def test_build_internal_runtime_view_includes_budget_and_blocks() -> None:
    builder = MessageViewBuilder()
    prepared = PreparedQueryContext(
        stable_system="system",
        stable_tools=None,
        runtime_blocks=[
            ContextBlock(kind="environment", content="env", required=True, token_estimate=1),
        ],
        working_transcript=[{"role": "user", "content": "hi"}],
        budget={"stable_system_tokens": 10, "stable_tools_tokens": 0, "required_runtime_tokens": 1},
    )

    view = builder.build(prepared, run_state=RunState())

    assert view.internal_runtime_view["runtime_blocks"] == ["environment"]
    assert view.internal_runtime_view["budget"]["stable_system_tokens"] == 10
    assert "working_transcript" in view.internal_runtime_view
