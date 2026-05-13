from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from typing import Any, Callable

from ..prompt.system_context import get_system_context, get_user_context
from ..llm.client import ModelGateway
from ..llm.anthropic_client import AnthropicClient
from ..policy.base import PolicyRunner
from ..policy.max_turns import MaxTurnsPolicy
from ..query.recovery import RecoveryManager
from ..shared.run_options import RunDisplayOptions
from .engine import SessionEngine
from .view_builder import MessageViewBuilder
from ..tools import ToolUseContext, registry
from ..tools.runtime import ToolExecutorRuntime
from ..tasks.models import TaskPacket
from ..skills.runtime import build_invoked_skill_record
from ..shared.stream_events import StreamEvent


class SubagentType(str, Enum):
    EXPLORE = "explore"
    PLAN = "plan"
    GENERAL = "general"


class SubagentContextMode(str, Enum):
    FRESH = "fresh"


class SubagentStopReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    API_ERROR = "api_error"
    EMPTY_RESPONSE = "empty_response"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SubagentDefinition:
    agent_type: SubagentType
    context_mode: SubagentContextMode
    default_max_turns: int
    include_project_context: bool
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    system_prompt_suffix: str = ""


@dataclass
class SubagentRequest:
    task_packet: TaskPacket
    agent_type: SubagentType = SubagentType.GENERAL
    description: str | None = None
    max_turns: int | None = None
    preloaded_skill_ids: list[str] = field(default_factory=list)


@dataclass
class SubagentRunResult:
    request: SubagentRequest
    output: str
    success: bool
    stop_reason: SubagentStopReason
    turns_used: int
    files_modified: list[str]


EXPLORE_AGENT = SubagentDefinition(
    agent_type=SubagentType.EXPLORE,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=10,
    include_project_context=False,
    allowed_tools=("find", "read_file", "todo"),
    disallowed_tools=("subagent",),
    system_prompt_suffix=(
        "你是只读探索代理。\n"
        "- 只允许搜索、读取、分析\n"
        "- 不要修改文件\n"
        "- 输出聚焦于发现、证据、可能原因"
    ),
)

PLAN_AGENT = SubagentDefinition(
    agent_type=SubagentType.PLAN,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=12,
    include_project_context=False,
    allowed_tools=("find", "read_file", "todo"),
    disallowed_tools=("subagent",),
    system_prompt_suffix=(
        "你是规划代理。\n"
        "- 只做分析与规划，不修改文件\n"
        "- 输出应包含实施步骤、关键文件、风险点、验证方式"
    ),
)

GENERAL_AGENT = SubagentDefinition(
    agent_type=SubagentType.GENERAL,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=20,
    include_project_context=True,
    allowed_tools=None,
    disallowed_tools=("subagent",),
    system_prompt_suffix=(
        "你是通用子代理。\n"
        "- 在隔离上下文中完成被分配的任务\n"
        "- 如修改文件，回复中列出修改点\n"
        "- 结论要简洁，避免把完整中间过程带回主代理"
    ),
)


DEFAULT_SUBAGENTS: dict[SubagentType, SubagentDefinition] = {
    SubagentType.EXPLORE: EXPLORE_AGENT,
    SubagentType.PLAN: PLAN_AGENT,
    SubagentType.GENERAL: GENERAL_AGENT,
}


def get_subagent_definition(agent_type: SubagentType) -> SubagentDefinition:
    """根据 agent type 返回内置子代理定义。"""
    try:
        return DEFAULT_SUBAGENTS[agent_type]
    except KeyError as e:
        raise ValueError(f"Unsupported subagent type: {agent_type}") from e


def _compute_allowed_names(definition: SubagentDefinition) -> set[str]:
    """根据子代理定义计算允许的工具名集合。"""
    if definition.allowed_tools is None:
        allowed_names = {schema["name"] for schema in registry.schemas()}
    else:
        allowed_names = set(definition.allowed_tools)
    allowed_names -= set(definition.disallowed_tools)
    return allowed_names


