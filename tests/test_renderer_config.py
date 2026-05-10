from __future__ import annotations

import importlib

from rich.console import Console


def test_renderer_uses_markdown_theme_from_project_config(monkeypatch) -> None:
    monkeypatch.setattr("dotenv.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("UI_MARKDOWN_CODE_THEME", "bw")
    monkeypatch.setenv("UI_MARKDOWN_INLINE_CODE_STYLE", "bold #ff0000")
    monkeypatch.setenv("UI_MARKDOWN_CODE_BLOCK_STYLE", "#00ff00")

    import core.shared.config as cfg
    import core.ui.renderer as renderer_mod

    importlib.reload(cfg)
    importlib.reload(renderer_mod)

    console = Console(record=True, width=80)
    renderer_mod.render_markdown(console, "`x = 1`\n\n```text\nhello\n```")

    html = console.export_html(inline_styles=True)

    assert "#ff0000" in html
    assert "background-color: #000000" not in html
    assert "background-color: #272822" not in html
