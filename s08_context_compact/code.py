#!/usr/bin/env python3
"""
s08: Context Compact — 四层压缩管线，在 LLM 调用前自动瘦身。

  L1: snip_compact       — 消息数 > 50 时裁剪中间消息
  L2: micro_compact      — 旧 tool_result 替换为占位符
  L3: tool_result_budget — 大输出持久化到磁盘
  L4: compact_history    — LLM 全文摘要（1 次 API 调用）

  Emergency: reactive_compact — API 返回 prompt_too_long 时触发

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

核心原则: 便宜的先跑，贵的后跑。前 3 层 0 API 调用，只有第 4 层才花 1 次 LLM 调用。

核心模式（4 步）:
  1. 启动时扫描 skills/（继承 s07）+ 创建 TRANSCRIPT_DIR / TOOL_RESULTS_DIR
  2. 8 个工具函数 + compact 工具（直接修改 messages 列表）
  3. 四层压缩管线: L3(budget) → L1(snip) → L2(micro) → L4(summary)
  4. agent_loop: 每轮 LLM 调用前过压缩管线 + reactive 兜底

s07 → s08 关键变化:
  + 四层压缩管线 (snip/micro/budget/compact_history)
  + reactive_compact 应急机制
  + compact 工具（Agent 可主动触发压缩）
  + TRANSCRIPT_DIR / TOOL_RESULTS_DIR 持久化
  循环新增: 压缩管线在 LLM 调用前运行

运行: python s08_context_compact/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, json, time
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []

# ═══════════════════════════════════════════════════════════
#  FROM s07 (unchanged): Skill 扫描 —— 启动时解析 skills/ 目录
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter。返回 (meta字典, 正文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()

SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """扫描 skills/ 目录，解析每个 SKILL.md 并写入 SKILL_REGISTRY。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    """列出所有已注册 skill（名称 + 一行描述）。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    """按 name 加载完整 skill 内容。通过 SKILL_REGISTRY 安全查表。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# s08: SYSTEM 包含 skill 目录（继承 s07 build_system）
def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# s08: 子 Agent 的独立 SYSTEM prompt —— 无 compact, 无 skill loading
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s07 (unchanged): 基本工具实现 —— 6 个工具函数
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令。安全措施: 120 秒超时 + 输出截断至 50000 字符。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。参数: path=文件路径, limit=可选行数限制。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """写入内容到文件。自动创建父目录。"""
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8"); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换（只替换首次出现）。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """按 glob 模式匹配文件，只返回工作目录内的匹配项。"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def run_todo_write(todos: list) -> str:
    """创建/更新任务列表。终端输出彩色任务列表。"""
    global CURRENT_TODOS
    for i, t in enumerate(todos):
        if "content" not in t or "status" not in t:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{t['status']}'"
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def extract_text(content) -> str:
    """从 message content blocks 中提取纯文本。"""
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  FROM s06-s07 (unchanged): 子 Agent —— 干净上下文 + 30 轮上限
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
# 注意: 无 task/无 load_skill/无 compact/无 todo_write —— 子 Agent 只干活
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(task: str) -> str:
    """派生子 Agent。独立循环 + 30 轮上限 + 只返回摘要。"""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": task}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  NEW in s08: 四层压缩管线 (Four-Layer Compaction Pipeline)
#  原则: 便宜的先跑（0 API 调用），贵的后跑（1 次 LLM 调用）
#  执行顺序: L3(budget) → L1(snip) → L2(micro) → L4(summary)
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000       # 触发 L4 自动摘要的 token 阈值
KEEP_RECENT = 3             # L2: 保留最近 N 个 tool_result 不压缩
PERSIST_THRESHOLD = 30000   # L3: 超过此大小的输出才持久化到磁盘（单条 tool_result 超过此大小 → 写磁盘）

def estimate_size(msgs): return len(str(msgs))


# L1: snipCompact —— 消息数超限时裁剪中间消息（0 API 调用）
def snip_compact(messages, max_messages=50):
    """L1: 当消息数 > max_messages 时，保留头尾，中间替换为一条占位消息。"""
    if len(messages) <= max_messages: return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return messages[:keep_head] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[-keep_tail:]


