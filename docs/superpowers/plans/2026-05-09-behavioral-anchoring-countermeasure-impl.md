# Behavioral Anchoring Countermeasure — Implementation Spec

> 日期: 2026-05-09
> 设计文档: [2026-05-09-behavioral-anchoring-countermeasure-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-09-behavioral-anchoring-countermeasure-design.md)
> 前置依赖: 无（独立于 Governance V1/V2，只依赖现有 PolicyRunner 基础设施）

---

## 实现顺序

按依赖关系排序，每步可独立测试：

1. SessionState 新增字段
2. SkillRelevancePolicy
3. SkillUsageNudgePolicy
4. Compact 后 skill_catalog_restore
5. Engine 注册新 Policy
6. 测试

---

## Step 1: SessionState 新增字段

**文件**: `core/session/state.py`
**行号**: 在 `SessionState` 类的 `user_intents` 字段之后（当前第 106 行之后）新增

**新增 3 个字段**:

```python
# core/session/state.py — SessionState 新增（在 user_intents 之后）

# ── Behavioral anchoring countermeasure state ──────────────────
# 跨 query 持久化（不能放 RunState，因为 RunState 每次 QueryLoop.run() 重建）
queries_since_skill_activation: int = 0
last_known_skill_keys: set[str] = field(default_factory=set)
skill_relevance_cooldown: dict[str, int] = field(default_factory=dict)
```

**为什么放 SessionState 而不是 RunState**:

- RunState 在 `core/query/loop.py:233` 每次用户输入都重新创建 (`state = RunState()`)
- 行为锚定需要跨数十次用户请求累积 stale 计数
- 如果放 RunState，"连续 6 次 query 没激活 skill" 的判断永远不会触发

**字段说明**:

| 字段 | 类型 | 语义 |
|---|---|---|
| `queries_since_skill_activation` | `int` | 距离上次新 skill 激活的 query 数。每次 `before_model_call` 时递增；当 `invoked_skills` 快照变化时重置为 0 |
| `last_known_skill_keys` | `set[str]` | 上次 `before_model_call` 时的 `invoked_skills` key 集合。用于快照对比检测 skill 使用 |
| `skill_relevance_cooldown` | `dict[str, int]` | `skill_id` → 上次注入 relevance 提醒时的 `queries_since_skill_activation` 值。用于冷却逻辑，避免同一 skill 连续多轮重复提醒 |

**验证方式**: 启动 agent loop，多次 query 后检查 `session_state.queries_since_skill_activation` 是否递增、不因 RunState 重建而归零。

---

## Step 2: SkillRelevancePolicy

**文件**: `core/policy/skill_relevance.py`（新建）

### 完整实现

