# Task Plan / Skill / Subagent 顺序不稳定问题复盘

> 日期：2026-05-13  
> 类型：问题复盘 / 实施记录  
> 关联文档：
> - [2026-05-10-skill-activation-enforcement-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-10-skill-activation-enforcement-design.md)
> - [2026-05-11-task-subagent-runtime-v2-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-11-task-subagent-runtime-v2-design.md)

---

## 1. 问题摘要

在真实使用中，harness 在处理复杂请求时，`task_plan`、`skill` 加载、`fresh_subagent` 派发三者的顺序不稳定，主要表现为：

1. 有时先 `task_plan`
2. 有时先加载一个或多个 skill
3. 大多数情况下根本不会进入 `fresh_subagent`
4. 即使进入 `fresh_subagent`，用户也看不清主线程到底派发了什么输入给子代理

结果是：

- 用户无法预测 runtime 会先规划、先搜索还是先加载 skill
- prompt 中明明写了“复杂任务先 task_plan”，实际却常被 skill 抢跑
- 用户误以为“subagent 会自己加载 skill / 继承完整上下文”，但实际并不会

---

## 2. 复现样例

典型输入：

> 帮我看看明天的天气情况，然后从深圳南山区大学城出发去桔钓沙玩 1 天，只能乘公共交通，需要结合天气做连贯行程规划，并推荐景点和热门项目。

实际现象：

1. `SkillRelevancePolicy` 先命中 `weather`、`serper-search`
2. 模型先调用 `skill`
3. 然后主线程直接用基础工具完成天气查询、搜索和总结
4. 没有稳定地产生 `TaskState`
5. 没有进入 `task_execute`
6. 因而也看不到 subagent 输入边界和执行过程

---

## 3. 根因

这不是单一 prompt 语句的问题，而是 runtime 同时存在三套相互竞争的驱动力。

### 3.1 `_FRAMEWORK_PROMPT` 同时推了三个优先级

旧提示词同时强调：

- 多步骤任务使用 `todo`
- 多个子目标先 `task_plan`
- 如果匹配 skill，应先加载 skill

这三个指令没有被 runtime 硬化成单一优先级，只是同时出现在系统提示词中。模型自然会漂移。

相关文件：

- [core/prompt/system_context.py](/Users/kino/works/kino/harness/core/prompt/system_context.py)

### 3.2 `TaskPlanningPolicy` 只有“消费”没有“生产”

`TaskPlanningPolicy` 原本只会在 `run_state.task_planning_required=True` 时才真正收窄工具集合，但代码中没有稳定路径去自动设置这个 flag。

这意味着：

- “复杂任务先 `task_plan`”大多只是软提示
- 不是 runtime gate

相关文件：

- [core/policy/task_planning.py](/Users/kino/works/kino/harness/core/policy/task_planning.py)
- [core/query/state.py](/Users/kino/works/kino/harness/core/query/state.py)

### 3.3 pre-plan 阶段原本允许 `skill`

旧的 `PREPLAN_ALLOWED_TOOLS` 包含：

- `task_plan`
- `skill`
- `find`
- `read_file`

所以即使进入 pre-plan，skill 仍然合法，顺序依旧不稳定。

相关文件：

- [core/policy/task_planning.py](/Users/kino/works/kino/harness/core/policy/task_planning.py)

### 3.4 `SkillRelevancePolicy` 会在规划前继续施压

`SkillRelevancePolicy` 每轮都可能往 transcript 末尾加“建议先加载 skill”的提醒，而它原先不知道当前请求是否已经进入 task planning gate。

这进一步放大了 “先 skill，再 planning” 的倾向。

相关文件：

- [core/policy/skill_relevance.py](/Users/kino/works/kino/harness/core/policy/skill_relevance.py)

### 3.5 subagent 的 skill 边界并未被正式传递

虽然 `SubagentRequest` 早就有 `preloaded_skill_ids` 字段，但主路径上：

