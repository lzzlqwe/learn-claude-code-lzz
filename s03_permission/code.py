#!/usr/bin/env python3
"""
s03_permission/code.py - 权限控制系统。

在 s02 多工具基础上，工具执行前插入三道闸门:

    Gate 1: 硬黑名单（rm -rf /、sudo 等 —— 无条件封杀）
    Gate 2: 规则匹配（写到工作目录外？危险命令？—— 命中则触发 Gate 3）
    Gate 3: 用户审批（暂停等待用户确认）

    +-------+    +--------+    +--------+    +--------+    +------+
    | Tool  | -> | Gate 1 | -> | Gate 2 | -> | Gate 3 | -> | Exec |
    | call  |    | deny?  |    | match? |    | allow? |    |      |
    +-------+    +--------+    +--------+    +--------+    +------+
         |            |             |             |
         v            v             v             v
      (放行)      (阻断)       (问用户)     (用户拒绝)

agent_loop 只加了一行:

    if not check_permission(block):
        continue

核心模式（4 步）:
  1. 定义 5 个工具函数 + schema + 分发映射（继承 s02）
  2. 定义三道闸门权限管道（s03 新增）
  3. agent_loop: 调 LLM → 过权限 → 执行工具 → 回传结果
  4. 交互入口: 读用户输入 → 追加到 history → agent_loop → 打印模型回复

需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
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

SYSTEM = f"You are a coding agent at {WORKDIR}. All destructive operations require user approval."


# ═══════════════════════════════════════════════════════════
#  FROM s02 (unchanged): 工具实现 —— 5 个工具函数，逻辑与 s02 完全一致
#  注意: run_bash 的内部黑名单已移至 Gate 1（职责分离）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 Shell 命令。（黑名单检查已移至 Gate 1）
    安全措施: 120 秒超时 + 输出截断至 50000 字符。
    """
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
    """写入内容到文件。
    参数:
        path: 相对于工作目录的文件路径
        content: 要写入的文本内容
    自动创建父目录。
    """
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
#  FROM s02 (unchanged): 工具定义 + 分发映射
#  每个工具包含 name、description、input_schema（JSON Schema 格式）
#  TOOL_HANDLERS 查表替代 if/elif，新增工具只需加一行映射
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
#  NEW in s03: 三道闸门权限管道 (Three-Gate Permission Pipeline)
#  流: Gate 1(硬黑名单) → Gate 2(规则匹配) → Gate 3(用户审批) → 执行
#  Gate 1 命中 → 直接阻断；Gate 2 命中 → 触发 Gate 3 问用户
# ═══════════════════════════════════════════════════════════

# Gate 1: 硬黑名单 —— 命中即无条件阻断，不可绕过
# 注意: 同时覆盖 Linux 和 Windows 命令
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda", "format "]

def check_deny_list(command: str) -> str | None:
    """遍历黑名单，命中返回错误信息，否则返回 None。"""
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# Gate 2: 规则匹配 —— 命中后不直接阻断，而是交给 Gate 3 由用户决定
# 每条规则包含: tools(适用工具列表), check(判断函数), message(警告信息)
PERMISSION_RULES = [
    {"tools": ["write_file", "edit_file"],
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},
    {"tools": ["bash"],
     # 同时覆盖 Linux (rm/rmdir) 和 Windows (del/erase/rd)
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "rmdir ", "del ", "erase ", "rd ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    """遍历规则列表，匹配到则返回警告信息，否则返回 None。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# Gate 3: 用户审批 —— 暂停等待用户输入 y/N
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """向用户展示警告并等待审批。返回 "allow" 或 "deny"。"""
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# 管道总控: 将三道闸门串联
def check_permission(block) -> bool:
    """权限检查总入口。
    流程: Gate 1(仅bash) → Gate 2(所有工具) → Gate 3(规则命中时)
    返回 True = 放行，False = 阻断。
    """
    # Gate 1: 仅对 bash 做硬黑名单检查
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False
    # Gate 2: 所有工具过规则匹配
    reason = check_rules(block.name, block.input)
    if reason:
        # Gate 3: 规则命中 → 问用户
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s02 结构一致，只在工具执行前插入了 check_permission()
#  s02: output = TOOL_HANDLERS[block.name](**block.input)
#  s03: if not check_permission(block): continue  → 多了权限闸门
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """智能体主循环 —— 调 LLM → 过权限 → 执行工具 → 回传结果 → 循环。"""
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")

            # s03 新增: 工具执行前过权限管道
            if not check_permission(block):
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 粗体品红 = 标签，品红 = 内容
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    """交互入口。
    流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s03 >>）
      3. 输入 q/exit/空 → 退出；否则进入 agent_loop
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s03: Permission")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                # 蓝色显示模型最终回复
                print(f"\033[34m{block.text}\033[0m")
        print()
