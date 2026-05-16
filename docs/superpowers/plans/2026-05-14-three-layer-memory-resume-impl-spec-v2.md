# 三层记忆 + /resume 实现规范 V2

> 日期：2026-05-14
> 状态：Draft
> 基于：
> - `docs/superpowers/specs/2026-05-14-three-layer-memory-resume-design.md`
> - `docs/superpowers/impl/2026-05-14-three-layer-memory-resume-impl-spec.md`
>
> 目标：给出一版和当前 Harness 代码边界对齐、并且包含明确代码骨架的 impl spec。本文不是纯方向性说明，而是“工程师可以直接照着改”的实现草案。

---

## 1. 实现原则

1. 第一层身份记忆进入 stable prompt，但必须影响 stable cache key。
2. 第二层先做“单 session 持久化 + /resume”，不在 V1 引入 compact lineage。
3. `/resume` 必须通过重建整个 `SessionEngine` 实现，不能替换已有 `SessionState` 引用。
4. 第三层 recalled memory 走 `build_query_overlay_blocks()`，不污染 transcript。
5. 第三层第一版直接复用 SQLite FTS5，先不手写 TF-IDF。

---

## 2. 关键边界映射

| 设计概念 | 当前代码入口 | 结论 |
| --- | --- | --- |
| 会话长期状态 | `core/session/state.py` | 新字段挂 `SessionState` |
| 对话写入口 | `core/session/store.py` | 持久化应基于 `store.snapshot()` |
| 工具状态回写 | `core/query/reducers.py` | memory/provider 通知走 `SessionUpdate` |
| stable prompt | `core/prompt/assembler.py::build_stable()` | 第一层在这里注入 |
| overlay prompt | `core/prompt/assembler.py::build_query_overlay_blocks()` | 第三层在这里注入 |
| 命令分流 | `01_agent_loop.py` + `core/session/commands.py` | `/resume` 走 CLI 路由 |
| 会话引擎初始化 | `core/session/engine.py` | resume 后要整体重建 |

---

## 3. Phase 1：第一层身份记忆

### 3.1 `core/session/state.py`

在 `SessionState` 增加持久化与记忆相关字段：

```python
# core/session/state.py

@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    session_id: str = field(default_factory=lambda: uuid4().hex[:16])
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    skill_events: list[SkillEvent] = field(default_factory=list)
    invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
    skills_revision: str | None = None
    read_file_state: dict[str, Any] = field(default_factory=dict)
    system_prompt_override: str | None = None
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
    todo_state: TodoState = field(default_factory=TodoState)
    task_state: TaskState = field(default_factory=TaskState)
    compact_state: dict[str, Any] = field(default_factory=_default_compact_state)
    content_replacement_state: ContentReplacementState = field(default_factory=ContentReplacementState)
    user_intents: list[str] = field(default_factory=list)

    queries_since_skill_activation: int = 0
    last_known_skill_keys: set[str] = field(default_factory=set)
    skill_relevance_cooldown: dict[str, int] = field(default_factory=dict)

    # ── memory / persistence ──────────────────────────────
    memory_store: Any = None
    session_db: Any = None
    memory_provider: Any = None
    _last_flushed_idx: int = 0
```

说明：

- 这里先用 `Any`，避免 phase 间循环依赖。
- 由于 `SessionState` 是 `slots=True`，所有测试里直接构造它的地方都要过一遍。

### 3.2 `core/memory/store.py`

新增第一层存储实现：

```python
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
_INVISIBLE_CHARS = re.compile(r"[\u200B-\u200F\u2060-\u206F\uFEFF]")


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
```

### 3.3 `core/tools/builtin/memory.py`

不要把 tool handler 放在 `core/memory/`，因为当前 auto-discover 只扫描 `core/tools/builtin/*.py`。

