"""入口点 — 用户输入从这里进入系统。

数据流总览：
    用户输入 >>
        → handle_input()
            → SessionEngine.submit_user_message()
                → QueryLoop.run()（think-act 主循环）
                    → MessageViewBuilder.build()（组装模型输入）
                    → ModelGateway.call_once()（调用 API）
                    → ToolExecutorRuntime.execute_batch()（执行工具）
                    → 回到循环顶部...
                ← QueryResult（最终回复）
        ← 显示给用户

组件装配（main 函数）：
    所有组件在这里一次性组装，依赖注入风格——每个组件不知道其他组件的存在，
    只通过方法参数传递数据。Engine 是唯一的协调者。

    AnthropicClient → ModelGateway → SessionEngine
    ToolRegistry → ToolExecutorRuntime → SessionEngine
    PolicyRunner(MaxTurnsPolicy, TodoPlanningPolicy) → SessionEngine
"""
from __future__ import annotations

import sys
import termios
import threading
import tty
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from select import select

from core.shared.config import MAX_TURNS
from core.shared.env_loader import load_project_env

load_project_env(Path(__file__).with_name(".env"))

from rich.console import Console

from core.llm.client import ModelGateway
from core.llm.anthropic_client import AnthropicClient
from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoPlanningPolicy
from core.policy.task_planning import TaskPlanningPolicy
from core.policy.skill_relevance import SkillRelevancePolicy
from core.policy.skill_usage_nudge import SkillUsageNudgePolicy
from core.query.recovery import RecoveryManager
from core.ui.renderer import RichRenderer, render_markdown
from core.session.commands import is_skills_command
from core.session.engine import SessionEngine
from core.session.view_builder import MessageViewBuilder
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime

console = Console()


def _char_display_width(ch: str) -> int:
    """Return the terminal column width for a single Unicode character."""
    if ch == "\t":
        return 4
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
        return 0
    if unicodedata.east_asian_width(ch) in {"W", "F"}:
        return 2
    return 1


def _text_display_width(text: str) -> int:
    return sum(_char_display_width(ch) for ch in text)


def _strip_surrogate_codepoints(text: str) -> str:
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


