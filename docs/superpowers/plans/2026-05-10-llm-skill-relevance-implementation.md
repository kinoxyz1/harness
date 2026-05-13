# LLM Skill Relevance Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `SkillRelevancePolicy._matches()` keyword matching with a configurable LLM classification call. When `SKILL_LLM_MATCH=true`, use `ModelGateway.call_once()` to classify skill relevance via a fast model. When `false` (default), keep existing keyword matching unchanged.

**Architecture:** Strategy pattern — `SkillRelevancePolicy` reads config at init time, dispatches to `_match_by_keywords()` (existing) or `_classify_via_llm()` (new). No behavioral change when feature is off.

**Design Spec:** `docs/superpowers/specs/2026-05-10-llm-skill-relevance-design.md`

**Tech Stack:** Python 3.12, pytest

---

## File Structure

### Modified Files

- `core/shared/config.py`
  Responsibility: Add `SKILL_LLM_MATCH` environment variable config.

- `core/policy/skill_relevance.py`
  Responsibility: Add LLM classification path alongside existing keyword matching. Add `__init__()` with `model_gateway` param. Rename `_matches()` to `_match_by_keywords()`. Add `_classify_via_llm()`, `_format_skill_summary()`, `_parse_classification()`.

- `01_agent_loop.py`
  Responsibility: Pass `model_gateway` to `SkillRelevancePolicy()` constructor.

- `tests/test_skill_relevance_policy.py`
  Responsibility: Add tests for LLM classification path and config-based dispatch.

---

### Task 1: Add SKILL_LLM_MATCH Config

**Files:**
- Modify: `core/shared/config.py`

- [ ] **Step 1: Add config entry**

In `core/shared/config.py`, add after the existing runtime config section (after line 57):

```python
# ─── Skill 匹配配置 ────────────────────────────────────────────────────────

# 是否使用 LLM 进行 skill 相关性匹配（增加一次轻量 API 调用，提升匹配准确率）
# false（默认）: 使用关键词精确子串匹配
# true: 使用 LLM 分类调用，支持语义匹配
SKILL_LLM_MATCH: bool = os.environ.get("SKILL_LLM_MATCH", "false").lower() in ("true", "1", "yes")
```

- [ ] **Step 2: Verify import works**

Run: `python3 -c "from core.shared.config import SKILL_LLM_MATCH; print(SKILL_LLM_MATCH)"`

Expected: `False` (default value).

- [ ] **Step 3: Commit**

```bash
git add core/shared/config.py
git commit -m "feat: add SKILL_LLM_MATCH config option"
```

---

### Task 2: Restructure SkillRelevancePolicy

**Files:**
- Modify: `core/policy/skill_relevance.py`
- Modify: `tests/test_skill_relevance_policy.py`

This is the core task. The file needs significant restructuring while keeping all existing behavior intact when `SKILL_LLM_MATCH=false`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skill_relevance_policy.py`:

```python
import os
from unittest.mock import MagicMock, patch
from core.llm.client import ModelGateway, ModelResponse


def test_llm_match_disabled_uses_keywords():
    """SKILL_LLM_MATCH=false (default) → keyword matching, no LLM call."""
    gateway = MagicMock(spec=ModelGateway)
    policy = SkillRelevancePolicy(model_gateway=gateway)
    assert policy._use_llm is False
    # Verify keyword matching still works
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "基于csv文件生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告,HTML报告"),
    }
    messages = policy.before_model_call(state, RunState())
    assert len(messages) == 1
    # Gateway should NOT have been called
    gateway.call_once.assert_not_called()


def test_llm_match_enabled_dispatches_to_llm():
    """SKILL_LLM_MATCH=true → LLM classification call."""
    mock_resp = ModelResponse(
        content="analysis-report",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=100,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch.dict(os.environ, {"SKILL_LLM_MATCH": "true"}):
        # Reimport to pick up env var
        from core.shared.config import SKILL_LLM_MATCH
        with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
            policy = SkillRelevancePolicy(model_gateway=gateway)
            assert policy._use_llm is True

            state = SessionState(conversation_messages=[
                {"role": "user", "content": "帮我看看这个项目的数据"},
            ])
            state.skill_catalog = {
                "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告"),
            }
            messages = policy.before_model_call(state, RunState())
            assert len(messages) == 1
            assert "analysis-report" in messages[0]["content"]
            gateway.call_once.assert_called_once()


