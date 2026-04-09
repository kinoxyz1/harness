from __future__ import annotations

import shlex
import subprocess
from typing import Any

from ..config import BASH_TIMEOUT
from . import ToolContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "在终端执行一条 Shell 命令。返回标准输出和标准错误的合并结果。"
            "命令在子进程中执行，不保留环境变量变更。"
            f"超时设置为 {BASH_TIMEOUT} 秒，长时间运行的命令会被自动终止。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令",
                },
            },
            "required": ["command"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

# ─── 内部逻辑 ───────────────────────────────────────

BLOCKED_COMMANDS: set[str] = {"mkfs", "dd"}
CONFIRM_COMMANDS: set[str] = {"rm", "sudo", "shutdown", "reboot", "halt", "init"}


def _extract_command_name(command: str) -> str:
    """从 shell 命令字符串中提取基本命令名称。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]


# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """执行 bash 命令，返回 ToolResult。"""
    command = args["command"]

    cmd_name = _extract_command_name(command)

    # 安全检查：黑名单
    if cmd_name in BLOCKED_COMMANDS:
        return ToolResult(output="Command blocked for safety.", success=False, error="blocked")

    # 安全检查：需确认
    if cmd_name in CONFIRM_COMMANDS:
        answer = input(f"\033[31m⚠ Command '{command}' looks dangerous. Run anyway? [y/N]: \033[0m")
        if answer.strip().lower() not in ("y", "yes"):
            return ToolResult(output="Command cancelled by user.", success=False, error="cancelled")

    # 执行
    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )
        out = (r.stdout + r.stderr).strip()
        return ToolResult(output=out if out else "(no output)", success=True)
    except subprocess.TimeoutExpired:
        return ToolResult(output=f"Timeout ({BASH_TIMEOUT}s)", success=False, error="timeout")
    except (FileNotFoundError, OSError) as e:
        return ToolResult(output=str(e), success=False, error="os_error")
