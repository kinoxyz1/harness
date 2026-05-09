from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ContentReplacementRecord:
    kind: Literal["tool-result"]
    tool_use_id: str
    replacement: str


def clone_content_replacement_state(source: ContentReplacementState) -> ContentReplacementState:
    return ContentReplacementState(
        seen_ids=set(source.seen_ids),
        replacements=dict(source.replacements),
    )


def reconstruct_content_replacement_state(
    messages: list[dict[str, object]],
    records: list[ContentReplacementRecord],
) -> ContentReplacementState:
    state = ContentReplacementState()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id"):
                state.seen_ids.add(str(tool_call["id"]))
    for record in records:
        state.seen_ids.add(record.tool_use_id)
        state.replacements[record.tool_use_id] = record.replacement
    return state