- `TaskRecord` 没有 `required_skill_ids`
- `task_plan` schema 也没有这个字段
- `task_execute` 没把 skill 传进 `SubagentRequest`

所以子代理不会自动拿到主线程认为“这个任务需要的 skill”。

相关文件：

- [core/tasks/models.py](/Users/kino/works/kino/harness/core/tasks/models.py)
- [core/tools/builtin/task_plan.py](/Users/kino/works/kino/harness/core/tools/builtin/task_plan.py)
- [core/tools/builtin/task_execute.py](/Users/kino/works/kino/harness/core/tools/builtin/task_execute.py)
- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)

### 3.6 subagent 输入边界过薄

`fresh_subagent` 最终看到的是 `_render_fresh_packet()` 渲染出来的一段文本，而旧的 `TaskPacket` 只稳定承载：

- `subject`
- `goal`
- `description`
- `done_criteria`

如果 planner 没把日期、地点、约束、输出格式写进任务，子代理就看不到。

相关文件：

- [core/tasks/dispatcher.py](/Users/kino/works/kino/harness/core/tasks/dispatcher.py)
- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)

---

## 4. 采取的修复

### 4.1 把“复杂任务先 `task_plan`”变成 runtime gate

新增了基于用户原始请求的复杂度判定：

- 多目标连接词
- 研究类信号（天气、路线、价格、最新等）
- 交付物信号（规划、方案、报告、分析等）

命中后，`TaskPlanningPolicy` 会主动设置：

- `run_state.task_planning_required = True`
- `run_state.task_planning_reason = "complex_user_request"`

这让复杂请求不再只靠 prompt 自觉。

相关文件：

- [core/policy/task_planning.py](/Users/kino/works/kino/harness/core/policy/task_planning.py)

### 4.2 pre-plan 工具集合移除 `skill`

把：

```python
{"task_plan", "skill", "find", "read_file"}
```

改成：

```python
{"task_plan", "find", "read_file"}
```

从 runtime 层面切断“规划前先 skill”的合法路径。

相关文件：

- [core/policy/task_planning.py](/Users/kino/works/kino/harness/core/policy/task_planning.py)

### 4.3 `SkillRelevancePolicy` 在 planning gate 前静默

增加条件：

- 如果当前 `task_planning_required=True`
- 且 `TaskState` 还不存在

则不再注入 skill 提醒。

这让 skill 不再和 planning 抢首轮控制权。

相关文件：

- [core/policy/skill_relevance.py](/Users/kino/works/kino/harness/core/policy/skill_relevance.py)

### 4.4 收敛 `_FRAMEWORK_PROMPT`

把原本互相竞争的指令改成单一路径：

1. 复杂多步骤任务先 `task_plan`
2. `TaskState` 是权威状态
3. `todo` 只是投影视图
4. `TaskState` 建立前不要先加载 skill、不要派发 subagent、不要开始执行
5. 只有当前任务明确需要时才加载 skill

相关文件：

- [core/prompt/system_context.py](/Users/kino/works/kino/harness/core/prompt/system_context.py)

### 4.5 给任务模型正式加上 `required_skill_ids`

新增字段：

- `TaskRecord.required_skill_ids`
- `TaskPacket.required_skill_ids`

并同步更新：

- `task_plan` schema
- planner runtime
- `compile_task_packet()`

让 planner 可以明确声明“这个 task 需要哪些 skill”，而不是靠子代理自己猜。

相关文件：

- [core/tasks/models.py](/Users/kino/works/kino/harness/core/tasks/models.py)
- [core/tasks/planner_runtime.py](/Users/kino/works/kino/harness/core/tasks/planner_runtime.py)
- [core/tasks/dispatcher.py](/Users/kino/works/kino/harness/core/tasks/dispatcher.py)
- [core/tools/builtin/task_plan.py](/Users/kino/works/kino/harness/core/tools/builtin/task_plan.py)

### 4.6 `task_execute` 传递并展示 skill 边界

`task_execute` 现在会：

