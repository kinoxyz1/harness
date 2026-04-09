from __future__ import annotations

from rich.console import Console

from core.agent import agent_loop

console = Console()

SYSTEM_PROMPT = "无论如何你都要使用中文回答用户"

def main() -> None:
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
        agent_loop(history)
        print()

if __name__ == "__main__":
    main()
