from __future__ import annotations

from pathlib import Path


def test_load_project_env_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_MODEL=from-dotenv\n", encoding="utf-8")

    from core.shared.env_loader import load_project_env

    load_project_env(env_file)

    assert __import__("os").environ["ANTHROPIC_MODEL"] == "from-dotenv"


def test_load_project_env_does_not_override_existing_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_MODEL=from-dotenv\n", encoding="utf-8")

    from core.shared.env_loader import load_project_env

    load_project_env(env_file)

    assert __import__("os").environ["ANTHROPIC_MODEL"] == "from-env"


def test_agent_entrypoint_loads_env_before_core_imports():
    content = Path("01_agent_loop.py").read_text(encoding="utf-8")
    load_idx = content.index('load_project_env(Path(__file__).with_name(".env"))')
    core_import_idx = content.index("from core.llm.client import ModelGateway")
    assert load_idx < core_import_idx