def coerce_stop_reason(value: str) -> SubagentStopReason:
    """将 QueryLoop 的 stop reason 规范化为子代理 stop reason。"""
    if value == "aborted":
        return SubagentStopReason.CANCELLED
    try:
        return SubagentStopReason(value)
    except ValueError:
        return SubagentStopReason.EMPTY_RESPONSE


def render_subagent_summary(result: SubagentRunResult) -> str:
    """将结构化子代理结果压缩为低噪声摘要文本。"""
    status_line = (
        f"子代理已完成（type={result.request.agent_type.value}, turns={result.turns_used}）。"
        if result.success
        else f"子代理未成功完成（reason={result.stop_reason.value}, turns={result.turns_used}）。"
    )

    lines = [status_line, "", "结论：", result.output or "(无有效输出)"]

    if result.files_modified:
        lines.extend(["", "修改文件："])
        lines.extend(f"- {path}" for path in result.files_modified)

    return "\n".join(lines)


def _render_fresh_packet(packet: TaskPacket) -> str:
    sections = [f"Task: {packet.title}", "", "Directive:", packet.directive]
    if packet.done_criteria:
        sections.extend(["", "Done criteria:"] + [f"- {item}" for item in packet.done_criteria])
    if packet.required_skill_ids:
        sections.extend(["", "Preloaded skills:"] + [f"- {item}" for item in packet.required_skill_ids])
    return "\n".join(sections)


def _preload_required_skills(engine, parent_context, skill_ids, turn):
    if parent_context is None or parent_context.skill_registry is None:
        return
    for skill_id in skill_ids:
        content = parent_context.skill_registry.load(skill_id)
        record = build_invoked_skill_record(
            state=engine.state,
            skill_id=skill_id,
            content=content,
            turn=turn,
        )
        engine.state.invoked_skills[skill_id] = record


class SubagentBridgeRenderer:
    """Bridges subagent tool events to parent via emit callback.

    Supports both consume_event() for live streaming and legacy show_*()
    for replayed events. Both paths emit through the same emit callback.
    """

    def __init__(self, *, task_id: str, agent_type: str, emit) -> None:
        self._task_id = task_id
        self._agent_type = agent_type
        self._emit = emit

    def _emit_replayed(self, event: str, *, content: str = "", tool_name: str = "", tool_args=None) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "task_id": self._task_id,
            "agent_type": self._agent_type,
            "source_mode": "replayed",
        }
        if content:
            payload["content"] = content
        if tool_name:
            payload["tool_name"] = tool_name
        if tool_args:
            payload["tool_args"] = dict(tool_args)
        self._emit(payload)

    def consume_event(self, event: StreamEvent) -> None:
        mapping = {
            "thinking_delta": "subagent_thinking_delta",
            "content_delta": "subagent_content_delta",
            "tool_call_start": "subagent_tool_call_start",
            "tool_call_result": "subagent_tool_call_result",
            "status": "subagent_status",
        }
        mapped = mapping.get(event.type)
        if mapped is None:
            return
        payload: dict[str, Any] = {
            "event": mapped,
            "task_id": self._task_id,
            "agent_type": self._agent_type,
            "source_mode": event.source_mode,
        }
        if "text" in event.payload:
            payload["content"] = event.payload["text"]
        if "content" in event.payload:
            payload["content"] = event.payload["content"]
        if "tool_name" in event.payload:
            payload["tool_name"] = event.payload["tool_name"]
        if "tool_args" in event.payload:
            payload["tool_args"] = dict(event.payload["tool_args"])
        self._emit(payload)

    def show_assistant(self, content: str | None) -> None:
        if content:
            self._emit_replayed("subagent_content_delta", content=content)

    def show_thinking(self, title: str, reasoning: str) -> None:
        self._emit_replayed("subagent_thinking_delta", content=reasoning)

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._emit_replayed("subagent_tool_call_start", tool_name=name, tool_args=args)

    def show_tool_result(self, name: str, output: str) -> None:
        self._emit_replayed("subagent_tool_call_result", tool_name=name, content=output)

    def show_status(self, message: str) -> None:
        self._emit_replayed("subagent_status", content=message)

    def show_error(self, message: str) -> None:
        self._emit_replayed("subagent_error", content=message)

    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None:
        return None

    def show_current_todo(self, item, completed: int, total: int) -> None:
        return None

    def show_progress(self, items) -> None:
        return None

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        return None

    def begin_stream(self, turn_id: str, meta: dict[str, Any]) -> None:
        pass

    def end_stream(self, turn_id: str, result_meta: dict[str, Any]) -> None:
        pass


