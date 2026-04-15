from __future__ import annotations

from typing import Protocol


class SupportsMessageDict(Protocol):
    def to_message(self) -> dict[str, object]:
        raise NotImplementedError
