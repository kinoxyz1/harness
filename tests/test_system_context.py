from __future__ import annotations

from core.prompt.system_context import get_system_context


def test_system_context_loads_profile_and_soul_files(tmp_path) -> None:
    context_dir = tmp_path / ".harness" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "PROFILE.md").write_text("user-profile", encoding="utf-8")
    (context_dir / "SOUL.md").write_text("soul-style", encoding="utf-8")

    content = get_system_context(str(tmp_path))

    assert "user-profile" in content
    assert "soul-style" in content


def test_system_context_falls_back_to_context_user_name(tmp_path) -> None:
    context_dir = tmp_path / ".harness" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "USER.md").write_text("legacy-user-context", encoding="utf-8")

    content = get_system_context(str(tmp_path))

    assert "legacy-user-context" in content


def test_system_context_falls_back_to_legacy_context_names(tmp_path) -> None:
    context_dir = tmp_path / ".harness" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "identity.md").write_text("legacy-identity", encoding="utf-8")
    (context_dir / "style.md").write_text("legacy-style", encoding="utf-8")

    content = get_system_context(str(tmp_path))

    assert "legacy-identity" in content
    assert "legacy-style" in content
