# Skill Progressive Loading 设计

> 日期: 2026-04-16
> 状态: 待审阅
> 关联文档:
> - [`docs/superpowers/specs/2026-04-15-skills-system-design.md`](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-04-15-skills-system-design.md) — Skills System 总体设计（已实现）
> - [`docs/superpowers/plans/2026-04-15-skills-system-implementation.md`](/Users/kino/works/kino/harness/docs/superpowers/plans/2026-04-15-skills-system-implementation.md) — 上次实现计划（已完成）

## 背景

Skills System v1 已支持以下能力：

- discover 本地 `.harness/skills/<id>/SKILL.md`
- 在 stable prompt 中注入可用 skill catalog
- 通过 `/skills use <id>` 激活 skill
- 将 active skill 的正文注入到模型输入

但在真实 skill 上，当前方案约束力很弱。以 `analysis-report` 为例：

- `SKILL.md` 只提供流程摘要和 reference 索引
- 详细规则分散在 `analysis-pipeline.md`、`style-system.md`、`card-catalog.md` 等文件
- 当前运行时只注入 `SKILL.md`，模型看不到这些 reference 的正文

结果是：

- 模型只遵守了粗粒度流程
- 样式、组件、检查清单等细节大面积丢失
- skill 表面“已激活”，实际上没有真正把工作流交给模型

## 问题重定义

本次设计不再把问题定义为“如何把整个 skill 目录都塞进 prompt”。

真正要解决的是：

> 如何让模型先看到 skill 的主指令，再在需要时低摩擦地读取 reference 文件。

这更接近 Claude Code 文档体现出来的模式：

1. discover 阶段只暴露 skill 元信息
2. invoke 阶段才加载 `SKILL.md` 正文
3. supporting files 作为真实文件存在
4. 模型需要细节时，再去读取这些文件

说明：

- 上述第 1、2 点有文档直接支持
- 第 3、4 点是基于 Claude Code 文档中 bundled skill `files`、inline prompt 注入、附件与文件体系的合理推断，不是本文档能直接逐字证明的内部实现细节

## 非目标

为了先解决“skill 好用”这个核心问题，本设计明确不处理以下内容：

- compact 后如何保留已读 reference
- `/skills reload` 后如何刷新 reference 缓存
- `/skills list` / `/skills show` 的完整体验优化
- 自动解析自然语言引用并猜测要加载哪些文件
- 激活时全量内联整个 skill 目录
- 新增专用 `load_skill_file` 工具

## 根因分析

### 问题 1：当前只加载 `SKILL.md`

[`core/skills/registry.py`](/Users/kino/works/kino/harness/core/skills/registry.py) 中的 `SkillRegistry.load()` 目前只读取 `meta.skill_file`，即 `SKILL.md` 本身。

这意味着：

- discover 阶段没有 reference 元信息
- invoke 阶段也没有 reference 文件索引
- 模型即使想读 reference，也拿不到稳定路径

### 问题 2：reference 的存在方式对模型不友好

很多真实 skill 的 `SKILL.md` 是“hub”而不是“全文”：

```markdown
## Analysis Pipeline
Every report follows this 6-stage pipeline. See `analysis-pipeline.md` for full detail.

## HTML Style System
All HTML output MUST follow the design tokens in `style-system.md`.
```

对人来说这很好用，因为人会顺手打开这些文件。

对模型来说，这还不够，因为当前运行时没有把以下信息明确给到模型：

- 这些 reference 文件确实存在
- 它们的稳定文件名是什么
- 它们的可读取路径是什么

### 问题 3：错误方向是“自动替模型展开所有 reference”

直觉上最容易想到的方案是：

- 激活 skill 时把所有 `.md` 全部内联
- 或者自动解析 `See X for full detail` 并替换成 X 的正文

这两个方向都不适合：

- token 成本太高，大 skill 很容易上万到数万字符
- 自动猜引用太脆弱，语义依赖 prose，不是协议
- 会把“按需读取”重新退化成“全量注入”

## 设计原则

本次设计遵循 4 条原则：

1. **catalog 和正文分离**
   - discover 只提供元信息
   - invoke 才提供 `SKILL.md`

2. **正文和 reference 分离**
   - `SKILL.md` 负责主工作流
   - reference 负责细节展开

3. **reference 必须显式声明**
   - 不扫描目录猜测
   - 不从 prose 中解析“可能是引用”

