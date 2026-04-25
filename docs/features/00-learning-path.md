# 00: 学习路径

> `docs/features` 现在分成两层：前 7 篇主要讲组件，后 5 篇主要讲“怎么把这些组件串成一个可以工作的 Agent”。这篇负责告诉你应该从哪里开始读。

---

## 先分清你是哪一种读者

### 1. 第一次接触这个框架

你的目标通常是：

- 搞清楚一条请求怎么跑完
- 理解 runtime 的核心设计
- 知道扩展时哪些边界不能碰

最推荐的阅读顺序见下文“完整入门路线”。

### 2. 已经懂一点 Agent，但第一次接这个仓库

你的目标通常是：

- 对齐这个仓库和常见 history-based agent 的差别
- 快速定位显式状态、协议边界、工具运行时

建议优先读：

```text
README.md
01-agent-loop.md
08-request-lifecycle-walkthrough.md
09-state-assembled-runtime.md
10-anthropic-protocol-boundary.md
```

### 3. 只想扩展一个能力

你的目标通常不是“完全理解一切”，而是：

- 找到扩展点
- 不要破坏现有 runtime

建议直接走“扩展短路径”。

---

## 完整入门路线

“从 0 到 1 读懂并做一个 Agent”，我建议按这个顺序。

### 第一段：先看主路径，不急着看所有细节

1. `README.md`
2. `01-agent-loop.md`
3. `08-request-lifecycle-walkthrough.md`

这一段的目标只有一个：

**先把一次请求从入口到退出的时间线跑通。**

### 第二段：看清这个仓库和常见 agent 的根本差异

4. `09-state-assembled-runtime.md`
5. `04-query-control-plane.md`
6. `03-tool-control-plane.md`

这一段的目标是理解：

- 为什么 transcript 不是运行时真相
- 为什么状态写入口要统一
- 为什么控制平面要和执行平面分开

### 第三段：再看模型输入和协议边界

7. `02-tool-system.md`
8. `06-skill.md`
9. `10-anthropic-protocol-boundary.md`

这一段的目标是理解：

- 模型到底知道哪些工具
- skill 怎么发现、加载、注入
- 内部消息最后怎么适配 Anthropic 协议

### 第四段：最后再看扩展路线

10. `07-subagent.md`
11. `11-extension-playbook.md`
12. `12-runtime-invariants.md`

这一段的目标是：

- 真正开始自己扩展
- 但不破坏当前架构边界

---

## 扩展短路径

如果你不打算完整入门，只是要在当前仓库里加点东西，可以按目的直接跳。

### 想加一个 tool

建议顺序：

```text
02-tool-system.md
03-tool-control-plane.md
11-extension-playbook.md
12-runtime-invariants.md
```

### 想加一个 skill

建议顺序：

```text
06-skill.md
09-state-assembled-runtime.md
11-extension-playbook.md
12-runtime-invariants.md
```

### 想改 loop / policy / max_turns / recovery

建议顺序：

```text
01-agent-loop.md
04-query-control-plane.md
08-request-lifecycle-walkthrough.md
12-runtime-invariants.md
```

### 想改协议适配或消息截断

建议顺序：

```text
08-request-lifecycle-walkthrough.md
09-state-assembled-runtime.md
10-anthropic-protocol-boundary.md
12-runtime-invariants.md
```

---

## 这 5 篇新增伴随文档分别解决什么问题

### `08-request-lifecycle-walkthrough.md`

回答：

- 一次请求从哪里进、从哪里出
- 每一轮都发生什么

### `09-state-assembled-runtime.md`

回答：

- 为什么这个 runtime 不靠完整 transcript 活着
- 为什么显式状态是设计核心

### `10-anthropic-protocol-boundary.md`

回答：

- 为什么内部消息结构不直接写成 Anthropic block 结构
- tool_use / tool_result 配对为什么会反向约束上游代码

---

## 一句话版

如果只能记住一句：

**先沿时间线看 08，再用 09 和 10 建立核心边界，最后用 11 和 12 指导扩展。**
