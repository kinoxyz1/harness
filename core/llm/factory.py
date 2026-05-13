from __future__ import annotations

import os

import anthropic

from ..shared.config import API_KEY, BASE_URL, LLM_REQUEST_TIMEOUT


def create_llm_client() -> anthropic.Anthropic:
    # Anthropic SDK 会自动读取 ANTHROPIC_AUTH_TOKEN 环境变量并添加
    # Authorization: Bearer 头，导致向第三方兼容 API（如 Kimi）发送错误的认证信息。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 受限环境中该变量可能指向不可写文件，导致 httpx/ssl 初始化直接失败。
    os.environ.pop("SSLKEYLOGFILE", None)

    kwargs: dict = {"api_key": API_KEY, "timeout": LLM_REQUEST_TIMEOUT}
    if BASE_URL:
        kwargs["base_url"] = BASE_URL
    return anthropic.Anthropic(**kwargs)
