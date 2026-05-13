# LLM Skill Relevance Matching 设计

> 日期：2026-05-10
> 状态：待评审
> 前序文档：
> - [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md)（指令强度修复，已实施 BLOCKING REQUIREMENT）
> - [2026-05-09-behavioral-anchoring-countermeasure-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-09-behavioral-anchoring-countermeasure-design.md)（行为锚定对策，首次引入 SkillRelevancePolicy）
> 当前相关实现：
> - [core/policy/skill_relevance.py](/Users/kino/works/kino/harness/core/policy/skill_relevance.py) — `SkillRelevancePolicy` 关键词匹配 + nudge 注入
> - [core/policy/base.py](/Users/kino/works/kino/harness/core/policy/base.py) — `PolicyRunner` / `RunPolicy` 协议
> - [core/llm/client.py](/Users/kino/works/kino/harness/core/llm/client.py) — `ModelGateway.call_once()` 模型调用接口
> - [core/skills/models.py](/Users/kino/works/kino/harness/core/skills/models.py) — `SkillMeta` 数据结构
> - [01_agent_loop.py](/Users/kino/works/kino/harness/01_agent_loop.py) — Policy 和 ModelGateway 的组装点（第 262-269 行）
> Claude Code 参考：
> - `src/tools/SkillTool/prompt.ts` — Skill listing 注入 + BLOCKING REQUIREMENT
> - `src/utils/attachments.ts` — `getSkillListingAttachments()` 每轮注入
> - **关键差异**：Claude Code 无服务端匹配，完全依赖模型判断（详见 §3）

---

## 1. 摘要

本文档解决 `SkillRelevancePolicy` 的**匹配准确性问题**：当用户输入的语义匹配某个 skill，但用词不完全一致时，现有的精确子串匹配算法无法检测到匹配，导致 skill 永远不会被推荐给模型。

### 解决方案

将 `_matches()` 从精确子串关键词匹配替换为**轻量 LLM 分类调用**：用 fast model（如 Claude Haiku）在 `before_model_call()` 中做一次极小输入/输出的分类判断，利用模型的语义理解能力替代手写匹配代码。

---

## 2. 问题定位

### 2.1 问题现象

用户在 harness REPL 中输入：

```
帮我看看最近很火的gstack项目是干什么的
```

系统中有 `code-explorer` skill，其 `when_to_use` 包含触发短语 `"看看这个项目"`。

直觉上这应该匹配——用户说"看看...项目"，skill 说"看看这个项目"。但模型既没有加载 `code-explorer` skill，`SkillRelevancePolicy` 也没有注入任何提醒。

### 2.2 根因分析

断链发生在 `SkillRelevancePolicy._matches()` 方法（`core/policy/skill_relevance.py:106-127`）。

当前匹配逻辑是**精确子串匹配**：将 `when_to_use` 按逗号拆分为候选短语，检查每个短语是否**完整、连续**地出现在用户输入中。

```
候选短语: "看看这个项目"     （6 个字，必须连续出现）
用户输入: "帮我看看最近很火的gstack项目"
                ────┯────                   ───┯──
                "看看"匹配                   "项目"匹配
                但中间隔着其他字 → 精确子串匹配失败
```

11 个候选短语全部失败，`_matches()` 返回 `False`，`before_model_call()` 返回空列表，模型完全不知道有 `code-explorer` 可用。

### 2.3 完整断链位置

| 步骤 | 状态 | 说明 |
|---|---|---|
| 1. Skill 发现 | ✅ | `skill_catalog` 中有 `code-explorer` |
| 2. 提取用户上下文 | ✅ | 最近 6 条消息文本正确提取 |
| 3. 冷却检查 | ✅ | `code-explorer` 不在冷却中 |
| 4. **关键词匹配** | **❌ 断链点** | 精确子串匹配全部失败 |
| 5. 注入提醒 | ❌ | `matched` 为空，无消息注入 |
| 6. 模型决策 | ❌ | 模型从未看到提醒 |
| 7. skill 激活 | ❌ | `invoked_skills` 保持为空 |

### 2.4 问题的本质

精确子串匹配对自然语言的变体表达无能为力：

