"""Tests for Anthropic client factory behavior."""
from __future__ import annotations

import pytest

import core.llm.factory as factory


def test_create_llm_client_sets_explicit_timeout_for_long_nonstreaming_requests(monkeypatch):
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    monkeypatch.setattr(factory, "API_KEY", "test-key")
    monkeypatch.setattr(factory, "BASE_URL", "")

    client = factory.create_llm_client()

    def fail_after_timeout_check(*args, **kwargs):
        raise RuntimeError("post called")

    monkeypatch.setattr(client.messages, "_post", fail_after_timeout_check)

    with pytest.raises(RuntimeError, match="post called"):
        client.messages.create(
            model="claude-sonnet-4-6-20250514",
            system="You are helpful.",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50_000,
        )
