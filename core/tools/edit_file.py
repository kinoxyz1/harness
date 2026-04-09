from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ToolContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "基于字符串替换编辑文件。在文件中查找 old_string 并替换为 new_string。"
            "默认只替换第一个匹配项，设置 replace_all 为 true 可替换所有匹配项。"
            "如果 old_string 在文件中不存在或不唯一（且未设置 replace_all），将返回错误。"
            "适用于对文件进行精确、局部的修改。"
            "如需完全重写文件，请使用 write_file 工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要查找的文本（必须与文件中的内容精确匹配，包括缩进）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项，默认 false（仅替换第一个）",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """基于字符串替换编辑文件。"""
    file_path = Path(args["path"])

    # 相对路径基于工作目录
    if not file_path.is_absolute():
        file_path = Path(context.working_dir) / file_path

    if not file_path.exists():
        return ToolResult(output=f"文件不存在: {file_path}", success=False, error="not_found")

    if not file_path.is_file():
        return ToolResult(output=f"路径不是文件: {file_path}", success=False, error="not_a_file")

    old_string = args["old_string"]
    new_string = args["new_string"]
    replace_all = args.get("replace_all", False)

    # 读取文件
    try:
        content = file_path.read_text(encoding="utf-8")
    except PermissionError:
        return ToolResult(output=f"权限不足: {file_path}", success=False, error="permission_denied")
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="os_error")

    # 检查 old_string 是否存在
    if old_string not in content:
        return ToolResult(
            output=f"未找到要替换的文本。请确保 old_string 与文件内容精确匹配（包括缩进和空行）。",
            success=False,
            error="not_found",
        )

    # 检查唯一性（非 replace_all 模式）
    count = content.count(old_string)
    if count > 1 and not replace_all:
        return ToolResult(
            output=f"找到 {count} 处匹配，但 replace_all 为 false。请提供更精确的 old_string 或设置 replace_all 为 true。",
            success=False,
            error="ambiguous_match",
        )

    # 执行替换
    if replace_all:
        new_content = content.replace(old_string, new_string)
        replaced = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replaced = 1

    # 写回文件
    try:
        file_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="write_error")

    return ToolResult(output=f"已替换 {replaced} 处匹配", success=True)