4. **按需读取复用现有文件读取能力**
   - 不新增专用 skill-reference 工具
   - 先把 reference 文件路径清楚暴露给模型，再让模型按需读取

## 方案概览

本设计采用三阶段 Progressive Loading：

### 阶段 1：Discover

discover 时只读取 `SKILL.md` frontmatter，构建 catalog：

- `skill_id`
- `name`
- `description`
- `when_to_use`
- `references` 元信息索引

这里的 `references` 只包含轻量元数据，不读取文件正文。

### 阶段 2：Invoke

当用户或模型决定使用某个 skill 时：

- 加载 `SKILL.md` 正文
- 将正文注入 `<active-skill>`
- 同时附带一个极小的 reference index

reference index 只告诉模型：

- 有哪些 reference 文件
- 它们分别做什么
- 它们相对于当前 `working_dir` 的可读路径是什么

但不把 reference 正文直接注入 prompt。

### 阶段 3：Reference Read

模型阅读 `SKILL.md` 后，如果判断需要细节：

- 通过现有文件读取能力读取某个 reference 文件
- 将读取行为作为正常工具调用的一部分
- 只读取当前任务真正需要的文件

这一步是按需发生的，不由运行时自动替模型决定。

## SKILL.md 格式扩展

为了让 reference 成为显式协议，`SKILL.md` frontmatter 新增 `references` 字段：

```yaml
---
name: analysis-report
description: Generate structured analysis reports
when-to-use: When creating long-form HTML analysis reports
references:
  - path: analysis-pipeline.md
    purpose: 6-stage step-by-step workflow
  - path: style-system.md
    purpose: HTML design tokens and CSS rules
  - path: card-catalog.md
    purpose: allowed card components
  - path: quality-checklist.md
    purpose: final report checks
---
```

约束：

- `path` 必须是相对 `skill_dir` 的相对路径
- 不允许自动扫描目录补全 `references`
- `purpose` 是给模型看的简短提示，不是文件摘要

## SKILL.md 正文写作约束

为了让“先读 `SKILL.md`，再按需读 reference”成立，`SKILL.md` 正文必须满足以下约束：

- `SKILL.md` 必须包含足够独立执行的主工作流指令
- reference 只应用于补充细节，不应用于承载核心步骤本身
- `See X for full detail` 可以存在，但 X 应该是样式规范、组件目录、检查清单、示例、补充策略等细化材料

也就是说：

- “先做什么，再做什么，最后如何收尾” 这类主流程应直接写在 `SKILL.md`
- “具体 CSS token 是什么”“允许使用哪些卡片”“最后要做哪些检查” 这类细节可以放在 reference 中

如果某个文件定义的是 skill 的核心工作流，而不是补充细节，那么它不应该只存在于 reference 中，而应被提升到 `SKILL.md` 正文。

## 数据模型调整

新增一个轻量 reference 元数据结构：

```python
@dataclass(slots=True)
class SkillReference:
    path: str
    purpose: str | None
    abs_path: Path
    prompt_path: str
```

说明：

- `path` 保留 skill 作者声明的相对路径，语义上相对于 `skill_dir`
- `abs_path` 仅供运行时校验文件存在性与内部访问，不注入 prompt
- `prompt_path` 是相对于当前 `working_dir` 的可读路径，用于告诉模型应传给 `read_file` 什么参数

`SkillMeta` 扩展为：

```python
@dataclass(slots=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_file: Path
    references: list[SkillReference]
```

`SkillContent` 仍然保持简单：

```python
@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str
```

本次不把 reference 正文并入 `SkillContent.body`。

## Prompt 注入格式

active skill 注入格式调整为：

```xml
<active-skills>
  <active-skill id="analysis-report">
    <instruction>
      ...SKILL.md 正文...
    </instruction>
    <reference-files>
      <file path=".harness/skills/analysis-report/analysis-pipeline.md">
        6-stage step-by-step workflow
      </file>
      <file path=".harness/skills/analysis-report/style-system.md">
        HTML design tokens and CSS rules
      </file>
      <file path=".harness/skills/analysis-report/card-catalog.md">
        allowed card components
      </file>
    </reference-files>
  </active-skill>
</active-skills>
```

这条消息要表达的意思非常直接：

- 先按 `SKILL.md` 工作
- 如果需要细节，再去读这些 reference 文件

## 运行时行为

### Discover 时

