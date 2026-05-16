"""后台任务的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class RuntimeTaskRecord:
    """单个后台任务的运行时记录。"""
    id: str
    tool_name: str       # 固定 "bash"
    command: str         # 命令摘要，最长 200 字符
    status: Literal["running", "completed", "failed", "timeout"]
    started_at: float    # time.time()
    result_preview: str  # 前 N 字符摘要


@dataclass(slots=True)
class BackgroundNotification:
    """后台任务完成后投递给主循环的通知。"""
    type: Literal["background_completed", "background_failed", "background_timeout"]
    task_id: str
    preview: str
