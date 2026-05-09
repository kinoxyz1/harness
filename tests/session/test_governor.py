from types import SimpleNamespace

from core.query.state import RunState
from core.session.governor import (
    AUTO_COMPACT_KEEP_LAST_MESSAGES,
    BLOCKING_GATE_KEEP_LAST_MESSAGES,
    ContextGovernor,
)
from core.session.offloader import ToolResultOffloader
from core.session.query_context import ContextBlock
from core.session.state import SessionState
from core.session.store import SessionStore


def test_keep_last_messages_constants() -> None:
    assert AUTO_COMPACT_KEEP_LAST_MESSAGES == 8
    assert BLOCKING_GATE_KEEP_LAST_MESSAGES == 4


def test_context_governor_runs_budget_preview_microcompact_then_summary(tmp_path) -> None:
    state = SessionState(
        conversation_messages=[
            {"role": "assistant", "tool_calls": [{"id": "toolu_big", "name": "web_fetch", "args": {"url": "https://example.com"}}]},
            {"role": "tool", "tool_call_id": "toolu_big", "content": "<persisted-output>\nOutput too large. Full output saved to: /tmp/toolu_big.txt\n\nPreview (first 20 bytes):\nabcdef\n</persisted-output>"},
            {"role": "user", "content": "x" * 70000},
        ]
    )
    store = SessionStore(state, working_dir=tmp_path)
    offloader = ToolResultOffloader(
        tool_result_dir=store.tool_result_dir,
        replacement_state=state.content_replacement_state,
    )
    governor = ContextGovernor(
        offloader=offloader,
        compact_service=SimpleNamespace(summarize_and_compact=lambda messages, **_: [{"role": "assistant", "content": "summary"}]),
        summary_gateway=object(),
        context_window_tokens=30000,
        max_output_tokens=10000,
    )

    prepared = governor.assess(
        session_state=state,
        run_state=RunState(),
        store=store,
        stable_system="stable",
        stable_tools=None,
        runtime_blocks=[ContextBlock(kind="environment", content="env", required=True, token_estimate=10)],
        overlay_blocks=[],
        query_source="main_loop",
    )

    assert prepared.observability["strategies_run"] == ["preview_strip", "microcompact", "auto_compact"]


def test_context_governor_recalc_prevents_unnecessary_auto_compact(tmp_path) -> None:
    """When microcompact reduces the water level below auto_compact threshold,
    auto_compact should NOT trigger. This tests the _recalc_water_level fix."""
    state = SessionState(
        conversation_messages=[
            # 3 bash calls: oldest gets compacted (not in keep_recent=2), recent 2 kept
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_bash1", "name": "bash", "args": {"command": "ls"}}],
                "_meta": {"created_at": 100.0},
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_bash1",
                "content": "x" * 28000,  # ~7000 tokens — pushes initial water level above threshold
                "_meta": {"created_at": 110.0},
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_bash2", "name": "bash", "args": {"command": "ls"}}],
                "_meta": {"created_at": 2000.0},
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_bash2",
                "content": "small",
                "_meta": {"created_at": 2010.0},
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_bash3", "name": "bash", "args": {"command": "ls"}}],
                "_meta": {"created_at": 2020.0},
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_bash3",
                "content": "small",
                "_meta": {"created_at": 2030.0},
            },
            {"role": "user", "content": "do something"},
        ]
    )
    store = SessionStore(state, working_dir=tmp_path)
    offloader = ToolResultOffloader(
        tool_result_dir=store.tool_result_dir,
        replacement_state=state.content_replacement_state,
    )

    summarize_called = False

    def mock_summarize(messages, **kwargs):
        nonlocal summarize_called
        summarize_called = True
        return [{"role": "assistant", "content": "summary"}]

    governor = ContextGovernor(
        offloader=offloader,
        compact_service=SimpleNamespace(summarize_and_compact=mock_summarize),
        summary_gateway=object(),
        context_window_tokens=30000,
        max_output_tokens=10000,
        time_fn=lambda: 10000.0,  # Make old messages old enough for microcompact (age > 1800s)
    )

    prepared = governor.assess(
        session_state=state,
        run_state=RunState(),
        store=store,
        stable_system="stable",
        stable_tools=None,
        runtime_blocks=[],
        overlay_blocks=[],
        query_source="main_loop",
    )

    # microcompact should clear the oldest bash output, reducing water level
    # auto_compact should NOT have been called because recalc drops below threshold
    assert "microcompact" in prepared.observability["strategies_run"]
    assert "auto_compact" not in prepared.observability["strategies_run"]
    assert not summarize_called
