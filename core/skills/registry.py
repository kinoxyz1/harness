from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from typing import Any
import yaml
from .models import SkillContent, SkillMeta, SkillReference


def _parse_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"SKILL.md missing frontmatter: {path}")
    end = text.find("---", 3)
    if end == -1:
        raise ValueError(f"SKILL.md frontmatter not closed: {path}")
    meta = yaml.safe_load(text[3:end]) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"frontmatter must be a mapping: {path}")
    body = text[end + 3 :].strip()
    return meta, body


def _digest_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _resolve_reference(
    *,
    skill_dir: Path,
    working_dir: Path,
    raw_path: str,
    purpose: str | None,
) -> SkillReference:
    abs_path = (skill_dir / raw_path).resolve()
    try:
        abs_path.relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"reference escapes skill dir: {raw_path}") from exc
    # Do NOT check is_file() here — existence is checked at load time
    prompt_path = os.path.relpath(abs_path, working_dir.resolve())
    return SkillReference(
        path=raw_path,
        purpose=purpose,
        abs_path=abs_path,
        prompt_path=prompt_path,
    )


def _parse_references(
    meta_dict: dict[str, Any],
    *,
    skill_dir: Path,
    working_dir: Path,
) -> list[SkillReference]:
    raw_refs = meta_dict.get("references") or []
    if not isinstance(raw_refs, list):
        raise ValueError("references must be a list")
    refs: list[SkillReference] = []
    for entry in raw_refs:
        if not isinstance(entry, dict):
            raise ValueError("each reference must be a mapping")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("reference path must be a non-empty string")
        purpose = entry.get("purpose")
        if purpose is not None and not isinstance(purpose, str):
            raise ValueError("reference purpose must be a string")
        refs.append(
            _resolve_reference(
                skill_dir=skill_dir,
                working_dir=working_dir,
                raw_path=raw_path,
                purpose=purpose,
            )
        )
    return refs


def _auto_discover_refs(skill_dir: Path) -> dict[str, str]:
    """自动发现 skill 目录下的所有 .md 文件（排除 SKILL.md）。

    当 skill 的 frontmatter 未声明 references 时调用，
    兼容从 GitHub 等来源获取的、不含 references 字段的 skill。
    """
    ref_bodies: dict[str, str] = {}
    if not skill_dir.is_dir():
        return ref_bodies
    for entry in sorted(skill_dir.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.endswith(".md"):
            continue
        if entry.name == "SKILL.md":
            continue
        try:
            ref_bodies[entry.name] = entry.read_text(encoding="utf-8")
        except Exception:
            pass
    return ref_bodies


def compute_skills_revision(catalog: dict[str, SkillMeta]) -> str:
    lines: list[str] = []
    for skill_id in sorted(catalog):
        meta = catalog[skill_id]
        mtime_ns = meta.skill_file.stat().st_mtime_ns
        lines.append(f"{skill_id}:{mtime_ns}")
    return _digest_text("\n".join(lines))


class SkillRegistry:
    """Discover local SKILL.md files and load their content on demand.

    Usage: call discover(skills_dir) to scan and catalog, then
    load(skill_id) to read a skill's body and compute its digest.
    Cache is invalidated on each discover() call.
    """

    def __init__(self) -> None:
        self._catalog: dict[str, SkillMeta] = {}
        self._cache: dict[str, SkillContent] = {}
        self.errors: dict[str, str] = {}
        self.skills_dir: Path | None = None
        self.working_dir: Path | None = None

    def discover(
        self, skills_dir: Path, *, working_dir: Path | None = None
    ) -> dict[str, SkillMeta]:
        self._catalog = {}
        self._cache = {}
        self.errors = {}
        self.skills_dir = skills_dir
        self.working_dir = (working_dir or Path.cwd()).resolve()

        if not skills_dir.is_dir():
            return {}

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                self.errors[skill_dir.name] = "SKILL.md missing"
                continue
            try:
                meta_dict, _ = _parse_skill_markdown(skill_file)
                name = str(meta_dict["name"])
                description = str(meta_dict["description"])
                when_to_use = meta_dict.get("when-to-use")
                references = _parse_references(
                    meta_dict,
                    skill_dir=skill_dir,
                    working_dir=self.working_dir,
                )
            except Exception as exc:
                self.errors[skill_dir.name] = str(exc)
                continue
            self._catalog[skill_dir.name] = SkillMeta(
                skill_id=skill_dir.name,
                name=name,
                description=description,
                when_to_use=str(when_to_use) if when_to_use else None,
                skill_dir=skill_dir,
                skill_file=skill_file,
                references=references,
            )
        return dict(self._catalog)

    def load(self, skill_id: str) -> SkillContent:
        if skill_id in self._cache:
            return self._cache[skill_id]
        if skill_id not in self._catalog:
            raise ValueError(f"unknown skill: {skill_id!r}")
        meta = self._catalog[skill_id]
        _, body = _parse_skill_markdown(meta.skill_file)

        ref_bodies: dict[str, str] = {}
        if meta.references:
            for ref in meta.references:
                try:
                    ref_bodies[ref.prompt_path] = ref.abs_path.read_text(encoding="utf-8")
                except Exception:
                    pass
        else:
            ref_bodies = _auto_discover_refs(meta.skill_dir)

        content = SkillContent(
            meta=meta,
            body=body,
            content_digest=_digest_text(body),
            reference_bodies=ref_bodies,
        )
        self._cache[skill_id] = content
        return content
