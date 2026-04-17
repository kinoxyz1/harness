from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunDisplayOptions:
    """控制一次运行过程的显示模式（静默与运行时 trace 级别）。"""

    quiet: bool = False
    runtime_trace: Literal["compact", "debug"] = "compact"
