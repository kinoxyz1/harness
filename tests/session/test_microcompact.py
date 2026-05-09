from core.session.microcompact import MICROCOMPACT_PLACEHOLDER, apply_time_based_microcompact


def test_apply_time_based_microcompact_skips_read_file_and_only_clears_old_ephemeral_outputs() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "toolu_read", "name": "read_file", "args": {"path": "a.py"}}], "_meta": {"created_at": 10.0}},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "alpha", "_meta": {"created_at": 20.0}},
        {"role": "assistant", "tool_calls": [{"id": "toolu_find", "name": "find", "args": {"pattern": "needle"}}], "_meta": {"created_at": 30.0}},
        {"role": "tool", "tool_call_id": "toolu_find", "content": "old matches", "_meta": {"created_at": 40.0}},
        {"role": "assistant", "tool_calls": [{"id": "toolu_web", "name": "web_fetch", "args": {"url": "https://example.com"}}], "_meta": {"created_at": 200.0}},
        {"role": "tool", "tool_call_id": "toolu_web", "content": "fresh html", "_meta": {"created_at": 220.0}},
    ]

    compacted = apply_time_based_microcompact(
        messages,
        age_cutoff_seconds=100,
        keep_recent_trajectories=1,
    )

    assert compacted[1]["content"] == "alpha"
    assert compacted[3]["content"] == MICROCOMPACT_PLACEHOLDER
    assert compacted[5]["content"] == "fresh html"
