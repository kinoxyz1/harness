from __future__ import annotations


class PromptCache:
    def get(self, store: dict[str, str], key: str) -> str | None:
        return store.get(key)

    def set(self, store: dict[str, str], key: str, value: str) -> str:
        store[key] = value
        return value
