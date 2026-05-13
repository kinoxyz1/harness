# Skill Activation Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade all skill-activation-related instructions from suggestive language to BLOCKING REQUIREMENT strength, so that when `SkillRelevancePolicy` detects a match, the model reliably calls the skill tool before generating any other response.

**Architecture:** Four text-only edits across four files, plus test assertions update. No data structure, API, or protocol changes. The edits target the three locations where the model reads skill-activation instructions: Tool Schema description (strongest, per-turn visible), system prompt framework layer, and policy nudge messages.

**Design Spec:** `docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md`

**Tech Stack:** Python 3.12, pytest

---

## File Structure

### Modified Files

- `core/tools/builtin/skill.py`
  Responsibility: Replace functional-only SCHEMA.description with BLOCKING REQUIREMENT instruction.

- `core/prompt/system_context.py`
  Responsibility: Upgrade `_FRAMEWORK_PROMPT` Skills section from "应先调用" to mandatory language.

- `core/policy/skill_relevance.py`
  Responsibility: Upgrade nudge message from "建议先调用" to "阻塞要求：必须先调用".

- `core/policy/skill_usage_nudge.py`
  Responsibility: Upgrade staleness nudge from "考虑先加载" to "必须在继续之前先激活它".

- `tests/test_skill_relevance_policy.py`
  Responsibility: Update assertions that check for the old nudge text.

- `tests/test_skill_usage_nudge_policy.py`
  Responsibility: Update assertions that check for the old nudge text.

---

### Task 1: Strengthen Skill Tool Description (BLOCKING REQUIREMENT)

**Files:**
- Modify: `core/tools/builtin/skill.py`
- Test: existing tool tests (no new test file needed — description is a constant, verified by inspection)

- [ ] **Step 1: Edit SCHEMA.description**

In `core/tools/builtin/skill.py`, replace lines 20-24:

```python
# BEFORE
"description": (
    "Load a local skill into runtime state. The skill guidance will be available "
    "on the next model turn, so use this when you need additional workflow "
    "instructions before subsequent reasoning or tool use."
),

# AFTER
"description": (
    "当用户任务与 <available-skills> 中某个 skill 的描述或触发场景匹配时，"
    "这是阻塞要求(BLOCKING REQUIREMENT)：必须先调用此工具激活该 skill，"
    "再生成任何其他响应或工具调用。\n"
    "绝不能只提到 skill 存在但不实际调用此工具。\n"
    "如果当前对话轮次中已看到 <command-name> 标签，说明 skill 已加载，"
    "直接遵循其指令即可，不要重复调用。"
),
```

- [ ] **Step 2: Verify no existing tests assert on old description text**

Run: `grep -r "Load a local skill" tests/`

Expected: No matches. If matches found, update those assertions.

- [ ] **Step 3: Run existing tests to confirm no breakage**

Run: `pytest tests/test_skill_relevance_policy.py tests/test_skill_usage_nudge_policy.py -v`

Expected: PASS (this file's tests don't inspect SCHEMA.description).

- [ ] **Step 4: Commit**

```bash
git add core/tools/builtin/skill.py
git commit -m "feat: add BLOCKING REQUIREMENT to skill tool description"
```

---

### Task 2: Strengthen Framework Prompt Skills Section

**Files:**
- Modify: `core/prompt/system_context.py`

- [ ] **Step 1: Edit `_FRAMEWORK_PROMPT`**

In `core/prompt/system_context.py`, replace lines 33-36:

```python
# BEFORE
## Skills

系统提示词中包含 <available-skills> 目录。
如果任务匹配某个 skill，应先调用 skill 工具立即加载它，再基于已展开的 skill 重新评估下一步。

# AFTER
## Skills

系统提示词中包含 <available-skills> 目录，列出所有可用的 skill。
当任务匹配某个 skill 的描述或触发场景时，这是阻塞要求(BLOCKING REQUIREMENT)：
必须先调用 skill 工具激活它，再基于已展开的 skill 重新评估下一步。
即使只有很小的可能性匹配，也必须先激活再行动——这不是可选的。
```

