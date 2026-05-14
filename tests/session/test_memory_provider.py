from __future__ import annotations

import time

import pytest

from core.memory.provider import MemoryProvider, NoopProvider
from core.memory.local_provider import LocalMemoryProvider
from core.session.db import SessionDB


class TestNoopProvider:
    def test_name(self):
        assert NoopProvider().name == "noop"

    def test_is_available(self):
        assert NoopProvider().is_available() is True

    def test_initialize(self):
        p = NoopProvider()
        p.initialize("sess1")

    def test_prefetch_returns_empty(self):
        assert NoopProvider().prefetch("query") == ""


class TestLocalMemoryProvider:
    @pytest.fixture
    def provider(self, tmp_path):
        return LocalMemoryProvider(
            db_path=tmp_path / "state.db",
            sessions_dir=tmp_path / "sessions",
        )

    def test_name(self, provider):
        assert provider.name == "local-fts"

    def test_is_available(self, provider):
        assert provider.is_available() is True

    def test_prefetch_empty_initially(self, provider):
        assert provider.prefetch("test") == ""

    def test_queue_prefetch_no_results(self, provider):
        provider.queue_prefetch("nonexistent")
        assert provider.prefetch("nonexistent") == ""

    def test_queue_prefetch_with_results(self, provider):
        db = provider._db
        db.ensure_session_row("sess1")
        db.append_messages("sess1", [
            {"role": "user", "content": "debug the authentication system", "_meta": {"created_at": time.time()}},
        ])
        provider.queue_prefetch("authentication")
        result = provider.prefetch("authentication")
        assert "authentication" in result
        assert "[user]" in result

    def test_sync_turn_noop(self, provider):
        provider.sync_turn("user msg", "assistant msg")

    def test_on_memory_write_noop(self, provider):
        provider.on_memory_write("add", "memory", "fact")
