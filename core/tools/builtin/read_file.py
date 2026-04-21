from __future__ import annotations

from typing import Any

from ...shared.config import MAX_OUTPUT_CHARS
from ..context import (
    FileState,
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
    make_tool_message,
    safe_path,
)

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "读取本地文件的文本内容。只读工具，不会修改文件。"
        "\n\n行为要点："
        "\n- 输出带行号，格式与 cat -n 一致。"
        "\n- 单次最多读取 2000 行，大文件请用 offset 和 limit 分段读取。"
        "\n- 检测到二进制文件时会拒绝读取。"
        "\n- 路径支持相对路径（基于工作目录）和绝对路径。"
        "\n\n重要：edit_file 强制要求先完整读取文件后才能编辑。"
        "如果只读了部分内容（使用了 offset/limit），edit_file 也会拒绝执行。"
        "\n\n使用场景："
        "\n- 查看文件内容（不要用 bash cat/head/tail，用本工具更安全）"
        "\n- 编辑前必须先读取文件"
        "\n- 搜索文件路径不确定时，先用 find 工具定位"
    ),
    "input_schema": {
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
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = True

ANNOTATIONS: dict[str, bool] = {
    "readonly": True,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": True,
}

# ─── Prompt（给模型的详细使用指南）────────────────────

PROMPT: str = """\
## read_file — 读取文件内容

读取本地文件的文本内容，只读工具，不会修改文件。

### 输出格式
- 输出带行号，格式为 `行号\\t内容`（与 cat -n 一致）。
- 默认从第 1 行开始，最多读取 2000 行。

### 分段读取
- 使用 offset 参数指定起始行（从 1 开始）。
- 使用 limit 参数指定最多读取的行数。
- 大文件应先读取开头了解结构，再按需读取特定部分。
- 如果输出末尾出现截断提示，说明文件还有更多内容，可用 offset 继续读取。

### 重要约束
- 在使用 edit_file 编辑文件之前，必须先用 read_file 完整读取该文件。
  如果只做了分段读取（使用了 offset 或 limit），edit_file 仍然会被拒绝。
  这是为了确保编辑前对文件内容有完整认知，避免误操作。
- 二进制文件会返回错误提示。

### 路径
- 支持相对路径（基于工作目录）和绝对路径。
- 路径不确定时，先用 find 工具确认路径。
"""

# ─── 内部逻辑 ───────────────────────────────────────

MAX_LINES = 2000
READ_FILE_OUTPUT_CHAR_BUDGET = min(MAX_OUTPUT_CHARS, 20_000)


def _continuation_notice(start_line: int, end_line: int, total_lines: int) -> str:
    return (
        f"\n\n(文件较大，已显示第 {start_line}-{end_line} 行，共 {total_lines} 行；"
        f"继续读取请使用 offset={end_line + 1})"
    )


def _render_chunk_within_budget(
    lines: list[str],
    *,
    start: int,
    end: int,
    total_lines: int,
) -> tuple[str, int]:
    actual_end = end

    while actual_end > start:
        numbered = [
            f"{line_no}\t{line}"
            for line_no, line in enumerate(lines[start:actual_end], start=start + 1)
        ]
        base_output = "\n".join(numbered)
        suffix = (
            _continuation_notice(start + 1, actual_end, total_lines)
            if actual_end < total_lines
            else ""
        )
        output = base_output + suffix
        if len(output) <= READ_FILE_OUTPUT_CHAR_BUDGET:
            return output, actual_end
        actual_end -= 1

    suffix = (
        _continuation_notice(start + 1, start + 1, total_lines)
        if start + 1 < total_lines
        else ""
    )
    prefix = f"{start + 1}\t"
    remaining = max(0, READ_FILE_OUTPUT_CHAR_BUDGET - len(prefix) - len(suffix) - 1)
    first_line = lines[start] if start < len(lines) else ""
    truncated_line = first_line[:remaining]
    if len(truncated_line) < len(first_line):
        truncated_line += "…"
    return prefix + truncated_line + suffix, min(start + 1, total_lines)


# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    """读取文件内容，返回结构化 outcome。"""
    try:
        file_path = safe_path(args["path"], context.working_dir)
    except ValueError as e:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="path_escape",
            messages=[make_tool_message(context, str(e))],
        )

    # 文件存在性检查
    if not file_path.exists():
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_found",
            messages=[make_tool_message(context, f"文件不存在: {file_path}")],
        )

    if not file_path.is_file():
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_a_file",
            messages=[make_tool_message(context, f"路径不是文件: {file_path}")],
        )

    # 读取文件
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="binary",
            messages=[make_tool_message(context, "无法读取：可能是二进制文件")],
        )
    except PermissionError:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="permission_denied",
            messages=[make_tool_message(context, f"权限不足: {file_path}")],
        )
    except OSError as e:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="os_error",
            messages=[make_tool_message(context, str(e))],
        )

    total_lines = len(lines)

    # 分段参数
    offset = max(1, args.get("offset", 1))
    limit = min(args.get("limit", MAX_LINES), MAX_LINES)

    # 切片
    start = offset - 1  # 转为 0-indexed
    if start >= total_lines and total_lines > 0:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="offset_out_of_range",
            messages=[make_tool_message(context, f"起始行超出范围：文件共 {total_lines} 行，请使用 1-{total_lines} 之间的 offset。")],
        )
    end = min(start + limit, total_lines)
    output, actual_end = _render_chunk_within_budget(
        lines,
        start=start,
        end=end,
        total_lines=total_lines,
    )
    visible_lines = lines[start:actual_end]

    abs_path = str(file_path)
    full_read = start == 0 and actual_end >= total_lines
    file_state = FileState(
        content="\n".join(visible_lines),
        timestamp=file_path.stat().st_mtime,
        offset=None if full_read else (start + 1),
        limit=None if full_read else (actual_end - start),
        total_lines=total_lines,
    )
    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[make_tool_message(context, output or "(空文件)")],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.UPSERT_FILE_STATE,
                payload={"path": abs_path, "file_state": file_state},
            )
        ],
    )
