from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ToolContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取本地文件的文本内容。只读工具，不会修改文件。"
            "支持通过 offset 和 limit 参数分段读取大文件。"
            "输出带行号，格式与 cat -n 一致。"
            "当文件不存在或不可访问时返回错误信息。"
            "文件路径不确定时应先确认路径，再使用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（从 1 开始），默认为 1",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认读取全部（最大 2000 行）",
                },
            },
            "required": ["path"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = True

# ─── 内部逻辑 ───────────────────────────────────────

MAX_LINES = 2000


# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """读取文件内容，返回 ToolResult。"""
    file_path = Path(args["path"])

    # 相对路径基于工作目录
    if not file_path.is_absolute():
        file_path = Path(context.working_dir) / file_path

    # 文件存在性检查
    if not file_path.exists():
        return ToolResult(output=f"文件不存在: {file_path}", success=False, error="not_found")

    if not file_path.is_file():
        return ToolResult(output=f"路径不是文件: {file_path}", success=False, error="not_a_file")

    # 读取文件
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return ToolResult(output="无法读取：可能是二进制文件", success=False, error="binary")
    except PermissionError:
        return ToolResult(output=f"权限不足: {file_path}", success=False, error="permission_denied")
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="os_error")

    total_lines = len(lines)

    # 分段参数
    offset = max(1, args.get("offset", 1))
    limit = min(args.get("limit", MAX_LINES), MAX_LINES)

    # 切片
    start = offset - 1  # 转为 0-indexed
    end = min(start + limit, total_lines)
    selected = lines[start:end]

    # 带行号输出
    numbered = [f"{i}\t{line}" for i, line in enumerate(selected, start=start + 1)]
    output = "\n".join(numbered)

    # 截断提示
    if end < total_lines:
        output += f"\n\n(已截断，显示第 {start + 1}-{end} 行，共 {total_lines} 行)"

    return ToolResult(output=output or "(空文件)", success=True)
