"""系统提示词上下文构建器。

三层组装：
1. 框架层（_FRAMEWORK_PROMPT）：核心身份和工作原则，始终存在
2. 用户定制层（.harness/context/*.md）：项目特定的身份、风格、规则，存在时加载
3. 环境信息层（get_user_context）：动态环境信息，每次请求生成

工具的详细使用指南通过各工具的 SCHEMA description 字段传递给模型，
不拼接到 system prompt 中（与 CC 的 tool.prompt() → description 字段一致）。
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# ─── 框架层提示词（不可通过文件覆盖）─────────────────────

_FRAMEWORK_PROMPT = """\
你是一个 AI 助手，运行在 harness 代理框架中。
你有以下可用工具：文件读写、文件搜索、文件编辑、bash 命令执行。工具的详细用法见各工具的描述。

判断用户意图：日常对话直接回答，需要操作时使用工具。
多步骤任务必须使用 todo 跟踪计划，保持恰好一个 in_progress。
如果 skill 刚展开，而任务明显是多步骤，在继续深入执行之前先刷新 todo。
优先使用工具而非文字描述。

## Skills

系统提示词中包含 <available-skills> 目录。
如果任务匹配某个 skill，应先调用 skill 工具立即加载它，再基于已展开的 skill 重新评估下一步。
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


class ContextPipeline:
    """上下文注入管道。管理多个 ContextPlugin，按注册顺序执行。"""

    def __init__(self) -> None:
        self._plugins: list[Any] = []

    def register(self, plugin: Any) -> None:
        """注册一个插件。"""
        self._plugins.append(plugin)

    def inject_all(self, messages: list[dict[str, Any]]) -> None:
        """执行所有已注册插件的注入。"""
        for plugin in self._plugins:
            plugin.inject(messages)


class SystemContextPlugin:
    """注入系统提示词。幂等（marker 检查）。"""

    def __init__(self, project_root: str | None = None) -> None:
        self._project_root = project_root or os.getcwd()

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """将通用系统提示词追加到已有的系统消息中。"""
        marker = "<!-- system-context-injected -->"
        for msg in messages:
            if msg.get("role") == "system" and marker in (msg.get("content") or ""):
                return

        system_ctx = get_system_context(self._project_root)

        for msg in messages:
            if msg.get("role") == "system":
                existing = msg.get("content") or ""
                msg["content"] = f"{existing}\n\n{marker}\n\n{system_ctx}"
                return

        messages.insert(0, {
            "role": "system",
            "content": f"{marker}\n\n{system_ctx}",
        })


class UserContextPlugin:
    """注入环境信息。幂等（marker 检查）。"""

    def __init__(self, working_dir: str | None = None) -> None:
        self._working_dir = working_dir or os.getcwd()

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """在消息列表中注入环境信息。"""
        marker = "<!-- user-context-injected -->"
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content", "").startswith(marker):
                return

        user_ctx = get_user_context(self._working_dir)
        content = f"{marker}\n{user_ctx}"

        insert_pos = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                insert_pos = i
                break
        else:
            insert_pos = len(messages)

        messages.insert(insert_pos, {"role": "user", "content": content})
