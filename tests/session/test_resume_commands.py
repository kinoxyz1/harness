from __future__ import annotations

from unittest.mock import MagicMock

from core.session.commands import (
    CommandResult,
    execute_resume_command,
    is_resume_command,
)


class TestIsResumeCommand:
    def test_resume_command(self):
        assert is_resume_command("/resume") is True

    def test_resume_with_id(self):
        assert is_resume_command("/resume abc123") is True

    def test_resume_list(self):
        assert is_resume_command("/resume list") is True

    def test_not_resume(self):
        assert is_resume_command("/skills list") is False
        assert is_resume_command("hello") is False


class TestExecuteResumeCommand:
    def test_resume_list_no_db(self):
        result = execute_resume_command("/resume list", session_db=None)
        assert result.handled is True
        assert "not available" in result.output.lower() or "not available" in result.output
        assert result.resume_session_id is None

    def test_resume_list_empty(self):
        db = MagicMock()
        db.list_sessions.return_value = []
        result = execute_resume_command("/resume", session_db=db)
        assert "No previous sessions" in result.output
        assert result.resume_session_id is None

    def test_resume_list_with_sessions(self):
        db = MagicMock()
        db.list_sessions.return_value = [
            {
                "id": "abc123",
                "message_count": 10,
                "updated_at": 1000.0,
                "preview": "Debug the auth pipeline",
            },
            {
                "id": "def456",
                "message_count": 5,
                "updated_at": 999.0,
                "preview": "Write deployment notes",
            },
        ]
        result = execute_resume_command("/resume list", session_db=db)
        assert "most recently active first" in result.output.lower()
        assert "abc123" in result.output
        assert "10 messages" in result.output
        assert "Debug the auth pipeline" in result.output
        assert result.resume_session_id is None

    def test_resume_list_filters_empty_sessions(self):
        db = MagicMock()
        db.list_sessions.return_value = [
            {"id": "empty123", "message_count": 0, "updated_at": 1001.0, "preview": ""},
            {"id": "full456", "message_count": 4, "updated_at": 1000.0, "preview": "Fix login loop"},
        ]
        result = execute_resume_command("/resume", session_db=db)
        assert "full456" in result.output
        assert "Fix login loop" in result.output
        assert "empty123" not in result.output

    def test_resume_specific_session(self):
        db = MagicMock()
        db.get_messages.return_value = [
            {"role": "user", "content": "Help me fix the auth module"},
            {"role": "assistant", "content": "First I'll inspect the login flow."},
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "Now I'll patch the guard clause."},
        ]
        result = execute_resume_command("/resume abc123", session_db=db)
        assert result.handled is True
        assert result.resume_session_id == "abc123"
        assert "Resuming" in result.output
        assert result.transcript_messages == db.get_messages.return_value

    def test_resume_specific_session_shows_all_messages_not_recent_slice(self):
        db = MagicMock()
        messages = [
            {"role": "user", "content": f"user-{i}"}
            for i in range(10)
        ]
        db.get_messages.return_value = messages
        result = execute_resume_command("/resume abc123", session_db=db)
        assert result.transcript_messages == messages

    def test_resume_invalid_usage(self):
        result = execute_resume_command("/resume too many args", session_db=None)
        assert "Usage" in result.output
