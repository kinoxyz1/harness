from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolUseContext, ToolResult, safe_path

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "基于字符串替换编辑文件。在文件中查找 old_string 并替换为 new_string。"
            "\n\n前置条件（系统强制）："
            "\n- 必须先用 read_file 完整读取目标文件，否则会被拒绝执行（error: not_read）。"
            "\n- 如果文件在读取后被外部修改，也会被拒绝执行（error: stale），需要重新读取。"
            "\n- 只读了部分内容（使用了 offset/limit）也不行，必须是完整读取。"
            "\n\n匹配规则："
            "\n- old_string 必须与文件内容精确匹配（包括缩进和空行）。"
            "\n- 默认只替换第一个匹配项。设置 replace_all=true 可替换所有匹配项。"
            "\n- 如果 old_string 在文件中存在多处匹配且未设置 replace_all，会报错（error: ambiguous_match）。"
            "\n\n使用场景："
            "\n- 对文件进行精确、局部的修改（不要用 bash sed/awk，用本工具更安全）"
            "\n- 如需完全重写文件，请使用 write_file 工具"
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

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}

# ─── Prompt（给模型的详细使用指南）────────────────────

PROMPT: str = """\
## edit_file — 基于字符串替换编辑文件

在文件中查找 old_string 并替换为 new_string。适用于对文件进行精确、局部的修改。

### 前置条件（必须遵守）
- 在使用 edit_file 之前，必须先用 read_file 完整读取目标文件。
  系统会强制检查：如果未读取或只做了分段读取，edit_file 将被拒绝并提示重新读取。
- 如果文件在读取后被外部修改（staleness 检测），系统会要求重新读取后再编辑。

### 匹配规则
- old_string 必须与文件内容精确匹配，包括缩进、空行、空格。
  系统提示的行号格式为 `行号\\t内容`，old_string 中不应包含行号前缀。
- 默认只替换第一个匹配项。
- 如果 old_string 在文件中出现多次且未设置 replace_all=true，系统会返回错误，
  因为无法确定要替换哪一处。此时应提供更精确的上下文使匹配唯一，或设置 replace_all=true。

### 使用建议
- 对于小范围修改（改一个函数名、修一个 bug、调整配置项），优先使用 edit_file。
- 如果需要重写文件的大部分内容，考虑使用 write_file。
- 每次编辑后系统会自动更新文件认知，无需重新读取即可继续编辑同一文件。
"""

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """基于字符串替换编辑文件。"""
    try:
        file_path = safe_path(args["path"], context.working_dir)
    except ValueError as e:
        return ToolResult(output=str(e), success=False, error="path_escape")

    # ── read-before-write 强制检查 ──
    abs_path = str(file_path)
    state = context.get_file_state(abs_path)
    if not state or not state.is_full_read:
        return ToolResult(
            output="请先使用 read_file 完整读取此文件，再进行编辑。",
            success=False,
            error="not_read",
        )

    # ── staleness 检测 ──
    if file_path.exists():
        current_mtime = file_path.stat().st_mtime
        if current_mtime != state.timestamp:
            return ToolResult(
                output="文件在你读取后被修改了，请重新读取后再编辑。",
                success=False,
                error="stale",
            )

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

    # 更新文件认知
    context.update_file_state(abs_path, new_content)
    context.mark_file_modified(abs_path)

    return ToolResult(output=f"已替换 {replaced} 处匹配", success=True)