```python
from __future__ import annotations

from typing import Any

from core.tools.context import (
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
    make_tool_message,
)


SCHEMA: dict[str, Any] = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory. "
        "Use target='user' for user preferences/profile; "
        "use target='memory' for your own long-lived notes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"]},
            "target": {"type": "string", "enum": ["memory", "user"]},
            "content": {"type": "string"},
            "old_text": {"type": "string"},
        },
        "required": ["action", "target"],
    },
}

READONLY = False
ANNOTATIONS = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    state = context.session_state
    store = getattr(state, "memory_store", None) if state is not None else None
    if store is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="memory_store_unavailable",
            messages=[make_tool_message(context, "Memory store unavailable.")],
        )

    action = str(args.get("action", ""))
    target = str(args.get("target", ""))
    content = str(args.get("content", ""))
    old_text = str(args.get("old_text", ""))

    if action == "add":
        result = store.add(target, content)
    elif action == "replace":
        result = store.replace(target, old_text, content)
    elif action == "remove":
        result = store.remove(target, old_text)
    else:
        result = {"ok": False, "error": f"unknown action: {action}"}

    if not result.get("ok"):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="memory_write_failed",
            messages=[make_tool_message(context, f"Memory update failed: {result['error']}")],
        )

    payload_content = content if action != "remove" else old_text
    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[make_tool_message(context, f"Memory updated. Usage: {result['usage']}")],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.MEMORY_WRITE,
                payload={
                    "action": action,
                    "target": target,
                    "content": payload_content,
                },
            )
        ],
    )
```

### 3.4 `core/tools/context.py` 和 `core/query/reducers.py`

新增 update kind：

```python
# core/tools/context.py
class SessionUpdateKind(str, Enum):
    INVOKE_SKILL = "invoke_skill"
    SET_TODO_ITEMS = "set_todo_items"
    SET_TASK_STATE = "set_task_state"
    UPSERT_FILE_STATE = "upsert_file_state"
    INVALIDATE_FILE_STATE = "invalidate_file_state"
    APPEND_SKILL_EVENT = "append_skill_event"
    MEMORY_WRITE = "memory_write"
```

reducer 侧接线：

```python
# core/query/reducers.py
def apply_session_update(session_state, update: SessionUpdate) -> None:
    payload = update.payload

    ...

    if update.kind == SessionUpdateKind.MEMORY_WRITE:
        provider = getattr(session_state, "memory_provider", None)
        if provider is not None:
            provider.on_memory_write(
                action=str(payload.get("action", "")),
                target=str(payload.get("target", "")),
                content=str(payload.get("content", "")),
            )
        return

    raise ValueError(f"Unsupported session update kind: {update.kind}")
```

### 3.5 `core/prompt/assembler.py`

稳定层注入和 cache key 一起改：

```python
def _stable_cache_key(state: SessionState, *, project_root: str | None = None) -> str:
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    memory_fp = "no-memory"
    if state.memory_store is not None:
        memory_blob = (
            state.memory_store.format_for_prompt("memory")
            + "\n"
            + state.memory_store.format_for_prompt("user")
        )
        memory_fp = hashlib.sha256(memory_blob.encode("utf-8")).hexdigest()[:8]
    return f"stable_system_prompt:{revision}:{digest}:{memory_fp}"


def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
    cache_key = _stable_cache_key(state, project_root=project_root)
    cached = self._cache.get(state.prompt_cache, cache_key)
    if cached is not None:
        return cached

    parts = [get_system_context(project_root=project_root)]
    catalog = _render_skill_catalog(state)
    if catalog:
        parts.append(catalog)
    if state.system_prompt_override:
        parts.append(state.system_prompt_override)
    if state.memory_store is not None:
        memory_text = state.memory_store.format_for_prompt("memory")
        user_text = state.memory_store.format_for_prompt("user")
        if memory_text:
            parts.append(memory_text)
        if user_text:
            parts.append(user_text)

    stable_prompt = "\n\n".join(part for part in parts if part)
    return self._cache.set(state.prompt_cache, cache_key, stable_prompt)
```

### 3.6 `core/session/engine.py`

初始化接 memory：

```python
from core.memory.store import MemoryStore


class SessionEngine:
    def __init__(..., session_id: str | None = None):
        self._state = SessionState(conversation_messages=[])
        working_dir = Path(getattr(tool_context, "working_dir", "."))

        self._state.memory_store = MemoryStore(base_dir=working_dir)
        self._state.memory_store.load_from_disk()

        self._store = SessionStore(self._state, working_dir=working_dir)
        ...
```

---

## 4. Phase 2：第二层对话持久化

### 4.1 `core/session/db.py`

V1 只做单 session transcript + snapshot，不做 compact lineage。

