from pathlib import Path

from core.session.content_replacement import (
    ContentReplacementRecord,
    ContentReplacementState,
    clone_content_replacement_state,
    reconstruct_content_replacement_state,
)
from core.session.state import SessionState
from core.session.store import SessionStore


def test_clone_content_replacement_state_copies_seen_ids_and_replacements(tmp_path: Path) -> None:
    source = ContentReplacementState(
        seen_ids={"toolu_1"},
        replacements={"toolu_1": "<persisted-output>saved</persisted-output>"},
    )

    cloned = clone_content_replacement_state(source)

    assert cloned is not source
    assert cloned.seen_ids == {"toolu_1"}
    assert cloned.replacements == {"toolu_1": "<persisted-output>saved</persisted-output>"}


def test_reconstruct_content_replacement_state_freezes_seen_results() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_keep", "name": "find", "args": {"pattern": "x"}}]},
        {"role": "tool", "tool_call_id": "toolu_keep", "content": "small result"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_big", "name": "bash", "args": {"command": "cat log"}}]},
        {"role": "tool", "tool_call_id": "toolu_big", "content": "<persisted-output>saved</persisted-output>"},
    ]
    records = [
        ContentReplacementRecord(
            kind="tool-result",
            tool_use_id="toolu_big",
            replacement="<persisted-output>saved</persisted-output>",
        )
    ]

    restored = reconstruct_content_replacement_state(messages, records)

    assert restored.seen_ids == {"toolu_keep", "toolu_big"}
    assert restored.replacements == {"toolu_big": "<persisted-output>saved</persisted-output>"}


def test_session_store_exposes_session_scoped_tool_result_dir_without_creating_it(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[], session_id="sess1234")
    store = SessionStore(state, working_dir=tmp_path)

    assert store.tool_result_dir == tmp_path / ".harness" / "sessions" / "sess1234" / "tool-results"
    assert not store.tool_result_dir.exists()