# L2: microCompact —— 旧 tool_result 替换为占位符（0 API 调用）
def collect_tool_results(messages):
    """收集 messages 中所有 tool_result 的位置 (msg_index, block_index, block)。"""
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    """L2: 保留最近 KEEP_RECENT 个 tool_result，其余的替换为占位符。"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# L3: toolResultBudget —— 大输出持久化到磁盘（0 API 调用）
def persist_large_output(tool_use_id, output):
    """将超大 tool_result 写入磁盘文件，返回引用路径 + 预览。"""
    if len(output) <= PERSIST_THRESHOLD: return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output, encoding="utf-8")
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_bytes=200_000):
    """L3: 当最新一轮 tool_result 总大小超 max_bytes，将最大的持久化到磁盘。"""
    """最后一轮所有 tool_result 总大小超过此值 max_bytes → 才启动 L3"""
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes: return messages
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes: break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD: continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# L4: autoCompact —— LLM 全文摘要（1 次 API 调用，最贵）
def write_transcript(messages):
    """将完整对话写入 .transcripts/ 目录备份。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    """调用 LLM 将完整对话压缩为摘要。保留: 当前目标/关键决策/文件变更/剩余工作/用户约束。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

def compact_history(messages):
    """L4: 备份对话 → LLM 压缩 → 返回一条压缩后的 user 消息替代整个 messages。"""
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# Emergency: reactiveCompact —— API 报 prompt_too_long 时触发
def reactive_compact(messages):
    """应急压缩: 备份 → 压缩 → 保留最近 5 条 + 摘要。"""
    transcript = write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]


# ═══════════════════════════════════════════════════════════
#  工具注册表 —— s02-s08 全部工具（9 个）
#  s08 新增 compact: 直接触发 compact_history() 替换整个 messages
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
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # s08: compact 工具 —— 触发 compact_history，直接替换 messages，不是一个普通返回值
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook 系统 —— 简化版（仅 PreToolUse / PostToolUse）
#  注意: 同时覆盖 Linux 和 Windows 命令
# ═══════════════════════════════════════════════════════════

HOOKS = {"PreToolUse": [], "PostToolUse": []}
def trigger_hooks(event, *args):
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None: return r
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "format "]
def permission_hook(block):
    """PreToolUse: 硬黑名单检查。命中 → 返回拒绝字符串阻断执行。"""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""): return "Permission denied"
    return None
def log_hook(block):
    """PreToolUse: 记录每次工具调用（灰色日志）。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s08 核心: 每次 LLM 调用前过四层压缩管线
#  管线: L3→L1→L2→[L4 if 超限] → LLM → [reactive if prompt_too_long]
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """智能体主循环 —— 压缩管线 → LLM → hook → 执行 → reactive 兜底。"""
    reactive_retries = 0
    while True:
        # s08: 前三层预处理（0 API 调用，便宜的先跑）
        # 执行顺序与 CC 源码一致: budget → snip → micro
        messages[:] = tool_result_budget(messages)    # L3: 大结果持久化到磁盘
        messages[:] = snip_compact(messages)          # L1: 裁剪中间消息
        messages[:] = micro_compact(messages)         # L2: 旧 tool_result 占位符

        # s08: token 仍超限 → 触发 LLM 摘要（1 次 API 调用）
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        try:
            response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000)
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use": return

        results = []
        for block in response.content:
            if block.type != "tool_use": continue

            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")

            # s08: compact 工具直接触发压缩，替换整个 messages
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break  # 结束当前轮，用压缩后的上下文重新开始

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            # 粗体品红 = 标签，品红 = 内容
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        else:
            # compact 未被调用
            messages.append({"role": "user", "content": results})
            continue
        # compact 被调用: results 已在上方追加
        continue


if __name__ == "__main__":
    """交互入口。流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s08 >>）
      3. 进入 agent_loop（含四层压缩管线）
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                # 蓝色显示模型最终回复
                print(f"\033[34m{block.text}\033[0m")
        print()