`SkillRegistry.discover()` 负责：

- 解析 `SKILL.md` frontmatter
- 读取 `references` 列表
- 将每个相对路径解析成 `abs_path`
- 将每个 reference 再计算为相对于当前 `working_dir` 的 `prompt_path`
- 把 reference 元信息保存在 `SkillMeta`

discover 时不读取 reference 正文。

### Load 时

`SkillRegistry.load(skill_id)` 仍然只负责：

- 读取 `SKILL.md` 正文
- 计算正文 digest
- 返回 `SkillContent`

不读取 reference 正文，不做多文件拼接。

### Prompt 组装时

`PromptAssembler.build_active_skill_messages()` 负责：

- 注入 `SkillContent.body`
- 读取 `content.meta.references`
- 渲染 `<reference-files>`

这一步只渲染 index，不渲染 reference 内容。

## 为什么不新增 `load_skill_file` 工具

现阶段不推荐新工具，原因有 3 个：

1. 运行时已经有文件读取能力，重复造一个“只给 skill 用的 read”价值不高
2. 新工具会引入新的权限模型、错误模型和测试面
3. 当前真正缺的是“模型不知道该读哪个文件”，不是“缺少读文件 API”

所以 v1 的最小闭环应该是：

- skill 激活时给出 reference index
- 模型自己用现有读文件能力读取需要的文件

## 风险与假设

本设计有一个明确假设：

> 当模型看到 `SKILL.md` 正文中的 reference 提示，以及 `<reference-files>` 中的可读路径后，会在需要细节时主动调用 `read_file`。

这不是一个可以静态证明的前提，而是一个运行时行为假设。

潜在失败方式包括：

- 模型误以为 reference 内容已经被注入
- 模型只读 `SKILL.md` 就开始执行，不再补读 reference
- 模型知道要读 reference，但没有稳定地选择正确文件

本次接受这个假设，原因是：

- 实现最小
- 与 Claude Code 暴露出来的使用模式更接近
- 可以先验证“reference index + 普通读文件”是否已经足够

如果这个假设在实测中不成立，下一步 fallback 是：

- 保留当前声明式 `references` 协议
- 在 v2 增加更强约束的 reference-loading 机制
- 候选方向包括专用 `load_skill_file` 工具，或在 prompt 中增加更强的读取指令模板

## 为什么不做自动展开

本次明确不采用以下方案：

### 方案 A：激活时全量内联

不采用，原因：

- token 成本过高
- 会把 progressive loading 退化成 eager loading

### 方案 B：自动解析 prose 引用

不采用，原因：

- 依赖自然语言表达，脆弱且不可测试
- skill 作者改一句 prose 就可能影响加载行为

### 方案 C：按目录自动扫描 `.md`

不采用，原因：

- 会把草稿、README、示例输出等无关文件一并暴露给模型
- 缺少作者显式意图

## 实现范围

本次实现只覆盖最小闭环：

1. `SKILL.md` frontmatter 支持 `references`
2. discover 阶段解析并保存 reference index
3. active skill 注入时渲染 reference index
4. 模型通过现有文件读取能力按需读取 reference

明确不在本次实现内：

- compact 恢复 reference-read 历史
- reload 后 reference 缓存一致性
- reference 内容预算的长期治理
- conditional skills 自动激活
- fork skill 对 reference 的独立处理

## 成功标准

满足以下条件即可认为本设计达标：

1. 激活 `analysis-report` 后，模型能在 prompt 中看到 `SKILL.md` 正文和 reference index
2. 模型在需要样式或组件细节时，会主动读取对应 reference 文件
3. 输出结果相比当前实现，能显著更多地遵守 `analysis-pipeline.md`、`style-system.md`、`card-catalog.md` 等约束
4. 激活 skill 时不会因为全量内联 reference 而显著放大 prompt

说明：

- 第 2 条是本设计的关键实证检验
- 如果第 2 条在真实任务中不稳定成立，应视为当前方案不足，而不是 prompt 作者使用不当

## 结论

这次不是要把 skill 重新做成“目录展开器”。

正确方向是：

- discover 时只暴露 skill 元信息
- invoke 时只注入 `SKILL.md`
- 同时明确告诉模型有哪些 reference 文件可读
- 让模型在工作流推进过程中按需读取 reference

一句话概括：

> skill 的主指令进入 prompt，skill 的细节材料留在文件系统里，模型需要时再读。