| 用户实际说法 | skill 的触发短语 | 语义是否匹配 | 精确子串是否匹配 |
|---|---|---|---|
| "帮我看看最近很火的gstack**项目**" | "看看这个**项目**" | ✅ | ❌ |
| "我想**理解**一下这个**代码**的**架构**" | "**理解代码**库" | ✅ | ❌ |
| "帮我**分析**一下这个**仓库**" | "**分析仓库**" | ✅ | ✅ |
| "**explore** this **codebase**" | "**explore** this **codebase**" | ✅ | ✅ |

中文的自然语言表达尤其灵活——用户不会精确复述 `when_to_use` 中的短语，中间经常插入修饰词、对象名、语气词。

---

## 3. Claude Code 的做法及我们为什么不能照搬

### 3.1 Claude Code 怎么做的

通过对 Claude Code 源码（`/Users/kino/works/opensource/Claude-Code-doc`）的逆向分析，发现 Claude Code **没有任何服务端匹配逻辑**：

1. 每轮通过 `getSkillListingAttachments()` 把所有可用 skill 的 name + description + whenToUse 注入为 `<system-reminder>` 消息
2. Tool Schema 的 description 里包含 BLOCKING REQUIREMENT："当 skill 匹配时，这是阻塞要求：必须先调用 Skill 工具"
3. 模型自己读取 listing，自己判断哪个 skill 匹配用户意图，自己决定调用 Skill 工具

**零行匹配代码。** 全靠 Claude 模型的语义理解 + 指令遵循能力。

### 3.2 为什么我们不能照搬

Claude Code 能这样做，是因为它运行在 **Claude 模型**上——Anthropic 专门针对 tool use 遵循做了训练和对齐。Claude 模型对 BLOCKING REQUIREMENT 级别的指令有极高的遵循率。

Harness 的情况不同：

| 维度 | Claude Code | Harness |
|---|---|---|
| 运行模型 | Claude（Anthropic 自家） | 用户选择的第三方模型 |
| 指令遵循能力 | 专门训练/对齐，极强 | 不确定，因模型而异 |
| skill 匹配方式 | 模型自己判断 | 需要代码辅助判断 |

如果 harness 完全依赖模型自己判断，在弱模型上会出现两种问题：
- **漏匹配**：模型没注意到 listing 中的 skill，跳过激活
- **误匹配**：模型错误地激活了不相关的 skill，浪费上下文

### 3.3 我们的设计选择

因此，harness 需要**代码辅助的 skill 匹配层**，但不能用死板的关键词匹配，而应该用**大模型的语义理解能力**来做匹配判断：

```
Claude Code:   skill listing → 模型自己判断（主模型）
Harness:       skill 元信息 → 专门的轻量分类调用（fast model） → 匹配结果注入 nudge → 主模型遵循 nudge
```

这样做的优势：
- **语义理解**：fast model 天然支持"看看...项目"到"看看这个项目"的语义匹配
- **无需维护规则**：不需要停用词表、bigram 逻辑、阈值调参
- **成本可控**：输入约 200-500 tokens，输出约 5 tokens，每次约 0.001 美元
- **延迟可控**：fast model 响应时间约 200-500ms

---

## 4. 解决方案

### 4.0 可配置开关

此功能通过环境变量 `SKILL_LLM_MATCH` 控制，默认关闭：

```
# .env
SKILL_LLM_MATCH=true    # 启用 LLM 分类匹配（增加延迟和 API 成本，提升匹配准确率）
```

| `SKILL_LLM_MATCH` 值 | 行为 | 适用场景 |
|---|---|---|
| 未设置 / `false` | 使用现有精确子串关键词匹配（`_matches()` 不变） | 成本敏感、延迟敏感、模型能力强 |
| `true` | 使用 LLM 分类调用替代关键词匹配 | 匹配准确率优先、可接受额外延迟 |

配置入口在 `core/shared/config.py`，与现有配置模式一致：

```python
# core/shared/config.py
SKILL_LLM_MATCH: bool = os.environ.get("SKILL_LLM_MATCH", "false").lower() in ("true", "1", "yes")
```

`SkillRelevancePolicy` 在构造时读取此配置，决定匹配策略：

