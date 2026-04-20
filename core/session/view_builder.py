from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .state import SessionState

if TYPE_CHECKING:
    from core.prompt.assembler import PromptAssembler


@dataclass(slots=True)
class ModelInputView:
    """模型输入视图：一次模型调用所需的完整输入数据。

    核心设计：将 system（系统提示）和 messages（对话记录）分离。
    system 由 PromptAssembler 实时从 SessionState 渲染，不依赖 transcript；
    messages 是从 conversation_messages 中按预算截取的 transcript slice。

    Attributes:
        system: 系统提示词，由 stable + runtime + overlay 三部分拼接而成。
        messages: 发送给模型的对话消息列表（transcript slice）。
        tools: 可用工具的 JSON schema 列表。None 表示不传 tools。
        internal_runtime_view: 调试用的内部状态快照，不会发送给模型。
    """
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    internal_runtime_view: dict[str, Any] = field(default_factory=dict)


class MessageViewBuilder:
    """消息视图构建器：将 SessionState 转换为 ModelInputView。

    职责：
    1. 从 conversation_messages 中按字符预算截取 transcript slice
    2. 调用 PromptAssembler 的三个接口组装 system 提示词
    3. 根据 run_state 的 allowed_tools_override 过滤工具列表
    4. 组装为 ModelInputView 返回
    """

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        """
        Args:
            tools: 可用工具的 JSON schema 列表。None 表示模型不使用工具。
        """
        self._tools = tools

    def _content_char_cost(self, content: Any) -> int:
        """估算消息内容的字符开销。list/dict 类型上限 6000 字符。"""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return len(str(content)[:6_000])
        if isinstance(content, dict):
            return len(str(content)[:6_000])
        return 0

    def _message_char_cost(self, message: dict[str, Any]) -> int:
        """估算消息的总字符开销，包含 reasoning 字段。"""
        cost = self._content_char_cost(message.get("content", ""))
        reasoning = message.get("reasoning", "")
        if reasoning:
            cost += len(reasoning)
        return cost

    def _strip_old_thinking(
        self, messages: list[dict[str, Any]], *, keep_last: int = 2
    ) -> list[dict[str, Any]]:
        """清理旧 thinking 块，只保留最近 N 个 assistant 消息的 reasoning。

        返回新列表（不修改原始 conversation_messages），
        超出 keep_last 的 assistant 消息的 reasoning/reasoning_signature 被移除。
        """
        # 找到所有 assistant 消息的索引
        assistant_indices = [
            i for i, msg in enumerate(messages) if msg.get("role") == "assistant"
        ]
        # 需要保留 reasoning 的 assistant 索引集合（最后 keep_last 个）
        keep_set = set(assistant_indices[-keep_last:]) if assistant_indices else set()

        cleaned: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and i not in keep_set:
                if msg.get("reasoning") or msg.get("reasoning_signature"):
                    stripped = {k: v for k, v in msg.items() if k not in ("reasoning", "reasoning_signature")}
                    cleaned.append(stripped)
                    continue
            cleaned.append(msg)
        return cleaned

    def _find_matching_tool_use(self, messages: list[dict[str, Any]], *, tool_call_id: str, before_index: int) -> int | None:
        """向前查找生成某个 tool_result 的 assistant tool_use 消息。"""
        for idx in range(before_index - 1, -1, -1):
            message = messages[idx]
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            if any(call.get("id") == tool_call_id for call in tool_calls if isinstance(call, dict)):
                return idx
        return None

    def _select_transcript_slice(
        self,
        messages: list[dict[str, Any]],
        *,
        char_budget: int,
    ) -> list[dict[str, Any]]:
        """从对话历史中按字符预算从末尾向前截取消息。

        保证最新的消息一定被包含（即使超出预算），然后从后往前填充
        直到达到 char_budget。保留消息的原始顺序。

        Args:
            messages: 完整的 conversation_messages 列表。
            char_budget: 允许的最大字符数。

        Returns:
            截取后的消息子列表，保持原始顺序。
        """
        selected_indices: list[int] = []
        used = 0
        for idx in range(len(messages) - 1, -1, -1):
            message = messages[idx]
            cost = self._message_char_cost(message)
            if selected_indices and used + cost > char_budget:
                continue
            selected_indices.append(idx)
            used += cost
            if used >= char_budget:
                break

        if not selected_indices:
            return []

        expanded_indices = set(selected_indices)
        for idx in list(selected_indices):
            message = messages[idx]
            if message.get("role") != "tool":
                continue
            tool_call_id = message.get("tool_call_id")
            if not tool_call_id:
                continue
            assistant_idx = self._find_matching_tool_use(messages, tool_call_id=tool_call_id, before_index=idx)
            if assistant_idx is not None:
                expanded_indices.add(assistant_idx)

        return [messages[idx] for idx in sorted(expanded_indices)]

    def build(
        self,
        state: SessionState,
        *,
        run_state,
        prompt_assembler: PromptAssembler,
        working_dir: str,
        project_root: str | None = None,
        transcript_char_budget: int | None = None,
    ) -> ModelInputView:
        """从 SessionState 构建 ModelInputView。

        组装流程：
        1. 截取 transcript slice（默认 24K 字符预算）
        2. 调用 prompt_assembler 组装 system 三件套（stable + runtime + overlay）
        3. 根据 run_state.allowed_tools_override 过滤工具
        4. 收集 internal_runtime_view 用于调试

        Args:
            state: 会话状态，包含对话历史、激活的 skill、todo、文件状态等。
            run_state: 当轮运行状态，控制工具过滤和 overlay 渲染。
            prompt_assembler: 提示词组装器。
            working_dir: 当前工作目录，传递给 assembler 生成环境信息。
            project_root: 项目根目录，传递给 assembler 加载项目级指令。
            transcript_char_budget: transcript 截取的字符预算。None 默认 24_000。

        Returns:
            ModelInputView 实例，包含 system、messages、tools、internal_runtime_view。
        """
        budget = transcript_char_budget or 24_000
        transcript_slice = self._select_transcript_slice(state.conversation_messages, char_budget=budget)
        transcript_slice = self._strip_old_thinking(transcript_slice, keep_last=2)
        system_parts = [
            prompt_assembler.build_stable_context(state, project_root=project_root),
            prompt_assembler.build_runtime_context(state, working_dir=working_dir),
            prompt_assembler.build_query_overlay(state, run_state),
        ]
        tools = self._tools
        if run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        internal_runtime_view = prompt_assembler.build_internal_runtime_view(state, run_state)
        internal_runtime_view["transcript_slice"] = list(transcript_slice)
        return ModelInputView(
            system="\n\n".join(part for part in system_parts if part),
            messages=transcript_slice,
            tools=tools,
            internal_runtime_view=internal_runtime_view,
        )
