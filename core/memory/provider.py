from __future__ import annotations

from abc import ABC, abstractmethod


class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...

    def prefetch(self, query: str) -> str:
        return ""

    def queue_prefetch(self, query: str) -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        pass

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        pass

    def shutdown(self) -> None:
        pass


class NoopProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "noop"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass
