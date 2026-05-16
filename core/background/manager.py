"""BackgroundManager — 后台任务的生命周期管理。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from core.background.models import BackgroundNotification, RuntimeTaskRecord
from core.shared.config import BG_MAX_CONCURRENT, BG_TASKS_DIR, BG_TASK_PREVIEW_CHARS

if TYPE_CHECKING:
    from core.session.offloader import ToolResultOffloader
    from core.tools import ToolRegistry
    from core.tools.context import ToolInvocationOutcome, ToolUseContext


class BackgroundManager:
    """管理后台任务：提交 → 后台执行 → 通知队列 → 排空。"""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        create_context: Callable[[], ToolUseContext],
        offloader: ToolResultOffloader,
        max_concurrent: int = BG_MAX_CONCURRENT,
    ) -> None:
        self._tasks: dict[str, RuntimeTaskRecord] = {}
        self._notifications: list[BackgroundNotification] = []
        self._lock = threading.Lock()
        self._registry = tool_registry
        self._create_context = create_context
        self._offloader = offloader
        self._max_concurrent = max_concurrent
        self._running_count = 0

    def submit(self, command: str, timeout: int = 300) -> tuple[str, str]:
        """提交后台任务。返回 (task_id, error_msg)。"""
        with self._lock:
            if self._running_count >= self._max_concurrent:
                return "", f"后台任务数已达上限 ({self._max_concurrent})，请等待已有任务完成"

            task_id = uuid4().hex[:8]
            self._tasks[task_id] = RuntimeTaskRecord(
                id=task_id,
                tool_name="bash",
                command=command[:200],
                status="running",
                started_at=time.time(),
                result_preview="",
            )
            self._running_count += 1

        thread = threading.Thread(
            target=self._execute,
            args=(task_id, command, timeout),
            daemon=True,
            name=f"bg-task-{task_id}",
        )
        thread.start()
        return task_id, ""

    def drain_notifications(self) -> list[BackgroundNotification]:
        """取走全部待处理通知。"""
        with self._lock:
            notifications = list(self._notifications)
            self._notifications.clear()
            return notifications

    def check(self, task_id: str) -> RuntimeTaskRecord | None:
        """查询单个任务状态。"""
        with self._lock:
            return self._tasks.get(task_id)

    def all_tasks(self) -> list[RuntimeTaskRecord]:
        """返回全部任务快照。"""
        with self._lock:
            return list(self._tasks.values())

    def cleanup_stale_tasks(self, max_age_days: int = 7) -> None:
        """清理超过 max_age_days 天的旧记录和磁盘文件。"""
        cutoff = time.time() - max_age_days * 86400
        to_remove: list[str] = []
        with self._lock:
            for tid, rec in self._tasks.items():
                if rec.started_at < cutoff and rec.status != "running":
                    to_remove.append(tid)
            for tid in to_remove:
                del self._tasks[tid]
        bg_dir = Path(BG_TASKS_DIR)
        if bg_dir.exists():
            for f in bg_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)

    def _execute(self, task_id: str, command: str, timeout: int = 300) -> None:
        """在后台线程里通过 registry.execute 走完整工具路径。"""
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            return
        try:
            context = self._create_context()
            context._set_call_identity(name="bash", call_id=f"bg-{task_id}", turn=0)
            outcome = self._registry.execute("bash", {"command": command, "timeout": timeout}, context)
            status = "completed"
            content = self._first_content(outcome)
            if outcome.status.value != "success":
                status = "failed"
        except Exception as exc:
            status = "failed"
            content = str(exc)

        self._offloader.maybe_persist(f"bg-{task_id}", content, tool_name="bash")

        preview = content[:BG_TASK_PREVIEW_CHARS]
        with self._lock:
            record.status = status  # type: ignore[assignment]
            record.result_preview = preview
            self._running_count -= 1
            self._notifications.append(BackgroundNotification(
                type=f"background_{status}",  # type: ignore[arg-type]
                task_id=task_id,
                preview=preview,
            ))

    @staticmethod
    def _first_content(outcome: Any) -> str:
        for msg in outcome.messages:
            if isinstance(msg, dict) and msg.get("content"):
                return str(msg["content"])
        return ""
