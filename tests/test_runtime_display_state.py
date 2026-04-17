from core.query.state import RunState
from core.shared.run_options import RunDisplayOptions


def test_run_display_options_defaults_to_compact_trace() -> None:
    options = RunDisplayOptions()
    assert options.quiet is False
    assert options.runtime_trace == "compact"


def test_run_display_options_accepts_debug_trace() -> None:
    options = RunDisplayOptions(runtime_trace="debug")
    assert options.runtime_trace == "debug"


def test_run_state_starts_without_todo_display_snapshot() -> None:
    state = RunState()
    assert state.last_displayed_todo_items is None
