"""测试配置层迁移：新环境变量读取、旧变量已移除。"""
import os
import importlib
import pytest


@pytest.fixture(autouse=True)
def _reload_config(monkeypatch):
    """每个测试前重置 config 模块，确保读到最新环境变量。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_SHOW_THINKING", raising=False)
    import core.shared.config as cfg
    importlib.reload(cfg)
    yield
    importlib.reload(cfg)


def test_reads_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.API_KEY == "sk-test-123"


def test_reads_model_id(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6-20250514")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MODEL == "claude-sonnet-4-6-20250514"


def test_default_model_id():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MODEL == "kimi-k2.5"


def test_reads_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kimi.example.com/v1")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.BASE_URL == "https://kimi.example.com/v1"


def test_base_url_empty_by_default():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.BASE_URL == "https://api.kimi.com/coding/"


def test_preserves_existing_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    monkeypatch.setenv("BASH_TIMEOUT", "60")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MAX_TOKENS == 4096
    assert cfg.ENABLE_THINKING is False
    assert cfg.BASH_TIMEOUT == 60
