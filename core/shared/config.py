"""全局配置：从环境变量或 .env 文件加载，所有模块通过此文件读取配置。"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)

# ─── 必须配置 ───────────────────────────────────────────────────────────────

# Anthropic 兼容 API 密钥，不设置则 API 调用会失败
API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── 模型配置 ───────────────────────────────────────────────────────────────

# 模型标识符，决定调用哪个模型
MODEL: str = os.environ.get("ANTHROPIC_MODEL", "kimi-k2.5")

# API 端点，适配不同的 Anthropic 兼容服务（Kimi、GLM、Claude 等）
BASE_URL: str = os.environ.get("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")

# 模型单次响应的最大 token 数，控制输出长度上限
MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

# ─── Thinking 配置 ─────────────────────────────────────────────────────────

# thinking 模式：auto=自适应（模型自决定思考深度），enabled=固定预算，disabled=关闭
THINKING_MODE: str = os.environ.get("LLM_THINKING_MODE", "auto")

# enabled 模式下的 thinking token 预算上限，auto 模式下由模型自行决定
THINKING_BUDGET: int = int(os.environ.get("LLM_THINKING_BUDGET", "4096"))

# 是否在终端显示 thinking 内容（蓝框中的思考过程）
SHOW_THINKING: bool = os.environ.get("LLM_SHOW_THINKING", "true").lower() in ("true", "1", "yes")

# 是否将 thinking 内容持久化到对话历史，供后续轮次引用
PERSIST_THINKING: bool = os.environ.get("LLM_PERSIST_THINKING", "true").lower() in ("true", "1", "yes")

# 持久化 thinking 文本的最大字符数，防止单轮 thinking 过大
MAX_REASONING_CHARS: int = int(os.environ.get("LLM_MAX_REASONING_CHARS", "200000"))

# ─── 运行时配置 ─────────────────────────────────────────────────────────────

# bash 命令执行超时时间（秒）
BASH_TIMEOUT: int = int(os.environ.get("BASH_TIMEOUT", "120"))

# 单次 query 允许的最大工具调用轮次，防止无限循环
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "300"))

# 工具输出截断阈值（字符数），防止超大输出撑爆上下文
MAX_OUTPUT_CHARS: int = int(os.environ.get("MAX_OUTPUT_CHARS", "100000"))
