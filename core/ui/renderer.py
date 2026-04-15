"""显示渲染实现。"""
from __future__ import annotations

from itertools import islice
from typing import Any

from rich.console import Console
from rich.panel import Panel

from ..shared.interfaces import Renderer
from ..shared.config import SHOW_THINKING


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    if name == "bash" and args.get("command"):
        return str(args["command"])

    preferred_keys = ("path", "pattern", "query", "task", "offset", "limit")
    parts = [f"{key}={args[key]!r}" for key in preferred_keys if key in args]
    if not parts:
        return name
    return f"{name} " + " ".join(parts)


def _preview_output(output: str, *, max_lines: int = 10, max_chars: int = 1200) -> str:
    if not output:
        return "(无输出)"

    lines = output.splitlines()
    preview_lines = list(islice(lines, max_lines))
    preview = "\n".join(preview_lines)
    truncated = len(lines) > max_lines or len(output) > max_chars
    if len(preview) > max_chars:
        preview = preview[:max_chars]
        truncated = True
    if truncated:
        preview += "\n... (已省略后续输出)"
    return preview


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
        self._console.print(Panel(
            f"[bold green]所有任务已完成[/bold green]\n\n"
            f"完成: {completed}/{total} 个任务\n"
            f"耗时: {elapsed:.1f}s",
            title="任务总结",
            border_style="green",
        ))

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """显示工具调用开始。"""
        self._console.print(f"\n[yellow]$ {_summarize_tool_args(name, args)}[/yellow]")

    def show_tool_result(self, name: str, output: str) -> None:
        """显示工具执行结果。"""
        self._console.print(_preview_output(output))

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
