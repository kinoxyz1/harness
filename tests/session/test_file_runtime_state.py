from __future__ import annotations

from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState
from core.shared.config import MAX_OUTPUT_CHARS
from core.tools.context import (
    FileState,
    RunUpdateKind,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
)


def _make_context(tmp_path, state: SessionState | None = None) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    if state is not None:
        ctx.bind_runtime(session_state=state)
    return ctx


def test_bind_runtime_does_not_alias_tool_file_cache_to_session_state(tmp_path) -> None:
    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)

    assert ctx._file_state is not state.read_file_state

    ctx._file_state[str(tmp_path / "a.txt")] = FileState(
        content="alpha",
        timestamp=1.0,
        offset=None,
        limit=None,
    )

    assert str(tmp_path / "a.txt") not in state.read_file_state


def test_get_file_state_stale_cache_does_not_mutate_session_state(tmp_path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    state.read_file_state[str(file_path)] = FileState(
        content="stale",
        timestamp=0.0,
        offset=None,
        limit=None,
    )
    ctx = _make_context(tmp_path, state)

    cached = ctx.get_file_state(str(file_path))

    assert cached is None
    assert str(file_path) in state.read_file_state


def test_read_file_returns_upsert_file_state_update(tmp_path) -> None:
    from core.tools.builtin.read_file import handle

    file_path = tmp_path / "a.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    ctx._set_call_identity(name="read_file", call_id="toolu_read", turn=1)

    outcome = handle({"path": str(file_path)}, ctx)

    assert isinstance(outcome, ToolInvocationOutcome)
    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert [update.kind for update in outcome.session_updates] == [SessionUpdateKind.UPSERT_FILE_STATE]
    assert outcome.run_updates == []

    apply_session_update(state, outcome.session_updates[0])

    saved = state.read_file_state[str(file_path)]
    assert isinstance(saved, FileState)
    assert saved.content == "alpha\nbeta"


def test_read_file_self_pages_large_files_before_runtime_truncation(tmp_path) -> None:
    from core.tools.builtin.read_file import handle

    file_path = tmp_path / "large.md"
    file_path.write_text(
        "\n".join(f"{i} " + ("x" * 240) for i in range(1, 726)),
        encoding="utf-8",
    )
    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    ctx._set_call_identity(name="read_file", call_id="toolu_read", turn=1)

    outcome = handle({"path": str(file_path)}, ctx)

    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert len(outcome.messages[0]["content"]) < MAX_OUTPUT_CHARS
    assert "继续读取请使用 offset=" in outcome.messages[0]["content"]

    apply_session_update(state, outcome.session_updates[0])
    saved = state.read_file_state[str(file_path)]
    assert saved.is_full_read is False
    assert saved.offset == 1
    assert saved.limit is not None
    assert saved.total_lines == 725


def test_edit_file_rejects_large_file_after_only_first_chunk_was_visible(tmp_path) -> None:
    from core.tools.builtin.edit_file import handle as edit_handle
    from core.tools.builtin.read_file import handle as read_handle

    file_path = tmp_path / "large.md"
    file_path.write_text(
        "\n".join(f"{i} " + ("x" * 240) for i in range(1, 726)),
        encoding="utf-8",
    )
    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    ctx._set_call_identity(name="read_file", call_id="toolu_read", turn=1)

    read_outcome = read_handle({"path": str(file_path)}, ctx)
    apply_session_update(state, read_outcome.session_updates[0])

    ctx._set_call_identity(name="edit_file", call_id="toolu_edit", turn=2)
    edit_outcome = edit_handle(
        {
            "path": str(file_path),
            "old_string": "1 " + ("x" * 240),
            "new_string": "patched",
        },
        ctx,
    )

    assert edit_outcome.status == ToolOutcomeStatus.FAILURE
    assert edit_outcome.error == "not_read"


def test_write_file_returns_file_state_and_mark_modified_updates(tmp_path) -> None:
    from core.tools.builtin.write_file import handle

    state = SessionState(conversation_messages=[])
    run_state = RunState()
    ctx = _make_context(tmp_path, state)
    ctx._set_call_identity(name="write_file", call_id="toolu_write", turn=1)

    outcome = handle({"path": "out.txt", "content": "hello\nworld\n"}, ctx)

    assert isinstance(outcome, ToolInvocationOutcome)
    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert [update.kind for update in outcome.session_updates] == [SessionUpdateKind.UPSERT_FILE_STATE]
    assert [update.kind for update in outcome.run_updates] == [RunUpdateKind.MARK_FILE_MODIFIED]

    apply_session_update(state, outcome.session_updates[0])
    apply_run_update(run_state, outcome.run_updates[0])

    abs_path = str(tmp_path / "out.txt")
    assert abs_path in state.read_file_state
    assert abs_path in run_state.files_modified


def test_edit_file_returns_file_state_and_mark_modified_updates(tmp_path) -> None:
    from core.tools.builtin.edit_file import handle

    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    state = SessionState(conversation_messages=[])
    state.read_file_state[str(file_path)] = FileState(
        content="alpha\nbeta\n",
        timestamp=file_path.stat().st_mtime,
        offset=None,
        limit=None,
    )
    run_state = RunState()
    ctx = _make_context(tmp_path, state)
    ctx._set_call_identity(name="edit_file", call_id="toolu_edit", turn=2)

    outcome = handle(
        {
            "path": str(file_path),
            "old_string": "beta",
            "new_string": "gamma",
        },
        ctx,
    )

    assert isinstance(outcome, ToolInvocationOutcome)
    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert [update.kind for update in outcome.session_updates] == [SessionUpdateKind.UPSERT_FILE_STATE]
    assert [update.kind for update in outcome.run_updates] == [RunUpdateKind.MARK_FILE_MODIFIED]

    apply_session_update(state, outcome.session_updates[0])
    apply_run_update(run_state, outcome.run_updates[0])

    assert "gamma" in state.read_file_state[str(file_path)].content
    assert str(file_path) in run_state.files_modified


def test_find_returns_message_only_outcome(tmp_path) -> None:
    from core.tools.builtin.find import handle

    (tmp_path / "alpha.py").write_text("print('alpha')\n", encoding="utf-8")
    ctx = _make_context(tmp_path)
    ctx._set_call_identity(name="find", call_id="toolu_find", turn=1)

    outcome = handle({"pattern": "*.py"}, ctx)

    assert isinstance(outcome, ToolInvocationOutcome)
    assert outcome.status == ToolOutcomeStatus.SUCCESS
    assert outcome.session_updates == []
    assert outcome.run_updates == []
    assert "alpha.py" in outcome.messages[0]["content"]


def test_bash_blocked_command_returns_blocked_outcome(tmp_path) -> None:
    from core.tools.builtin.bash import handle

    ctx = _make_context(tmp_path)
    ctx._set_call_identity(name="bash", call_id="toolu_bash", turn=1)

    outcome = handle({"command": "dd if=/dev/zero of=/tmp/x"}, ctx)

    assert isinstance(outcome, ToolInvocationOutcome)
    assert outcome.status == ToolOutcomeStatus.BLOCKED
    assert outcome.error == "blocked"
    assert outcome.session_updates == []
    assert outcome.run_updates == []