```python
"""第二层：SQLite + JSON 的对话持久化。"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable


class SessionDB:
    def __init__(self, db_path: str | Path, sessions_dir: str | Path) -> None:
        self._db_path = Path(db_path)
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                tool_call_id TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                reasoning TEXT DEFAULT '',
                timestamp REAL NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        self._conn.commit()

    def _execute_write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        for _ in range(10):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = fn(self._conn)
                self._conn.commit()
                return result
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    time.sleep(random.uniform(0.02, 0.1))
                    continue
                raise
        raise sqlite3.OperationalError("database remained locked after retries")

    def ensure_session_row(self, session_id: str, title: str = "") -> None:
        now = time.time()

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, title, now, now),
            )

        self._execute_write(_upsert)

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return

        def _insert(conn: sqlite3.Connection) -> None:
            for msg in messages:
                conn.execute(
                    """
                    INSERT INTO messages
                    (session_id, role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        msg.get("role", ""),
                        msg.get("content", ""),
                        json.dumps(msg.get("tool_calls", []), ensure_ascii=False) if msg.get("tool_calls") else "",
                        msg.get("tool_call_id", ""),
                        msg.get("name", ""),
                        msg.get("reasoning", ""),
                        msg.get("_meta", {}).get("created_at", time.time()),
                    ),
                )
            conn.execute(
                """
                UPDATE sessions
                SET updated_at = ?, message_count = (
                    SELECT COUNT(*) FROM messages WHERE session_id = ?
                )
                WHERE id = ?
                """,
                (time.time(), session_id, session_id),
            )

        self._execute_write(_insert)

    def update_token_counts(self, session_id: str, input_tokens: int, output_tokens: int) -> None:
        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE sessions
                SET total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (input_tokens, output_tokens, time.time(), session_id),
            )

        self._execute_write(_update)

    def save_session_snapshot(self, session_id: str, state_dict: dict[str, Any]) -> None:
        session_dir = self._sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "state.json"
        tmp = session_dir / ".state.json.tmp"
        tmp.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        )
        rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for role, content, tool_calls, tool_call_id, tool_name, reasoning, timestamp in rows:
            msg: dict[str, Any] = {"role": role, "content": content}
            if tool_calls:
                msg["tool_calls"] = json.loads(tool_calls)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["name"] = tool_name
            if reasoning:
                msg["reasoning"] = reasoning
            msg["_meta"] = {"created_at": timestamp}
            result.append(msg)
        return result
```

### 4.2 `core/session/engine.py`

初始化 SessionDB：

```python
from core.session.db import SessionDB


class SessionEngine:
    def __init__(..., session_id: str | None = None):
        working_dir = Path(getattr(tool_context, "working_dir", "."))

        self._state = SessionState(conversation_messages=[])
        self._state.session_db = SessionDB(
            db_path=working_dir / ".harness" / "state.db",
            sessions_dir=working_dir / ".harness" / "sessions",
        )
        self._state.session_db.ensure_session_row(self._state.session_id)
        ...
```

### 4.3 `core/query/loop.py`

增加统一持久化入口，并在 tool batch 后、最终文本返回前、异常退出前调用。

```python
def _persist_state(self, session_state, store, *, usage: dict[str, int] | None = None) -> None:
    db = getattr(session_state, "session_db", None)
    if db is None:
        return

    all_messages = store.snapshot()
    flushed = int(getattr(session_state, "_last_flushed_idx", 0))
    if len(all_messages) > flushed:
        new_messages = all_messages[flushed:]
        try:
            db.append_messages(session_state.session_id, new_messages)
            session_state._last_flushed_idx = len(all_messages)
        except Exception:
            pass

    if usage:
        try:
            db.update_token_counts(
                session_state.session_id,
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
            )
        except Exception:
            pass

    try:
        from core.session.serializer import SessionSerializer

        state_dict = SessionSerializer.serialize(session_state)
        db.save_session_snapshot(session_state.session_id, state_dict)
    except Exception:
        pass
```

tool batch 后调用：

```python
batch = tool_runtime.execute_batch(...)
store.extend(persisted_messages)
self._persist_state(session_state, store)
```

最终文本返回前调用：

```python
if model_resp.has_final_text:
    self._persist_state(
        session_state,
        store,
        usage={
            "input_tokens": getattr(model_resp, "prompt_tokens", 0),
            "output_tokens": getattr(model_resp, "completion_tokens", 0),
        },
    )
    return QueryResult(...)
```

异常退出前最佳努力调用：

```python
except RequestCancelledError:
    self._persist_state(session_state, store)
    return QueryResult(...)
```

---

## 5. Phase 3：`/resume`

### 5.1 `core/session/serializer.py`

序列化只保留真正的会话真相，不序列化 runtime handles。

