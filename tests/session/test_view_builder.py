from __future__ import annotations

from pathlib import Path

from core.query.state import RunState
from core.session.state import SessionState
from core.session.view_builder import MessageViewBuilder


def test_build_returns_conversation_messages_directly(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "hello"},
        ],
    )
    builder = MessageViewBuilder()

    view = builder.build(state)

    assert len(view.messages) == 2
    assert view.messages[0]["content"] == "stable prompt"
    assert view.messages[1] == {"role": "user", "content": "hello"}


def test_build_with_run_state_filters_tools(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[{"role": "system", "content": "stable"}],
    )
    run_state = RunState(allowed_tools_override={"todo"})
    builder = MessageViewBuilder(
        tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ]
    )

    view = builder.build(state, run_state=run_state)

    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_without_run_state_returns_all_tools(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[{"role": "system", "content": "stable"}],
    )
    builder = MessageViewBuilder(
        tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ]
    )

    view = builder.build(state)

    assert [tool["name"] for tool in view.tools] == ["skill", "todo"]
