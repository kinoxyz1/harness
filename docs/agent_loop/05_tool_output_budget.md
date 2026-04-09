# 05 - 工具结果截断

> 对应差异全景 #5：上下文管理 — 工具结果预算 `applyToolResultBudget()`

## 问题

当前工具输出原样塞入 messages，没有任何截断：

```python
# tools.py — run_bash 的输出直接返回
out = (r.stdout + r.stderr).strip()
return out if out else "(no output)"
```

一个 `cat large_file.json` 或 `find / -name "*.py"` 的输出可能几万甚至几十万字符，直接塞入 messages 后果：

1. **撑爆上下文窗口** — 百炼 200k token 窗口被一个命令填满
2. **后续 API 调用费用暴增** — 这些无用内容每次都作为 input token 计费
3. **模型注意力被稀释** — 大量无关输出干扰模型的判断

## 设计参考

### Claude Code 的做法

Claude Code 在 `applyToolResultBudget()` 中限制每个工具结果的大小。在消息进入 API 调用前，对超长工具输出进行截断。

### 核心原则

1. **截断发生在工具执行后** — 先拿到完整输出，截断后存入 messages
2. **保留首尾** — 开头通常是最相关的，末尾可能有错误信息
3. **告知模型输出被截断** — 让模型知道它没有看到完整内容

## 实现方案

### config.py — 新增配置

```python
MAX_TOOL_OUTPUT_CHARS: int = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", "30000"))
```

> 30000 字符约 7500-10000 token（中文约 3-4 字符/token，英文约 4 字符/token），在 200k 窗口中留足空间。

### tools.py — 截断函数

```python
from .config import MAX_TOOL_OUTPUT_CHARS

def _truncate_output(output: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """截断超长的工具输出，保留首尾。"""
    if len(output) <= max_chars:
        return output

    head_size = int(max_chars * 0.7)  # 保留 70% 的开头
    tail_size = int(max_chars * 0.2)  # 保留 20% 的结尾
    # 中间 10% 用于截断提示

    total = len(output)
    head = output[:head_size]
    tail = output[-tail_size:]
    omitted = total - head_size - tail_size

    return (
        f"{head}\n"
        f"\n... (已省略 {omitted} 字符) ...\n"
        f"\n{tail}"
    )
```

### tools.py — 在 run_bash 中使用

```python
def run_bash(command: str) -> str:
    if _is_blocked(command):
        return "Error: Command blocked for safety."

    if _needs_confirmation(command):
        answer = input(f"\033[31m⚠ Command '{command}' looks dangerous. Run anyway? [y/N]: \033[0m")
        if answer.strip().lower() not in ("y", "yes"):
            return "Error: Command cancelled by user."

    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=BASH_TIMEOUT,
        )
        out = (r.stdout + r.stderr).strip()
        if not out:
            return "(no output)"
        return _truncate_output(out)
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({BASH_TIMEOUT}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
```

## 后续扩展

- **按命令类型动态调整预算**：`ls` 结果可以多留，`cat` 结果需要更激进截断
- **工具结果摘要**：CC 的 `emitToolUseSummaries`，对长输出生成摘要替代原文

## 参考资料

- Claude Code 源码：`applyToolResultBudget()` — 工具结果预算管理
