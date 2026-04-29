from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.transcript_rewriter import (
    build_post_compact_messages,
    create_compact_boundary,
    create_compact_summary,
)


def test_replace_working_transcript_replaces_in_memory_messages() -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "before"}])
    store = SessionStore(state)

    store.replace_working_transcript([{"role": "user", "content": "after"}])

    assert len(state.conversation_messages) == 1
    assert state.conversation_messages[0]["role"] == "user"
    assert state.conversation_messages[0]["content"] == "after"
    assert "created_at" in state.conversation_messages[0]["_meta"]


def test_store_managed_writes_stamp_created_at() -> None:
    state = SessionState(conversation_messages=[])
    store = SessionStore(state)

    store.append({"role": "assistant", "content": "tail"})
    store.prepend({"role": "user", "content": "head"})
    store.extend([{"role": "tool", "tool_call_id": "toolu_1", "content": "ok"}])

    assert [message["role"] for message in state.conversation_messages] == [
        "user",
        "assistant",
        "tool",
    ]
    for message in state.conversation_messages:
        assert "created_at" in message["_meta"]


def test_store_managed_writes_preserve_existing_created_at() -> None:
    state = SessionState(conversation_messages=[])
    store = SessionStore(state)

    store.append(
        {
            "role": "assistant",
            "content": "kept timestamp",
            "_meta": {"created_at": 123.0},
        }
    )
    store.replace_working_transcript(
        [
            {
                "role": "user",
                "content": "rewritten",
                "_meta": {"created_at": 456.0},
            }
        ]
    )

    assert state.conversation_messages == [
        {
            "role": "user",
            "content": "rewritten",
            "_meta": {"created_at": 456.0},
        }
    ]


def test_build_post_compact_messages_orders_boundary_summary_kept_restore() -> None:
    messages = build_post_compact_messages(
        boundary=create_compact_boundary(reason="summary_compact", summarized_messages=3),
        summary=create_compact_summary("Primary Request and Intent: inspect query loop"),
        kept=[{"role": "assistant", "content": "recent working set"}],
        runtime_restore=[
            {
                "role": "meta_runtime_restore",
                "kind": "file_runtime",
                "content": "core/query/loop.py",
            }
        ],
    )

    assert [message["role"] for message in messages] == [
        "meta_compact_boundary",
        "meta_compact_summary",
        "assistant",
        "meta_runtime_restore",
    ]