def test_llm_match_returns_none():
    """LLM returns NONE → no nudge injected."""
    mock_resp = ModelResponse(
        content="NONE",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=100,
        completion_tokens=2,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "今天天气怎么样"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
        }
        messages = policy.before_model_call(state, RunState())
        assert messages == []
        gateway.call_once.assert_called_once()


def test_llm_match_failure_silently_degrades():
    """LLM call raises → silent fallback, no nudge, no crash."""
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.side_effect = RuntimeError("API error")

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我分析这个项目"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
        }
        # Should NOT raise, just return empty
        messages = policy.before_model_call(state, RunState())
        assert messages == []


def test_llm_match_semantic_match_succeeds():
    """User says '看看...项目' → LLM matches code-explorer even though keywords wouldn't."""
    mock_resp = ModelResponse(
        content="code-explorer",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=150,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我看看最近很火的gstack项目是干什么的"},
        ])
        state.skill_catalog = {
            "code-explorer": _make_meta(
                "code-explorer",
                "Explore codebase",
                "探索项目, 理解代码库, 分析仓库, 看看这个项目",
            ),
        }
        messages = policy.before_model_call(state, RunState())
        assert len(messages) == 1
        assert "code-explorer" in messages[0]["content"]


def test_no_gateway_disables_llm():
    """model_gateway=None → always uses keywords regardless of config."""
    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=None)
        assert policy._use_llm is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_skill_relevance_policy.py -v`

Expected: FAIL — `SkillRelevancePolicy.__init__()` doesn't accept `model_gateway`, `_use_llm` doesn't exist, `SKILL_LLM_MATCH` not imported.

- [ ] **Step 3: Write the implementation**

Replace the entire `core/policy/skill_relevance.py` with:

```python
"""Skill 相关性策略 — 每轮检查当前任务是否匹配未激活的 skill，注入 transcript 尾部提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillRelevancePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)                        ← 注入到 conversation_messages 尾部

匹配策略（通过 SKILL_LLM_MATCH 配置切换）：
    false（默认）: 精确子串关键词匹配（_match_by_keywords）
    true:          LLM 分类调用（_classify_via_llm），支持语义匹配

注入通道：PolicyRunner → store.extend() → conversation_messages
不通过 build_query_overlay_blocks（那个拼进 system prompt，达不到尾部强化目标）。
"""
from __future__ import annotations

import logging
from typing import Any

from core.llm.client import ModelGateway, ModelRequestOptions
from core.shared.config import SKILL_LLM_MATCH

logger = logging.getLogger(__name__)

_CLASSIFICATION_SYSTEM_PROMPT = (
    "你是 skill 匹配器。根据用户意图，从可用 skill 列表中选出匹配的。\n"
    "只返回匹配的 skill ID，逗号分隔。无匹配则返回 NONE。\n"
    "不要解释，不要输出其他内容。"
)


