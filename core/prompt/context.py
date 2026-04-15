from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PromptContext:
    stable_system_prompt: str
    dynamic_prompt: str
