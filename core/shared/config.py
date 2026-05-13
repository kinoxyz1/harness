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

# 单次 LLM HTTP 请求的客户端超时（秒）。应显著小于上游网关超时，避免本地无限等待。
LLM_REQUEST_TIMEOUT: float = float(os.environ.get("LLM_REQUEST_TIMEOUT", "180"))

# 遇到 502/503/504 这类瞬时网关错误时的最大重试次数。
LLM_RETRY_ATTEMPTS: int = int(os.environ.get("LLM_RETRY_ATTEMPTS", "2"))

# 瞬时网关错误重试的基础退避时间（秒）。
LLM_RETRY_BACKOFF_SECONDS: float = float(os.environ.get("LLM_RETRY_BACKOFF_SECONDS", "1.5"))

# 模型单次响应的最大 token 数，控制输出长度上限
MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

# ─── Thinking 配置 ─────────────────────────────────────────────────────────

# thinking 模式：auto=自适应（模型自决定思考深度），enabled=固定预算，disabled=关闭
THINKING_MODE: str = os.environ.get("LLM_THINKING_MODE", "enabled")

# enabled 模式下的 thinking token 预算上限，auto 模式下由模型自行决定
THINKING_BUDGET: int = int(os.environ.get("LLM_THINKING_BUDGET", "254"))

# 是否在终端显示 thinking 内容（蓝框中的思考过程）
SHOW_THINKING: bool = os.environ.get("LLM_SHOW_THINKING", "true").lower() in ("true", "1", "yes")

# 是否将 thinking 内容持久化到对话历史，供后续轮次引用
PERSIST_THINKING: bool = os.environ.get("LLM_PERSIST_THINKING", "true").lower() in ("true", "1", "yes")

# 持久化 thinking 文本的最大字符数，防止单轮 thinking 过大
MAX_REASONING_CHARS: int = int(os.environ.get("LLM_MAX_REASONING_CHARS", "200000"))

# ─── 运行时配置 ─────────────────────────────────────────────────────────────

# bash 命令执行超时时间（秒）
BASH_TIMEOUT: int = int(os.environ.get("BASH_TIMEOUT", "30"))

# 单次 query 允许的最大工具调用轮次，防止无限循环
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "300"))

# 工具输出截断阈值（字符数），防止超大输出撑爆上下文
MAX_OUTPUT_CHARS: int = int(os.environ.get("MAX_OUTPUT_CHARS", "100000"))

# 上下文窗口大小（token 数），决定水位线阈值。不同模型上下文窗口不同：
# Claude Sonnet/Opus: 200000, Kimi K2.5: 131072, GLM: 128000
# 必须与实际模型匹配，否则上下文管理阈值会错位
CONTEXT_WINDOW_TOKENS: int = int(os.environ.get("CONTEXT_WINDOW_TOKENS", "2000000"))

# ─── Skill 匹配配置 ────────────────────────────────────────────────────────

# 是否使用 LLM 进行 skill 相关性匹配（增加一次轻量 API 调用，提升匹配准确率）
# false（默认）: 使用关键词精确子串匹配
# true: 使用 LLM 分类调用，支持语义匹配
SKILL_LLM_MATCH: bool = os.environ.get("SKILL_LLM_MATCH", "false").lower() in ("true", "1", "yes")

# ─── 终端渲染配置 ───────────────────────────────────────────────────────────

# Markdown 代码块使用的 Rich/Pygments 主题，控制 fenced code block 的配色与背景
UI_MARKDOWN_CODE_THEME: str = os.environ.get("UI_MARKDOWN_CODE_THEME", "friendly")

# 行内代码的终端样式，使用 Rich style 语法
UI_MARKDOWN_INLINE_CODE_STYLE: str = os.environ.get("UI_MARKDOWN_INLINE_CODE_STYLE", "bold cyan")

# 代码块文本的基础终端样式，使用 Rich style 语法
UI_MARKDOWN_CODE_BLOCK_STYLE: str = os.environ.get("UI_MARKDOWN_CODE_BLOCK_STYLE", "cyan")

# ─── 工具结果 Offloader 配置 ─────────────────────────────────────────────────

# 工具结果超过此字符数时落盘到 .harness/sessions/<id>/tool-results/
TOOL_RESULT_PERSIST_THRESHOLD: int = int(os.environ.get("TOOL_RESULT_PERSIST_THRESHOLD", "2000"))

# bash 工具结果的独立落盘阈值（通常输出较短，需要更低的阈值）
BASH_RESULT_PERSIST_THRESHOLD: int = int(os.environ.get("BASH_RESULT_PERSIST_THRESHOLD", "1000"))

# 所有工具结果聚合字符数上限，超过时从最大的开始强制落盘
TOOL_RESULTS_AGGREGATE_BUDGET: int = int(os.environ.get("TOOL_RESULTS_AGGREGATE_BUDGET", "30000"))

# 落盘后保留在上下文中的预览字节数
TOOL_RESULT_PREVIEW_BYTES: int = int(os.environ.get("TOOL_RESULT_PREVIEW_BYTES", "10000"))
