#!/usr/bin/env python3
"""
s04: Hooks — 把扩展逻辑从循环中移出来，挂到 hook 上。

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

核心模式（4 步）:
  1. 5 个工具函数 + schema + 分发映射（继承 s02/s03）
  2. HOOKS 注册表 + register_hook() + trigger_hooks()（s04 新增）
  3. 5 个 hook 回调覆盖完整 agent cycle（s04 新增）
  4. agent_loop: 调 LLM → trigger_hooks → 执行 → trigger_hooks → 回传

s03 → s04 关键变化:
  s03: if not check_permission(block): continue   ← 硬编码在循环里
  s04: if trigger_hooks("PreToolUse", block): ...  ← 交给 hook 系统
  + PostToolUse hook（执行后处理）
  + Stop hook（退出前拦截）
  + UserPromptSubmit hook（输入后注入上下文）

"""

import os, subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 (unchanged): 工具实现 —— 5 个工具函数
#  读/写/编辑受 safe_path 路径约束，bash 有超时+截断保护
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令。安全措施: 120 秒超时 + 输出截断至 50000 字符。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。
    参数:
        path: 相对于工作目录的文件路径
        limit: 可选，限制返回行数
    """
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

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换（只替换首次出现）。
    参数:
        path: 文件路径
        old_text: 要被替换的原文本（必须精确匹配）
        new_text: 替换后的新文本
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """按 glob 模式匹配文件，只返回工作目录内的匹配项。"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 (unchanged): 工具定义 + 分发映射
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s04: Hook 系统 —— 注册表 + 注册函数 + 触发器
#  核心约定: 回调返回 None = 放行，返回字符串 = 阻断
#  4 个事件覆盖完整 agent cycle:
#    UserPromptSubmit → PreToolUse → PostToolUse → Stop

# 教学版中，PreToolUse 的非 None 返回值会阻止本次工具执行，Stop 的非 None 返回值会强制续跑。UserPromptSubmit 和 PostToolUse 的返回值未被使用。
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """注册 hook: 把回调追加到指定事件的回调列表。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发 hook: 遍历该事件的所有回调。一旦返回非 None 就停止并返回。
    返回值语义: None = 放行/不干预，非 None(字符串) = 阻断/拦截。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # 教学简化: 非 None 即阻断本次调用
            return result
    return None


# ── PreToolUse 回调 ──
# s03 的 check_permission() 逻辑搬到这里，包装成 hook 回调

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "format "]
# 注意: 同时覆盖 Linux (rm/rmdir) 和 Windows (del/erase/rd)，防止模型用不同命令绕过
DESTRUCTIVE = ["rm ", "rmdir ", "del ", "erase ", "rd ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse: 权限检查（s03 三道闸门逻辑）。
    Gate 1: 硬黑名单 → 返回字符串阻断
    Gate 2+3: 危险命令/写工作区外 → 问用户 → 返回字符串或 None
    """
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse: 记录每次工具调用（灰色日志，不干预执行）。"""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


# ── PostToolUse 回调 ──

def large_output_hook(block, output):
    """PostToolUse: 大输出提醒（>100k 字符时黄色警告）。"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


# ── UserPromptSubmit 回调 ──

def context_inject_hook(query: str):
    """UserPromptSubmit: 在用户输入进入 LLM 前，打印当前工作目录。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


# ── Stop 回调 ──

def summary_hook(messages: list):
    """Stop: 循环退出前打印工具调用次数统计。"""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# 注册所有 hook
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — 结构同 s03，但权限检查改为 trigger_hooks
#  s03: if not check_permission(block): continue
#  s04: if trigger_hooks("PreToolUse", block): continue
#  新增: UserPromptSubmit(输入后)、PostToolUse hook（执行后）、Stop hook（退出前）
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """智能体主循环 —— 调 LLM → PreToolUse hook → 执行 → PostToolUse hook → 回传。"""
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Stop hook: 退出前触发，返回非 None 则注入提示词强制续跑
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")

            # s04: hook 替代硬编码的 check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 粗体品红 = 标签，品红 = 内容
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")

            # s04: PostToolUse hook（执行后处理，如大输出警告）
            trigger_hooks("PostToolUse", block, output)

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    """交互入口。
    流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s04 >>）
      3. UserPromptSubmit hook → 进入 agent_loop
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # UserPromptSubmit hook: 在进入 LLM 前触发
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                # 蓝色显示模型最终回复
                print(f"\033[34m{block.text}\033[0m")
        print()
