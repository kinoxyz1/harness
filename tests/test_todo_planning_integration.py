from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.recovery import RecoveryManager
from core.session.engine import SessionEngine
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime


def write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def write_analysis_report_fixture(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        "---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the report workflow.\n",
    )
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    (tmp_path / "c.txt").write_text("gamma", encoding="utf-8")
    (tmp_path / "d.txt").write_text("delta", encoding="utf-8")


def response_with_tool(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[{"id": call_id, "name": name, "args": args}],
        content="",
        has_final_text=False,
        to_message=lambda: {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": name, "args": args}],
        },
    )


def response_with_text(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[],
        content=text,
        has_final_text=True,
        to_message=lambda: {"role": "assistant", "content": text},
    )


class StubModelGateway:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)

    def call_once(self, messages, *, system="", tools):
        return self._responses.pop(0)


def make_engine_with_stubbed_model(tmp_path: Path, responses: list[SimpleNamespace]) -> SessionEngine:
    tool_context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    return SessionEngine(
        model_gateway=StubModelGateway(responses),
        tool_runtime=ToolExecutorRuntime(registry, tool_context),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoPlanningPolicy()]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),
        renderer=None,
    )


def test_analysis_report_todo_preserves_skill_workflow_labels(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_tool(
                "toolu_todo",
                "todo",
                {
                    "items": [
                        {"content": "Collect target inputs", "active_form": "Collecting target inputs", "status": "completed", "workflow_ref": "1"},
                        {"content": "Perform primary analysis", "active_form": "Performing primary analysis", "status": "completed", "workflow_ref": "2"},
                        {"content": "Cross-check findings", "active_form": "Cross-checking findings", "status": "in_progress", "workflow_ref": "2.5"},
                        {"content": "Draft the final report", "active_form": "Drafting the final report", "status": "pending", "workflow_ref": "3"},
                        {"content": "Verify report completeness", "active_form": "Verifying report completeness", "status": "pending", "workflow_ref": "4"},
                    ]
                },
            ),
            response_with_text("final"),
        ],
    )

    engine.submit_user_message("Generate the analysis report")

    assert any(item.workflow_ref == "2.5" for item in engine.state.todo_state.items)
    assert len(engine.state.todo_state.items) == 5
    assert engine.state.todo_state.last_write_turn == 1


def test_stale_reminder_does_not_fire_during_normal_two_step_scoping(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_tool("toolu_read_1", "read_file", {"path": "a.txt"}),
            response_with_tool("toolu_read_2", "read_file", {"path": "b.txt"}),
            response_with_tool(
                "toolu_todo",
                "todo",
                {
                    "items": [
                        {"content": "Collect scope", "active_form": "Collecting scope", "status": "completed", "workflow_ref": "1"},
                        {"content": "Analyze inputs", "active_form": "Analyzing inputs", "status": "in_progress", "workflow_ref": "2"},
                    ]
                },
            ),
            response_with_text("final"),
        ],
    )

    engine.submit_user_message("Generate the analysis report")

    assert not any(
        "todo_stale" in message.get("content", "")
        for message in engine.state.conversation_messages
        if message["role"] == "user"
    )


def test_stale_reminder_fires_after_four_non_todo_turns(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_tool(
                "toolu_todo",
                "todo",
                {
                    "items": [
                        {"content": "Analyze inputs", "active_form": "Analyzing inputs", "status": "in_progress", "workflow_ref": "2"},
                    ]
                },
            ),
            response_with_tool("toolu_read_1", "read_file", {"path": "a.txt"}),
            response_with_tool("toolu_read_2", "read_file", {"path": "b.txt"}),
            response_with_tool("toolu_read_3", "read_file", {"path": "c.txt"}),
            response_with_tool("toolu_read_4", "read_file", {"path": "d.txt"}),
            response_with_text("final"),
        ],
    )

    engine.submit_user_message("Generate the analysis report")

    assert any(
        "todo_stale" in message.get("content", "")
        for message in engine.state.conversation_messages
        if message["role"] == "user"
    )


def test_skill_invocation_records_the_current_query_turn(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_read_1", "read_file", {"path": "a.txt"}),
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_text("final"),
        ],
    )

    engine.submit_user_message("Generate the analysis report")

    assert engine.state.invoked_skills["analysis-report"].invoked_at_turn == 1


def test_session_engine_default_query_loop_path_wires_context_manager(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[response_with_text("final")],
    )

    result = engine.submit_user_message("Generate the analysis report")

    assert result.final_output == "final"


def test_session_engine_default_summary_compaction_supports_legacy_gateway(tmp_path: Path) -> None:
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_text("summary"),
            response_with_text("final"),
        ],
    )
    for _ in range(5):
        engine.append_message({"role": "user", "content": "x" * 90_000})

    result = engine.submit_user_message("Generate the analysis report")

    assert result.final_output == "final"
