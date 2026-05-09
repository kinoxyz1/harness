from core.session.read_working_set import (
    collect_recent_read_restore_messages,
    read_tool_ids_to_protect,
)
from core.session.state import SessionState


def test_read_tool_ids_to_protect_tracks_recent_read_results() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_read_old", "name": "read_file", "args": {"path": "a.py"}}]},
        {"role": "tool", "tool_call_id": "toolu_read_old", "content": "alpha"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_bash", "name": "bash", "args": {"command": "pwd"}}]},
        {"role": "tool", "tool_call_id": "toolu_bash", "content": "pwd"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_read_new", "name": "read_file", "args": {"path": "b.py"}}]},
        {"role": "tool", "tool_call_id": "toolu_read_new", "content": "beta"},
    ]

    protected = read_tool_ids_to_protect(messages, keep_last_reads=2)

    assert protected == {"toolu_read_old", "toolu_read_new"}


def test_collect_recent_read_restore_messages_skips_files_already_kept_in_tail() -> None:
    state = SessionState(conversation_messages=[])
    state.read_file_state = {
        "/tmp/a.py": type("FileState", (), {"content": "alpha", "timestamp": 10.0, "is_full_read": True}),
        "/tmp/b.py": type("FileState", (), {"content": "beta", "timestamp": 20.0, "is_full_read": True}),
    }
    kept = [{"role": "tool", "tool_call_id": "toolu_read_b", "content": "beta"}]

    restored = collect_recent_read_restore_messages(state, kept_messages=kept, limit=2)

    assert len(restored) == 1
    assert restored[0]["kind"] == "file_runtime"
    assert "/tmp/a.py" in restored[0]["content"]
