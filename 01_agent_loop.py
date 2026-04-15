"""Session-driven CLI entrypoint."""
from __future__ import annotations

from rich.console import Console

from core.llm.client import ModelGateway
from core.llm.openai_client import OpenAIClient
from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoTrackingPolicy
from core.query.recovery import RecoveryManager
from core.session.engine import SessionEngine
from core.session.view_builder import MessageViewBuilder
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime

console = Console()


def main() -> None:
    tool_context = ToolUseContext(working_dir=".", max_turns=20)
    engine = SessionEngine(
        model_gateway=ModelGateway(OpenAIClient()),
        tool_runtime=ToolExecutorRuntime(registry, tool_context),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoTrackingPolicy()]),
        recovery=RecoveryManager(),
        view_builder=MessageViewBuilder(tools=registry.schemas()),
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

        if not query.strip():
            continue

        result = engine.submit_user_message(query)
        if result.final_output:
            console.print(result.final_output)
        print()


if __name__ == "__main__":
    main()
