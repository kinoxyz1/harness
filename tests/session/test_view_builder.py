from pathlib import Path

from core.llm.protocol import normalize_messages
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


def test_build_preserves_reasoning_for_older_assistant_tool_call_messages(tmp_path: Path) -> None:
    state = SessionState(
        conversation_messages=[
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
    builder = MessageViewBuilder()
    assembler = PromptAssembler()

    view = builder.build(
        state,
        run_state=RunState(),
        prompt_assembler=assembler,
        working_dir=str(tmp_path),
        project_root=str(tmp_path),
    )
    _, normalized = normalize_messages(view.messages)

    assistant_tool_messages = [
        message
        for message in normalized
        if message["role"] == "assistant"
    ]
    first_tool_message_blocks = assistant_tool_messages[0]["content"]

    assert first_tool_message_blocks[0] == {
        "type": "thinking",
        "thinking": "Need to load the skill first.",
        "signature": "sig-skill",
    }


def test_build_uses_explicit_transcript_messages_when_supplied(tmp_path: Path) -> None:
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
