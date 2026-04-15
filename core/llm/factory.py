from __future__ import annotations

import anthropic

from ..shared.config import API_KEY, BASE_URL


def create_llm_client() -> anthropic.Anthropic:
    # Use an explicit client timeout so the SDK does not reject large
    # non-streaming requests with its default 10-minute heuristic.
    kwargs: dict = {"api_key": API_KEY, "timeout": 3600.0}
    if BASE_URL:
        kwargs["base_url"] = BASE_URL
    return anthropic.Anthropic(**kwargs)