```python
"""Skill 相关性策略 — 每轮检查当前任务是否匹配未激活的 skill，注入 transcript 尾部提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillRelevancePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)                        ← 注入到 conversation_messages 尾部

对抗行为锚定的核心机制：
    在 transcript 尾部（最新位置）注入与 transcript 模式相反的信号，
    提醒模型"有匹配的 skill 可以用"，打破 in-context learning bias。

注入通道：PolicyRunner → store.extend() → conversation_messages
不通过 build_query_overlay_blocks（那个拼进 system prompt，达不到尾部强化目标）。
"""
from __future__ import annotations

from typing import Any


class SkillRelevancePolicy:
    """每轮分析当前任务上下文，如果匹配某个 skill 的 when_to_use，
    注入 <system-reminder> 到 transcript 尾部。

    与 TodoPlanningPolicy 使用完全相同的注入通道：
    before_model_call() → [user message] → store.extend()。
    """

    RELEVANCE_BUDGET_CHARS = 1500
    COOLDOWN_QUERIES = 3
    CONTEXT_WINDOW_MESSAGES = 6  # 从最近 N 条消息中提取文本做匹配

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        invoked = session_state.invoked_skills
        cooldown = session_state.skill_relevance_cooldown
        current_stale = session_state.queries_since_skill_activation

        # 从最近消息中提取文本作为匹配上下文
        context_text = self._extract_recent_context(session_state.conversation_messages)
        if not context_text:
            return []

        matched: list[tuple[str, str]] = []  # (skill_id, description)

        for skill_id, meta in sorted(catalog.items()):
            if skill_id in invoked:
                continue  # 已激活的不需要提醒

            # 冷却检查
            last_reminded = cooldown.get(skill_id, -999)
            if current_stale - last_reminded < self.COOLDOWN_QUERIES:
                continue

            # 关键词匹配：when_to_use 或 description 中的关键词出现在上下文中
            if self._matches(context_text, meta):
                matched.append((skill_id, meta.description))

        if not matched:
            return []

        # 更新冷却
        for skill_id, _ in matched:
            cooldown[skill_id] = current_stale

        # 构造注入消息（budget 截断）
        lines: list[str] = []
        budget = self.RELEVANCE_BUDGET_CHARS
        for skill_id, desc in matched:
            line = f"- {skill_id}: {desc}"
            if len("\n".join(lines)) + len(line) > budget:
                break
            lines.append(line)

        content = (
            "<system-reminder type=\"skill_relevance\">\n"
            "以下 skill 与当前任务相关但尚未激活。"
            "如果匹配你的工作，建议先调用 skill 工具加载：\n\n"
            + "\n".join(lines)
            + "\n</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None

    # ── 内部方法 ──────────────────────────────────────────────

    def _extract_recent_context(self, messages: list[dict[str, Any]]) -> str:
        """从最近 N 条消息中提取纯文本作为匹配上下文。"""
        recent = messages[-self.CONTEXT_WINDOW_MESSAGES:] if messages else []
        parts: list[str] = []
        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and role in ("user", "assistant"):
                parts.append(content)
        return " ".join(parts).lower()

    def _matches(self, context_text: str, meta: Any) -> bool:
        """关键词匹配：when_to_use 或 description 中的关键片段出现在上下文中。

        匹配策略：
        1. 优先使用 when_to_use（如果存在），取其中的关键词
        2. 回退到 description
        3. 对中文文本做子串匹配，对英文做单词匹配
        """
        # when_to_use 中的逗号/分号分隔短语
        source = meta.when_to_use or meta.description or ""
        if not source:
            return False

        # 将 when_to_use 拆分为独立短语/关键词
        keywords = [kw.strip().lower() for kw in source.replace("，", ",").replace("；", ",").split(",") if kw.strip()]

        # 至少一个关键词出现在上下文中
        for kw in keywords:
            if len(kw) >= 2 and kw in context_text:
                return True
        return False
```

### 设计决策记录

| 决策 | 理由 |
|---|---|
| 注入为 `role: "user"` | 与 TodoPlanningPolicy 一致；进入 transcript 尾部而非 system prompt |
| 匹配源用 `when_to_use` 而非全量 skill body | when_to_use 是专门为此设计的短语列表；body 太长，匹配噪声大 |
| 冷却基于 `queries_since_skill_activation` 差值而非绝对 turn | 跨 query 持久化，不依赖 RunState.turn_count |
| `CONTEXT_WINDOW_MESSAGES = 6` | 最近 6 条消息通常覆盖当前工具调用和用户最近输入；太多会引入噪声 |

### 测试文件: `tests/test_skill_relevance_policy.py`

