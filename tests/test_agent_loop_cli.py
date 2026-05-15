from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text


agent_loop = import_module("01_agent_loop")


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.messages = []
        self._renderer = None

    def handle_command(self, raw: str) -> str:
        self.commands.append(raw)
        return "command output"

    def submit_user_message(self, text: str):
        self.messages.append(text)
        return SimpleNamespace(final_output="reply", streaming_displayed=False)


def test_cli_routes_skills_command_to_handle_command():
    engine = FakeEngine()
    with patch.object(agent_loop.console, "print") as mock_print:
        should_continue, resume_id = agent_loop.handle_input("/skills list", engine)

    assert should_continue is True
    assert resume_id is None
    assert engine.commands == ["/skills list"]
    assert engine.messages == []
    mock_print.assert_called_with("command output")


def test_cli_routes_normal_input_to_submit():
    engine = FakeEngine()
    with patch.object(agent_loop, "render_markdown") as mock_render:
        should_continue, resume_id = agent_loop.handle_input("hello world", engine)

    assert should_continue is True
    assert resume_id is None
    assert engine.commands == []
    assert engine.messages == ["hello world"]
    mock_render.assert_called_once()


def test_cli_renders_resume_transcript_with_stream_renderer_when_available():
    engine = FakeEngine()
    engine.state = SimpleNamespace(session_db=object())
    replay_renderer = SimpleNamespace(
        begin_stream=MagicMock(),
        consume_event=MagicMock(),
        end_stream=MagicMock(),
        show_thinking=MagicMock(),
        show_assistant=MagicMock(),
        show_tool_result=MagicMock(),
    )
    engine._renderer = replay_renderer
    transcript = [
        {"role": "user", "content": "帮我修一下 resume"},
        {"role": "user", "content": "<system-reminder type=\"skill_nudge\">ignore me</system-reminder>"},
        {
            "role": "assistant",
            "content": "我先检查 session db。",
            "reasoning": "先确认 resume 回放链路是否绕过了 renderer。",
            "tool_calls": [{"id": "toolu_1", "name": "memory", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "Memory updated. Usage: 20/1375 chars (1 entries)"},
    ]

    with (
        patch.object(agent_loop, "execute_resume_command", return_value=SimpleNamespace(
            handled=True,
            output="Resuming session abc123...",
            resume_session_id="abc123",
            transcript_messages=transcript,
        )),
        patch.object(agent_loop.console, "print") as mock_print,
    ):
        should_continue, resume_id = agent_loop.handle_input("/resume abc123", engine)

    assert should_continue is True
    assert resume_id == "abc123"
    mock_print.assert_any_call("Resuming session abc123...")
    mock_print.assert_any_call("Full transcript:")
    mock_print.assert_any_call(">> 帮我修一下 resume")
    replay_renderer.show_thinking.assert_not_called()
    replay_renderer.show_assistant.assert_not_called()
    replay_renderer.begin_stream.assert_called_once()
    replay_renderer.end_stream.assert_called_once()
    consume_calls = replay_renderer.consume_event.call_args_list
    assert len(consume_calls) == 2
    assert consume_calls[0].args[0].type == "thinking_delta"
    assert consume_calls[0].args[0].source_mode == "replayed"
    assert consume_calls[0].args[0].payload == {"text": "先确认 resume 回放链路是否绕过了 renderer。"}
    assert consume_calls[1].args[0].type == "content_delta"
    assert consume_calls[1].args[0].source_mode == "replayed"
    assert consume_calls[1].args[0].payload == {"text": "我先检查 session db。"}
    replay_renderer.show_tool_result.assert_called_once_with(
        "memory",
        "Memory updated. Usage: 20/1375 chars (1 entries)",
    )


def test_cli_renders_resume_transcript_with_markdown_for_legacy_renderer():
    engine = FakeEngine()
    engine.state = SimpleNamespace(session_db=object())
    replay_renderer = SimpleNamespace(
        show_thinking=MagicMock(),
        show_assistant=MagicMock(),
        show_tool_result=MagicMock(),
    )
    engine._renderer = replay_renderer
    transcript = [
        {"role": "user", "content": "帮我修一下 resume"},
        {
            "role": "assistant",
            "content": "我先检查 session db。",
            "reasoning": "先确认 resume 回放链路是否绕过了 renderer。",
        },
    ]

    with (
        patch.object(agent_loop, "execute_resume_command", return_value=SimpleNamespace(
            handled=True,
            output="Resuming session abc123...",
            resume_session_id="abc123",
            transcript_messages=transcript,
        )),
        patch.object(agent_loop.console, "print") as mock_print,
    ):
        should_continue, resume_id = agent_loop.handle_input("/resume abc123", engine)

    assert should_continue is True
    assert resume_id == "abc123"
    replay_renderer.show_thinking.assert_not_called()
    assert any(
        call.args
        and isinstance(call.args[0], Text)
        and call.args[0].plain == "先确认 resume 回放链路是否绕过了 renderer。"
        and call.args[0].style == "dim"
        for call in mock_print.call_args_list
    )
    replay_renderer.show_assistant.assert_called_once_with("我先检查 session db。")


def test_cli_resume_keeps_tool_name_mapping_when_assistant_content_is_empty():
    engine = FakeEngine()
    engine.state = SimpleNamespace(session_db=object())
    replay_renderer = SimpleNamespace(
        show_tool_result=MagicMock(),
    )
    engine._renderer = replay_renderer
    transcript = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "name": "memory", "args": {}}],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "Memory updated. Usage: 20/1375 chars (1 entries)"},
    ]

    with patch.object(agent_loop, "execute_resume_command", return_value=SimpleNamespace(
        handled=True,
        output="Resuming session abc123...",
        resume_session_id="abc123",
        transcript_messages=transcript,
    )):
        should_continue, resume_id = agent_loop.handle_input("/resume abc123", engine)

    assert should_continue is True
    assert resume_id == "abc123"
    replay_renderer.show_tool_result.assert_called_once_with(
        "memory",
        "Memory updated. Usage: 20/1375 chars (1 entries)",
    )