```python
class SkillRelevancePolicy:
    def __init__(self, model_gateway: ModelGateway | None = None):
        self._model_gateway = model_gateway
        self._use_llm = SKILL_LLM_MATCH and model_gateway is not None

    def _matches(self, context_text, candidates):
        if self._use_llm:
            return self._classify_via_llm(context_text, candidates)
        return self._match_by_keywords(context_text, candidates)
```

组装点（`01_agent_loop.py`）无需条件判断——总是传入 `model_gateway`，由 Policy 内部根据配置决定是否使用：

```python
# 01_agent_loop.py — 无需改动，总是传入 model_gateway
SkillRelevancePolicy(model_gateway=model_gateway),
```

### 4.1 架构

```
SKILL_LLM_MATCH=false（默认）:
  before_model_call() → _match_by_keywords() → 精确子串匹配（现有逻辑不变）

SKILL_LLM_MATCH=true:
  QueryLoop.run()
    → policy_runner.before_model_call()
      → SkillRelevancePolicy.before_model_call()
        → 提取用户最近输入
        → 收集未激活 skill 的元信息（不含 body）
        → 冷却检查（复用现有逻辑）
        → _classify_via_llm()
          → 构造分类 prompt
          → ModelGateway.call_once()（轻量 LLM 调用）
          → 解析返回的 skill ID 列表
        → 注入 nudge 消息（复用现有逻辑）
```

### 4.2 分类 Prompt 设计

**system prompt**（固定，约 50 tokens）：

```
你是 skill 匹配器。根据用户意图，从可用 skill 列表中选出匹配的。
只返回匹配的 skill ID，逗号分隔。无匹配则返回 NONE。
不要解释，不要输出其他内容。
```

**user prompt**（动态，约 100-300 tokens）：

```
可用 skill:
- code-explorer | 探索项目, 理解代码库, 分析仓库, 看看这个项目 | 当用户需要探索或理解代码库时使用
- serper-search | 搜索网页, web search | 当需要搜索互联网信息时使用

用户最近输入:
帮我看看最近很火的gstack项目是干什么的
```

**预期输出**（约 2-10 tokens）：

```
code-explorer
```

或

```
NONE
```

### 4.3 调用参数

| 参数 | 值 | 理由 |
|---|---|---|
| system | 分类 system prompt（约 50 tokens） | 固定指令，明确输出格式 |
| messages | 单条 user 消息 | 无需多轮对话 |
| tools | `None` | 不传工具，纯文本输出 |
| request_options | `thinking_mode="disabled"` | 分类任务不需要 thinking |
| request_options | `max_output_tokens=50` | 输出极短，限制 token 预算 |

### 4.4 Skill 元信息格式化

将 `SkillMeta` 格式化为单行摘要，控制总输入长度：

```python
def _format_skill_summary(self, catalog: dict[str, SkillMeta]) -> str:
    lines = []
    for skill_id, meta in sorted(catalog.items()):
        triggers = meta.when_to_use or ""
        # 只取前 100 字符，避免 description 过长
        desc = (meta.description or "")[:100]
        lines.append(f"- {skill_id} | {triggers} | {desc}")
    return "\n".join(lines)
```

### 4.5 输出解析

```python
def _parse_classification(self, response_text: str) -> list[str]:
    text = response_text.strip().upper()
    if text == "NONE" or not text:
        return []
    return [sid.strip().lower() for sid in text.split(",") if sid.strip()]
```

### 4.6 改动范围

| 文件 | 改动 | 说明 |
|---|---|---|
| `core/shared/config.py` | 新增 `SKILL_LLM_MATCH` 配置项 | 环境变量开关，默认 `false` |
| `core/policy/skill_relevance.py` | 重构匹配逻辑 | 保留关键词匹配为 fallback，新增 LLM 分类路径 |
| `01_agent_loop.py:268` | 传入 `model_gateway` | `SkillRelevancePolicy()` → `SkillRelevancePolicy(model_gateway)` |
| `tests/test_skill_relevance_policy.py` | 更新测试 | 覆盖两种匹配模式 |

`skill_relevance.py` 内部改动明细：