- [ ] **Step 2: Verify no existing tests assert on old prompt text**

Run: `grep -r "应先调用" tests/`

Expected: No matches.

- [ ] **Step 3: Run existing tests**

Run: `pytest tests/ -k "system_context or prompt" -v`

Expected: PASS. If any test asserts on the old `_FRAMEWORK_PROMPT` text, update it.

- [ ] **Step 4: Commit**

```bash
git add core/prompt/system_context.py
git commit -m "feat: upgrade skill activation instruction to BLOCKING REQUIREMENT"
```

---

### Task 3: Strengthen SkillRelevancePolicy Nudge Language

**Files:**
- Modify: `core/policy/skill_relevance.py`
- Modify: `tests/test_skill_relevance_policy.py`

- [ ] **Step 1: Edit nudge message**

In `core/policy/skill_relevance.py`, replace lines 79-84:

```python
# BEFORE
content = (
    "<system-reminder type=\"skill_relevance\">\n"
    "以下 skill 与当前任务相关但尚未激活。如果匹配你的工作，建议先调用 skill 工具加载：\n\n"
    + "\n".join(lines)
    + "\n</system-reminder>"
)

# AFTER
content = (
    "<system-reminder type=\"skill_relevance\">\n"
    "以下 skill 与当前任务高度匹配但尚未激活。\n"
    "阻塞要求：必须先调用 skill 工具激活匹配的 skill，再继续处理任务。\n\n"
    + "\n".join(lines)
    + "\n</system-reminder>"
)
```

- [ ] **Step 2: Update test assertions**

In `tests/test_skill_relevance_policy.py`, the test `test_matching_skill_injects_reminder()` at line 39 asserts:

```python
assert "skill_relevance" in messages[0]["content"]
```

This assertion still passes (the `<system-reminder type="skill_relevance">` tag is unchanged). No test asserts on the old nudge text "建议先调用" or "如果匹配你的工作". Verify:

Run: `grep -n "建议先调用\|如果匹配你的工作" tests/test_skill_relevance_policy.py`

Expected: No matches. If found, update those assertions.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_skill_relevance_policy.py -v`

Expected: PASS with all 5 tests.

- [ ] **Step 4: Commit**

```bash
git add core/policy/skill_relevance.py
git commit -m "feat: upgrade skill relevance nudge to blocking requirement"
```

---

### Task 4: Strengthen SkillUsageNudgePolicy Reminder Language

**Files:**
- Modify: `core/policy/skill_usage_nudge.py`
- Modify: `tests/test_skill_usage_nudge_policy.py`

- [ ] **Step 1: Edit nudge message**

In `core/policy/skill_usage_nudge.py`, replace lines 45-52:

```python
# BEFORE
content = (
    "<system-reminder type=\"skill_nudge\">\n"
    f"当前会话有 {len(catalog)} 个可用 skill，"
    f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
    "如果当前任务涉及数据分析、报告生成、调试、TDD 等场景，"
    "考虑先加载对应 skill。\n"
    "</system-reminder>"
)

# AFTER
content = (
    "<system-reminder type=\"skill_nudge\">\n"
    f"当前会话有 {len(catalog)} 个可用 skill，"
    f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
    "请检查 <available-skills> 中是否有匹配当前任务的 skill。"
    "如果有，必须在继续之前先激活它。\n"
    "</system-reminder>"
)
```

- [ ] **Step 2: Update test assertions**

In `tests/test_skill_usage_nudge_policy.py`, the test `test_nudge_fires_after_stale_threshold()` at line 31 asserts:

```python
assert "skill_nudge" in messages[0]["content"]
```

This still passes (the `<system-reminder type="skill_nudge">` tag is unchanged). No test asserts on the old text "考虑先加载" or "数据分析、报告生成". Verify:

Run: `grep -n "考虑先加载\|数据分析" tests/test_skill_usage_nudge_policy.py`

Expected: No matches. If found, update those assertions.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_skill_usage_nudge_policy.py -v`

Expected: PASS with all 5 tests.

- [ ] **Step 4: Commit**

