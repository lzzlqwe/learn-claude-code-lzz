#!/usr/bin/env python3
"""
s13: Background Tasks — 线程异步执行 + 通知注入。

核心模式（4 步）:
  1. should_run_background: 模型显式请求(run_in_background) 或 启发式(慢操作关键词)
  2. start_background_task: daemon 线程派发 → 立即返回占位符，不阻塞主循环
  3. collect_background_results: 每轮结束后收集完成的后台任务 → 以 <task_notification> 注入
  4. threading.Lock: background_tasks / background_results 线程安全

s12 → s13 关键变化:
  + threading.Thread 后台执行
  + background_tasks 字典（生命周期追踪）+ background_results 字典 + threading.Lock
  + should_run_background: 模型显式 run_in_background + 慢操作启发式（install/build/test...）
  + start_background_task: daemon 线程派发，返回 bg_id
  + collect_background_results: 收集已完成任务，以 <task_notification> 格式注入
  + agent_loop: 慢操作 → 后台 + 占位符，每轮结束注入通知

注意: s13 是教学骨架版，聚焦后台任务。s11 的完整 error recovery 省略。

运行: python s13_background_tasks/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, json, time, random, threading
from pathlib import Path
from dataclasses import dataclass, asdict

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
MODEL = os.environ["MODEL_ID"]

# ═══════════════════════════════════════════════════════════
#  FROM s12: Task System —— 文件持久化任务图 + blockedBy 依赖
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    """任务数据类。status: pending→in_progress→completed。blockedBy: 依赖的前置任务 ID。"""
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent 名称（多 Agent 场景）
    blockedBy: list[str] # 依赖的前置任务 ID 列表


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建任务并持久化。ID = task_{timestamp}_{random}。"""
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """持久化任务到 .tasks/{id}.json。"""
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")


def load_task(task_id: str) -> Task:
    """从 .tasks/{id}.json 加载任务。"""
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


def list_tasks() -> list[Task]:
    """列出所有任务（按文件名排序）。"""
    return [Task(**json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回任务完整 JSON 详情。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """检查所有 blockedBy 依赖是否已完成。缺失的依赖文件视为阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领 pending 任务。校验状态 + 依赖检查 → 设置 owner + pending→in_progress。"""
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成 in_progress 任务。报告被解锁的下游任务。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═══════════════════════════════════════════════════════════
#  FROM s10: Prompt 组装 —— PROMPT_SECTIONS + assemble + cache
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task.",
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
    """带缓存的 prompt 获取。json.dumps 做确定性 key。"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  基本工具实现 —— 3 个基础工具 + 5 个任务工具
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    """执行 Shell 命令。run_in_background 由 agent_loop 分发层处理，不在函数内部。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。"""
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
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task 工具（从 s12 继承）

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """创建任务（可选 blockedBy 依赖）。蓝色终端输出。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """列出所有任务（状态图标: ○ pending / ● in_progress / ✓ completed）。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """获取任务完整 JSON 详情。"""
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    """认领任务（pending→in_progress，依赖未满足时拒绝）。"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """完成任务（in_progress→completed，报告解锁的下游任务）。"""
    return complete_task(task_id)


TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},  #输入参数多了run_in_background
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
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s13: Background Tasks —— 线程异步执行 + 通知注入
#  两条判断路径: 模型显式 run_in_background 或 慢操作关键词启发式
#  完成后以 <task_notification> 格式注入到 user message 中
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()        # 线程安全锁


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断: 命令包含慢操作关键词（install/build/test/deploy...）。"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断是否后台执行: 模型显式请求优先，其次启发式判断。"""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    """执行工具调用 block，返回输出。"""
    handler = TOOL_HANDLERS.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    """派发工具到 daemon 线程执行，立即返回 bg_id（不等待完成）。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        """daemon 线程 worker: 执行工具 → 写入结果。"""
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成的后台任务，以 <task_notification> XML 格式返回通知列表。"""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


# ── Context（从 s10 继承）──

def update_context(context: dict, messages: list) -> dict:
    """根据真实状态更新 context: 工具列表 / 工作目录 / 记忆索引。"""
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
#  agent_loop — s13 核心: 慢操作后台化 + 通知注入
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """主循环 —— 后台任务分流: should_run_background → 线程派发 → 通知收集注入。"""
    system = get_system_prompt(context)
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            # 黄色显示正在调用的工具名
            print(f"\033[33m> {block.name}\033[0m")

            # s13: 慢操作 → 后台线程 + 返回占位符
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Command: {block.input.get('command', '')}. "
                                           f"Result will be available when complete."})
            else:
                output = execute_tool(block)
                # 粗体品红 = 标签，品红 = 内容
                print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:300]}\033[0m")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # s13: 注入 工具结果 + 后台通知 到同一条 user message
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
            print(f"  \033[32m[inject] {len(bg_notifications)} background "
                  f"notification(s)\033[0m")
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    """交互入口。流程:
      1. 打印欢迎信息
      2. 循环读取用户输入（青色提示符 s13 >>）
      3. 进入 agent_loop（慢操作自动后台化 + 通知注入）
      4. agent_loop 返回后，蓝色打印模型文本回复
    """
    print("s13: background tasks")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms13 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                # 蓝色显示模型最终回复
                print(f"\033[34m{block.text}\033[0m")
        print()