```python
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.session.content_replacement import ContentReplacementState
from core.session.state import SessionState, TodoItem, TodoState
from core.skills.models import InvokedSkillRecord, SkillEvent
from core.tasks.models import TaskRecord, TaskState
from core.tools.context import FileState


class SessionSerializer:
    SKIP_FIELDS = {
        "prompt_cache",
        "memory_store",
        "session_db",
        "memory_provider",
        "skill_catalog",
        "skills_revision",
        "discovered_tools",
    }

    @classmethod
    def serialize(cls, state: SessionState) -> dict[str, Any]:
        data = asdict(state)
        for key in cls.SKIP_FIELDS:
            data.pop(key, None)
        return data

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> SessionState:
        state = SessionState(
            conversation_messages=list(data.get("conversation_messages", [])),
            session_id=str(data.get("session_id", "")) or None,
        )
        if not state.session_id:
            state.session_id = SessionState(conversation_messages=[]).session_id

        state.system_prompt_override = data.get("system_prompt_override")
        state.session_metadata = dict(data.get("session_metadata", {}))
        state.usage_totals = dict(data.get("usage_totals", {}))
        state.user_intents = list(data.get("user_intents", []))
        state.compact_state = dict(data.get("compact_state", state.compact_state))
        state.queries_since_skill_activation = int(data.get("queries_since_skill_activation", 0))
        state.last_known_skill_keys = set(data.get("last_known_skill_keys", []))
        state.skill_relevance_cooldown = dict(data.get("skill_relevance_cooldown", {}))
        state._last_flushed_idx = int(data.get("_last_flushed_idx", 0))

        todo_raw = data.get("todo_state", {})
        state.todo_state = TodoState(
            items=[TodoItem(**item) for item in todo_raw.get("items", [])],
            last_completed_items=[TodoItem(**item) for item in todo_raw.get("last_completed_items", [])],
            last_write_turn=todo_raw.get("last_write_turn"),
            last_reminder_turn=todo_raw.get("last_reminder_turn"),
        )

        task_raw = data.get("task_state", {})
        tasks_by_id = {
            task_id: TaskRecord(**task_dict)
            for task_id, task_dict in task_raw.get("tasks_by_id", {}).items()
        }
        state.task_state = TaskState(
            tasks_by_id=tasks_by_id,
            ordered_task_ids=list(task_raw.get("ordered_task_ids", [])),
            last_planned_turn=task_raw.get("last_planned_turn"),
            last_projection_turn=task_raw.get("last_projection_turn"),
            current_task_id=task_raw.get("current_task_id"),
        )

        state.invoked_skills = {
            skill_id: InvokedSkillRecord(**record)
            for skill_id, record in data.get("invoked_skills", {}).items()
        }
        state.skill_events = [SkillEvent(**event) for event in data.get("skill_events", [])]
        state.read_file_state = {
            path: FileState(**raw)
            for path, raw in data.get("read_file_state", {}).items()
        }

        replacement_raw = data.get("content_replacement_state", {})
        state.content_replacement_state = ContentReplacementState(
            seen_ids=set(replacement_raw.get("seen_ids", [])),
            replacements=dict(replacement_raw.get("replacements", {})),
        )
        return state
```

### 5.2 `core/session/commands.py`

扩展 `CommandResult`，增加 `/resume` 路由：

```python
@dataclass(slots=True)
class CommandResult:
    handled: bool
    output: str = ""
    resume_session_id: str | None = None


def is_resume_command(raw: str) -> bool:
    return raw.strip().startswith("/resume")


def execute_resume_command(raw: str, *, session_db) -> CommandResult:
    parts = raw.strip().split()
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "list"):
        if session_db is None:
            return CommandResult(True, "SessionDB not available.")
        sessions = session_db.list_sessions(limit=20)
        if not sessions:
            return CommandResult(True, "No previous sessions found.")
        lines = ["Recent sessions:"]
        for item in sessions:
            sid = item["id"]
            lines.append(f"- {sid} ({item['message_count']} messages)")
        lines.append("")
        lines.append("Use /resume <session_id> to restore a session.")
        return CommandResult(True, "\n".join(lines))

    if len(parts) == 2:
        return CommandResult(
            handled=True,
            output=f"Resuming session {parts[1]}...",
            resume_session_id=parts[1],
        )

    return CommandResult(True, "Usage: /resume | /resume list | /resume <session_id>")
```

### 5.3 `core/session/engine.py`

