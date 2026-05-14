"""第二层：SQLite + JSON 的对话持久化。"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable


class SessionDB:
    def __init__(self, db_path: str | Path, sessions_dir: str | Path) -> None:
        self._db_path = Path(db_path)
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                tool_call_id TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                reasoning TEXT DEFAULT '',
                timestamp REAL NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        self._conn.commit()

    def _execute_write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        for _ in range(10):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = fn(self._conn)
                self._conn.commit()
                return result
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    time.sleep(random.uniform(0.02, 0.1))
                    continue
                raise
        raise sqlite3.OperationalError("database remained locked after retries")

    def ensure_session_row(self, session_id: str, title: str = "") -> None:
        now = time.time()

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, title, now, now),
            )

        self._execute_write(_upsert)

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return

        def _insert(conn: sqlite3.Connection) -> None:
            for msg in messages:
                conn.execute(
                    """
                    INSERT INTO messages
                    (session_id, role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        msg.get("role", ""),
                        msg.get("content", ""),
                        json.dumps(msg.get("tool_calls", []), ensure_ascii=False) if msg.get("tool_calls") else "",
                        msg.get("tool_call_id", ""),
                        msg.get("name", ""),
                        msg.get("reasoning", ""),
                        msg.get("_meta", {}).get("created_at", time.time()),
                    ),
                )
            conn.execute(
                """
                UPDATE sessions
                SET updated_at = ?, message_count = (
                    SELECT COUNT(*) FROM messages WHERE session_id = ?
                )
                WHERE id = ?
                """,
                (time.time(), session_id, session_id),
            )

        self._execute_write(_insert)

    def update_token_counts(self, session_id: str, input_tokens: int, output_tokens: int) -> None:
        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE sessions
                SET total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (input_tokens, output_tokens, time.time(), session_id),
            )

        self._execute_write(_update)

    def save_session_snapshot(self, session_id: str, state_dict: dict[str, Any]) -> None:
        session_dir = self._sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "state.json"
        tmp = session_dir / ".state.json.tmp"
        tmp.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        )
        rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp in rows:
            msg: dict[str, Any] = {"role": role, "content": content}
            if tool_calls:
                msg["tool_calls"] = json.loads(tool_calls)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["name"] = tool_name
            if reasoning:
                msg["reasoning"] = reasoning
            msg["_meta"] = {"created_at": timestamp}
            result.append(msg)
        return result

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT id, title, message_count, updated_at
            FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {"id": row[0], "title": row[1], "message_count": row[2], "updated_at": row[3]}
            for row in cur.fetchall()
        ]

    def search_messages(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT m.role, m.content
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        )
        return [{"role": row[0], "content": row[1]} for row in cur.fetchall()]