class SkillRelevancePolicy:
    """每轮分析当前任务上下文，如果匹配某个 skill 的 when_to_use，
    注入 <system-reminder> 到 transcript 尾部。

    匹配策略通过 SKILL_LLM_MATCH 环境变量配置：
    - false（默认）: 精确子串关键词匹配
    - true: LLM 分类调用，支持语义匹配

    与 TodoPlanningPolicy 使用完全相同的注入通道：
    before_model_call() → [user message] → store.extend()。
    """

    RELEVANCE_BUDGET_CHARS = 1500
    COOLDOWN_QUERIES = 3
    CONTEXT_WINDOW_MESSAGES = 6

    def __init__(self, model_gateway: ModelGateway | None = None):
        self._model_gateway = model_gateway
        self._use_llm = SKILL_LLM_MATCH and model_gateway is not None

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        invoked = session_state.invoked_skills
        cooldown = session_state.skill_relevance_cooldown
        current_stale = session_state.queries_since_skill_activation

        context_text = self._extract_recent_context(session_state.conversation_messages)
        if not context_text:
            return []

        # 收集未冷却、未激活的候选 skill
        candidates: dict[str, Any] = {}
        for skill_id, meta in sorted(catalog.items()):
            if skill_id in invoked:
                continue
            last_reminded = cooldown.get(skill_id, -999)
            if current_stale - last_reminded < self.COOLDOWN_QUERIES:
                continue
            candidates[skill_id] = meta

        if not candidates:
            return []

        # 根据配置选择匹配策略
        matched_ids = self._match(context_text, candidates)

        if not matched_ids:
            return []

        # 从 catalog 中获取匹配 skill 的描述
        matched: list[tuple[str, str]] = []
        for skill_id in matched_ids:
            if skill_id in catalog:
                matched.append((skill_id, catalog[skill_id].description))

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
            "以下 skill 与当前任务高度匹配但尚未激活。\n"
            "阻塞要求：必须先调用 skill 工具激活匹配的 skill，再继续处理任务。\n\n"
            + "\n".join(lines)
            + "\n</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None

    # ── 匹配分派 ──────────────────────────────────────────────

    def _match(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """根据配置分派到关键词匹配或 LLM 分类。"""
        if self._use_llm:
            return self._classify_via_llm(context_text, candidates)
        return self._match_by_keywords(context_text, candidates)

    # ── 关键词匹配（默认，现有逻辑）──────────────────────────

    def _match_by_keywords(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """精确子串关键词匹配。保留原有行为。"""
        matched: list[str] = []
        for skill_id, meta in candidates.items():
            source = meta.when_to_use or meta.description or ""
            if not source:
                continue
            keywords = [
                kw.strip().lower()
                for kw in source.replace("，", ",").replace("；", ",").split(",")
                if kw.strip()
            ]
            for kw in keywords:
                if len(kw) >= 2 and kw in context_text:
                    matched.append(skill_id)
                    break
        return matched

    # ── LLM 分类匹配（SKILL_LLM_MATCH=true 时启用）────────────

    def _classify_via_llm(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """通过轻量 LLM 调用判断 skill 相关性。"""
        if self._model_gateway is None:
            return []

        skill_summary = self._format_skill_summary(candidates)
        user_prompt = (
            f"可用 skill:\n{skill_summary}\n\n"
            f"用户最近输入:\n{context_text}"
        )

        try:
            resp = self._model_gateway.call_once(
                messages=[{"role": "user", "content": user_prompt}],
                system=_CLASSIFICATION_SYSTEM_PROMPT,
                tools=None,
                request_options=ModelRequestOptions(
                    thinking_mode="disabled",
                    max_output_tokens=50,
                ),
            )
        except Exception:
            logger.debug("Skill relevance LLM call failed, skipping", exc_info=True)
            return []

        return self._parse_classification(resp.content, set(candidates.keys()))

    def _format_skill_summary(self, candidates: dict[str, Any]) -> str:
        """将 skill 元信息格式化为 prompt 片段。"""
        lines = []
        for skill_id, meta in sorted(candidates.items()):
            triggers = meta.when_to_use or ""
            desc = (meta.description or "")[:100]
            lines.append(f"- {skill_id} | {triggers} | {desc}")
        return "\n".join(lines)

    def _parse_classification(self, response_text: str, valid_ids: set[str]) -> list[str]:
        """解析 LLM 返回的 skill ID 列表，过滤掉无效 ID。"""
        text = response_text.strip()
        if not text or text.upper() == "NONE":
            return []
        return [sid.strip() for sid in text.split(",") if sid.strip() in valid_ids]

    # ── 共用方法 ──────────────────────────────────────────────

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_skill_relevance_policy.py -v`

Expected: PASS with all existing + new tests green.

- [ ] **Step 5: Commit**

```bash
git add core/policy/skill_relevance.py
git commit -m "feat: add LLM-based skill relevance matching (configurable)"
```

---

### Task 3: Update Assembly Point

**Files:**
- Modify: `01_agent_loop.py`

- [ ] **Step 1: Update SkillRelevancePolicy construction**

In `01_agent_loop.py`, line 268, change:

```python
# BEFORE
SkillRelevancePolicy(),

# AFTER
SkillRelevancePolicy(model_gateway=model_gateway),
```

Note: `model_gateway` is already defined on line 262, so no additional variable needed.

- [ ] **Step 2: Verify import**

Run: `python3 -c "from importlib import reload; import importlib; exec(open('01_agent_loop.py').read().split('engine = SessionEngine')[0]); print('OK')" 2>&1 || python3 -c "print('Syntax OK')" `

Or simply verify the file parses:

Run: `python3 -c "import ast; ast.parse(open('01_agent_loop.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add 01_agent_loop.py
git commit -m "feat: pass model_gateway to SkillRelevancePolicy"
```

---

### Task 4: Run Full Regression

**Files:**
- No modifications — verification only.

- [ ] **Step 1: Run all skill-related tests**

Run: `pytest tests/test_skill_relevance_policy.py tests/test_skill_usage_nudge_policy.py tests/test_behavioral_anchoring.py -v`

Expected: PASS with all tests green.

- [ ] **Step 2: Run broader test suite**

Run: `pytest tests/ -v`

Expected: Same results as before Task 1 (292 passed, 3 pre-existing failures unrelated to this change).

- [ ] **Step 3: Verify default behavior unchanged**

Run: `SKILL_LLM_MATCH=false python3 -c "from core.shared.config import SKILL_LLM_MATCH; print(f'SKILL_LLM_MATCH={SKILL_LLM_MATCH}')"`

Expected: `SKILL_LLM_MATCH=False`

- [ ] **Step 4: Verify config can be enabled**

Run: `SKILL_LLM_MATCH=true python3 -c "from core.shared.config import SKILL_LLM_MATCH; print(f'SKILL_LLM_MATCH={SKILL_LLM_MATCH}')"`

Expected: `SKILL_LLM_MATCH=True`

---

### Task 5: Behavioral Validation (Manual)

- [ ] **Step 1: Test with SKILL_LLM_MATCH=false (default)**

Launch: `python 01_agent_loop.py`

Enter: `帮我看看最近很火的gstack项目是干什么的`

Expected: Keyword matching. `code-explorer` will NOT match (known limitation). This confirms default behavior is unchanged.

- [ ] **Step 2: Test with SKILL_LLM_MATCH=true**

Set `SKILL_LLM_MATCH=true` in `.env`, then launch: `python 01_agent_loop.py`

Enter: `帮我看看最近很火的gstack项目是干什么的`

Expected: LLM classification matches `code-explorer` despite keyword gap. Nudge injected. Model activates skill via BLOCKING REQUIREMENT.

---

## Self-Review

### Spec Coverage

| Design Spec Section | Implementation Task |
|---|---|
| §4.0 可配置开关 | Task 1 (config) + Task 2 (dispatch) |
| §4.1 架构 | Task 2 (restructure) |
| §4.2 分类 Prompt 设计 | Task 2 (`_CLASSIFICATION_SYSTEM_PROMPT`) |
| §4.3 调用参数 | Task 2 (`ModelRequestOptions`) |
| §4.4 Skill 元信息格式化 | Task 2 (`_format_skill_summary`) |
| §4.5 输出解析 | Task 2 (`_parse_classification`) |
| §4.6 改动范围 | All tasks |
| §4.7 冷却和性能优化 | Task 2 (candidates filtering before LLM call) |

### Placeholder Scan

- No `TODO` / `TBD` implementation steps remain.
- Every code-edit step contains complete implementation code.
- Every verification step names an exact command and expected result.

### Backward Compatibility

- Default behavior (`SKILL_LLM_MATCH=false`): identical to current keyword matching. No behavioral change.
- All existing tests continue to pass without modification (they don't set `SKILL_LLM_MATCH`).
- `SkillRelevancePolicy()` constructor without arguments still works (`model_gateway` defaults to `None`).
- `01_agent_loop.py` change is backward compatible: passing `model_gateway` when config is off simply means the gateway is stored but not used.

### Risk Assessment

| Change | Risk | Mitigation |
|---|---|---|
| Config default is `false` | Zero — no behavioral change | Existing tests prove this |
| LLM call in policy | Latency (~200-500ms per turn) | Only fires when config enabled; cooldown mechanism reduces frequency |
| LLM call failure | Silent degradation | `try/except` returns empty list, no crash |
| `_parse_classification` filters invalid IDs | False negative if LLM returns slightly wrong ID | Valid ID check against `candidates.keys()` ensures only known skills are matched |
