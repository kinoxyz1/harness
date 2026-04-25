# Agent 入门文档补充实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐一组面向项目组同事的中文文档，让他们能从零理解这个 runtime 里一个 Agent 是如何构建、运行和扩展的。

**Architecture:** 保留现有 `docs/features` 按组件拆解的说明，再新增一层“伴随式”教学文档，分别从执行时间线、状态权威来源、协议边界、扩展路径四个角度讲清当前 runtime。同时顺手修正旧文档里和当前代码不一致的历史残留表述。

**Tech Stack:** Markdown、现有 `docs/features` 目录结构、`core/` 下的运行时代码、`tests/` 下用于证明行为的 pytest 测试

---

### Task 1: 确定文档布局

**Files:**
- Create: `docs/features/00-learning-path.md`
- Create: `docs/features/08-request-lifecycle-walkthrough.md`
- Create: `docs/features/09-state-assembled-runtime.md`
- Create: `docs/features/10-anthropic-protocol-boundary.md`
- Create: `docs/features/11-extension-playbook.md`
- Create: `docs/features/12-runtime-invariants.md`
- Modify: `README.md`

- [ ] **Step 1: 重读当前文档目录并确定补充文档文件名**

Run: `find docs -maxdepth 2 -type d | sort`
Expected: 确认 `docs/features` 是这批补充文档的合适落点

- [ ] **Step 2: 重读现有 feature 文档集合与 README 阅读路径**

Run: `rg -n "## 怎么读这个仓库|docs/features|Agent Loop|Tool System|Skill System|Subagent" README.md docs/features`
Expected: 确认现有文档以组件说明为主，还缺“从零做一个 Agent”的明确学习路径

- [ ] **Step 3: 确认这批文档的文件布局**

计划新增：

```text
docs/features/00-learning-path.md
docs/features/08-request-lifecycle-walkthrough.md
docs/features/09-state-assembled-runtime.md
docs/features/10-anthropic-protocol-boundary.md
docs/features/11-extension-playbook.md
docs/features/12-runtime-invariants.md
```

- [ ] **Step 4: 实现完成后提交这批文档布局的改动**

```bash
git add README.md docs/features/00-learning-path.md docs/features/08-request-lifecycle-walkthrough.md docs/features/09-state-assembled-runtime.md docs/features/10-anthropic-protocol-boundary.md docs/features/11-extension-playbook.md docs/features/12-runtime-invariants.md
git commit -m "docs: add agent onboarding companion docs"
```

### Task 2: 编写请求生命周期 walkthrough

**Files:**
- Create: `docs/features/08-request-lifecycle-walkthrough.md`
- Read first: `01_agent_loop.py`
- Read first: `core/session/engine.py`
- Read first: `core/query/loop.py`
- Read first: `core/session/view_builder.py`
- Read first: `core/prompt/assembler.py`
- Read first: `core/tools/runtime.py`

- [ ] **Step 1: 落笔前重读请求入口和主循环代码**

Run: `sed -n '1,240p' 01_agent_loop.py && sed -n '1,260p' core/session/engine.py && sed -n '194,347p' core/query/loop.py`
Expected: 拿到 REPL 入口、bootstrap、transcript 追加、循环分支与返回路径的准确顺序

- [ ] **Step 2: 重读视图组装和工具运行时编排**

Run: `sed -n '1,231p' core/session/view_builder.py && sed -n '116,228p' core/prompt/assembler.py && sed -n '76,330p' core/tools/runtime.py`
Expected: 确认 `system`、`messages`、`tools`、批处理执行和 update 应用分别发生在哪一层

- [ ] **Step 3: 编写 walkthrough 文档**

必含章节：

```text
1. 一次用户请求的时间线
2. 什么进入 SessionState，什么进入 RunState
3. 模型在每一轮真正收到什么
4. 模型要求调用工具时发生什么
5. 循环停止时发生什么
6. 新同事最容易误解的点
```

- [ ] **Step 4: 验证 walkthrough 只描述当前行为**

Run: `rg -n "allowed_tools_override|stop_reason|max_turns|tool_calls|build\(|execute_batch" docs/features/08-request-lifecycle-walkthrough.md`
Expected: 新文档中的术语都能在当前代码里直接找到对应名字

