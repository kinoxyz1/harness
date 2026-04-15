from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..context import ToolUseContext, ToolResult

# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find",
        "description": (
            "按 glob 模式搜索文件路径。只读工具，可安全并行执行。"
            "\n\n行为要点："
            "\n- 返回匹配的文件列表，按修改时间排序（最近修改的在前）。"
            "\n- 最多返回 200 个结果，超出部分截断。"
            "\n- 输出相对路径（相对于搜索目录）。"
            "\n\n模式语法（Python pathlib glob）："
            "\n- `*.py` — 当前目录下的 Python 文件"
            "\n- `**/*.py` — 递归搜索所有 Python 文件"
            "\n- `src/**/*.ts` — src 目录下递归搜索 TypeScript 文件"
            "\n- `path` 参数可指定搜索根目录，默认为当前工作目录"
            "\n\n使用场景："
            "\n- 查找特定类型的文件（不要用 bash find/ls，用本工具更高效）"
            "\n- 确认文件路径是否存在"
            "\n- 浏览项目结构"
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

ANNOTATIONS: dict[str, bool] = {
    "readonly": True,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": True,
}

# ─── Prompt（给模型的详细使用指南）────────────────────

PROMPT: str = """\
## find — 搜索文件路径

按 glob 模式搜索文件路径，返回匹配的文件列表。只读工具，不会修改任何文件。

### 模式语法
- 使用标准 Python glob 模式：
  - `*.py` — 当前目录下所有 .py 文件
  - `**/*.py` — 递归搜索所有 .py 文件
  - `src/**/*.ts` — src 目录下递归搜索所有 .ts 文件
  - `**/config.json` — 递归搜索所有名为 config.json 的文件

### 输出格式
- 结果按修改时间排序，最近修改的文件排在最前面。
- 最多返回 200 个结果，超出部分会被截断并提示。
- 输出为相对路径（相对于搜索目录）。

### 使用建议
- 不确定文件位置时，先用 find 搜索，再用 read_file 读取。
- 可以指定 path 参数限制搜索范围，提高效率。
- 与 bash 中的 find 命令不同，本工具使用 glob 模式而非正则表达式。
"""

# ─── 内部逻辑 ───────────────────────────────────────

MAX_RESULTS = 200

# ─── Handler（执行逻辑）─────────────────────────────


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
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
