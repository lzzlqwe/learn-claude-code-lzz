#!/usr/bin/env python3
"""
s11: Error Recovery — 三条恢复路径 + 指数退避 + 模型切换。

  ASCII 流程图:
  messages → prompt assembly → compress+load → [try] LLM [except] → tools → loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? → compact
                                              escalate /    429/529? → backoff
                                              continue      other? → log + exit

核心模式（3 条恢复路径）:
  Path 1: max_tokens 截断 → 先升级到 64K → 再不有用 continuation prompt（最多 3 次）
  Path 2: prompt_too_long → reactive_compact（tail 保留）→ 重试（1 次）
  Path 3: 429/529 → 指数退避 + 抖动（最多 10 次），连续 529 则切换 fallback 模型

s10 → s11 关键变化:
  + RecoveryState 类跟踪恢复状态（escalation/compact/529/model）
  + with_retry() 指数退避包装器（429/529 重试）
  + max_tokens 升级: 8K → 64K → continuation prompt
  + 529 连续 3 次 → 切换 FALLBACK_MODEL_ID
  + is_prompt_too_long_error() 精确错误检测
  + reactive_compact 教学简化版（保留最后 5 条消息）

运行: python s11_error_recovery/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, time, random, json
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ── Constants ──

ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS = 8000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# ═══════════════════════════════════════════════════════════
#  FROM s10: Prompt 组装 —— PROMPT_SECTIONS + assemble + cache
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """根据 context 拼接 prompt。始终加载 identity/tools/workspace，按需加载 memory。"""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    """带缓存的 prompt 获取。json.dumps 做确定性 key，命中则跳过拼接。"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  基本工具实现 —— 3 个工具（骨架版，聚焦 error recovery 教学）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 Shell 命令。120 秒超时 + 输出截断至 50000 字符。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。参数: path=文件路径, limit=可选行数限制。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入内容到文件。自动创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ═══════════════════════════════════════════════════════════
#  NEW in s11: Error Recovery —— 三条恢复路径
#  Path 1: max_tokens → 升级 8K→64K → continuation prompt
#  Path 2: prompt_too_long → reactive_compact → 重试
#  Path 3: 429/529 → 指数退避 + jitter + 模型切换
# ═══════════════════════════════════════════════════════════

class RecoveryState:
    """跟踪循环中的恢复状态。
    has_escalated: 是否已升级过 max_tokens
    recovery_count: continuation prompt 次数
    consecutive_529: 连续 529 错误计数（≥3 切模型）
    has_attempted_reactive_compact: prompt_too_long 只试一次
    current_model: 当前使用的模型名称
    """
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """指数退避 + jitter。如果 API 返回 Retry-After 则优先使用。
    公式: min(500ms * 2^attempt, 32s) + 25% 随机抖动。"""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """包装器: 429/529 自动重试（指数退避），其他错误向外抛出。"""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # 429 rate limit → 指数退避重试
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 529 overloaded → 指数退避 + 连续 3 次切换 fallback 模型
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 非瞬时错误 → 向外抛出，由外层 Path 2 处理
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """检查 API 错误是否表示 prompt 过长（多种错误消息格式）。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(messages: list) -> list:
    """应急压缩 —— 教学简化版保留最后 5 条消息。
    真实 CC 会调 LLM 生成压缩摘要后重试；教学版简化为尾部保留。"""
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ── Context（从 s10 继承）──

def update_context(context: dict, messages: list) -> dict:
    """根据真实状态更新 context: 工具列表 / 工作目录 / 记忆索引是否存在。"""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text(encoding="utf-8").strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═══════════════════════════════════════════════════════════
#  agent_loop — s11 核心: LLM 调用包裹在 try/except 中，三条恢复路径
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """主循环 —— with_retry 处理 429/529，外层处理 max_tokens 和 prompt_too_long。"""
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ── LLM 调用: with_retry 处理 429/529，外层处理其余错误 ──
        try:
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # Path 2: prompt_too_long → reactive_compact（仅一次）
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return

            # 无法恢复的错误
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ── Path 1: max_tokens 截断 → 升级或 continuation prompt ──
        if response.stop_reason == "max_tokens":
            # 第一次: 不追加截断输出，直接升级到 64K 重新请求
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue
            # 64K 仍然截断: 保存截断输出 + 注入 continuation prompt 续写
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        # 正常: 追加 assistant 回复
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        # ── 工具执行 ──
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 粗体品红 = 标签，品红 = 内容
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    """交互入口。流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s11 >>）
      3. 进入 agent_loop（含三条错误恢复路径）
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        turn_start = len(history)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for msg in history[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    # 蓝色显示模型最终回复
                    print(f"\033[34m{block.text}\033[0m")
        print()
