from __future__ import annotations

import json
import time

import pytest

from core.session.db import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(
        db_path=tmp_path / "state.db",
        sessions_dir=tmp_path / "sessions",
    )


class TestSessionDBSchema:
    def test_tables_created(self, db):
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions', 'messages')"
        )
        tables = {row[0] for row in cur.fetchall()}
        assert "sessions" in tables
        assert "messages" in tables

    def test_fts_table_created(self, db):
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        )
        assert cur.fetchone() is not None


class TestSessionDBUpsert:
    def test_ensure_session_row(self, db):
        db.ensure_session_row("sess1", "Test Session")
        cur = db._conn.execute("SELECT id, title FROM sessions WHERE id = ?", ("sess1",))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "sess1"
        assert row[1] == "Test Session"

    def test_ensure_session_row_idempotent(self, db):
        db.ensure_session_row("sess1")
        db.ensure_session_row("sess1")
        cur = db._conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", ("sess1",))
        assert cur.fetchone()[0] == 1


class TestSessionDBMessages:
    def test_append_and_get_messages(self, db):
        db.ensure_session_row("sess1")
        messages = [
            {"role": "user", "content": "hello", "_meta": {"created_at": time.time()}},
            {"role": "assistant", "content": "hi there", "_meta": {"created_at": time.time()}},
        ]
        db.append_messages("sess1", messages)
        result = db.get_messages("sess1")
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "hi there"

    def test_append_empty_messages(self, db):
        db.ensure_session_row("sess1")
        db.append_messages("sess1", [])
        assert db.get_messages("sess1") == []

    def test_message_count_updated(self, db):
        db.ensure_session_row("sess1")
        db.append_messages("sess1", [
            {"role": "user", "content": "a", "_meta": {"created_at": time.time()}},
            {"role": "assistant", "content": "b", "_meta": {"created_at": time.time()}},
        ])
        cur = db._conn.execute("SELECT message_count FROM sessions WHERE id = ?", ("sess1",))
        assert cur.fetchone()[0] == 2

    def test_messages_with_tool_calls(self, db):
        db.ensure_session_row("sess1")
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "bash", "id": "c1", "args": {"cmd": "ls"}}],
            "_meta": {"created_at": time.time()},
        }
        db.append_messages("sess1", [msg])
        result = db.get_messages("sess1")
        assert len(result) == 1
        assert result[0]["tool_calls"] == [{"name": "bash", "id": "c1", "args": {"cmd": "ls"}}]


class TestSessionDBTokenCounts:
    def test_update_token_counts(self, db):
        db.ensure_session_row("sess1")
        db.update_token_counts("sess1", 100, 50)
        db.update_token_counts("sess1", 200, 100)
        cur = db._conn.execute(
            "SELECT total_input_tokens, total_output_tokens FROM sessions WHERE id = ?",
            ("sess1",),
        )
        row = cur.fetchone()
        assert row[0] == 300
        assert row[1] == 150


class TestSessionDBSnapshot:
    def test_save_session_snapshot(self, db, tmp_path):
        db.ensure_session_row("sess1")
        state_dict = {"session_id": "sess1", "conversation_messages": []}
        db.save_session_snapshot("sess1", state_dict)
        snapshot_path = tmp_path / "sessions" / "sess1" / "state.json"
        assert snapshot_path.exists()
        data = json.loads(snapshot_path.read_text())
        assert data["session_id"] == "sess1"


class TestSessionDBListSessions:
    def test_list_sessions(self, db):
        db.ensure_session_row("s1")
        db.ensure_session_row("s2")
        sessions = db.list_sessions()
        assert len(sessions) == 2
        ids = {s["id"] for s in sessions}
        assert ids == {"s1", "s2"}

    def test_list_sessions_limit(self, db):
        for i in range(30):
            db.ensure_session_row(f"s{i}")
        sessions = db.list_sessions(limit=5)
        assert len(sessions) == 5

    def test_list_sessions_empty(self, db):
        assert db.list_sessions() == []

    def test_list_sessions_include_preview_and_order_by_recent_activity(self, db):
        db.ensure_session_row("older")
        db.append_messages("older", [
            {"role": "user", "content": "Inspect the billing worker", "_meta": {"created_at": time.time()}},
        ])
        db.ensure_session_row("newer")
        db.append_messages("newer", [
            {"role": "user", "content": "Fix the resume transcript display", "_meta": {"created_at": time.time()}},
        ])

        sessions = db.list_sessions()

        assert sessions[0]["id"] == "newer"
        assert sessions[0]["preview"] == "Fix the resume transcript display"
        assert sessions[1]["id"] == "older"

    def test_list_sessions_recomputes_message_count_from_messages_table(self, db):
        db.ensure_session_row("stale")
        db.append_messages("stale", [
            {"role": "user", "content": "hello", "_meta": {"created_at": time.time()}},
            {"role": "assistant", "content": "world", "_meta": {"created_at": time.time()}},
        ])

        db._conn.execute("DELETE FROM messages WHERE session_id = ?", ("stale",))
        db._conn.commit()

        sessions = db.list_sessions()
        stale = next(item for item in sessions if item["id"] == "stale")
        assert stale["message_count"] == 0
        assert stale["preview"] == ""


class TestSessionDBSearch:
    def test_search_messages(self, db):
        db.ensure_session_row("sess1")
        db.append_messages("sess1", [
            {"role": "user", "content": "I need to debug the authentication module", "_meta": {"created_at": time.time()}},
            {"role": "user", "content": "What's the weather like today", "_meta": {"created_at": time.time()}},
        ])
        results = db.search_messages("authentication")
        assert len(results) >= 1
        assert "authentication" in results[0]["content"]

    def test_search_no_results(self, db):
        db.ensure_session_row("sess1")
        db.append_messages("sess1", [
            {"role": "user", "content": "hello world", "_meta": {"created_at": time.time()}},
        ])
        results = db.search_messages("xyznonexistent123")
        assert len(results) == 0