支持加载已有 session，并且注意：resume 后要重建整套依赖对象。

```python
import json

from core.memory.store import MemoryStore
from core.session.db import SessionDB
from core.session.serializer import SessionSerializer


class SessionEngine:
    def __init__(..., session_id: str | None = None):
        working_dir = Path(getattr(tool_context, "working_dir", "."))

        if session_id:
            self._state = self._load_session(session_id, working_dir)
        else:
            self._state = SessionState(conversation_messages=[])

        self._state.memory_store = MemoryStore(base_dir=working_dir)
        self._state.memory_store.load_from_disk()

        self._state.session_db = SessionDB(
            db_path=working_dir / ".harness" / "state.db",
            sessions_dir=working_dir / ".harness" / "sessions",
        )
        self._state.session_db.ensure_session_row(self._state.session_id)

        self._store = SessionStore(self._state, working_dir=working_dir)
        self._offloader = ToolResultOffloader(
            tool_result_dir=self._store.tool_result_dir,
            replacement_state=self._state.content_replacement_state,
            default_persist_threshold=TOOL_RESULT_PERSIST_THRESHOLD,
            bash_persist_threshold=BASH_RESULT_PERSIST_THRESHOLD,
            aggregate_budget=TOOL_RESULTS_AGGREGATE_BUDGET,
            preview_size=TOOL_RESULT_PREVIEW_BYTES,
        )
        ...

    def _load_session(self, session_id: str, working_dir: Path) -> SessionState:
        snapshot_path = working_dir / ".harness" / "sessions" / session_id / "state.json"
        if snapshot_path.is_file():
            raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
            state = SessionSerializer.deserialize(raw)
            state.session_id = session_id
        else:
            db = SessionDB(
                db_path=working_dir / ".harness" / "state.db",
                sessions_dir=working_dir / ".harness" / "sessions",
            )
            state = SessionState(
                conversation_messages=db.get_messages(session_id),
                session_id=session_id,
            )

        stale_paths = []
        for path in state.read_file_state:
            if not Path(path).exists():
                stale_paths.append(path)
        for path in stale_paths:
            state.read_file_state.pop(path, None)
        return state
```

### 5.4 `01_agent_loop.py`

把 engine 创建抽出来，并增加 `/resume` 分流。

```python
from core.session.commands import (
    execute_resume_command,
    is_resume_command,
)


def create_engine(*, session_id: str | None = None) -> SessionEngine:
    renderer = RichRenderer(console)
    tool_context = ToolUseContext(working_dir=".", max_turns=MAX_TURNS)
    model_gateway = ModelGateway(AnthropicClient())
    return SessionEngine(
        model_gateway=model_gateway,
        tool_runtime=ToolExecutorRuntime(registry, tool_context, renderer=renderer),
        tool_context=tool_context,
        policy_runner=PolicyRunner([
            TaskPlanningPolicy(),
            MaxTurnsPolicy(MAX_TURNS),
            TodoPlanningPolicy(),
            SkillRelevancePolicy(model_gateway=model_gateway),
            SkillUsageNudgePolicy(),
        ]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),
        renderer=renderer,
        session_id=session_id,
    )


def handle_input(raw: str, engine: SessionEngine) -> tuple[bool, str | None]:
    text = _strip_surrogate_codepoints(raw).strip()
    if not text:
        return True, None

    if is_skills_command(text):
        output = engine.handle_command(text)
        if output:
            console.print(output)
        return True, None

    if is_resume_command(text):
        result = execute_resume_command(text, session_db=engine.state.session_db)
        if result.output:
            console.print(result.output)
        return True, result.resume_session_id

    result = engine.submit_user_message(text)
    if result.final_output and not result.streaming_displayed:
        render_markdown(console, result.final_output)
    return True, None


def main() -> None:
    engine = create_engine()
    ...
    with RunAbortMonitor(sys.stdin, lambda: engine.request_cancel()):
        should_continue, resume_session_id = handle_input(query, engine)
        if resume_session_id:
            engine = create_engine(session_id=resume_session_id)
```

---

## 6. Phase 4：第三层 recalled memory

### 6.1 `core/memory/provider.py`

```python
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
```

### 6.2 `core/memory/local_provider.py`

V1 直接复用 SQLite FTS5：

