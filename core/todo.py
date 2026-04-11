"""Todo 基础类型定义。

TodoManager 和标记解析已删除（改用 todo_manage 工具）。
保留 TodoStatus 枚举和 TodoItem 数据类供其他模块使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TodoStatus(Enum):
    """任务状态枚举。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TodoItem:
    """单个任务项。"""
    id: int
    content: str
    status: TodoStatus = TodoStatus.PENDING
