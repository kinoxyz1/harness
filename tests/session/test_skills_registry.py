from __future__ import annotations
from pathlib import Path
from core.skills.registry import SkillRegistry, compute_skills_revision


def write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_discovers_standard_skill_metadata(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
when-to-use: Use when the user wants a finished report
---

Skill body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    meta = catalog["analysis-report"]
    assert meta.skill_id == "analysis-report"
    assert meta.name == "Analysis Report"
    assert meta.description == "Generate HTML reports"
    assert meta.when_to_use == "Use when the user wants a finished report"


def test_rejects_skill_without_description(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "broken-skill",
        """---
name: Broken
---

Skill body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    assert "broken-skill" not in catalog
    assert "broken-skill" in registry.errors


def test_rejects_skill_without_name(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "no-name",
        """---
description: Has description but no name
---

Body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    assert "no-name" not in catalog
    assert "no-name" in registry.errors


def test_load_returns_body_and_digest(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
---

Body line 1
Body line 2
""",
    )

    registry = SkillRegistry()
    registry.discover(tmp_path)
    content = registry.load("analysis-report")

    assert content.body == "Body line 1\nBody line 2"
    assert content.content_digest


def test_compute_revision_changes_when_mtime_changes(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        """---
name: Analysis Report
description: Generate HTML reports
---

Body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)
    first = compute_skills_revision(catalog)
    skill_file = tmp_path / "analysis-report" / "SKILL.md"
    skill_file.touch()
    catalog = registry.discover(tmp_path)
    second = compute_skills_revision(catalog)

    assert first != second


def test_skills_dir_stored_after_discover(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "test-skill",
        """---
name: Test
description: Test
---

Body
""",
    )

    registry = SkillRegistry()
    assert registry.skills_dir is None
    registry.discover(tmp_path)
    assert registry.skills_dir == tmp_path


def test_discover_skips_non_directory_entries(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("not a skill", encoding="utf-8")
    write_skill(
        tmp_path,
        "valid-skill",
        """---
name: Valid
description: A valid skill
---

Body
""",
    )

    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)

    assert "valid-skill" in catalog
    assert "readme.txt" not in catalog


def test_discover_empty_dir_returns_empty(tmp_path: Path) -> None:
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path)
    assert catalog == {}


def test_discover_nonexistent_dir_returns_empty(tmp_path: Path) -> None:
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / "nonexistent")
    assert catalog == {}


def test_load_raises_for_unknown_skill(tmp_path: Path) -> None:
    registry = SkillRegistry()
    registry.discover(tmp_path)

    import pytest
    with pytest.raises(ValueError, match="unknown skill"):
        registry.load("nonexistent")


def test_discovers_declared_skill_references(tmp_path: Path) -> None:
    """Discover parses references from SKILL.md frontmatter."""
    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate HTML reports
references:
  - path: style-system.md
    purpose: CSS rules
  - path: cards/catalog.md
    purpose: Card catalog
---

Main workflow instructions.
""",
        encoding="utf-8",
    )
    (skill_dir / "style-system.md").write_text("body", encoding="utf-8")
    (skill_dir / "cards").mkdir()
    (skill_dir / "cards" / "catalog.md").write_text("cards", encoding="utf-8")

    registry = SkillRegistry()
    catalog = registry.discover(
        tmp_path / ".harness" / "skills",
        working_dir=tmp_path,
    )

    refs = catalog["analysis-report"].references
    assert [ref.path for ref in refs] == ["style-system.md", "cards/catalog.md"]
    assert [ref.prompt_path for ref in refs] == [
        ".harness/skills/analysis-report/style-system.md",
        ".harness/skills/analysis-report/cards/catalog.md",
    ]
    assert refs[0].abs_path == (
        tmp_path / ".harness" / "skills" / "analysis-report" / "style-system.md"
    ).resolve()


def test_rejects_reference_that_escapes_skill_dir(tmp_path: Path) -> None:
    """References with paths that escape skill_dir should be rejected."""
    skill_dir = tmp_path / ".harness" / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Bad Skill
description: Broken references
references:
  - path: ../secrets.md
    purpose: should fail
---

Body
""",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    catalog = registry.discover(
        tmp_path / ".harness" / "skills",
        working_dir=tmp_path,
    )

    assert "bad-skill" not in catalog
    assert "bad-skill" in registry.errors


def test_load_still_returns_only_skill_body(tmp_path: Path) -> None:
    """load() body must NOT contain reference content, but reference_bodies should."""
    skill_dir = tmp_path / ".harness" / "skills" / "analysis-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Analysis Report
description: Generate HTML reports
references:
  - path: style-system.md
    purpose: CSS rules
---

Step 1
Step 2
""",
        encoding="utf-8",
    )
    (skill_dir / "style-system.md").write_text("css rules body", encoding="utf-8")

    registry = SkillRegistry()
    registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    content = registry.load("analysis-report")

    assert content.body == "Step 1\nStep 2"
    assert "style-system.md" not in content.body
    assert content.reference_bodies == {
        ".harness/skills/analysis-report/style-system.md": "css rules body",
    }


def test_load_skips_missing_reference_files(tmp_path: Path) -> None:
    """load() silently skips reference files that cannot be read."""
    skill_dir = tmp_path / ".harness" / "skills" / "fragile-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Fragile
description: Has a missing reference
references:
  - path: exists.md
    purpose: exists
  - path: missing.md
    purpose: gone
---

Body
""",
        encoding="utf-8",
    )
    (skill_dir / "exists.md").write_text("present content", encoding="utf-8")
    # missing.md is intentionally NOT created

    registry = SkillRegistry()
    registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    content = registry.load("fragile-skill")

    assert content.reference_bodies == {
        ".harness/skills/fragile-skill/exists.md": "present content",
    }


def test_load_returns_empty_reference_bodies_for_skill_without_refs(tmp_path: Path) -> None:
    """load() returns empty dict for skills with no references."""
    write_skill(
        tmp_path,
        "plain-skill",
        """---
name: Plain
description: No references
---

Plain body
""",
    )

    registry = SkillRegistry()
    registry.discover(tmp_path)
    content = registry.load("plain-skill")

    assert content.reference_bodies == {}
