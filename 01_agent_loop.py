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

from pathlib import Path

from core.shared.config import MAX_TURNS
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
    """处理一行用户输入。返回 True 继续，False 退出。

    分流逻辑：
    - /skills 命令 → 直接在 engine 层处理，不进 QueryLoop
    - 普通文本 → 进入 QueryLoop 的完整 think-act 循环
    """
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
    # ── UI 层 ──────────────────────────────────────────────
    renderer = RichRenderer(console)

    # ── 工具层 ─────────────────────────────────────────────
    # ToolUseContext: 工具执行的运行时环境（工作目录、文件状态缓存等）
    tool_context = ToolUseContext(working_dir=".", max_turns=MAX_TURNS)

    # ── 组装 Engine（所有组件的唯一协调者）──────────────────
    # Engine 持有 SessionState，其他组件通过 Engine 间接共享状态
    engine = SessionEngine(
        model_gateway=ModelGateway(AnthropicClient()),
        tool_runtime=ToolExecutorRuntime(registry, tool_context, renderer=renderer),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(MAX_TURNS), TodoPlanningPolicy()]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),     # 工具的 JSON schema，传给 API 让模型知道可以调什么
        renderer=renderer,
    )

    # ── REPL 主循环 ─────────────────────────────────────────
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
