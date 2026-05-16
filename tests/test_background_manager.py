"""BackgroundManager 单元测试。"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from core.background.manager import BackgroundManager
from core.background.models import BackgroundNotification, RuntimeTaskRecord
from core.tools.context import ToolInvocationOutcome, ToolOutcomeStatus


def _mock_context():
    ctx = MagicMock()
    ctx.tool_call_id = "test_call_0"
    ctx.session_state = MagicMock()
    return ctx


def _mock_offloader():
    o = MagicMock()
    o.maybe_persist.side_effect = lambda tid, content, **kw: content
    return o


def _mock_registry(content="(no output)"):
    r = MagicMock()
    r.execute.return_value = ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[{"role": "tool", "tool_call_id": "bg-0", "content": content}],
    )
    return r


def _wait_for_notifications(mgr, expected: int, timeout: float = 3.0) -> list[BackgroundNotification]:
    """轮询直到通知数达到 expected，避免裸 sleep 导致的 flaky 测试。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        notifs = mgr.drain_notifications()
        if len(notifs) >= expected:
            return notifs
        time.sleep(0.05)
    return mgr.drain_notifications()


def _wait_for_status(mgr, task_id: str, status: str, timeout: float = 3.0) -> RuntimeTaskRecord | None:
    """轮询直到任务达到目标 status。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = mgr.check(task_id)
        if rec is not None and rec.status == status:
            return rec
        time.sleep(0.05)
    return mgr.check(task_id)


class TestSubmit:
    def test_returns_task_id(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        tid, err = mgr.submit("echo hello")
        assert tid
        assert err == ""

    def test_rejects_over_limit(self):
        slow = MagicMock()
        slow.execute.side_effect = lambda *a, **kw: (
            time.sleep(1),
            ToolInvocationOutcome(
                status=ToolOutcomeStatus.SUCCESS,
                messages=[{"role": "tool", "tool_call_id": "x", "content": "done"}],
            ),
        )[1]
        mgr = BackgroundManager(
            tool_registry=slow,
            create_context=_mock_context,
            offloader=_mock_offloader(),
            max_concurrent=1,
        )
        tid1, _ = mgr.submit("sleep 1")
        assert tid1
        tid2, err2 = mgr.submit("echo x")
        assert tid2 == ""
        assert "上限" in err2

    def test_completed_notification(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry("3 passed"),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        mgr.submit("pytest")
        notifs = _wait_for_notifications(mgr, 1)
        assert len(notifs) == 1
        assert notifs[0].type == "background_completed"
        assert "3 passed" in notifs[0].preview

    def test_failure_status(self):
        fail = MagicMock()
        fail.execute.return_value = ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            messages=[{"role": "tool", "tool_call_id": "x", "content": "Error: exit 1"}],
        )
        mgr = BackgroundManager(
            tool_registry=fail,
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        tid, _ = mgr.submit("false")
        rec = _wait_for_status(mgr, tid, "failed")
        assert rec is not None
        assert rec.status == "failed"

    def test_exception_becomes_failed(self):
        crash = MagicMock()
        crash.execute.side_effect = RuntimeError("boom")
        mgr = BackgroundManager(
            tool_registry=crash,
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        mgr.submit("crash")
        notifs = _wait_for_notifications(mgr, 1)
        assert len(notifs) == 1
        assert notifs[0].type == "background_failed"
        assert "boom" in notifs[0].preview


class TestDrainNotifications:
    def test_drain_clears_queue(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        mgr.submit("echo 1")
        mgr.submit("echo 2")
        notifs = _wait_for_notifications(mgr, 2)
        assert len(notifs) == 2
        assert len(mgr.drain_notifications()) == 0


class TestCheck:
    def test_check_running(self):
        slow = MagicMock()
        slow.execute.side_effect = lambda *a, **kw: (
            time.sleep(2),
            ToolInvocationOutcome(
                status=ToolOutcomeStatus.SUCCESS,
                messages=[{"role": "tool", "tool_call_id": "x", "content": "done"}],
            ),
        )[1]
        mgr = BackgroundManager(
            tool_registry=slow,
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        tid, _ = mgr.submit("sleep 2")
        rec = mgr.check(tid)
        assert rec is not None
        assert rec.status == "running"

    def test_check_unknown_returns_none(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        assert mgr.check("nonexistent") is None


class TestAllTasks:
    def test_returns_all_submitted_tasks(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        tid1, _ = mgr.submit("echo 1")
        tid2, _ = mgr.submit("echo 2")
        tasks = mgr.all_tasks()
        ids = {t.id for t in tasks}
        assert tid1 in ids
        assert tid2 in ids

    def test_returns_empty_when_no_tasks(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        assert mgr.all_tasks() == []


class TestCleanup:
    def test_removes_old_tasks(self):
        mgr = BackgroundManager(
            tool_registry=_mock_registry(),
            create_context=_mock_context,
            offloader=_mock_offloader(),
        )
        old = RuntimeTaskRecord(
            id="old1",
            tool_name="bash",
            command="echo old",
            status="completed",
            started_at=time.time() - 8 * 86400,
            result_preview="done",
        )
        mgr._tasks["old1"] = old
        mgr.cleanup_stale_tasks(max_age_days=7)
        assert "old1" not in mgr._tasks
