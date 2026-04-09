from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import ToolContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find",
        "description": (
            "按模式匹配搜索文件路径。返回匹配的文件列表，按修改时间排序（最近修改的在前）。"
            "支持标准 find 模式，如 *.py、src/**/*.ts、**/config.json。"
            "只读工具，不会修改任何文件。"
            "适用于在已知目录结构中查找特定类型的文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "find 匹配模式，如 '**/*.py' 匹配所有 Python 文件",
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录，默认为当前工作目录",
                },
            },
            "required": ["pattern"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = True

# ─── 内部逻辑 ───────────────────────────────────────

MAX_RESULTS = 200

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """按 glob 模式搜索文件。"""
    pattern = args["pattern"]
    search_dir = args.get("path", context.working_dir)
    search_path = Path(search_dir)

    if not search_path.is_absolute():
        search_path = Path(context.working_dir) / search_path

    if not search_path.is_dir():
        return ToolResult(output=f"目录不存在: {search_path}", success=False, error="not_found")

    # 执行 glob 匹配
    try:
        matches = sorted(
            search_path.glob(pattern),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
    except OSError as e:
        return ToolResult(output=str(e), success=False, error="os_error")

    # 只保留文件（排除目录），限制数量
    files = [p for p in matches if p.is_file()][:MAX_RESULTS]
    truncated = len([p for p in matches if p.is_file()]) > MAX_RESULTS

    if not files:
        return ToolResult(output=f"未找到匹配 '{pattern}' 的文件", success=True)

    # 输出相对路径（如果可能）
    try:
        lines = [str(f.relative_to(search_path)) for f in files]
    except ValueError:
        lines = [str(f) for f in files]

    output = "\n".join(lines)

    if truncated:
        output += f"\n\n(结果过多，仅显示前 {MAX_RESULTS} 个)"

    return ToolResult(output=output, success=True)
