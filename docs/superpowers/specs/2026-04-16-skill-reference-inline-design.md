# Skill Reference Inline Loading Design

> Date: 2026-04-16
> Status: pending review
> Supersedes: `docs/superpowers/specs/2026-04-16-skill-reference-loading-design.md` (progressive loading v1)
> Depends on: `docs/superpowers/specs/2026-04-15-skills-system-design.md` (skills system base)

## Background

Skill Progressive Loading v1 implemented a three-stage model: discover (metadata only), invoke (SKILL.md body + reference index), reference-read (model reads files on demand via `read_file`).

In practice this model has two critical gaps:

1. **Skill knowledge effectiveness**: The LLM rarely reads reference files despite seeing the `<reference-files>` index. It falls back to pre-trained knowledge, producing output that ignores skill-specific rules and style guides.

2. **Skill observability**: Skill activation produces no visible log output when triggered through the prompt catalog. Users cannot tell whether a skill was used or what knowledge it contributed.

Evidence: A request to restyle an HTML page using an Apple-design skill showed the model listing Apple design principles from its own training data without reading any skill reference files. The `[Runtime]` logs showed only tool dispatch, with zero skill-related events.

## Non-goals

- Changing superpowers skill file formats or SKILL.md conventions
- Automatic summarization or truncation of reference content
- Adding skill-specific renderer methods
- Changing the stable prompt cache mechanism
- Conditional or automatic skill activation

## Design

### Part 1: Inline Reference Loading

When a skill is activated via `/skills use <id>`, all declared reference files are read and their content is inlined directly into the `<active-skill>` system message.

#### Data model

`SkillContent` gains a `reference_bodies` field:

```python
@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str
    reference_bodies: dict[str, str]  # {prompt_path: file_content}
```

The key is `prompt_path` (relative to working dir), the value is the file's full text content.

#### Registry changes

`SkillRegistry.load()` now reads all reference file bodies after loading SKILL.md:

```python
def load(self, skill_id: str) -> SkillContent:
    if skill_id in self._cache:
        return self._cache[skill_id]
    if skill_id not in self._catalog:
        raise ValueError(f"unknown skill: {skill_id!r}")
    meta = self._catalog[skill_id]
    _, body = _parse_skill_markdown(meta.skill_file)

    ref_bodies: dict[str, str] = {}
    for ref in meta.references:
        try:
            ref_bodies[ref.prompt_path] = ref.abs_path.read_text(encoding="utf-8")
        except Exception:
            pass

    content = SkillContent(
        meta=meta,
        body=body,
        content_digest=_digest_text(body),
        reference_bodies=ref_bodies,
    )
    self._cache[skill_id] = content
    return content
```

Failed reads are silently skipped. A missing reference file should not prevent skill activation.

#### Prompt rendering changes

`PromptAssembler.build_active_skill_messages()` inlines reference bodies instead of rendering just the index:

```xml
<active-skills>
  <active-skill id="apple-design">
    <instruction>
      ...SKILL.md body...
    </instruction>
    <reference-files>
      <file path=".harness/skills/apple-design/style-system.md">
        ...full content of style-system.md...
      </file>
    </reference-files>
  </active-skill>
</active-skills>
```

The `_render_reference_files()` function changes from rendering just `purpose` lines to rendering the full file content from `content.reference_bodies`.

#### Budget control

The character budget check in `/skills use` must account for reference bodies:

```python
def _skill_total_chars(content: SkillContent) -> int:
    return len(content.body) + sum(len(v) for v in content.reference_bodies.values())

total_chars = sum(
    _skill_total_chars(registry.load(sid))
    for sid in state.active_skills
) + _skill_total_chars(content)
```

This ensures `MAX_TOTAL_SKILL_CHARS` (currently 24000) covers the full inline cost.

### Part 2: Skill Observability via Runtime Logs

Skill lifecycle events emit `[Skill]` prefixed logs in the same cyan ANSI style as existing `[Runtime]` logs.

#### Log format

```
[Skill] 激活 apple-design (3 refs, 4,200 chars 内联)
[Skill] 停用 apple-design
[Skill] 重新加载 skills 目录 (2 skills discovered)
```

#### Log locations

All logs are emitted in `core/session/commands.py` at the point where the event occurs:

- `/skills use <id>` success path: log activation with ref count and inline chars
- `/skills off <id>` success path: log deactivation
- `/skills reload` success path: log reload with discovered skill count

Implementation uses `sys.stdout.write` with `\033[36m` ANSI cyan, matching the `[Runtime]` style:

```python
sys.stdout.write(
    f"\033[36m[Skill] 激活 {skill_id}"
    f" ({ref_count} refs, {ref_chars:,} chars 内联)\033[0m\n"
)
```

#### Why not renderer

Skill activation is a session-level event, not a tool-call result. The renderer (`RichRenderer`) handles tool output and model text. Adding skill methods to the renderer would couple it to skill lifecycle concerns that belong in the command layer. Direct stdout with consistent formatting is simpler and matches the existing Runtime log pattern.

## File changes

### Modified files

| File | Change |
|------|--------|
| `core/skills/models.py` | Add `reference_bodies: dict[str, str]` to `SkillContent` |
| `core/skills/registry.py` | `load()` reads reference file bodies into `reference_bodies` |
| `core/prompt/assembler.py` | `build_active_skill_messages()` inlines reference content; `_render_reference_files()` reads from `reference_bodies` |
| `core/session/commands.py` | Budget check includes reference chars; `[Skill]` logs on use/off/reload |

### Unchanged files

- `core/skills/__init__.py` — exports unchanged
- `core/session/engine.py` — bootstrap and submit paths unchanged
- `core/session/view_builder.py` — message assembly unchanged
- `core/tools/runtime.py` — no skill-specific changes needed
- `core/ui/renderer.py` — no skill rendering methods added
- `core/query/loop.py` — query loop unchanged

### Test updates

| Test file | Change |
|-----------|--------|
| `tests/session/test_skills_registry.py` | Verify `load()` returns `reference_bodies` with correct content; verify failed reads are skipped |
| `tests/session/test_prompt_assembler.py` | Verify active skill message contains reference body text, not just index |
| `tests/session/test_engine_commands.py` | Verify budget check includes reference chars; verify `/skills use` rejection when budget exceeded |

## Risks

1. **Token inflation**: Large reference files increase prompt size. Mitigated by `MAX_TOTAL_SKILL_CHARS` budget cap and the existing 24000 char limit.
2. **Reference read failures**: Handled by silent skip. If a critical reference is missing, the skill body should contain enough standalone instruction (per the SKILL.md authoring constraint).
3. **Cache invalidation**: `SkillRegistry.load()` caches by skill_id. If a reference file changes on disk without SKILL.md changing, the cache will be stale. This is an existing limitation that `/skills reload` already addresses (it calls `discover()` which resets the cache).

## Success criteria

1. Activating a skill with references causes the full reference content to appear in the model-facing system message
2. The `[Skill]` log line is visible at activation, showing ref count and inline character count
3. Budget enforcement correctly rejects activation that would exceed `MAX_TOTAL_SKILL_CHARS` including reference bodies
4. Model output shows measurable improvement in following skill-specific rules compared to v1 progressive loading