```python
from core.policy.skill_relevance import SkillRelevancePolicy
from core.session.state import SessionState
from core.skills.models import SkillMeta
from core.query.state import RunState
from pathlib import Path


def _make_meta(skill_id: str, description: str, when_to_use: str | None = None) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id,
        name=skill_id,
        description=description,
        when_to_use=when_to_use,
        skill_dir=Path("."),
        skill_file=Path("./SKILL.md"),
    )


def test_no_catalog_returns_empty():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []


def test_matching_skill_injects_reminder():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "基于csv文件生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告,HTML报告"),
    }
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "analysis-report" in messages[0]["content"]
    assert "skill_relevance" in messages[0]["content"]


def test_already_invoked_skill_is_skipped():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
    }
    state.invoked_skills["analysis-report"] = ...  # 任意非 None 值表示已激活
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert messages == []


def test_cooldown_prevents_repeated_reminder():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
    }
    run_state = RunState()

    # 第一次调用：注入
    messages1 = policy.before_model_call(state, run_state)
    assert len(messages1) == 1

    # 第二次调用（stale 没变）：冷却中，不注入
    messages2 = policy.before_model_call(state, run_state)
    assert messages2 == []


def test_no_matching_context_returns_empty():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "帮我写一个 hello world 程序"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告,数据报告"),
    }
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []
```

---

## Step 3: SkillUsageNudgePolicy

**文件**: `core/policy/skill_usage_nudge.py`（新建）

### 完整实现

```python
"""Skill 使用间隔提醒策略 — 长时间未激活 skill 时注入温和提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillUsageNudgePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)

类似 Claude Code 的 todo_reminder / task_reminder 机制。

skill 使用检测基于 session_state.invoked_skills 的快照对比，
覆盖两条激活路径：
  - 模型调用 skill 工具（经过 tool batch）
  - 用户执行 /skills use <id>（不经过 tool batch，直接写入 invoked_skills）
"""
from __future__ import annotations


class SkillUsageNudgePolicy:
    STALE_QUERIES = 6

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        current_keys = set(session_state.invoked_skills.keys())
        last_keys = session_state.last_known_skill_keys

        # 快照对比：如果 invoked_keys 增长了，说明有新 skill 被激活
        if current_keys != last_keys:
            session_state.queries_since_skill_activation = 0
            session_state.last_known_skill_keys = current_keys
        else:
            session_state.queries_since_skill_activation += 1

        if session_state.queries_since_skill_activation < self.STALE_QUERIES:
            return []

        # 统计未激活的 skill 数量
        uninvited = [sid for sid in catalog if sid not in current_keys]
        if not uninvited:
            return []

        content = (
            "<system-reminder type=\"skill_nudge\">\n"
            f"当前会话有 {len(catalog)} 个可用 skill，"
            f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
            "如果当前任务涉及数据分析、报告生成、调试、TDD 等场景，"
            "考虑先加载对应 skill。\n"
            "</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
```

### 关键设计决策

**为什么不检查 tool call 名称**:

```
方案 A（被否决）: if tool_name == "skill": reset counter
  问题：/skills use 命令通过 apply_skill_invocation() 直接写入
       session_state.invoked_skills，不经过 tool batch。
       用户刚手动激活过 skill，计数器不会重置 → 错误提醒

方案 B（采用）: 对比 invoked_skills 快照
  优势：覆盖 /skills use 和 tool call 两条路径。
       无论哪条路径激活 skill，invoked_skills 都会增长，
       快照对比自然检测到变化并重置计数器。
```

### 测试文件: `tests/test_skill_usage_nudge_policy.py`

```python
from core.policy.skill_usage_nudge import SkillUsageNudgePolicy
from core.session.state import SessionState
from core.skills.models import SkillMeta, InvokedSkillRecord
from core.query.state import RunState
from pathlib import Path


def _make_meta(skill_id: str) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id, name=skill_id, description=f"{skill_id} desc",
        when_to_use=None, skill_dir=Path("."), skill_file=Path("./SKILL.md"),
    )


def test_no_catalog_returns_empty():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []


def test_nudge_fires_after_stale_threshold():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    # 模拟 6 次 query 没有 skill 激活
    state.queries_since_skill_activation = 5  # before_model_call 会 +1 = 6
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert len(messages) == 1
    assert "skill_nudge" in messages[0]["content"]


def test_nudge_does_not_fire_below_threshold():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.queries_since_skill_activation = 3  # +1 = 4, < 6
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert messages == []


def test_skills_use_command_resets_counter():
    """模拟 /skills use 命令：直接写入 invoked_skills，不经过 tool batch。"""
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.queries_since_skill_activation = 5

    # 模拟 /skills use 命令直接激活 skill
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path="./SKILL.md",
        content_digest="abc",
        content="...",
        invoked_at_turn=0,
    )
    # last_known_skill_keys 仍是空集 = 快照对比会检测到变化

    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    # 检测到新 skill → 重置计数器 → 不触发 nudge
    assert messages == []
    assert state.queries_since_skill_activation == 0


def test_all_skills_invoked_no_nudge():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report", skill_path="./SKILL.md",
        content_digest="abc", content="...", invoked_at_turn=0,
    )
    state.last_known_skill_keys = {"analysis-report"}
    state.queries_since_skill_activation = 10
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    # 所有 skill 都已激活，uninvited 为空
    assert messages == []
```