```python
from __future__ import annotations

from pathlib import Path

from core.memory.provider import MemoryProvider
from core.session.db import SessionDB


class LocalMemoryProvider(MemoryProvider):
    def __init__(self, db_path: str | Path, sessions_dir: str | Path) -> None:
        self._db = SessionDB(db_path=db_path, sessions_dir=sessions_dir)
        self._session_id = ""
        self._prefetch_cache = ""

    @property
    def name(self) -> str:
        return "local-fts"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id

    def prefetch(self, query: str) -> str:
        return self._prefetch_cache

    def queue_prefetch(self, query: str) -> None:
        if not query.strip():
            self._prefetch_cache = ""
            return
        rows = self._db.search_messages(query, limit=5)
        if not rows:
            self._prefetch_cache = ""
            return
        self._prefetch_cache = "\n".join(
            f"[{row['role']}] {row['content'][:300]}"
            for row in rows
        )

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        # V1 直接复用第二层 transcript，无额外表
        return

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        # V1 可以先 no-op；后续如需单独表，再补镜像写入
        return
```

### 6.3 `core/prompt/assembler.py`

只走 overlay blocks，不碰 transcript。

```python
def build_memory_context_block(raw_context: str) -> str:
    if not raw_context or not raw_context.strip():
        return ""
    return (
        "<memory-context>\n"
        "[System note: recalled memory context, not new user input.]\n\n"
        f"{raw_context}\n"
        "</memory-context>"
    )


def build_query_overlay_blocks(
    self,
    state: SessionState,
    run_state: RunState,
) -> list[ContextBlock]:
    provider = getattr(state, "memory_provider", None)
    if provider is None:
        return []
    query = state.user_intents[-1] if state.user_intents else ""
    recalled = provider.prefetch(query)
    block = build_memory_context_block(recalled)
    if not block:
        return []
    return [
        ContextBlock(
            kind="memory_context",
            content=block,
            required=False,
            token_estimate=_estimate_block_tokens(block),
        )
    ]
```

### 6.4 `core/query/loop.py`

在最终完成态后做 provider 同步和下一轮预取：

```python
if model_resp.has_final_text:
    provider = getattr(session_state, "memory_provider", None)
    if provider is not None and user_message_content:
        try:
            provider.sync_turn(user_message_content, model_resp.content)
            provider.queue_prefetch(user_message_content)
        except Exception:
            pass

    self._persist_state(...)
    return QueryResult(...)
```

---

## 7. 必须补的测试

新增建议：

- `tests/session/test_memory_store.py`
- `tests/session/test_memory_tool.py`
- `tests/session/test_session_db.py`
- `tests/session/test_session_serializer.py`
- `tests/session/test_resume_commands.py`
- `tests/session/test_memory_provider.py`

建议最少补这些断言：

```python
def test_memory_tool_is_registered():
    names = {schema["name"] for schema in registry.schemas()}
    assert "memory" in names


def test_resume_command_returns_target_session_id():
    result = execute_resume_command("/resume abc123", session_db=None)
    assert result.resume_session_id == "abc123"


def test_overlay_memory_context_does_not_enter_transcript():
    state = SessionState(conversation_messages=[{"role": "user", "content": "hello"}])
    state.user_intents = ["hello"]
    state.memory_provider = FakeProvider("remembered fact")

    prepared = PreparedQueryContext(
        stable_system="system",
        stable_tools=None,
        runtime_blocks=[],
        working_transcript=state.conversation_messages,
        observability={},
        budget={},
    )
    blocks = PromptAssembler().build_query_overlay_blocks(state, RunState())

    assert blocks[0].kind == "memory_context"
    assert state.conversation_messages == [{"role": "user", "content": "hello"}]
```

---

## 8. 明确不做

本版不做：

- compact 后自动创建 child session
- `parent_session_id` lineage
- cross-session transcript merge
- 真正 embedding / vector DB
- 手写 TF-IDF 增量索引

这些能力都可以后续叠加，但不应阻塞第一版可用实现。

---

## 9. 交付标准

做到以下几点即视为 V2 impl spec 对齐成功：

1. `memory` 工具可写 `.harness/memories/*`
2. 新会话启动后记忆重新进入 stable prompt
3. session transcript 与状态快照会落盘到 SQLite + JSON
4. `/resume <session_id>` 能恢复 transcript、todo/task、skills、file runtime、content replacement
5. 第三层 recalled memory 仅出现在 overlay，不写入 transcript

如果后续开始编码，应以本文件中的代码骨架为第一参考，而不是旧版纯叙述 impl spec。
