from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ToolContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "将内容写入文件。如文件已存在则完全覆盖，如不存在则创建（包括父目录）。"
            "适用于创建新文件或需要完全重写文件内容的场景。"
            "如只需对文件进行局部修改，请使用 edit_file 工具，避免意外覆盖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """将内容写入文件。"""
    file_path = Path(args["path"])

    # 相对路径基于工作目录
    if not file_path.is_absolute():
        file_path = Path(context.working_dir) / file_path

    content = args["content"]

    # 创建父目录（如不存在）
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # 写入文件
    try:
        file_path.write_text(content, encoding="utf-8")
    except PermissionError:
        return ToolResult(output=f"权限不足: {file_path}", success=False, error="permission_denied")
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="os_error")

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    action = "已覆盖" if file_path.exists() else "已创建"
    return ToolResult(output=f"{action} {file_path} ({lines} 行)", success=True)
