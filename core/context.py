"""系统提示词上下文构建器。

三层组装：
1. 框架层（_FRAMEWORK_PROMPT）：核心身份和工作原则，始终存在
2. 用户定制层（.harness/context/*.md）：项目特定的身份、风格、规则，存在时加载
3. 环境信息层（get_user_context）：动态环境信息，每次请求生成

工具的详细使用指南通过各工具的 SCHEMA description 字段传递给模型，
不拼接到 system prompt 中（与 CC 的 tool.prompt() → description 字段一致）。
"""
from __future__ import annotations

import platform
import sys
from datetime import datetime
from pathlib import Path


# ─── 框架层提示词（不可通过文件覆盖）─────────────────────

_FRAMEWORK_PROMPT = """\
你是一个 AI 编程助手，运行在 harness 代理框架中。你可以通过工具与用户的开发环境交互。

工作原则：
1. 先理解用户需求，再选择合适的工具。不要盲目执行命令。
2. 使用工具前，确保你了解工具的行为和限制（详见各工具的使用说明）。
3. 对文件操作遵循"先读后改"原则：读取文件理解内容，再进行编辑。
4. 优先使用专用工具而非 bash 命令来操作文件，专用工具更安全、更精确。
5. 遇到不确定的情况，先探索再行动。
6. 如果工具返回错误，仔细阅读错误信息，理解原因后调整策略。

行为准则：
- 优先使用专用工具操作文件（搜索、读取、编辑、创建），而非通过 bash 执行等效命令。
- 你可以在一次回复中调用多个工具。框架会自动处理：只读工具并行执行，写工具串行执行。
- 工具返回错误时不要重试相同操作，先分析错误原因。
"""

# 用户定制文件的加载顺序（后面的覆盖前面的）
_CONTEXT_FILES = ["identity.md", "style.md", "rules.md"]


def get_system_context(project_root: str | None = None) -> str:
    """组装系统提示词：框架层 + 用户定制层。

    Args:
        project_root: 项目根目录，用于查找 .harness/context/ 下的定制文件。
                      为 None 时只返回框架层提示词。
    """
    parts = [_FRAMEWORK_PROMPT]

    if project_root:
        context_dir = Path(project_root) / ".harness" / "context"
        if context_dir.is_dir():
            for filename in _CONTEXT_FILES:
                filepath = context_dir / filename
                if filepath.is_file():
                    try:
                        content = filepath.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if content:
                        parts.append(content)

    return "\n\n".join(parts)


def get_user_context(working_dir: str) -> str:
    """返回环境信息字符串，作为用户上下文注入。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plat = platform.system()
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    return (
        f"<environment>\n"
        f"  working_dir: {working_dir}\n"
        f"  date: {now}\n"
        f"  platform: {plat}\n"
        f"  python: {python_ver}\n"
        f"</environment>"
    )
