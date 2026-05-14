from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.memory.store import MemoryStore
from core.tools import registry
from core.tools.builtin.memory import SCHEMA, handle
from core.tools.context import (
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
)


class TestMemoryToolRegistration:
    def test_tool_is_registered(self):
        names = {schema["name"] for schema in registry.schemas()}
        assert "memory" in names

    def test_schema_structure(self):
        assert SCHEMA["name"] == "memory"
        assert "action" in SCHEMA["input_schema"]["properties"]
        assert "target" in SCHEMA["input_schema"]["properties"]
        assert SCHEMA["input_schema"]["required"] == ["action", "target"]


def _make_context(session_state=None):
    ctx = MagicMock(spec=ToolUseContext)
    ctx.tool_call_id = "call_123"
    ctx.session_state = session_state
    return ctx


def _make_state(tmp_path):
    from core.session.state import SessionState
    state = SessionState(conversation_messages=[])
    state.memory_store = MemoryStore(base_dir=tmp_path)
    state.memory_store.load_from_disk()
    return state


class TestMemoryToolHandle:
    def test_add_success(self, tmp_path):
        state = _make_state(tmp_path)
        ctx = _make_context(state)
        result = handle({"action": "add", "target": "memory", "content": "test fact"}, ctx)
        assert result.status == ToolOutcomeStatus.SUCCESS
        assert len(result.session_updates) == 1
        update = result.session_updates[0]
        assert update.kind == SessionUpdateKind.MEMORY_WRITE
        assert update.payload["action"] == "add"

    def test_add_no_store(self):
        ctx = _make_context(None)
        result = handle({"action": "add", "target": "memory", "content": "test"}, ctx)
        assert result.status == ToolOutcomeStatus.FAILURE
        assert result.error == "memory_store_unavailable"

    def test_add_failure(self, tmp_path):
        state = _make_state(tmp_path)
        ctx = _make_context(state)
        result = handle({"action": "add", "target": "memory", "content": ""}, ctx)
        assert result.status == ToolOutcomeStatus.FAILURE
        assert result.error == "memory_write_failed"

    def test_replace_success(self, tmp_path):
        state = _make_state(tmp_path)
        state.memory_store.add("memory", "old fact")
        ctx = _make_context(state)
        result = handle({"action": "replace", "target": "memory", "old_text": "old fact", "content": "new fact"}, ctx)
        assert result.status == ToolOutcomeStatus.SUCCESS

    def test_remove_success(self, tmp_path):
        state = _make_state(tmp_path)
        state.memory_store.add("memory", "fact to remove")
        ctx = _make_context(state)
        result = handle({"action": "remove", "target": "memory", "old_text": "fact to remove"}, ctx)
        assert result.status == ToolOutcomeStatus.SUCCESS

    def test_unknown_action(self, tmp_path):
        state = _make_state(tmp_path)
        ctx = _make_context(state)
        result = handle({"action": "invalid", "target": "memory"}, ctx)
        assert result.status == ToolOutcomeStatus.FAILURE

    def test_remove_uses_old_text_as_payload_content(self, tmp_path):
        state = _make_state(tmp_path)
        state.memory_store.add("memory", "the fact")
        ctx = _make_context(state)
        result = handle({"action": "remove", "target": "memory", "old_text": "the fact"}, ctx)
        assert result.session_updates[0].payload["content"] == "the fact"
