from core.session.pairing_repair import repair_tool_result_pairs


def test_inserts_placeholder_for_missing_tool_result() -> None:
    repaired = repair_tool_result_pairs([
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
    ])

    assert repaired == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "(cancelled)"},
    ]


def test_drops_orphaned_tool_results() -> None:
    repaired = repair_tool_result_pairs([
        {"role": "tool", "tool_call_id": "toolu_missing", "content": "orphan"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "/tmp"},
    ])

    assert repaired == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "/tmp"},
    ]


def test_drops_orphan_and_still_inserts_missing_placeholder() -> None:
    repaired = repair_tool_result_pairs([
        {"role": "tool", "tool_call_id": "toolu_missing", "content": "orphan"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
    ])

    assert repaired == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "(cancelled)"},
    ]


def test_strips_duplicate_tool_use_ids() -> None:
    repaired = repair_tool_result_pairs([
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}},
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "/tmp"},
    ])

    assert repaired == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pwd"}},
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "/tmp"},
    ]
