from __future__ import annotations

import os

API_KEY: str = os.environ.get("ANTHROPIC_API_KEY1", "***REMOVED***")
MODEL: str = os.environ.get("ANTHROPIC_MODEL1", "kimi-k2.5")
BASE_URL: str = os.environ.get("ANTHROPIC_BASE_URL1", "https://api.kimi.com/coding/")
MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
ENABLE_THINKING: bool = os.environ.get("LLM_ENABLE_THINKING", "true").lower() in ("true", "1", "yes")
SHOW_THINKING: bool = os.environ.get("LLM_SHOW_THINKING", "true").lower() in ("true", "1", "yes")
BASH_TIMEOUT: int = int(os.environ.get("BASH_TIMEOUT", "120"))
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "300"))
MAX_OUTPUT_CHARS: int = int(os.environ.get("MAX_OUTPUT_CHARS", "30000"))
