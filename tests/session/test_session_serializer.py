from __future__ import annotations

import pytest

from core.session.serializer import SessionSerializer
from core.session.state import SessionState, TodoItem, TodoState
from core.tasks.models import TaskRecord, TaskState, TaskStatus, TaskExecutionMode


class TestSessionSerializerRoundTrip:
    def test_basic_round_trip(self):
        state = SessionState(conversation_messages=[])
        state.user_intents = ["hello", "help"]
        state.usage_totals = {"input_tokens": 100, "output_tokens": 50}
        state.system_prompt_override = "be concise"

        data = SessionSerializer.serialize(state)
        restored = SessionSerializer.deserialize(data)

        assert restored.user_intents == ["hello", "help"]
        assert restored.usage_totals == {"input_tokens": 100, "output_tokens": 50}
        assert restored.system_prompt_override == "be concise"

    def test_skip_fields_not_in_output(self):
        state = SessionState(conversation_messages=[])
        state.prompt_cache = {"key": "value"}
        state.skills_revision = "abc"

        data = SessionSerializer.serialize(state)
        assert "prompt_cache" not in data
        assert "skills_revision" not in data
        assert "memory_store" not in data
        assert "session_db" not in data

    def test_todo_state_round_trip(self):
        state = SessionState(conversation_messages=[])
        state.todo_state = TodoState(
            items=[
                TodoItem(content="task A", active_form="Doing A", status="pending"),
                TodoItem(content="task B", active_form="Doing B", status="completed"),
            ],
            last_write_turn=5,
            last_reminder_turn=3,
        )

        data = SessionSerializer.serialize(state)
        restored = SessionSerializer.deserialize(data)

        assert len(restored.todo_state.items) == 2
        assert restored.todo_state.items[0].content == "task A"
        assert restored.todo_state.items[1].status == "completed"
        assert restored.todo_state.last_write_turn == 5

    def test_session_id_fallback(self):
        data = {"conversation_messages": [], "session_id": ""}
        restored = SessionSerializer.deserialize(data)
        assert restored.session_id  # should get a generated ID

    def test_empty_conversation_messages(self):
        data = {}
        restored = SessionSerializer.deserialize(data)
        assert restored.conversation_messages == []

    def test_memory_review_counters_round_trip(self):
        state = SessionState(conversation_messages=[])
        state.user_turn_count = 7
        state.turns_since_memory_review = 2
        state.memory_review_interval = 3

        data = SessionSerializer.serialize(state)
        restored = SessionSerializer.deserialize(data)

        assert restored.user_turn_count == 7
        assert restored.turns_since_memory_review == 2
        assert restored.memory_review_interval == 3