---

## Step 4: Compact 后 skill_catalog_restore

**文件**: `core/session/compact_service.py`
**修改位置**: `build_runtime_restore_messages` 函数（当前第 80-123 行）
**在第 121 行（`restored.extend(collect_recent_read_restore_messages(...))` 之后）插入**

```python
# core/session/compact_service.py — build_runtime_restore_messages 新增段

    # 新增：skill_catalog_restore — 重建完整 skill 可用性感知
    # 只提醒未激活的 skills（已激活的通过 skills_restore 恢复）
    if state.skill_catalog:
        catalog_lines: list[str] = []
        for skill_id, meta in sorted(state.skill_catalog.items()):
            if skill_id not in state.invoked_skills:
                line = f"- {skill_id}: {meta.description}"
                if meta.when_to_use:
                    line += f"（适用：{meta.when_to_use}）"
                catalog_lines.append(line)
        if catalog_lines:
            restored.append({
                "role": "meta_runtime_restore",
                "kind": "skill_catalog_restore",
                "content": (
                    "以下 skill 可用但尚未激活。如果当前任务匹配，"
                    "建议先调用 skill 工具：\n"
                    + "\n".join(catalog_lines)
                ),
            })
```

**验证**: 在集成测试中触发 auto-compact，检查 post-compact 消息中是否包含 `skill_catalog_restore` 类型的消息。

---

## Step 5: Engine 注册新 Policy

**文件**: `01_agent_loop.py`
**修改位置**: 第 86 行

```python
# 修改前
policy_runner=PolicyRunner([MaxTurnsPolicy(MAX_TURNS), TodoPlanningPolicy()]),

# 修改后
from core.policy.skill_relevance import SkillRelevancePolicy
from core.policy.skill_usage_nudge import SkillUsageNudgePolicy

policy_runner=PolicyRunner([
    MaxTurnsPolicy(MAX_TURNS),
    TodoPlanningPolicy(),
    SkillRelevancePolicy(),
    SkillUsageNudgePolicy(),
]),
```

**文件**: `core/policy/__init__.py` — 无需修改（各 policy 独立 import）

---

## Step 6: 测试计划

### 单元测试

| 测试文件 | 覆盖 | 关键断言 |
|---|---|---|
| `tests/test_skill_relevance_policy.py` | 匹配/不匹配/冷却/已激活跳过/budget 截断 | `messages[0]["role"] == "user"`（transcript 通道） |
| `tests/test_skill_usage_nudge_policy.py` | stale 阈值/快照重置/`/skills use` 路径/all-invited | `state.queries_since_skill_activation` 跨 query 累积 |

### 集成测试

**文件**: `tests/test_behavioral_anchoring.py`（新建）

