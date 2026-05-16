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
复杂多步骤任务必须先调用 task_plan 建立完整任务列表，再逐个执行；不要边做边加任务。
TaskState 激活时不要再把 todo 当权威状态；todo 只是从任务投影出的用户视图。
如果需要展示当前进度，可以维护 todo 投影视图；每个展示项都应具体可操作，包含做什么、对什么对象、预期产出什么。
LOCAL 任务直接用普通工具（bash/edit_file等）执行，不要用 task_execute；完成后调用 task_plan 更新状态。
fresh_subagent 任务用 task_execute 执行。
选择 execution_mode 的原则：评估每个任务的复杂度和上下文隔离需求。
适合 fresh_subagent 的任务特征：需要多轮探索/分析、能独立完成不依赖主上下文、复杂度足以拆分为独立子流程。
适合 local 的任务特征：简单直接的操作、需要操作当前项目上下文、单步即可完成。
只有当前请求已进入 task planning 阶段时，才不要先加载 skill、不要派发 subagent、不要开始执行；此时只允许为规划读取少量只读信息。
**指令优先级规则**：当指令存在冲突时，按以下优先级执行：
1. 必须完成的硬性规则（如“复杂任务先调用 task_plan”）
2. 工具/skill 调用的规范流程（如“任务匹配 skill 则先加载 skill”）
3. 效率优先的操作逻辑（如“直接给方案，避免不必要的询问”）
4. 辅助性建议（如“优先自主决策”）

## Skills

系统提示词中包含 <available-skills> 目录。
如果当前请求不需要 task_plan，且任务匹配某个 skill，可以直接调用 skill 工具加载它。
如果当前请求已进入 task planning 阶段，则先完成 task_plan，再在当前任务明确需要时调用 skill 工具加载对应 skill。
"""

# 用户定制文件的加载顺序。每组优先使用新命名，回退兼容旧命名。
_CONTEXT_FILE_GROUPS = [
    ("USER.md", "identity.md"),
    ("SOUL.md", "style.md"),
    ("RULES.md", "rules.md"),
]


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
            for group in _CONTEXT_FILE_GROUPS:
                for filename in group:
                    filepath = context_dir / filename
                    if not filepath.is_file():
                        continue
                    try:
                        content = filepath.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if content:
                        parts.append(content)
                    break

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
