from __future__ import annotations

from typing import Any


def create_compact_boundary(*, reason: str, summarized_messages: int) -> dict[str, Any]:
    return {
        "role": "meta_compact_boundary",
        "kind": "compact_boundary",
        "content": f"reason={reason};summarized_messages={summarized_messages}",
    }


def create_compact_summary(summary_text: str) -> dict[str, Any]:
    return {
        "role": "meta_compact_summary",
        "kind": "compact_summary",
        "content": summary_text,
    }


def build_post_compact_messages(
    *,
    boundary: dict[str, Any],
    summary: dict[str, Any],
    kept: list[dict[str, Any]],
    runtime_restore: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [boundary, summary, *kept, *runtime_restore]