```python
"""集成测试 — 验证行为锚定对抗机制在完整 QueryLoop 流程中的工作。

关键验证点：
1. Policy 注入的消息进入 transcript（conversation_messages），不进入 system prompt
2. 长查询后 skill_relevance 仍然注入
3. /skills use 命令后 stale 计数器正确重置
"""


def test_policy_messages_enter_transcript_not_system():
    """验证 Policy 注入的消息进入 conversation_messages，不进入 system prompt。

    方法：
    1. 创建带 skill_catalog 的 SessionState
    2. 调用 SkillRelevancePolicy.before_model_call()
    3. 模拟 store.extend(messages)
    4. 检查 messages[0]["role"] == "user"（transcript 通道）
    5. 检查 PromptAssembler.build_stable_context() 不包含 skill_relevance 内容
    """
    ...


def test_long_session_skill_relevance_still_injects():
    """模拟长 session：50 次 query（300+ 工具调用），skill_relevance 仍然注入。

    方法：
    1. 创建 50 次 query 的 conversation_messages（大量 bash/read/write 调用）
    2. 最近一条用户消息包含"生成分析报告"
    3. skill_catalog 中有 analysis-report
    4. SkillRelevancePolicy 仍然匹配并注入
    """
    ...
```

### 手动验证清单

启动 `python 01_agent_loop.py`，执行以下场景：

| 场景 | 预期 | 验证方法 |
|---|---|---|
| 新 session，第一次输入"帮我分析 csv 数据" | `skill_relevance` 注入，模型调用 skill 工具 | 观察日志中的 `<system-reminder type="skill_relevance">` |
| 执行 5-6 次普通任务后，输入需要 skill 的任务 | `skill_nudge` 注入 + `skill_relevance` 注入 | 观察 `queries_since_skill_activation` 递增 |
| 执行 `/skills use analysis-report` 后立即输入任务 | 无 `skill_nudge`（计数器已重置） | 观察 `queries_since_skill_activation` 归零 |
| 触发 auto-compact 后继续任务 | post-compact 消息中包含 `skill_catalog_restore` | 检查 compact 后的消息 |

---

## 文件变更汇总

| 操作 | 文件 | 变更内容 |
|---|---|---|
| **修改** | `core/session/state.py` | SessionState 新增 3 个字段 |
| **新建** | `core/policy/skill_relevance.py` | SkillRelevancePolicy（~100 行） |
| **新建** | `core/policy/skill_usage_nudge.py` | SkillUsageNudgePolicy（~50 行） |
| **新建** | `tests/test_skill_relevance_policy.py` | 5 个单元测试 |
| **新建** | `tests/test_skill_usage_nudge_policy.py` | 5 个单元测试 |
| **新建** | `tests/test_behavioral_anchoring.py` | 2 个集成测试 |
| **修改** | `core/session/compact_service.py` | `build_runtime_restore_messages` 新增 `skill_catalog_restore` |
| **修改** | `01_agent_loop.py` | PolicyRunner 注册新增 2 个 Policy |

**不修改的文件**:

| 文件 | 原因 |
|---|---|
| `core/policy/base.py` | PolicyRunner / RunPolicy 协议不变 |
| `core/session/governor.py` | 水位计算不受影响；Policy 消息通过 transcript 自动参与水位 |
| `core/prompt/assembler.py` | `build_query_overlay_blocks` 保持空实现 |
| `core/query/loop.py` | 不需要修改；`store.extend()` 是已有的注入通道 |
| `core/query/state.py` | RunState 不变；状态放在 SessionState |

---

## 实现风险

| 风险 | 缓解 |
|---|---|
| 关键词匹配精度不足 | `when_to_use` 由 skill 作者控制，可精确设计短语；后续可升级为 embedding 匹配 |
| 提醒过于频繁导致模型"banner blindness" | `COOLDOWN_QUERIES = 3`（同一 skill 至少间隔 3 次 query）；`STALE_QUERIES = 6`（nudge 仅在长时间未用时触发） |
| microcompact 误删 `<system-reminder>` 消息 | microcompact 只处理 `role: "tool"` 的消息（`microcompact.py` 的白名单），不影响 `role: "user"` 的 policy 注入消息 |
| policy 注入消息累积导致上下文膨胀 | 每次注入 ~375-500 tokens；auto-compact 会正常处理这些消息；且 governor 的水位计算自动包含它们 |
