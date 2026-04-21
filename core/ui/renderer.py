"""显示渲染实现。"""
from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ..shared.interfaces import Renderer
from ..shared.config import SHOW_THINKING


def _tool_call_label(name: str, args: dict[str, Any]) -> str:
    if name == "skill":
        return f"Skill({args.get('skill', '')})"
    if name == "read_file":
        path = str(args.get("path", ""))
        return f"Read({Path(path).name})"
    if name == "bash":
        if args.get("description"):
            return f"Bash({args['description']})"
        if args.get("command"):
            return f"Bash({args['command']})"
    if name == "find" and args.get("pattern"):
        return f"Find({args['pattern']})"
    if name == "write_file":
        path = str(args.get("path", ""))
        return f"Write({Path(path).name})"
    if name == "edit_file":
        path = str(args.get("path", ""))
        return f"Edit({Path(path).name})"

    preferred_keys = ("path", "pattern", "query", "task", "offset", "limit")
    parts = [f"{key}={args[key]!r}" for key in preferred_keys if key in args]
    if not parts:
        return name
    return f"{name} " + " ".join(parts)


def _preview_output(output: str, *, max_lines: int = 10, max_chars: int = 1200) -> str:
    if not output:
        return "(无输出)"

    runtime_truncated = _has_runtime_truncation_marker(output)
    lines = output.splitlines()
    preview_lines = list(islice(lines, max_lines))
    preview = "\n".join(preview_lines)
    truncated = len(lines) > max_lines or len(output) > max_chars
    if len(preview) > max_chars:
        preview = preview[:max_chars]
        truncated = True
    if runtime_truncated:
        preview = (
            "[提示] 这是 runtime 截断，不是文件只有这些内容。\n"
            + preview
        )
    if truncated:
        preview += "\n... (已省略后续输出)"
    return preview


def _has_runtime_truncation_marker(output: str) -> bool:
    return "输出已截断，原始 " in output and "显示前 " in output


def _has_read_file_continuation_marker(output: str) -> bool:
    return "继续读取请使用 offset=" in output


def _line_count_preview(output: str) -> int | None:
    count = 0
    for line in output.splitlines():
        if "\t" not in line:
            if count > 0:
                break
            continue
        line_no, _ = line.split("\t", 1)
        if line_no.strip().isdigit():
            count += 1
        elif count > 0:
            break
    return count or None


def _tool_result_summary(name: str, output: str) -> str | None:
    # NOTE: 在 runtime 提供结构化结果元数据前，这里有意基于当前工具输出文案做模式匹配；
    # 任何不匹配都返回 None，由调用方走通用 preview 回退，避免绑定更深接口耦合。
    if name == "skill" and output.startswith("Skill loaded:"):
        return "已加载 skill，等待重新规划"

    if name == "read_file":
        line_count = _line_count_preview(output)
        if line_count is not None:
            summary = f"已读取文件内容，预览 {line_count} 行"
            if _has_runtime_truncation_marker(output):
                summary += "（runtime 截断，不是文件只有这些内容）"
            elif _has_read_file_continuation_marker(output):
                summary += "（文件较大，继续用 offset 读取）"
            return summary

    if name == "find":
        stripped = output.strip()
        if not stripped:
            return None
        if stripped.startswith("未找到匹配"):
            return None
        if stripped.startswith("目录不存在:"):
            return None
        lines = [
            line for line in output.splitlines()
            if line.strip() and not line.startswith("(结果过多")
        ]
        if lines:
            return f"已找到 {len(lines)} 个匹配文件"

    return None


class RichRenderer:
    """基于 Rich Console 的终端渲染器。"""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def show_thinking(self, title: str, reasoning: str) -> None:
        """显示推理/思考过程。"""
        if SHOW_THINKING and reasoning and reasoning.strip():
            self._console.print(Panel(reasoning.strip(), title=title, border_style="dim"))

    def show_assistant(self, content: str | None) -> None:
        """显示助手文字内容。"""
        if content and content.strip():
            print(content)

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        """显示 LLM 调用计时信息。"""
        self._console.print(
            f"[dim]{elapsed:.1f}s │ token {prompt_tokens}↓ {completion_tokens}↑"
            f" │ finish={finish_reason}[/dim]"
        )

    def show_current_todo(self, item: Any, completed: int, total: int) -> None:
        """显示当前聚焦的 todo。"""
        self._console.print(
            f"[bold cyan]⚡ {item.content}  ({completed}/{total})[/bold cyan]"
        )

    def show_progress(self, items: list[Any]) -> None:
        """显示完整进度概览。"""
        completed = sum(1 for item in items if item.status in ("completed", "COMPLETED"))
        total = len(items)
        bar_len = 20
        filled = int(bar_len * completed / total) if total else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        self._console.print(f"\n[bold]📋 进度 {bar} {completed}/{total}[/bold]")
        for i, item in enumerate(items, 1):
            status = item.status if isinstance(item.status, str) else item.status.value
            if status in ("completed", "COMPLETED"):
                icon = "[green]✅[/green]"
                style = "[dim]"
                label = item.content
            elif status in ("in_progress", "IN_PROGRESS"):
                icon = "[yellow]⚡[/yellow]"
                style = "[bold]"
                label = item.active_form or item.content
            elif status in ("failed", "FAILED"):
                icon = "[red]❌[/red]"
                style = ""
                label = item.content
            else:
                icon = "[dim]⬜[/dim]"
                style = "[dim]"
                label = item.content
            self._console.print(f"  {icon} {style}{i}. {label}[/]")
        self._console.print("")

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        """显示任务完成总结面板。"""
        body = f"[bold green]所有任务已完成[/bold green]\n\n完成: {completed}/{total} 个任务"
        if elapsed > 0:
            body += f"\n耗时: {elapsed:.1f}s"
        self._console.print(Panel(
            body,
            title="任务总结",
            border_style="green",
        ))

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """显示工具调用开始。"""
        label = escape(_tool_call_label(name, args))
        self._console.print(f"\n[yellow]$ {label}[/yellow]")

    def show_tool_result(self, name: str, output: str) -> None:
        """显示工具执行结果。"""
        summary = _tool_result_summary(name, output)
        self._console.print(summary if summary is not None else _preview_output(output), markup=False)

    def show_error(self, message: str) -> None:
        """显示错误信息。"""
        self._console.print(f"[red]{message}[/red]")

    def show_status(self, message: str) -> None:
        """显示状态信息（灰色 dim）。"""
        self._console.print(f"[dim]{message}[/dim]")


class QuietRenderer:
    """静默渲染器。

    只解决 renderer 层输出，不负责 suppress llm/runtime 的直接 stdout。
    """

    def show_thinking(self, title: str, reasoning: str) -> None:
        pass

    def show_assistant(self, content: str | None) -> None:
        pass

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        pass

    def show_current_todo(self, item: Any, completed: int, total: int) -> None:
        pass

    def show_progress(self, items: list[Any]) -> None:
        pass

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        pass

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        pass

    def show_tool_result(self, name: str, output: str) -> None:
        pass

    def show_error(self, message: str) -> None:
        pass

    def show_status(self, message: str) -> None:
        pass