1. 读取 `task.required_skill_ids`
2. 写入 `SubagentRequest.preloaded_skill_ids`
3. 在派发前显示当前 task 的 skill 列表
4. 把 subagent 生命周期事件转发到主线程 UI

这样用户至少能看到：

- 派发的是哪个 task
- 子代理类型是什么
- 预加载了哪些 skill
- 子代理何时启动、调用工具、结束

相关文件：

- [core/tools/builtin/task_execute.py](/Users/kino/works/kino/harness/core/tools/builtin/task_execute.py)
- [core/session/subagent.py](/Users/kino/works/kino/harness/core/session/subagent.py)
- [core/tools/context.py](/Users/kino/works/kino/harness/core/tools/context.py)
- [core/tools/runtime.py](/Users/kino/works/kino/harness/core/tools/runtime.py)

---

## 5. 修复后的行为模型

复杂请求的期望主路径现在是：

```text
User Request
  -> TaskPlanningPolicy 判定复杂度
  -> runtime 开启 pre-plan gate
  -> 仅允许 task_plan/find/read_file
  -> LLM 先建立 TaskState
  -> 当前任务如需 skill，再显式加载 skill
  -> local 任务主线程执行
  -> fresh_subagent 任务通过 task_execute 派发
  -> subagent 预加载 required_skill_ids
  -> 主线程消费结果并继续推进 TaskState
```

这条路径和之前相比，最大区别是：

- “先规划”从建议变成门禁
- “是否加载 skill”从全局抢优先级变成 task 级决策
- “subagent 要什么 skill”从隐式假设变成显式字段

---

## 6. 验证

本轮为这条修复补了回归测试，覆盖：

1. 复杂请求会自动要求 pre-plan
2. 简单请求不会误触 pre-plan
3. pre-plan 工具集合不再允许 `skill`
4. `task_plan` schema 暴露 `required_skill_ids`
5. `task_execute` 会把 `required_skill_ids` 传给 `SubagentRequest`
6. stable prompt 文案已切换到单一路径

验证命令：

```bash
env -u SSLKEYLOGFILE pytest \
  tests/test_task_planning_policy.py \
  tests/session/test_task_plan_tool.py \
  tests/session/test_task_execute_tool.py \
  tests/session/test_prompt_assembler.py \
  tests/session/test_subagent_runtime.py \
  tests/session/test_state_assembled_runtime.py \
  tests/test_runtime_control_plane.py \
  tests/test_tool_registry.py -q
```

当时的验证结果为：

```text
97 passed in 0.44s
```

后续连同取消、504、防止 raw-mode 破坏输出等修复一起做过更大回归，结果为：

```text
161 passed in 1.71s
```

---

## 7. 剩余问题

这次修复解决的是“顺序不稳定”和“skill/subagent 边界不显式”的主问题，但还没有彻底完成 V2 设计文档里的全部目标。

仍然存在的缺口：

1. 复杂度判定目前是启发式，不是正式 planner classifier
2. `fresh_subagent` 的输入边界仍然主要是文本渲染，不是更强约束的结构化 packet
3. subagent 返回值还是以自然语言摘要为主，不是完整 result envelope
4. UI 现在有“派发 / 启动 / 工具调用 / 结束”提示，但还不是正式任务卡片

也就是说，这次是把主路径从“不稳定”修到“可预测”，不是把整个 task/subagent runtime 一次性做完。

---

## 8. 结论

这次问题的关键不是模型“不听话”，而是 runtime 之前没有给出单一、强制、可验证的执行顺序。

修复后的核心原则可以浓缩成三句：

1. 复杂任务先 `task_plan`，这是 gate，不是建议。
2. `todo` 只负责展示，`TaskState` 才是执行权威。
3. skill 和 subagent 的边界必须显式写进 task，而不是靠模型自己领会。

后续如果再出现“为什么这次先 skill / 为什么没起 subagent / 为什么子代理没看到关键约束”，优先从这三条原则回看，而不是先怀疑模型输出本身。
