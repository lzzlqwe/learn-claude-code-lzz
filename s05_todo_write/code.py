#!/usr/bin/env python3
"""
s05: TodoWrite — 在 s04 hooks 之上增加一个计划工具。

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                      todo_write ← 新增
                                   +------------------+
                                        |
                         in-memory CURRENT_TODOS
                                        |
                        if rounds_since_todo >= 3:
                          inject <reminder>

核心模式（4 步）:
  1. 6 个工具函数（5 个继承 s02-s04 + 1 个新增 todo_write）
  2. Hook 系统（继承 s04）+ 跨平台权限检查
  3. todo_write: 纯内存任务列表，校验 status 字段，彩色终端输出
  4. agent_loop: nag 提醒（3 轮未更新 todo → 注入提示）+ 调用计数

s04 → s05 关键变化:
  + todo_write 工具 + run_todo_write() 实现
  + Nag 提醒（每 3 轮未更新 todo 就注入 <reminder> 提示）
  + SYSTEM prompt 加入"先计划再执行"引导
  + rounds_since_todo 计数器
  循环不变: 新工具通过 TOOL_HANDLERS 自动分发

运行: python s05_todo_write/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess
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
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []

# s05 change: SYSTEM prompt adds planning guidance
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): 工具实现 —— 5 个工具函数
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
        limit: 可选，限制返回行数（超出显示省略提示）
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
#  NEW in s05: todo_write 工具 —— 纯计划工具，不执行任何操作
#  任务列表存在内存 CURRENT_TODOS 中，不写文件
#  每个 todo 必须含 content(描述) 和 status(pending/in_progress/completed)
# ═══════════════════════════════════════════════════════════

def run_todo_write(todos: list) -> str:
    """创建/更新任务列表。校验必填字段和 status 枚举值后存入内存。
    终端输出彩色任务列表: pending=灰，in_progress=青色，completed=绿色。"""
    global CURRENT_TODOS
    # 校验必填字段
    for i, t in enumerate(todos):
        if "content" not in t or "status" not in t:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{t['status']}'"
    CURRENT_TODOS = todos
    # 彩色终端输出任务列表
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


# ═══════════════════════════════════════════════════════════
#  工具定义 + 分发映射 —— s02 的 Dispatch Map 模式
#  新增 todo_write 只需: 1. 写 run_todo_write 2. 加 schema 3. 加一行映射
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
    # s05 新增工具
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook 系统 —— 注册表 + 注册函数 + 触发器
#  核心约定: 回调返回 None = 放行，返回字符串 = 阻断
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """注册 hook: 把回调追加到指定事件的回调列表。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发 hook: 遍历该事件的所有回调。一旦返回非 None 就停止并返回。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── PreToolUse 回调 ──

# 注意: 同时覆盖 Linux 和 Windows 命令，防止 LLM 切换平台绕过
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "format "]

def permission_hook(block):
    """PreToolUse: 硬黑名单检查（仅 Gate 1，教学简化版）。
    命中黑名单 → 红色输出并返回拒绝字符串阻断执行。"""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    """PreToolUse: 记录每次工具调用（灰色日志，不干预执行）。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
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
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s04 结构 + nag 提醒计数器
#  s05 新增: nag 提醒（3 轮未更新 todo → 注入提示） + rounds_since_todo 管理
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0

def agent_loop(messages: list):
    """智能体主循环 —— nag 提醒 → LLM → hook → 执行 → hook → todo 计数重置。"""
    global rounds_since_todo
    while True:
        # s05: nag 提醒 —— 连续 3 轮未更新 todo，注入一条用户消息催促
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

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

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 粗体品红 = 标签，品红 = 内容
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")

            trigger_hooks("PostToolUse", block, output)

            # s05: todo_write 被调用时，重置 nag 计数器
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    """交互入口。
    流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s05 >>）
      3. UserPromptSubmit hook → 进入 agent_loop（含 nag 提醒机制）
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
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