@dataclass
class LineBuffer:
    """A tiny line editor buffer that tracks text and cursor by character."""

    text: str = ""
    cursor: int = 0

    def insert(self, chunk: str) -> None:
        if not chunk:
            return
        self.text = self.text[:self.cursor] + chunk + self.text[self.cursor:]
        self.cursor += len(chunk)

    def backspace(self) -> bool:
        if self.cursor == 0:
            return False
        self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1
        return True

    def delete(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        return True

    def move_left(self) -> bool:
        if self.cursor == 0:
            return False
        self.cursor -= 1
        return True

    def move_right(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.cursor += 1
        return True

    def move_home(self) -> None:
        self.cursor = 0

    def move_end(self) -> None:
        self.cursor = len(self.text)

    def cursor_column(self, prompt: str = "") -> int:
        return _text_display_width(prompt) + _text_display_width(self.text[:self.cursor])


class TerminalLineEditor:
    """Minimal raw-mode line editor with prompt protection and wide-char redraw."""

    def __init__(self, stdin, stdout) -> None:
        self.stdin = stdin
        self.stdout = stdout

    def readline(self, prompt: str = "") -> str | None:
        if not self.stdin.isatty() or not self.stdout.isatty():
            self.stdout.write(prompt)
            self.stdout.flush()
            line = self.stdin.readline()
            if not line:
                return None
            return line.rstrip("\n")

        fd = self.stdin.fileno()
        original = termios.tcgetattr(fd)
        buffer = LineBuffer()

        try:
            tty.setraw(fd)
            self._redraw(prompt, buffer)
            while True:
                ch = self.stdin.read(1)
                if ch == "":
                    self.stdout.write("\n")
                    self.stdout.flush()
                    return None
                if ch in ("\r", "\n"):
                    self.stdout.write("\r\n")
                    self.stdout.flush()
                    return buffer.text
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch == "\x04":
                    if not buffer.text:
                        self.stdout.write("\r\n")
                        self.stdout.flush()
                        return None
                    continue
                if ch in ("\x08", "\x7f"):
                    buffer.backspace()
                    self._redraw(prompt, buffer)
                    continue
                if ch == "\x1b":
                    self._handle_escape_sequence(buffer)
                    self._redraw(prompt, buffer)
                    continue
                if unicodedata.category(ch).startswith("C"):
                    continue

                buffer.insert(ch)
                self._redraw(prompt, buffer)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def _handle_escape_sequence(self, buffer: LineBuffer) -> None:
        seq = self.stdin.read(1)
        if seq != "[":
            return
        command = self.stdin.read(1)
        if command == "D":
            buffer.move_left()
            return
        if command == "C":
            buffer.move_right()
            return
        if command == "H":
            buffer.move_home()
            return
        if command == "F":
            buffer.move_end()
            return
        if command in {"1", "3", "4", "7", "8"}:
            trailer = self.stdin.read(1)
            if trailer != "~":
                return
            if command == "1" or command == "7":
                buffer.move_home()
            elif command == "3":
                buffer.delete()
            elif command == "4" or command == "8":
                buffer.move_end()

    def _redraw(self, prompt: str, buffer: LineBuffer) -> None:
        text = prompt + buffer.text
        self.stdout.write("\r\x1b[2K")
        self.stdout.write(text)
        end_column = _text_display_width(text)
        cursor_column = buffer.cursor_column(prompt)
        if end_column > cursor_column:
            self.stdout.write(f"\x1b[{end_column - cursor_column}D")
        self.stdout.flush()


class RunAbortMonitor:
    """During execution, watch stdin for Esc/Ctrl-C and request cancellation."""

    def __init__(self, stdin, callback) -> None:
        self.stdin = stdin
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._triggered = False
        self._original = None

    def __enter__(self):
        if not self.stdin.isatty():
            return self
        fd = self.stdin.fileno()
        self._original = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        if self._original is not None:
            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self._original)

    def _watch(self) -> None:
        fd = self.stdin.fileno()
        while not self._stop.is_set():
            ready, _, _ = select([fd], [], [], 0.1)
            if not ready:
                continue
            ch = self.stdin.read(1)
            if ch in ("\x1b", "\x03") and not self._triggered:
                self._triggered = True
                self._callback()
                return


def read_user_input(prompt: str = ">> ") -> str | None:
    return TerminalLineEditor(sys.stdin, sys.stdout).readline(prompt)


def handle_input(raw: str, engine: SessionEngine) -> bool:
    """处理一行用户输入。返回 True 继续，False 退出。

    分流逻辑：
    - /skills 命令 → 直接在 engine 层处理，不进 QueryLoop
    - 普通文本 → 进入 QueryLoop 的完整 think-act 循环
    """
    # macOS CJK 输入法删除字符时可能留下 partial UTF-8 字节，
    # Python input() 用 surrogateescape 解码，产生 \udce5 等代理字符，
    # 导致 Anthropic SDK JSON 序列化崩溃。在此清除。
    text = _strip_surrogate_codepoints(raw).strip()
    if not text:
        return True
    if is_skills_command(text):
        output = engine.handle_command(text)
        if output:
            console.print(output)
        return True
    result = engine.submit_user_message(text)
    if result.final_output and not result.streaming_displayed:
        render_markdown(console, result.final_output)
    return True


def main() -> None:
    # ── UI 层 ──────────────────────────────────────────────
    renderer = RichRenderer(console)

    # ── 工具层 ─────────────────────────────────────────────
    # ToolUseContext: 工具执行的运行时环境（工作目录、文件状态缓存等）
    tool_context = ToolUseContext(working_dir=".", max_turns=MAX_TURNS)

    # ── 组装 Engine（所有组件的唯一协调者）──────────────────
    # Engine 持有 SessionState，其他组件通过 Engine 间接共享状态
    model_gateway = ModelGateway(AnthropicClient())

    engine = SessionEngine(
        model_gateway=model_gateway,
        tool_runtime=ToolExecutorRuntime(registry, tool_context, renderer=renderer),
        tool_context=tool_context,
        policy_runner=PolicyRunner([
            TaskPlanningPolicy(),
            MaxTurnsPolicy(MAX_TURNS),
            TodoPlanningPolicy(),
            SkillRelevancePolicy(model_gateway=model_gateway),
            SkillUsageNudgePolicy(),
        ]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),     # 工具的 JSON schema，传给 API 让模型知道可以调什么
        renderer=renderer,
    )

    # ── REPL 主循环 ─────────────────────────────────────────
    # 注意：不使用 input()/sys.stdin.readline() 的默认行编辑。
    # 默认终端 cooked mode 不会保护提示符，并且回删是按终端列宽工作，
    # 遇到 CJK 宽字符时容易出现“删一个字要按两次”以及把 `>> ` 一起删掉。
    # 这里使用应用层行编辑，按 Unicode 字符维护缓冲区，再整体重绘一行。
    console.print("[bold green]Agent Loop 已启动。[/bold green] 输入 [dim]exit[/dim] 或 [dim]quit[/dim] 退出。\n")
    while True:
        try:
            query = read_user_input(">> ")
            if query is None:
                console.print("\n[dim]再见！[/dim]")
                break
        except KeyboardInterrupt:
            console.print("\n[dim]再见！[/dim]")
            break

        if query.strip().lower() in ("exit", "quit"):
            console.print("[dim]再见！[/dim]")
            break

        with RunAbortMonitor(sys.stdin, lambda: engine.request_cancel()):
            handle_input(query, engine)
        print()


if __name__ == "__main__":
    main()