### Task 3: 编写 state-assembled runtime 专题

**Files:**
- Create: `docs/features/09-state-assembled-runtime.md`
- Read first: `core/session/state.py`
- Read first: `core/query/state.py`
- Read first: `core/session/view_builder.py`
- Read first: `core/prompt/assembler.py`
- Read first: `tests/session/test_state_assembled_runtime.py`

- [ ] **Step 1: 重读状态容器及其消费者**

Run: `sed -n '1,180p' core/session/state.py && sed -n '1,120p' core/query/state.py`
Expected: 确认哪些字段归 session 拥有，哪些字段归单次 query 拥有，以及为什么两者都需要

- [ ] **Step 2: 重读状态到视图的组装路径和 transcript 独立性证明**

Run: `sed -n '1,231p' core/session/view_builder.py && sed -n '116,228p' core/prompt/assembler.py && sed -n '1,120p' tests/session/test_state_assembled_runtime.py`
Expected: 确认“runtime truth 来自显式状态而不是 transcript”的完整论证链条

- [ ] **Step 3: 编写 state-assembled runtime 文档**

必含章节：

```text
1. 为什么 transcript 不是 runtime truth
2. SessionState 为什么是权威来源
3. RunState 为什么是单次 query 的控制状态
4. PromptAssembler 如何每轮重建 system
5. transcript slice 如何退化为优化手段而不是事实来源
6. 哪些测试证明了这个性质
```

- [ ] **Step 4: 验证新文档明确指向证明性测试**

Run: `rg -n "test_state_assembled_runtime|transcript|runtime truth|state-assembled" docs/features/09-state-assembled-runtime.md`
Expected: 文档明确提到证明性测试和相关 builder/assembler 代码

### Task 4: 编写 Anthropic 协议边界专题

**Files:**
- Create: `docs/features/10-anthropic-protocol-boundary.md`
- Read first: `core/llm/protocol.py`
- Read first: `tests/test_protocol.py`
- Read first: `core/session/view_builder.py`
- Read first: `core/query/loop.py`

- [ ] **Step 1: 重读协议适配层和对应测试**

Run: `sed -n '1,240p' core/llm/protocol.py && sed -n '1,220p' tests/test_protocol.py`
Expected: 确认 `system`、`assistant tool_calls`、`tool` 消息到 Anthropic 格式的精确映射

- [ ] **Step 2: 重读依赖这些协议约束的循环与 transcript 代码**

Run: `sed -n '30,190p' core/session/view_builder.py && sed -n '227,347p' core/query/loop.py`
Expected: 确认为什么 tool 配对和 transcript 保留规则会反向约束上游 runtime

- [ ] **Step 3: 编写协议边界文档**

必含章节：

```text
1. 内部消息形状和 Anthropic 消息形状的区别
2. 为什么 system 要单独组装
3. 为什么 runtime 先存内部 `tool` 消息
4. tool_use / tool_result 配对是怎么完成的
5. 为什么 transcript slice 必须保持协议有效性
6. 哪些地方贡献者不能随意改
```

- [ ] **Step 4: 验证新文档使用的是当前协议名字**

Run: `rg -n "tool_use|tool_result|normalize_messages|system|assistant|user" docs/features/10-anthropic-protocol-boundary.md`
Expected: 文档里的协议术语和当前代码、测试完全一致

### Task 5: 编写扩展手册和运行时不变量速查

**Files:**
- Create: `docs/features/11-extension-playbook.md`
- Create: `docs/features/12-runtime-invariants.md`
- Read first: `core/tools/__init__.py`
- Read first: `core/tools/builtin/read_file.py`
- Read first: `core/tools/builtin/todo.py`
- Read first: `core/tools/builtin/skill.py`
- Read first: `core/skills/registry.py`
- Read first: `core/policy/base.py`
- Read first: `core/session/subagent.py`
- Read first: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 落笔前重读扩展点代码**

Run: `sed -n '1,140p' core/tools/__init__.py && sed -n '1,220p' core/tools/builtin/skill.py && sed -n '1,255p' core/skills/registry.py && sed -n '1,120p' core/policy/base.py && sed -n '1,220p' core/session/subagent.py`
Expected: 确认 tools、skills、policies、subagents 的真实扩展缝隙

