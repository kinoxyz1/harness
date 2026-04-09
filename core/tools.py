from __future__ import annotations

import shlex
import subprocess
from typing import Any

from .config import BASH_TIMEOUT

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条 Shell 命令。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

BLOCKED_COMMANDS: set[str] = {"mkfs", "dd"}
CONFIRM_COMMANDS: set[str] = {"rm", "sudo", "shutdown", "reboot", "halt", "init"}

def _extract_command_name(command: str) -> str:
    """从shell命令字符串中提取基本命令名称。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]

def _is_blocked(command: str) -> bool:
    """检查该命令是否在黑名单列表中。"""
    cmd_name = _extract_command_name(command)
    return cmd_name in BLOCKED_COMMANDS

def _needs_confirmation(command: str) -> bool:
    """检查命令在执行前是否需要用户确认。"""
    cmd_name = _extract_command_name(command)
    return cmd_name in CONFIRM_COMMANDS

def run_bash(command: str) -> str:
    if _is_blocked(command):
        return "Error: Command blocked for safety."

    if _needs_confirmation(command):
        answer = input(f"\033[31m⚠ Command '{command}' looks dangerous. Run anyway? [y/N]: \033[0m")
        if answer.strip().lower() not in ("y", "yes"):
            return "Error: Command cancelled by user."

    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )
        out = (r.stdout + r.stderr).strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({BASH_TIMEOUT}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """使用给定参数按名称执行工具。"""
    if name == "bash":
        return run_bash(arguments["command"])
    return f"Error: Unknown tool '{name}'"
