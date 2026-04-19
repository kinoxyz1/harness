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
        return SimpleNamespace(final_output="reply")


def test_cli_routes_skills_command_to_handle_command():
    engine = FakeEngine()
    with patch.object(agent_loop.console, "print") as mock_print:
        result = agent_loop.handle_input("/skills list", engine)

    assert result is True
    assert engine.commands == ["/skills list"]
    assert engine.messages == []
    mock_print.assert_called_with("command output")


def test_cli_routes_normal_input_to_submit():
    engine = FakeEngine()
    with patch.object(agent_loop.console, "print") as mock_print:
        result = agent_loop.handle_input("hello world", engine)

    assert result is True
    assert engine.commands == []
    assert engine.messages == ["hello world"]
    mock_print.assert_called_with("reply")


def test_cli_returns_true_for_empty_input():
    engine = FakeEngine()
    result = agent_loop.handle_input("  ", engine)
    assert result is True
    assert engine.commands == []
    assert engine.messages == []
