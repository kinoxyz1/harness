from pathlib import Path

from core.session.content_replacement import ContentReplacementState
from core.session.offloader import ToolResultOffloader


def test_maybe_persist_writes_large_tool_results_and_freezes_replacement(tmp_path: Path) -> None:
    state = ContentReplacementState()
    offloader = ToolResultOffloader(tool_result_dir=tmp_path, replacement_state=state)

    replacement = offloader.maybe_persist(
        "toolu_big",
        "header\n" + ("x" * 60_000),
        tool_name="web_fetch",
    )

    saved = tmp_path / "toolu_big.txt"
    assert saved.exists()
    assert state.seen_ids == {"toolu_big"}
    assert state.replacements["toolu_big"] == replacement
    assert "<persisted-output>" in replacement
    assert str(saved) in replacement


def test_enforce_per_message_budget_reapplies_frozen_replacements_and_skips_read_file(tmp_path: Path) -> None:
    state = ContentReplacementState(
        seen_ids={"toolu_old"},
        replacements={"toolu_old": "<persisted-output>old</persisted-output>"},
    )
    offloader = ToolResultOffloader(tool_result_dir=tmp_path, replacement_state=state)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_old", "name": "bash", "args": {"command": "cat log"}},
                {"id": "toolu_read", "name": "read_file", "args": {"path": "alpha.py"}},
                {"id": "toolu_new", "name": "web_fetch", "args": {"url": "https://example.com"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_old", "content": "small"},
        {"role": "tool", "tool_call_id": "toolu_read", "content": "r" * 120000},
        {"role": "tool", "tool_call_id": "toolu_new", "content": "n" * 250_000},
    ]

    rewritten = offloader.enforce_per_message_budget(messages)

    assert rewritten[1]["content"] == "<persisted-output>old</persisted-output>"
    assert rewritten[2]["content"] == "r" * 120000
    assert "<persisted-output>" in rewritten[3]["content"]
    assert "toolu_new" in state.seen_ids