class SubagentRuntime:
    """负责运行隔离上下文中的子代理。"""

    def __init__(
        self,
        *,
        parent_context: ToolUseContext | None = None,
        llm_factory: Callable[[], Any] | None = None,
        tools_registry=registry,
    ) -> None:
        self._parent_context = parent_context
        self._llm_factory = llm_factory or AnthropicClient
        self._tools_registry = tools_registry

    def run(self, request: SubagentRequest, emit=None) -> SubagentRunResult:
        """运行一个 fresh 模式的子代理任务。"""
        definition = get_subagent_definition(request.agent_type)
        if definition.context_mode is not SubagentContextMode.FRESH:
            raise ValueError(f"Unsupported context mode in V1 runtime: {definition.context_mode.value}")

        working_dir = self._parent_context.working_dir if self._parent_context else os.getcwd()
        max_turns = request.max_turns or definition.default_max_turns

        # Bridge renderer for event forwarding
        bridge = (
            SubagentBridgeRenderer(
                task_id=request.task_packet.task_id,
                agent_type=request.agent_type.value,
                emit=emit,
            )
            if emit is not None
            else None
        )
        child_display = RunDisplayOptions(quiet=emit is None)

        # 构建系统提示
        project_root = working_dir if definition.include_project_context else None
        system_prompt = get_system_context(project_root=project_root)
        if definition.system_prompt_suffix.strip():
            system_prompt = f"{system_prompt}\n\n{definition.system_prompt_suffix.strip()}"

        env_context = get_user_context(working_dir)

        # 构建过滤后的注册表和 schema
        allowed_names = _compute_allowed_names(definition)
        sub_registry = self._tools_registry.filtered(allowed_names)
        sub_schemas = sub_registry.schemas()

        # 创建工具上下文
        tool_context = ToolUseContext(working_dir=working_dir, max_turns=max_turns)

        # 创建 session engine - pass tools and renderer explicitly
        engine = SessionEngine(
            model_gateway=ModelGateway(self._llm_factory()),
            tool_runtime=ToolExecutorRuntime(sub_registry, tool_context, display=child_display, renderer=bridge),
            tool_context=tool_context,
            policy_runner=PolicyRunner([MaxTurnsPolicy(max_turns)]),
            recovery=RecoveryManager(),
            view_builder=MessageViewBuilder(tools=sub_schemas),
            tools=sub_schemas,
            renderer=bridge,
        )

        # Set system prompt override
        engine.state.system_prompt_override = system_prompt

        # Preload required skills
        _preload_required_skills(engine, self._parent_context, request.preloaded_skill_ids, turn=0)

        # Emit start event
        if emit is not None:
            emit({"event": "subagent_start", "task_id": request.task_packet.task_id, "agent_type": request.agent_type.value})

        # 执行任务
        prompt_text = _render_fresh_packet(request.task_packet)
        result = engine.submit_user_message(prompt_text)

        # Normalize stop reason
        stop_reason = coerce_stop_reason(result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason))

        # Emit done event
        if emit is not None:
            emit(
                {
                    "event": "subagent_done",
                    "task_id": request.task_packet.task_id,
                    "agent_type": request.agent_type.value,
                    "stop_reason": stop_reason.value,
                    "turns_used": result.turns_used,
                }
            )

        return SubagentRunResult(
            request=request,
            output=result.final_output,
            success=result.success,
            stop_reason=stop_reason,
            turns_used=result.turns_used,
            files_modified=list(result.files_modified),
        )