| 改动 | 说明 |
|---|---|
| `__init__()` | 新增 `model_gateway` 参数，读取 `SKILL_LLM_MATCH` 配置 |
| `_matches()` → 改为分派方法 | 根据 `_use_llm` 选择 `_match_by_keywords()` 或 `_classify_via_llm()` |
| `_match_by_keywords()` | 重命名自现有 `_matches()`，逻辑不变，作为 fallback |
| 新增 `_classify_via_llm()` | 构造分类 prompt → 调用 `ModelGateway.call_once()` → 解析返回 |
| 新增 `_format_skill_summary()` | 将 skill 元信息格式化为 prompt 片段 |
| 新增 `_parse_classification()` | 解析 LLM 返回的 skill ID 列表 |
| 保留 `_extract_recent_context()` | 两种模式共用 |

### 4.7 冷却和性能优化

现有冷却机制（`COOLDOWN_QUERIES = 3`）完全保留：

- 被提醒过的 skill 在 3 轮内不再参与分类，减少 LLM 调用频率
- 已激活的 skill 不参与分类（现有逻辑）
- 只有存在未冷却、未激活的 skill 时才发起 LLM 调用

额外优化：

| 优化 | 说明 |
|---|---|
| 跳过条件 | 如果只有 0 个未冷却未激活的 skill，直接返回空（不调用 LLM） |
| thinking_mode | 设置为 `"disabled"`，避免 thinking token 开销 |
| max_output_tokens | 限制为 50，避免冗长输出 |

---

## 5. 与前序文档的关系

本文档是 [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md) 的直接后续。两份文档解决的是 skill 激活链路中两个不同环节的问题：

```
用户输入
  │
  ▼
┌─────────────────────────┐
│  ① 匹配检测              │ ← 本文档解决
│  "用户意图是否匹配 skill？"│    用 LLM 替代关键词匹配
│                          │    解决"看到了但不匹配"的问题
└──────────┬──────────────┘
           │ 匹配的 skill ID 列表
           ▼
┌─────────────────────────┐
│  ② 指令驱动              │ ← 前序文档解决
│  "模型是否遵循 nudge？"   │    BLOCKING REQUIREMENT
│                          │    解决"提醒了但模型不激活"的问题
└──────────┬──────────────┘
           │ 模型调用 skill 工具
           ▼
┌─────────────────────────┐
│  ③ 内容注入              │
│  skill body → system prompt│  已有机制，无需改动
└─────────────────────────┘
```

两个问题**互不包含**，需要独立解决：
- 前序文档解决的是②：nudge 注入了但模型不遵循 → 加强指令
- 本文档解决的是①：匹配算法根本没检测到 → 改用 LLM 语义理解

---

## 6. 风险评估

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| LLM 调用增加延迟 | 中 | fast model 约 200-500ms；冷却机制减少调用频率；无未激活 skill 时跳过 |
| LLM 调用增加成本 | 低 | 输入约 300 tokens + 输出约 5 tokens，每次 < 0.001 美元 |
| LLM 返回格式不一致 | 低 | 明确的 system prompt 约束 + `_parse_classification()` 容错解析 |
| LLM 误匹配 | 低 | 分类 prompt 包含 skill 元信息，准确率高于关键词匹配 |
| ModelGateway 不可用 | 低 | 降级：LLM 调用失败时静默返回空列表（不注入 nudge），不阻断主循环 |

---

## 7. 开放问题

1. **是否需要缓存分类结果？** 如果用户连续两轮输入相似的上下文，分类结果应该相同。可以在 `session_state` 上缓存 `(context_hash, matched_ids)` 对，避免重复调用。但这增加了状态管理复杂度，可以在后续迭代中考虑。

2. **是否应该限制每轮分类的 skill 数量？** 如果 skill catalog 非常大（>20 个），分类 prompt 的 token 数会增加。可以设置 `MAX_SKILLS_PER_CLASSIFICATION` 上限，优先包含最近被提醒/冷却中的 skill。当前项目 skill 数量较少，暂不需要。

3. **是否需要支持多模型策略？** 当前设计通过 `ModelGateway` 调用，自动使用主模型。如果主模型较慢/较贵，可以引入独立的分类模型配置。当前阶段使用主模型即可。
