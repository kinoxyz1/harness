"""第一层：文件后备的身份记忆。"""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
from pathlib import Path
from typing import Any


_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|prior|above)\s+instruction", re.I),
    re.compile(r"disregard\s+(previous|prior|above)\s+instruction", re.I),
    re.compile(r"forget\s+(previous|prior|above)\s+instruction", re.I),
]
_LEAK_PATTERNS = [
    re.compile(r"curl\s+\$[A-Z_]+", re.I),
    re.compile(r"printenv", re.I),
    re.compile(r"env\s*\|", re.I),
]
_INVISIBLE_CHARS = re.compile(r"[​-‏⁠-⁯﻿]")


class MemoryStore:
    MEMORY_LIMIT = 2200
    USER_LIMIT = 1375
    ENTRY_SEPARATOR = "\n§\n"

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._mem_dir = self._base_dir / ".harness" / "memories"
        self._mem_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self._snapshot: dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self) -> None:
        self.memory_entries = self._read_entries("MEMORY.md")
        self.user_entries = self._read_entries("USER.md")
        self._snapshot["memory"] = self._render("memory")
        self._snapshot["user"] = self._render("user")

    def format_for_prompt(self, target: str) -> str:
        return self._snapshot.get(target, "")

    def add(self, target: str, content: str) -> dict[str, Any]:
        normalized = content.strip()
        if not normalized:
            return {"ok": False, "error": "content is empty"}
        err = self._security_scan(normalized)
        if err:
            return {"ok": False, "error": err}
        entries = self._entries_for(target)
        if normalized in entries:
            return {"ok": False, "error": "duplicate entry"}
        entries.append(normalized)
        return self._write_back(target, entries)

    def replace(self, target: str, old: str, new: str) -> dict[str, Any]:
        normalized = new.strip()
        if not normalized:
            return {"ok": False, "error": "content is empty"}
        err = self._security_scan(normalized)
        if err:
            return {"ok": False, "error": err}
        entries = self._entries_for(target)
        try:
            idx = entries.index(old)
        except ValueError:
            return {"ok": False, "error": "old_text not found"}
        entries[idx] = normalized
        return self._write_back(target, entries)

    def remove(self, target: str, old: str) -> dict[str, Any]:
        entries = self._entries_for(target)
        try:
            entries.remove(old)
        except ValueError:
            return {"ok": False, "error": "old_text not found"}
        return self._write_back(target, entries)

    def _entries_for(self, target: str) -> list[str]:
        if target == "memory":
            return self.memory_entries
        if target == "user":
            return self.user_entries
        raise ValueError(f"Unknown memory target: {target}")

    def _path_for(self, target: str) -> Path:
        return self._mem_dir / ("MEMORY.md" if target == "memory" else "USER.md")

    def _limit_for(self, target: str) -> int:
        return self.MEMORY_LIMIT if target == "memory" else self.USER_LIMIT

    def _read_entries(self, filename: str) -> list[str]:
        path = self._mem_dir / filename
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return [chunk.strip() for chunk in text.split(self.ENTRY_SEPARATOR) if chunk.strip()]

    def _render(self, target: str) -> str:
        entries = self._entries_for(target)
        if not entries:
            return ""
        tag = "agent-memory" if target == "memory" else "user-profile"
        lines = [f"<{tag}>"]
        for entry in entries:
            lines.append(f"  <entry>{entry}</entry>")
        lines.append(f"</{tag}>")
        return "\n".join(lines)

    def _write_back(self, target: str, entries: list[str]) -> dict[str, Any]:
        rendered = self.ENTRY_SEPARATOR.join(entries)
        limit = self._limit_for(target)
        if len(rendered) > limit:
            return {"ok": False, "error": f"Over limit: {len(rendered)}/{limit} chars"}
        self._atomic_write(self._path_for(target), rendered)
        self._snapshot[target] = self._render(target)
        return {"ok": True, "usage": f"{len(rendered)}/{limit} chars ({len(entries)} entries)"}

    def _atomic_write(self, path: Path, content: str) -> None:
        lock_path = path.with_suffix(path.suffix + ".lock")
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                os.replace(tmp_name, path)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def _security_scan(self, text: str) -> str | None:
        if _INVISIBLE_CHARS.search(text):
            return "invisible unicode characters detected"
        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                return "potential instruction injection"
        for pat in _LEAK_PATTERNS:
            if pat.search(text):
                return "potential secret leak pattern"
        return None
