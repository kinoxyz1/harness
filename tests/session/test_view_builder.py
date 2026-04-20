from pathlib import Path

from core.prompt.assembler import PromptAssembler
from core.query.state import RunState
from core.session.state import SessionState
from core.session.view_builder import MessageViewBuilder, ModelInputView


def test_build_returns_system_and_transcript_slice_separately(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    )
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert isinstance(view, ModelInputView)
    assert isinstance(view.system, str)
    assert view.messages == state.conversation_messages
    assert "transcript_slice" in view.internal_runtime_view


def test_build_with_run_state_filters_tools(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    run_state = RunState(allowed_tools_override={"todo"})
    builder = MessageViewBuilder(
        tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ]
    )
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=run_state,
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert [tool["name"] for tool in view.tools] == ["todo"]


def test_build_respects_transcript_char_budget(tmp_path: Path) -> None:
    long_text = "x" * 300
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": long_text},
            {"role": "user", "content": "u2"},
        ],
    )
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
        transcript_char_budget=50,
    )

    assert view.messages[-1] == {"role": "user", "content": "u2"}
    assert sum(len(m.get("content", "")) for m in view.messages if isinstance(m.get("content"), str)) <= 50


def test_build_keeps_tool_use_with_trailing_tool_result_when_budget_is_tight(tmp_path: Path) -> None:
    tool_result = "x" * 30_000
    state = SessionState(
        conversation_messages=[
            {"role": "user", "content": "Analyze the CSV"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "toolu_read_1", "name": "read_file", "args": {"path": "data.csv"}},
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_read_1", "content": tool_result},
        ],
    )
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
        transcript_char_budget=24_000,
    )

    assert [message["role"] for message in view.messages] == ["assistant", "tool"]
    assert view.messages[0]["tool_calls"][0]["id"] == "toolu_read_1"
    assert view.messages[1]["tool_call_id"] == "toolu_read_1"
