from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import pytest


agent_loop = import_module("01_agent_loop")


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.messages = []

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