```bash
git add core/policy/skill_usage_nudge.py
git commit -m "feat: upgrade skill usage nudge to mandatory language"
```

---

### Task 5: Run Full Regression Suite

**Files:**
- No new modifications — verification only.

- [ ] **Step 1: Run all skill-related tests**

Run: `pytest tests/test_skill_relevance_policy.py tests/test_skill_usage_nudge_policy.py tests/test_behavioral_anchoring.py -v`

Expected: PASS with all tests green.

- [ ] **Step 2: Run broader test suite**

Run: `pytest tests/ -v --timeout=60`

Expected: PASS. If any failures, they should only be from tests asserting on old prompt text (unlikely based on grep analysis).

- [ ] **Step 3: Fix any remaining assertion drift**

If any tests fail due to text assertions on old nudge/prompt content, update those specific assertions to match the new text.

- [ ] **Step 4: Final commit (if fixes were needed)**

```bash
git add tests/
git commit -m "test: update assertions for skill activation enforcement"
```

---

### Task 6: Behavioral Validation

**Files:**
- No code changes — manual verification.

- [ ] **Step 1: Launch harness REPL and reproduce the original scenario**

```
>> 帮我看看 gstack 项目是干什么的
```

Expected behavior:
1. `SkillRelevancePolicy` detects match → injects `<system-reminder type="skill_relevance">` with "阻塞要求" language
2. Model reads Tool Schema → sees BLOCKING REQUIREMENT in skill tool description
3. Model calls `skill({"skill": "code-explorer"})` BEFORE generating any other response
4. Skill content appears in next turn's system prompt via `<active-skills>`
5. Model follows the 4-phase exploration pipeline from code-explorer

- [ ] **Step 2: Test edge case — daily chat (no skill match)**

```
>> 你好，今天天气怎么样
```

Expected: Normal response, no skill activation, no nudge.

- [ ] **Step 3: Test edge case — user explicitly says not to use skill**

```
>> 不用 skill，直接告诉我 gstack 是什么
```

Expected: Model respects user instruction and responds directly without skill activation (user instructions override BLOCKING REQUIREMENT per system design).

---

## Self-Review

### Spec Coverage

| Design Spec Section | Implementation Task |
|---|---|
| §5.1 改动 A: Tool description BLOCKING REQUIREMENT | Task 1 |
| §5.2 改动 B: 框架提示词强化 | Task 2 |
| §5.3 改动 C: SkillRelevancePolicy nudge 强化 | Task 3 |
| §5.4 改动 D: SkillUsageNudgePolicy nudge 强化 | Task 4 |
| §8 验证方案: 回归测试 | Task 5 |
| §8 验证方案: 行为验证 + 边界场景 | Task 6 |

### Placeholder Scan

- No `TODO` / `TBD` implementation steps remain.
- Every code-edit step contains exact before/after code with line numbers.
- Every verification step names an exact `pytest` command and expected result.

### Risk Assessment

| Change | Risk | Mitigation |
|---|---|---|
| Task 1: Tool description | Model may over-activate irrelevant skills | `SkillRelevancePolicy._matches()` provides keyword-level filtering; model retains judgment |
| Task 2: Framework prompt | Token increase (~50 chars) | Negligible |
| Task 3: Relevance nudge | Nudge text slightly longer | Budget check at 1500 chars unchanged |
| Task 4: Usage nudge | Removes specific skill category hints ("数据分析、报告生成") | Categories were outdated anyway; model should check `<available-skills>` directly |
| Task 6: Behavioral | Model may still ignore (edge case) | Escalate to auto-activation mechanism (Design Spec §10 Open Question 1) if needed |

### Backward Compatibility

- All changes are text-only; no data structure, API, or protocol modifications.
- `SkillRelevancePolicy` cooldown logic (`COOLDOWN_QUERIES = 3`) unchanged.
- `SkillUsageNudgePolicy` staleness threshold (`STALE_QUERIES = 6`) unchanged.
- Skill tool input/output schema (`name`, `input_schema`) unchanged.
- `invoked_skills` write path and `PromptAssembler.build_active_skill_messages()` read path unchanged.
