"""Session-driven CLI entrypoint."""
from __future__ import annotations

from pathlib import Path

from core.shared.env_loader import load_project_env

load_project_env(Path(__file__).with_name(".env"))

from rich.console import Console

from core.llm.client import ModelGateway
from core.llm.anthropic_client import AnthropicClient
from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.recovery import RecoveryManager
from core.ui.renderer import RichRenderer
from core.session.commands import is_skills_command
from core.session.engine import SessionEngine
from core.session.view_builder import MessageViewBuilder
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime

console = Console()


def handle_input(raw: str, engine: SessionEngine) -> bool:
    """Process one line of user input. Returns True to continue, False to quit."""
    text = raw.strip()
    if not text:
        return True
    if is_skills_command(text):
        output = engine.handle_command(text)
        if output:
            console.print(output)
        return True
    result = engine.submit_user_message(text)
    if result.final_output:
        console.print(result.final_output)
    return True


def main() -> None:
    renderer = RichRenderer(console)
    tool_context = ToolUseContext(working_dir=".", max_turns=20)
    engine = SessionEngine(
        model_gateway=ModelGateway(AnthropicClient()),
        tool_runtime=ToolExecutorRuntime(registry, tool_context, renderer=renderer),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoPlanningPolicy()]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),
        renderer=renderer,
    )

    console.print("[bold green]Agent Loop 已启动。[/bold green] 输入 [dim]exit[/dim] 或 [dim]quit[/dim] 退出。\n")
    while True:
        try:
            query = input(">> ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]再见！[/dim]")
            break

        if query.strip().lower() in ("exit", "quit"):
            console.print("[dim]再见！[/dim]")
            break

        handle_input(query, engine)
        print()


if __name__ == "__main__":
    main()
