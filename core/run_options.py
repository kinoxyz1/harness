from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunDisplayOptions:
    """控制一次运行过程中是否输出日志。"""

    quiet: bool = False
