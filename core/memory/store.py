"""第一层：文件后备的身份记忆。"""
from __future__ import annotations

import difflib
import fcntl
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


_FINGERPRINT_STOPWORDS = frozenset(
    "the a an is are was were has have had with from that this "
    "and or but not very also just quite rather really "
    "le la les un une des der die das ein eine "
    "的 了 在 是 有 和 与 也 都 就 而 ".split()
)


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
    ENTRY_SEPARATOR = "\n\n---\n\n"
    _LEGACY_ENTRY_SEPARATORS = ("\n§\n", ENTRY_SEPARATOR)
    _FINGERPRINT_MAP_PATH = "fingerprint_map.json"
    _fingerprint_map: dict[str, str]

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._mem_dir = self._base_dir / ".harness" / "memories"
        self._mem_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self._snapshot: dict[str, str] = {"memory": "", "user": ""}
        self._fingerprint_map = self._load_fingerprint_map()

    def load_from_disk(self) -> None:
        self.memory_entries = self._read_entries("MEMORY.md")
        self.user_entries = self._read_entries("USER.md")
        self._snapshot["memory"] = self._render("memory")
        self._snapshot["user"] = self._render("user")
        self._rewrite_canonical_file("memory")
        self._rewrite_canonical_file("user")

    def format_for_prompt(self, target: str) -> str:
        return self._snapshot.get(target, "")

    def add(self, target: str, content: str) -> dict[str, Any]:
        normalized = self._normalize_entry(target, content)
        if not normalized:
            return {"ok": False, "error": "content is empty"}
        err = self._security_scan(normalized)
        if err:
            return {"ok": False, "error": err}
        entries = self._entries_for(target)
        if any(self._is_duplicate_entry(target, normalized, existing) for existing in entries):
            return {"ok": False, "error": "duplicate entry"}
        entries.append(normalized)
        return self._write_back(target, entries)

    def replace(self, target: str, old: str, new: str) -> dict[str, Any]:
        normalized = self._normalize_entry(target, new)
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
        target = "memory" if filename == "MEMORY.md" else "user"
        return self._dedupe_entries(target, self._split_entries(text))

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
        deduped_entries = self._dedupe_entries(target, entries)
        rendered = self._render_file(deduped_entries)
        limit = self._limit_for(target)
        if len(rendered) > limit:
            return {"ok": False, "error": f"Over limit: {len(rendered)}/{limit} chars"}
        self._atomic_write(self._path_for(target), rendered)
        if target == "memory":
            self.memory_entries = deduped_entries
        else:
            self.user_entries = deduped_entries
        return {"ok": True, "usage": f"{len(rendered)}/{limit} chars ({len(deduped_entries)} entries)"}

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

    def _render_file(self, entries: list[str]) -> str:
        return self.ENTRY_SEPARATOR.join(entry.strip() for entry in entries if entry.strip())

    def _rewrite_canonical_file(self, target: str) -> None:
        path = self._path_for(target)
        if not path.exists():
            return
        try:
            current = path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        rendered = self._render_file(self._entries_for(target))
        if current != rendered:
            self._atomic_write(path, rendered)

    def _split_entries(self, text: str) -> list[str]:
        for separator in self._LEGACY_ENTRY_SEPARATORS:
            if separator in text:
                return [chunk.strip() for chunk in text.split(separator) if chunk.strip()]
        return [text.strip()] if text.strip() else []

    def _normalize_entry(self, target: str, content: str) -> str:
        normalized = re.sub(r"\s+", " ", content or "").strip()
        if target == "user":
            stripped = re.sub(
                r"^(?:the\s+user|user)\s+(?=(?:enjoys?|likes?|prefers?|uses?|works?|responds?|is|has|wants?)\b)",
                "",
                normalized,
                flags=re.I,
            )
            if stripped != normalized and stripped:
                normalized = stripped
                normalized = normalized[0].upper() + normalized[1:]
        return normalized

    def _dedupe_entries(self, target: str, entries: list[str]) -> list[str]:
        deduped: list[str] = []
        for entry in entries:
            normalized = self._normalize_entry(target, entry)
            if not normalized:
                continue
            if any(self._is_duplicate_entry(target, normalized, existing) for existing in deduped):
                continue
            deduped.append(normalized)
        return deduped

    def _is_duplicate_entry(self, target: str, candidate: str, existing: str) -> bool:
        left = self._normalize_entry(target, candidate)
        right = self._normalize_entry(target, existing)
        if left == right:
            return True
        if target != "user":
            return False
        left_lower = left.casefold()
        right_lower = right.casefold()
        if left_lower in right_lower or right_lower in left_lower:
            return True
        return difflib.SequenceMatcher(a=self._fingerprint(left), b=self._fingerprint(right)).ratio() >= 0.62

    def _fingerprint(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text.casefold())
        normalized = normalized.replace('"', "")
        tokens = re.findall(r"[a-z]+|[\u4e00-\u9fff]", normalized)
        tokens = [t for t in tokens if t not in _FINGERPRINT_STOPWORDS]
        tokens = [self._fingerprint_map.get(t, t) for t in tokens]
        return "".join(tokens)

    def _load_fingerprint_map(self) -> dict[str, str]:
        path = self._mem_dir / self._FINGERPRINT_MAP_PATH
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
        return {}
