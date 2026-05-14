from __future__ import annotations

from pathlib import Path

from core.memory.provider import MemoryProvider
from core.session.db import SessionDB


class LocalMemoryProvider(MemoryProvider):
    def __init__(self, db_path: str | Path, sessions_dir: str | Path) -> None:
        self._db = SessionDB(db_path=db_path, sessions_dir=sessions_dir)
        self._session_id = ""
        self._prefetch_cache = ""

    @property
    def name(self) -> str:
        return "local-fts"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id

    def prefetch(self, query: str) -> str:
        return self._prefetch_cache

    def queue_prefetch(self, query: str) -> None:
        if not query.strip():
            self._prefetch_cache = ""
            return
        rows = self._db.search_messages(query, limit=5)
        if not rows:
            self._prefetch_cache = ""
            return
        self._prefetch_cache = "\n".join(
            f"[{row['role']}] {row['content'][:300]}"
            for row in rows
        )

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        return

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        return