- [ ] **Step 2: 重读内置工具和控制平面测试里的具体不变量**

Run: `sed -n '1,237p' core/tools/builtin/read_file.py && sed -n '1,210p' core/tools/builtin/todo.py && sed -n '1,220p' tests/test_runtime_control_plane.py`
Expected: 提炼出“完整读取后才能编辑”“todo 全量替换”“allowed_tools 只会收窄”等真实不变量

- [ ] **Step 3: 编写扩展手册**

必含章节：

```text
1. 如果你要新增一个 tool
2. 如果你要新增一个 skill
3. 如果你要新增一个 policy
4. 如果你要新增一个 subagent 约束
5. 每条路径应该先读或补哪些测试
```

- [ ] **Step 4: 编写运行时不变量速查表**

必含章节：

```text
1. 状态只能通过结构化 updates 回写
2. skill 本轮加载、下一轮生效
3. todo 是全量替换，且只能有一个 `in_progress`
4. 只读工具可以并行批处理，写工具不行
5. transcript 截断不能破坏消息和协议有效性
6. 工具限制只能收窄，不能放宽
```

- [ ] **Step 5: 验证两篇文档都引用了具体代码和测试**

Run: `rg -n "read_file|todo|skill|ToolInvocationOutcome|allowed_tools_override|RunUpdate|SessionUpdate" docs/features/11-extension-playbook.md docs/features/12-runtime-invariants.md`
Expected: 两篇文档都基于真实 runtime 结构，而不是泛泛的 agent 框架语言

### Task 6: 修正文档漂移并建立入口索引

**Files:**
- Modify: `docs/features/01-agent-loop.md`
- Modify: `docs/features/03-tool-control-plane.md`
- Modify: `docs/features/06-skill.md`
- Modify: `README.md`
- Create: `docs/features/00-learning-path.md`

- [ ] **Step 1: 编辑前重读有漂移的旧文档段落**

Run: `sed -n '218,226p' docs/features/01-agent-loop.md && sed -n '219,240p' docs/features/03-tool-control-plane.md && sed -n '560,610p' docs/features/06-skill.md && sed -n '1,120p' core/session/commands.py`
Expected: 确认 `allowed_tools_override`、`INVOKE_SKILL` payload、`/skills off` 的具体漂移表述

- [ ] **Step 2: 编写学习路径索引文档**

必含章节：

```text
1. 不同角色应该读哪些文档
2. 第一次入门的推荐顺序
3. “我只想扩展一个能力”时的短路径
4. 指向新伴随文档的链接
```

- [ ] **Step 3: 修正旧 feature 文档里的漂移表述**

必须修正：

```text
- 删除“`skill` 当前会收窄 `allowed_tools_override`”的说法
- 把 `INVOKE_SKILL` payload 示例改成 `payload={"invoked_skill": record}`
- 把 `/skills off` 改成当前“inline skill 无法停用”的行为描述
```

- [ ] **Step 4: 从仓库主入口连到这批新文档**

README 必须补充：

```text
- 在“怎么读这个仓库”附近加入这组伴随文档的顺序
- 说明 `docs/features` 现在同时包含组件说明和入门陪跑文档
```

- [ ] **Step 5: 对所有受影响文档做最终一致性检查**

Run: `rg -n "allowed_tools_override|invoked_skill|cannot be deactivated|Skill activated:|state-assembled|tool_result" README.md docs/features`
Expected: 不再残留旧表述，并且新文档能从入口被发现

- [ ] **Step 6: 提交文档修正和交叉链接**

```bash
git add README.md docs/features/00-learning-path.md docs/features/01-agent-loop.md docs/features/03-tool-control-plane.md docs/features/06-skill.md docs/features/08-request-lifecycle-walkthrough.md docs/features/09-state-assembled-runtime.md docs/features/10-anthropic-protocol-boundary.md docs/features/11-extension-playbook.md docs/features/12-runtime-invariants.md
git commit -m "docs: add onboarding guides and fix runtime doc drift"
```
