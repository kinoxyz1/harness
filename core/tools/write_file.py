from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ToolUseContext, ToolResult, safe_path

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "将内容写入文件。支持创建、覆盖和追加三种模式。"
            "\n\n行为要点："
            "\n- mode='write'（默认）：完全覆盖文件，如不存在则自动创建（包括父目录）。"
            "\n- mode='append'：在文件末尾追加内容，如不存在则自动创建。适合分块写入大文件。"
            "\n- 写入后系统自动记录文件认知，后续可用 edit_file 继续编辑。"
            "\n\n使用场景："
            "\n- 创建新文件（不要用 bash echo > file，用本工具更可靠）"
            "\n- 需要完全重写文件内容（如生成配置文件、脚本等）"
            "\n- 分块写入大文件：先 write 创建，再多次 append 追加"
            "\n- 如果只需对文件进行局部修改，优先使用 edit_file，避免意外覆盖未修改的部分"
            "\n\n大文件策略："
            "\n- 如果文件内容很长（超过 200 行），建议分块写入："
            "\n  1. 第一次调用 write_file(path, 第一块内容, mode='write')"
            "\n  2. 后续调用 write_file(path, 下一块内容, mode='append')"
            "\n  3. 直到所有内容写完"
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
                    "description": "要写入的文件内容（当前块）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["write", "append"],
                    "description": "写入模式：'write' 覆盖写入（默认），'append' 追加到文件末尾",
                },
            },
            "required": ["path", "content"],
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
## write_file — 写入文件

将内容写入文件。支持创建、覆盖和追加三种模式。

### 写入模式
- `mode='write'`（默认）：完全覆盖文件内容。如文件不存在则自动创建（包括父目录）。
- `mode='append'`：在文件末尾追加内容。如文件不存在则自动创建。

### 大文件策略
如果文件内容很长（超过 200 行），**必须分块写入**以避免参数截断：
1. 第一次：`write_file(path, 第一块内容)` — 创建文件
2. 后续：`write_file(path, 下一块内容, mode='append')` — 追加内容
3. 重复直到所有内容写完

### 使用建议
- 适用于创建新文件。
- 适用于需要完全重写文件内容的场景。
- 如果只需对文件进行局部修改，优先使用 edit_file。
- 写入后系统会自动记录文件认知，后续可以用 edit_file 继续编辑。
"""

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """将内容写入文件。"""
    try:
        file_path = safe_path(args["path"], context.working_dir)
    except ValueError as e:
        return ToolResult(output=str(e), success=False, error="path_escape")

    content = args["content"]
    mode = args.get("mode", "write")

    if mode not in ("write", "append"):
        return ToolResult(output=f"未知的写入模式: {mode}，请使用 'write' 或 'append'", success=False, error="invalid_mode")

    # 创建父目录（如不存在）
    is_new = not file_path.exists()
    is_append = mode == "append" and file_path.exists()

    file_path.parent.mkdir(parents=True, exist_ok=True)

    # 写入文件
    try:
        if is_append:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(content)
        else:
            file_path.write_text(content, encoding="utf-8")
    except PermissionError:
        return ToolResult(output=f"权限不足: {file_path}", success=False, error="permission_denied")
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="os_error")

    # 读取最终文件内容更新认知
    final_content = file_path.read_text(encoding="utf-8")
    context.update_file_state(str(file_path), final_content)

    lines = final_content.count("\n") + (1 if final_content and not final_content.endswith("\n") else 0)
    if is_append:
        return ToolResult(output=f"已追加到 {file_path} (共 {lines} 行)", success=True)
    action = "已创建" if is_new else "已覆盖"
    return ToolResult(output=f"{action} {file_path} ({lines} 行)", success=True)