def test_cli_returns_true_for_empty_input():
    engine = FakeEngine()
    should_continue, resume_id = agent_loop.handle_input("  ", engine)
    assert should_continue is True
    assert resume_id is None
    assert engine.commands == []
    assert engine.messages == []


def test_line_buffer_backspace_deletes_one_cjk_character():
    buffer = agent_loop.LineBuffer()

    buffer.insert("你好")
    deleted = buffer.backspace()

    assert deleted is True
    assert buffer.text == "你"
    assert buffer.cursor == 1


def test_line_buffer_backspace_stops_at_prompt_boundary():
    buffer = agent_loop.LineBuffer()

    deleted = buffer.backspace()

    assert deleted is False
    assert buffer.text == ""
    assert buffer.cursor == 0


def test_line_buffer_cursor_column_counts_wide_characters():
    buffer = agent_loop.LineBuffer()

    buffer.insert("你a")

    assert buffer.cursor_column(prompt=">> ") == 6


def test_handle_input_drops_surrogate_characters_before_submit():
    engine = FakeEngine()

    with patch.object(agent_loop, "render_markdown"):
        agent_loop.handle_input("你\udce5好", engine)

    assert engine.messages == ["你好"]


def test_run_abort_monitor_uses_cbreak_not_raw_for_live_cancellation():
    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return 0

    stdin = FakeStdin()
    callback_calls = []

    with (
        patch.object(agent_loop.termios, "tcgetattr", return_value=["orig"]) as mock_getattr,
        patch.object(agent_loop.termios, "tcsetattr") as mock_setattr,
        patch.object(agent_loop.tty, "setcbreak") as mock_setcbreak,
        patch.object(agent_loop.tty, "setraw") as mock_setraw,
        patch.object(agent_loop, "select", return_value=([], [], [])),
    ):
        with agent_loop.RunAbortMonitor(stdin, lambda: callback_calls.append(True)):
            pass

    mock_getattr.assert_called_once_with(0)
    mock_setcbreak.assert_called_once_with(0)
    mock_setraw.assert_not_called()
    mock_setattr.assert_called_once()
    assert callback_calls == []
