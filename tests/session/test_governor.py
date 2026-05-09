from types import SimpleNamespace

from core.query.state import RunState
from core.session.governor import ContextGovernor
from core.session.offloader import ToolResultOffloader
from core.session.query_context import ContextBlock
from core.session.state import SessionState
from core.session.store import SessionStore


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
