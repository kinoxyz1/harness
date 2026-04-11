"""简化版入口：LLM 自主管理 todo，无需 planner。"""
from __future__ import annotations

from rich.console import Console

from core.llm_client import OpenAIClient
from core.context import ContextPipeline, SystemContextPlugin, UserContextPlugin
from core.renderer import RichRenderer
from core.agent import AgentLoop

console = Console()

SYSTEM_PROMPT = "无论如何你都要使用中文回答用户"


def main() -> None:
    # 装配依赖（循环外一次）
    renderer = RichRenderer(console)
    llm = OpenAIClient()

    history: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

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

        history.append({"role": "user", "content": query})

        # 装配 context pipeline
        context = ContextPipeline()
        context.register(SystemContextPlugin())
        context.register(UserContextPlugin())

        # 执行（LLM 自主管理 todo）
        AgentLoop(llm, renderer, context).run(history)
        print()


if __name__ == "__main__":
    main()
