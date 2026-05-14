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
            {"id": "abc123", "message_count": 10, "updated_at": 1000.0},
            {"id": "def456", "message_count": 5, "updated_at": 999.0},
        ]
        result = execute_resume_command("/resume list", session_db=db)
        assert "abc123" in result.output
        assert "10 messages" in result.output
        assert result.resume_session_id is None

    def test_resume_specific_session(self):
        result = execute_resume_command("/resume abc123", session_db=None)
        assert result.handled is True
        assert result.resume_session_id == "abc123"
        assert "Resuming" in result.output

    def test_resume_invalid_usage(self):
        result = execute_resume_command("/resume too many args", session_db=None)
        assert "Usage" in result.output
