from __future__ import annotations

from core.session.state import SessionState
from core.tools.context import FileState, ToolUseContext


def test_bind_runtime_aliases_tool_file_cache_to_session_state(tmp_path) -> None:
    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)

    ctx.bind_runtime(session_state=state)
    ctx.set_file_state(
        str(tmp_path / "a.txt"),
        FileState(content="alpha", timestamp=1.0, offset=None, limit=None),
    )

    assert str(tmp_path / "a.txt") in state.read_file_state


def test_bind_runtime_without_session_state_keeps_own_cache(tmp_path) -> None:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.set_file_state(
        str(tmp_path / "b.txt"),
        FileState(content="beta", timestamp=2.0, offset=None, limit=None),
    )

    assert str(tmp_path / "b.txt") in ctx._file_state
    assert str(tmp_path / "b.txt") not in {}


def test_read_file_updates_session_read_file_state(tmp_path) -> None:
    from core.tools.builtin.read_file import handle

    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)

    result = handle({"path": str(file_path)}, ctx)

    assert result.success is True
    saved = state.read_file_state[str(file_path)]
    assert isinstance(saved, FileState)
    assert saved.content == "alpha\nbeta"
