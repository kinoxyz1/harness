from __future__ import annotations

import shlex
import subprocess
from typing import Any

from ...shared.config import BASH_TIMEOUT
from ..context import ToolUseContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "name": "bash",
    "description": (
        f"在终端执行一条 Shell 命令。命令在子进程中执行，不保留环境变量变更。"
        f"超时设置为 {BASH_TIMEOUT} 秒，长时间运行的命令会被自动终止。"
        "\n\n安全机制："
        "\n- 黑名单命令（mkfs, dd）会被直接拒绝。"
        "\n- 危险命令（rm, sudo, shutdown 等）需要用户确认后才会执行。"
        "\n\n使用场景："
        "\n- 运行测试、构建、git 等需要 Shell 环境的操作"
        "\n- 安装依赖、启动服务等"
        "\n\n不要用 bash 执行以下操作（有专用工具更安全）："
        "\n- cat/head/tail 读取文件 → 用 read_file"
        "\n- find/ls 搜索文件 → 用 find"
        "\n- sed/awk 修改文件 → 用 edit_file"
        "\n- echo > 创建文件 → 用 write_file"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
        },
        "required": ["command"],
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}

# ─── Prompt（给模型的详细使用指南）────────────────────

PROMPT: str = """\
## bash — Shell 命令执行

在子进程中执行一条 Shell 命令，返回标准输出和标准错误的合并结果。

### 行为要点
- 命令在子进程中执行，每次调用都是独立的环境，环境变量变更不会在调用之间保留。
- 默认超时 {timeout} 秒，超时后命令会被自动终止。长时间运行的命令请提前告知用户。
- 返回 (no output) 表示命令执行成功但没有输出。

### 安全策略
- 完全禁止的命令：mkfs, dd（会直接返回错误）。
- 需要用户确认的命令：rm, sudo, shutdown, reboot, halt, init（会提示用户确认）。

### 使用建议
- 优先使用专用工具而非 bash：
  - 读文件 → 用 read_file（带行号、支持分段）
  - 写文件 → 用 write_file
  - 编辑文件 → 用 edit_file
  - 搜索文件 → 用 find
- 适合用 bash 的场景：运行测试、启动服务、git 操作、安装依赖、编译构建等需要 Shell 环境的操作。
- 如果需要保留环境状态（如 cd 后续命令依赖目录），用 && 将多条命令串联成一次调用。
""".format(timeout=BASH_TIMEOUT)

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

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """执行 bash 命令，返回 ToolResult。"""
    command = args["command"]

    cmd_name = _extract_command_name(command)

    # 安全检查：黑名单
    if cmd_name in BLOCKED_COMMANDS:
        return ToolResult(output="Command blocked for safety.", success=False, error="blocked")

    # 安全检查：需确认
    if cmd_name in CONFIRM_COMMANDS:
        answer = input(f"\033[31m⚠ Command '{command}' looks dangerous. Run anyway? [y/N]: \n \033[0m")
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
