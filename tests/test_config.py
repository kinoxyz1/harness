"""测试配置层：环境变量读取与默认值。"""
import importlib
import pytest


@pytest.fixture(autouse=True)
def _reload_config(monkeypatch):
    """每个测试前重置 config 模块，确保读到最新环境变量。

    跳过 load_dotenv 以防止 .env 文件覆盖 monkeypatch 设置的环境变量。
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda **kwargs: None)
    for var in [
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL",
        "LLM_MAX_TOKENS", "LLM_THINKING_MODE", "LLM_THINKING_BUDGET",
        "LLM_SHOW_THINKING", "BASH_TIMEOUT",
    ]:
        monkeypatch.delenv(var, raising=False)
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


def test_base_url_default():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.BASE_URL == "https://api.kimi.com/coding/"


def test_thinking_mode_default():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.THINKING_MODE == "auto"


def test_thinking_mode_override(monkeypatch):
    monkeypatch.setenv("LLM_THINKING_MODE", "disabled")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.THINKING_MODE == "disabled"


def test_preserves_existing_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    monkeypatch.setenv("LLM_THINKING_MODE", "enabled")
    monkeypatch.setenv("BASH_TIMEOUT", "60")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MAX_TOKENS == 4096
    assert cfg.THINKING_MODE == "enabled"
    assert cfg.BASH_TIMEOUT == 60
