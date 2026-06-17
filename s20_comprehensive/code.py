#!/usr/bin/env python3
"""
s20: Comprehensive Agent — 全部教学组件汇聚于一个循环。
核心模式（完整 Harness 层）:
  全部 s01-s19 的机制归位到同一个 agent_loop 中：
  工具分发、权限闸门、hooks 扩展、todo 计划、子 Agent、技能加载、
  上下文压缩、记忆系统、prompt 组装、错误恢复、任务图、后台任务、
  cron 调度、团队协作、协议通信、自主认领、worktree 隔离、MCP 插件。

Run:  python s20_comprehensive/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

This final chapter intentionally puts the earlier teaching mechanisms back
together: dispatch, permission, hooks, todo, subagent, skills, compaction,
memory, prompt assembly, error recovery, task graph, background tasks, cron,
teams, protocols, autonomous agents, worktrees, and MCP.
"""

import os, subprocess, json, time, random, threading, re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36ms20 >> \033[0m"
CLI_ACTIVE = False


def terminal_print(text: str):
    """终端安全打印函数。主线程直接 print；后台线程先清行再打印，保留用户输入行。"""
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ""
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)

# ═══════════════════════════════════════════════════════════════
# ── Task System ── 任务系统
# 任务是轻量级持久化记录。后续系统（ownership/dependencies/worktree/teammate）
# 都建立在同一套文件备份状态之上。
# ═══════════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)
CURRENT_TODOS: list[dict] = []


@dataclass
class Task:
    """任务数据类。status: pending→in_progress→completed。worktree 绑定到 git worktree。"""
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent 名称（多 Agent 场景用）
    blockedBy: list[str] # 依赖的前置任务 ID 列表
    worktree: str | None = None  # s18: 绑定的 worktree 名称


def _task_path(task_id: str) -> Path:
    """任务文件路径：.tasks/{id}.json。"""
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


def get_task_json(task_id: str) -> str:
    """返回任务完整 JSON 详情。"""
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    """检查所有 blockedBy 依赖是否已完成。缺失的依赖文件 → 阻塞。
    Dependencies are intentionally simple: every blocker must exist and be
    completed before the task can be claimed."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领 pending 任务。校验：status + owner + 依赖 → 设置 owner + pending→in_progress。"""
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if _task_path(d).exists() and load_task(d).status != "completed"]
        missing = [d for d in task.blockedBy if not _task_path(d).exists()]
        parts = []
        if deps: parts.append(f"blocked by: {deps}")
        if missing: parts.append(f"missing deps: {missing}")
        return "Cannot start — " + ", ".join(parts)
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成 in_progress 任务。报告被解锁的下游任务（blockedBy 全完成）。"""
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
    return msg


# ═══════════════════════════════════════════════════════════════
# ── Worktree System ── Worktree 隔离系统
# 由于 Worktree（工作树）的名称会被用作文件系统路径，因此教学版本（teaching version）
# 保留了严格的验证规则，并在创建、删除和保留操作中复用这些规则。
# ═══════════════════════════════════════════════════════════════

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str) -> str | None:
    """校验 worktree 名称。拒绝路径遍历（./..）和非法字符，仅允许 [A-Za-z0-9._-]{1,64}。"""
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    """执行 git 命令。返回 (ok, output)。30 秒超时 + 输出截断至 5000 字符。"""
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    """追加生命周期事件到 events.jsonl。类型: create/remove/keep。"""
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    """创建 git worktree（独立分支 wt/{name}）。可选绑定到任务。
    Tool-layer validation is part of the safety boundary; do it before git
    sees the name, not only after git happens to reject something."""
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    if task_id:
        try:
            load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    """绑定任务到 worktree。仅写 worktree 字段，保持 status=pending 供自动认领。"""
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)


def _count_worktree_changes(path: Path) -> tuple[int, int]:
    """统计 worktree 中未提交的文件和未推送的提交数。"""
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """删除 worktree。有未提交变更时拒绝删除，除非 discard_changes=true。"""
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return "Cannot verify status. Use discard_changes=true to force."
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    """保留 worktree 供人工审查。分支 wt/{name} 保留不删除。"""
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


# ═══════════════════════════════════════════════════════════════
# ── Skill Loading ── 技能加载系统
# 两级加载：目录注入 SYSTEM（便宜）+ 内容按需 tool_result 注入（昂贵）。
# ═══════════════════════════════════════════════════════════════

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter（--- 包裹的元数据块）。返回 (meta_dict, body)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def scan_skills():
    """启动时扫描 skills/ 目录，构建 SKILL_REGISTRY。每个子目录的 SKILL.md 为一个技能。"""
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return
    for directory in sorted(SKILLS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "SKILL.md"
        if not manifest.exists():
            continue
        raw = manifest.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw)
        name = meta.get("name", directory.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
        }


scan_skills()


def list_skills() -> str:
    """列出所有可用技能（名称 + 一行描述）。用于 SYSTEM prompt 的 Layer 1 目录。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    """按需加载技能完整内容（Layer 2）。通过 tool_result 注入 messages。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["content"]


# ═══════════════════════════════════════════════════════════════
# ── Prompt Assembly ── Prompt 组装
# SYSTEM prompt 每轮从实时上下文重建，记忆/技能目录/MCP 状态/teammate 在此汇入。
# ═══════════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
             "todo_write, task, load_skill, compact, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree, "
             "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """拼装 SYSTEM prompt。注入：身份、工具、工作目录、当前时间、技能目录、记忆、已连接 MCP 服务器。
    The system prompt is rebuilt each turn from live context."""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + list_skills() +
                    "\nUse load_skill(name) when a skill is relevant.")
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════════
# ── Basic Tools ── 基础工具函数
# File tools stay inside the workspace or teammate worktree. Bash remains
# powerful on purpose and is controlled by the permission hook instead.
# ═══════════════════════════════════════════════════════════════

def safe_path(p: str, cwd: Path = None) -> Path:
    """路径安全校验 —— 防止 LLM 通过 ../ 逃逸工作目录。支持自定义 cwd。"""
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False) -> str:
    """执行 Shell 命令。120 秒超时 + 输出截断至 50000 字符。
    run_in_background 由 dispatcher 消费，直接执行时忽略。"""
    try:
        r = subprocess.run(command, shell=True, cwd=cwd or WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None) -> str:
    """读取文件内容。参数: path=文件路径, limit=行数限制, offset=起始行, cwd=工作目录。"""
    try:
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    """写入内容到文件。自动创建父目录。支持 cwd 参数。"""
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path = None) -> str:
    """精确替换文件中首次出现的 old_text 为 new_text。仅替换一次。"""
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path = None) -> str:
    """查找匹配 glob 模式的文件列表。基于 cwd 校验不逃逸。"""
    import glob as g
    try:
        base = cwd or WORKDIR
        results = []
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def call_tool_handler(handler, args: dict, name: str) -> str:
    """统一工具调用入口。包装 handler(**args)，捕获 TypeError 和异常。"""
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"


def run_todo_write(todos: list) -> str:
    """更新当前会话的 todo 列表。校验每个 todo 的 content/status 字段。"""
    global CURRENT_TODOS
    for i, todo in enumerate(todos):
        if "content" not in todo or "status" not in todo:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{todo['status']}'"
    CURRENT_TODOS = todos
    print(f"  \033[33m[todo] updated {len(CURRENT_TODOS)} item(s)\033[0m")
    return f"Updated {len(CURRENT_TODOS)} todos"


# ═══════════════════════════════════════════════════════════════
# ── MessageBus ── 消息总线
# 团队通信采用仅追加（append-only）的 JSONL 邮箱机制。
# 这种方式不仅让协议在磁盘上易于检查（inspectable），还允许后台运行的团队成员发送消息。
# ═══════════════════════════════════════════════════════════════

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """基于文件的消息总线。每个 Agent 有一个 .jsonl 收件箱。读取即消费。"""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        """发送消息。追加一行 JSON 到收件人邮箱文件。"""
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        terminal_print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
                       f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读取收件箱。消费式读取（read + unlink），取走即删除。"""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()
active_teammates: dict[str, bool] = {}

# ═══════════════════════════════════════════════════════════════
# ── Protocol State ── 协议状态管理
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProtocolState:
    """协议状态数据类。type: shutdown / plan_approval。status: pending → approved/rejected。"""
    request_id: str
    type: str       # "shutdown" | "plan_approval"
    sender: str     # 发起方 Agent 名
    target: str     # 目标 Agent 名
    status: str     # pending | approved | rejected
    payload: str    # 计划文本或关机原因
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    """生成唯一请求 ID：req_{随机6位数字}。"""
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """关联响应到原始请求。通过 request_id 校验 → type 匹配 → 更新状态。
    Responses are matched by request_id so one protocol reply cannot approve
    a different pending request."""
    state = pending_requests.get(request_id)
    if not state:
        return
    if state.type == "shutdown" and response_type != "shutdown_response":
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        return
    state.status = "approved" if approve else "rejected"


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    """读取 Lead 收件箱。自动路由协议响应 → 返回所有消息。"""
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


# ═══════════════════════════════════════════════════════════════
# ── Autonomous Agent ── 自主 Agent
# ═══════════════════════════════════════════════════════════════

IDLE_POLL_INTERVAL = 5   # 空闲轮询间隔（秒）
IDLE_TIMEOUT = 60         # 空闲超时（秒）


def scan_unclaimed_tasks() -> list[dict]:
    """扫描任务板：找 status=pending、无 owner、依赖已全部完成的待办任务。"""
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text(encoding="utf-8"))
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def idle_poll(agent_name: str, messages: list,
              name: str, role: str,
              worktree_context: dict | None = None) -> str:
    """空闲轮询（60s / 5s 间隔）。三通道：收件箱 → 任务板 → 超时。
    返回 'work' / 'shutdown' / 'timeout'。
    Autonomous teammates wake up for inbox messages first, then look for
    unclaimed tasks. This keeps direct protocol messages higher priority."""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    return "shutdown"
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                if task_data.get("worktree"):
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                    if worktree_context is not None:
                        worktree_context["path"] = str(wt_path)
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                return "work"
    return "timeout"


# ═══════════════════════════════════════════════════════════════
# ── Teammate Thread ── Teammate 线程
# ═══════════════════════════════════════════════════════════════

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """在后台线程中创建自主 teammate。支持：protocol_ctx（计划审批门禁）、wt_ctx（worktree cwd 切换）。
    Plan approval is a real gate: after submit_plan, the teammate stops
    taking model/tool steps until lead sends plan_approval_response."""
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    protocol_ctx = {"waiting_plan": None}  # 计划审批等待状态
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"If a task has a worktree, work in that directory.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        """协议消息分发器。shutdown_request → 回复并返回 True（停止）。
        plan_approval_response → 注入审批结果并返回 False（继续）。"""
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if req_id == protocol_ctx["waiting_plan"]:
                protocol_ctx["waiting_plan"] = None
            messages.append({"role": "user",
                "content": "[Plan approved]" if approve
                           else f"[Plan rejected] {msg['content']}"})
        return False

    def run():
        wt_ctx = {"path": None}

        def _wt_cwd():
            """获取当前 worktree 目录。认领带 worktree 的任务后自动切换，bash/read/write 在此执行。"""
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            return run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str) -> str:
            return run_read(path, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            """列出所有任务（含 worktree 绑定信息）。"""
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            """认领任务。如果任务绑定了 worktree，自动切换 cwd。"""
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                wt_ctx["path"] = (str(WORKTREES_DIR / task.worktree)
                                  if task.worktree else None)
            return result

        def _run_complete_task(task_id: str):
            """完成任务。清除 worktree cwd 绑定。"""
            result = complete_task(task_id)
            wt_ctx["path"] = None
            return result

        messages = [{"role": "user", "content": prompt}]
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "limit": {"type": "integer"},
                                             "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        # 外层循环：WORK → IDLE 周期
        while True:
            # 上下文压缩后身份重注入（Identity re-injection）
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                # 计划审批门禁：等待审批期间暂停模型步骤
                if protocol_ctx["waiting_plan"]:
                    time.sleep(IDLE_POLL_INTERVAL)
                    continue
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>"})
                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if not has_tool_use(response.content):
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "submit_plan":
                            output = _teammate_submit_plan(
                                name, block.input.get("plan", ""))
                            match = re.search(r"\((req_\d+)\)", output)
                            protocol_ctx["waiting_plan"] = (
                                match.group(1) if match else output)
                        else:
                            handler = sub_handlers.get(block.name)
                            output = call_tool_handler(handler, block.input,
                                                       block.name)
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                        if protocol_ctx["waiting_plan"]:
                            # 忽略同一轮 LLM 响应中 submit_plan 之后的其他 tool_use；
                            # 它们属于审批之后，不属于现在。
                            break
                messages.append({"role": "user", "content": results})
                if protocol_ctx["waiting_plan"]:
                    break
            if should_shutdown:
                break
            if protocol_ctx["waiting_plan"]:
                continue
            # IDLE 阶段：轮询收件箱 + 任务板
            idle_result = idle_poll(name, messages, name, role, wt_ctx)
            if idle_result in ("shutdown", "timeout"):
                break

        # 提取最终摘要：从最后一条 assistant text 中获取
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {role}"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """Teammate 提交计划供 Lead 审批。创建 ProtocolState + BUS.send 协议请求。"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id})"


# ═══════════════════════════════════════════════════════════════
# ── Lead Protocol Tools ── Lead 协议工具
# ═══════════════════════════════════════════════════════════════

def run_request_shutdown(teammate: str) -> str:
    """Lead 发送 shutdown 协议请求给指定 teammate。"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Shut down.", "shutdown_request",
             {"request_id": req_id})
    return f"Shutdown request sent to {teammate}"


def run_request_plan(teammate: str, task: str) -> str:
    """Lead 要求 teammate 提交执行计划。"""
    BUS.send("lead", teammate, f"Submit plan for: {task}", "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    """Lead 审批或拒绝 teammate 提交的计划。通过 BUS 发送审批结果。"""
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {'approved' if approve else 'rejected'}"


# ═══════════════════════════════════════════════════════════════
# ── Hooks + Permission Pipeline ── Hooks + 权限流水线
# Hooks are intentionally outside tool handlers. The loop can add permission,
# logging, and stop behavior without changing each individual tool.
# 权限不是写死在工具里，而是作为 PreToolUse hook；这样 permission/log/audit
# 都挂在同一个 hook 点上。
# ═══════════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    """注册 hook 回调到指定事件类型。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """触发指定事件的所有 hook 回调。回调返回非 None 值时停止并返回该值。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# 权限黑名单（跨平台：Linux + Windows）
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def permission_hook(block):
    """PreToolUse hook：权限闸门。三道检查：bash 黑名单 → 破坏性命令确认 → write_file 路径校验 → MCP 破坏性工具。
    The permission layer sees the raw tool_use before dispatch."""
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        if any(token in command for token in DESTRUCTIVE):
            print(f"\n\033[33m[permission] destructive command\033[0m")
            print(f"  {command}")
            choice = input("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        try:
            safe_path(path)
        except Exception:
            return f"Permission denied: path escapes workspace: {path}"
    if block.name.startswith("mcp__") and "deploy" in block.name:
        print(f"\n\033[33m[permission] MCP destructive-looking tool: {block.name}\033[0m")
        choice = input("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            return "Permission denied by user"
    return None


def log_hook(block):
    """PreToolUse hook：记录工具调用日志。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output):
    """PostToolUse hook：大输出告警（>100k 字符时警告）。"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: "
              f"{len(str(output))} chars\033[0m")
    return None


def user_prompt_hook(query: str):
    """UserPromptSubmit hook：记录用户输入所在的工作目录。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: {WORKDIR}\033[0m")
    return None


def stop_hook(messages: list):
    """Stop hook：统计本次对话中执行了多少次工具调用。"""
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(1 for item in content
                              if isinstance(item, dict)
                              and item.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


# 注册所有 hook
register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)


# ═══════════════════════════════════════════════════════════════
# ── Subagent Tool ── 子 Agent
# 一次性 subagent：独立 messages[]，30 轮限制，中间过程丢弃，只返回最终摘要。
# 解决"上下文隔离"问题——不让子任务的中间步骤污染主 Agent 的上下文。
# ═══════════════════════════════════════════════════════════════

SUB_SYSTEM = (
    f"You are a coding subagent at {WORKDIR}. "
    "Complete the task, then return a concise final summary. "
    "Do not spawn more agents."
)


SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]


SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read,
    "write_file": run_write, "edit_file": run_edit,
    "glob": run_glob,
}


def extract_text(content) -> str:
    """从 LLM 响应 content 中提取文本内容。处理 list 和 str 两种类型。"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def has_tool_use(content) -> bool:
    """检查 LLM 响应中是否包含 tool_use block。不依赖 stop_reason，以实际 block 为准。"""
    return any(getattr(block, "type", None) == "tool_use"
               for block in content)


def spawn_subagent(description: str) -> str:
    """创建一次性子 Agent。独立 messages[]、独立 tools，最多 30 轮。
    只返回最终文本摘要，中间过程全部丢弃。"""
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM, messages=messages,
            tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."


# ═══════════════════════════════════════════════════════════════
# ── Context Compaction ── 上下文压缩
# 压缩是分层进行的：先缩小过大的 tool_result，再裁剪旧消息范围，
# 最后才调用模型做摘要（仅当上下文仍超限或模型显式要求 compact 时）。
# ═══════════════════════════════════════════════════════════════

def estimate_size(messages: list) -> int:
    """估算 messages 的 JSON 序列化大小（字节）。"""
    return len(json.dumps(messages, default=str))


def collect_tool_results(messages: list):
    """收集 messages 中所有 tool_result block 的位置 (msg_idx, block_idx, block)。"""
    found = []
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((mi, bi, block))
    return found


def persist_large_output(tool_use_id: str, output: str) -> str:
    """超大输出（>30k 字符）持久化到文件，返回预览引用。"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return (f"<persisted-output>\nFull output: {path}\n"
            f"Preview:\n{output[:2000]}\n</persisted-output>")


def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    """Layer 1：对最后一轮 user 消息中的 tool_result 做预算控制。
    超过 max_bytes 时优先持久化最大的输出。"""
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return messages
    blocks = [(i, b) for i, b in enumerate(content)
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    for _, block in sorted(blocks,
                           key=lambda pair: len(str(pair[1].get("content", ""))),
                           reverse=True):
        if total <= max_bytes:
            break
        text = str(block.get("content", ""))
        block["content"] = persist_large_output(
            block.get("tool_use_id", "unknown"), text)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


def snip_compact(messages: list, max_messages: int = 50) -> list:
    """Layer 2：当消息数 > max_messages 时，保留头 3 + 尾 (max-3) 条，中间标记 snipped。"""
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return (messages[:keep_head]
            + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
            + messages[-keep_tail:])


def micro_compact(messages: list) -> list:
    """Layer 3：将旧的 tool_result（>120 字符）替换为占位符，只保留最近 K 条。"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(block.get("content", ""))) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def write_transcript(messages: list) -> Path:
    """将当前 messages 写入 transcript JSONL 文件，用于事后审计。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    """调用 LLM 生成对话摘要。保留当前目标、关键发现、已修改文件、剩余工作、用户约束。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue. "
              "Preserve current goal, key findings, changed files, remaining work, "
              "and user constraints.\n\n" + conversation)
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000)
    return extract_text(response.content) or "(empty summary)"


def compact_history(messages: list) -> list:
    """Layer 4：保存 transcript → 调用 LLM 生成摘要 → 替换 messages 为 compacted 内容。"""
    transcript = write_transcript(messages)
    print(f"  \033[36m[compact] transcript saved: {transcript}\033[0m")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages: list) -> list:
    """响应 prompt-too-long 错误时的 reactive compact。保留最近 5 条 + LLM 摘要。"""
    transcript = write_transcript(messages)
    print(f"  \033[31m[reactive compact] transcript saved: {transcript}\033[0m")
    try:
        summary = summarize_history(messages)
    except Exception:
        summary = "Earlier conversation was trimmed after a prompt-too-long error."
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
            *messages[-5:]]


# ═══════════════════════════════════════════════════════════════
# ── Error Recovery ── 错误恢复
# 三条恢复路径：max_tokens 升级 → prompt too long reactive compact → 429/529 指数退避重试。
# ═══════════════════════════════════════════════════════════════

class RecoveryState:
    """错误恢复状态。追踪：has_escalated / recovery_count / consecutive_529 / current_model。"""
    def __init__(self):
        self.has_escalated = False          # 是否已升级过 max_tokens
        self.recovery_count = 0             # continuation 次数
        self.consecutive_529 = 0            # 连续 529 次数
        self.has_attempted_reactive_compact = False  # 是否已尝试 reactive compact
        self.current_model = PRIMARY_MODEL  # 当前使用的模型


def retry_delay(attempt: int) -> float:
    """指数退避延迟（ms）：base * 2^attempt，上限 32s，加 25% 随机抖动。"""
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState):
    """带重试的函数调用。处理：429（限流）指数退避重试、529（过载）切 fallback model。"""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__.lower()
            msg = str(e).lower()
            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """判断异常是否为 prompt too long / context_length_exceeded。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


# ═══════════════════════════════════════════════════════════════
# ── Background Tasks ── 后台任务
# Slow tools return a placeholder tool_result immediately. Their real output is
# later injected as a task_notification, so the main loop can keep moving.
# ═══════════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断命令是否可能耗时 > 30s。仅检查 bash 工具的关键词（install/build/test/deploy...）。"""
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """两条判断路径：1. run_in_background 参数显式请求  2. 启发式关键词兜底。"""
    if tool_name != "bash":
        return False
    return bool(tool_input.get("run_in_background")) or is_slow_operation(tool_name, tool_input)


def start_background_task(block, handlers: dict) -> str:
    """在 daemon 线程中启动后台工具执行。立即返回占位符，完成后注入 task_notification。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    command = block.input.get("command", block.name)

    def worker():
        handler = handlers.get(block.name)
        result = call_tool_handler(handler, block.input, block.name)
        trigger_hooks("PostToolUse", block, result)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = str(result)

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": command,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成的后台任务，pop 后格式化为 <task_notification> XML。pop 防止重复注入。"""
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] == "completed"]
    notifications = []
    for bg_id in ready:
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
    return notifications


# ═══════════════════════════════════════════════════════════════
# ── Cron Scheduler ── Cron 定时调度器
# Cron jobs are stored separately from conversation history. When a job fires,
# it becomes a scheduled prompt that is injected back into the same agent loop.
# ═══════════════════════════════════════════════════════════════

DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    """定时任务数据类。cron: 5字段表达式。recurring: True=重复 / False=一次性。durable: True=持久化。"""
    id: str
    cron: str        # "0 9 * * *"
    prompt: str      # 触发时注入的消息
    recurring: bool  # True = 重复, False = 一次性
    durable: bool    # True = 持久化到磁盘


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.Lock()
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"（防重复触发）


def _cron_field_matches(field: str, value: int) -> bool:
    """匹配单个 cron 字段。支持 * / , - 四种模式。"""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查 5 字段 cron 表达式是否匹配给定时间。DOM 和 DOW 同时约束时使用 OR 逻辑。"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0
    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)
    if not (m and h and month_ok):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """校验单个 cron 字段值是否在 [lo, hi] 范围内。支持 * / , - 模式。"""
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """校验 5 字段 cron 表达式。返回 None 表示合法，否则返回错误描述。"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """将 durable 任务持久化到 .scheduled_tasks.json。"""
    durable = [asdict(job) for job in scheduled_jobs.values() if job.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2), encoding="utf-8")


def load_durable_jobs():
    """启动时从磁盘加载 durable 任务。跳过 cron 表达式不合法的任务。"""
    if not DURABLE_PATH.exists():
        return
    try:
        for item in json.loads(DURABLE_PATH.read_text(encoding="utf-8")):
            job = CronJob(**item)
            if not validate_cron(job.cron):
                scheduled_jobs[job.id] = job
    except Exception:
        pass


def schedule_job(cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True) -> CronJob | str:
    """注册新的 cron 定时任务。先校验表达式 → 创建 CronJob → 持久化。"""
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable)
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    return job


def cancel_job(job_id: str) -> str:
    """取消 cron 定时任务。从内存移除 + 更新持久化文件。"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    """独立守护线程：每 1s 轮询，匹配 cron 表达式 → 投递到 cron_queue。
    日期感知 marker 防止日级任务重复触发。单任务错误不杀死整个线程。"""
    while True:
        time.sleep(1)
        now = datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now) and _last_fired.get(job.id) != marker:
                        cron_queue.append(job)
                        _last_fired[job.id] = marker
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """消费 cron_queue 中已触发的任务（由 agent_loop 调用）。取出后清空队列。"""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    """schedule_cron 工具处理函数。"""
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' -> {prompt}"


def run_list_crons() -> str:
    """list_crons 工具处理函数。列出所有已注册的 cron 任务。"""
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."
    return "\n".join(
        f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs)


def run_cancel_cron(job_id: str) -> str:
    """cancel_cron 工具处理函数。"""
    return cancel_job(job_id)


load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# ── MCP System ── MCP 插件系统
# MCP is modeled as late-bound tools: connect first, then discovered server
# tools are merged into the normal tool pool with mcp__server__tool names.
# ═══════════════════════════════════════════════════════════════

class MCPClient:
    """MCP 客户端：管理单个 MCP 服务器的工具发现与调用。教学版用 mock handler 替代真实 MCP 协议。"""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []       # 工具定义（schema）→ 发给 LLM
        self._handlers: dict[str, callable] = {}  # 工具 handler → 实际执行

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, callable]):
        """注册工具定义和对应的 handler 函数。"""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """调用已注册的 MCP 工具。未知工具返回错误信息。"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


mcp_clients: dict[str, MCPClient] = {}

_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """安全规范化名称：将非 [a-zA-Z0-9_-] 字符替换为下划线。防止工具名注入非法字符。"""
    return _DISALLOWED_CHARS.sub('_', name)


def _mock_server_docs():
    """创建 mock MCP 服务器 'docs'：提供 search（readOnly）和 get_version（readOnly）工具。"""
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy():
    """创建 mock MCP 服务器 'deploy'：提供 trigger（destructive）和 status（readOnly）工具。"""
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    """连接到 MCP 服务器并发现工具。教学版从 MOCK_SERVERS 字典获取 mock 实现。"""
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Unknown server '{name}'. Available: {available}"
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    tool_names = [t["name"] for t in mcp_client.tools]
    print(f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


def assemble_tool_pool() -> tuple[list[dict], dict]:
    """组装完整工具池：BUILTIN_TOOLS + 所有已连接 MCP 服务器的工具。
    MCP 工具名格式: mcp__{server}__{tool}。闭包默认参数捕获当前循环变量。"""
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
            # 闭包默认参数陷阱：必须用 = 捕获当前循环变量，否则所有闭包引用最后一次迭代的值
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw))
    return tools, handlers


# ═══════════════════════════════════════════════════════════════
# ── Lead Worktree Tools ── Lead Worktree 工具
# ═══════════════════════════════════════════════════════════════

def run_create_worktree(name: str, task_id: str = "") -> str:
    """create_worktree 工具处理函数。"""
    return create_worktree(name, task_id)

def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    """remove_worktree 工具处理函数。"""
    return remove_worktree(name, discard_changes)

def run_keep_worktree(name: str) -> str:
    """keep_worktree 工具处理函数。"""
    return keep_worktree(name)


# ═══════════════════════════════════════════════════════════════
# ── Basic Tool Handlers ── 基础工具处理函数
# ═══════════════════════════════════════════════════════════════

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """create_task 工具处理函数。终端蓝色输出创建信息。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """list_tasks 工具处理函数。显示 worktree 绑定信息。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    """get_task 工具处理函数。"""
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    """claim_task 工具处理函数。"""
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    """complete_task 工具处理函数。"""
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """spawn_teammate 工具处理函数。"""
    return spawn_teammate_thread(name, role, prompt)

def run_send_message(to: str, content: str) -> str:
    """send_message 工具处理函数。"""
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_check_inbox() -> str:
    """check_inbox 工具处理函数。自动路由协议响应。"""
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)

def run_connect_mcp(name: str) -> str:
    """connect_mcp 工具处理函数。"""
    return connect_mcp(name)


# ═══════════════════════════════════════════════════════════════
# ── Tool Definitions ── 工具 Schema 定义
# The model sees tool schemas; Python executes handlers. S20 keeps both tables
# explicit so every added capability is visible in one place.
# ═══════════════════════════════════════════════════════════════

BUILTIN_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write",
     "description": "Create and manage a task list for the current session.",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array",
                          "items": {"type": "object",
                                    "properties": {
                                        "content": {"type": "string"},
                                        "status": {"type": "string",
                                                   "enum": ["pending", "in_progress", "completed"]}},
                                    "required": ["content", "status"]}}},
                      "required": ["todos"]}},
    {"name": "task",
     "description": "Launch a focused subagent. Returns only its final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
    {"name": "create_task", "description": "Create a task.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "schedule_cron",
     "description": ("Schedule a cron job. cron is 5-field: min hour dom "
                     "month dow. For one-shot reminders, compute the target "
                     "minute and set recurring=false."),
     "input_schema": {"type": "object",
                      "properties": {"cron": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "recurring": {"type": "boolean"},
                                     "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List registered cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check inbox for messages and protocol responses.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
     "description": "Create an isolated git worktree.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task_id": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if changes exist.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "discard_changes": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
]

BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
    "connect_mcp": run_connect_mcp,
}


# ═══════════════════════════════════════════════════════════════
# ── Context ── 上下文管理
# ═══════════════════════════════════════════════════════════════

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(context: dict, messages: list) -> dict:
    """根据真实状态更新上下文。读取 MEMORY.md（截断 2000 字符）+ MCP 连接 + teammate 列表。"""
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text(encoding="utf-8")[:2000]
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }


# ═══════════════════════════════════════════════════════════════
# ── Agent Loop ── Agent 主循环
# 一个 while True 承载全部 19 章机制：cron 注入 → 上下文预算压缩 → LLM 调用 →
# 工具分发（权限/hooks/后台/MCP）→ tool_result 回传 → 下一轮。
# ═══════════════════════════════════════════════════════════════

rounds_since_todo = 0  # 跟踪"多久没更新 todo了"（>=3 轮触发提醒）
agent_lock = threading.Lock()


def prepare_context(messages: list) -> list:
    """每轮 LLM 调用前执行上下文预算管道：tool_result_budget → snip → micro → compact。"""
    messages[:] = tool_result_budget(messages)
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        messages[:] = compact_history(messages)
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    """构建 user 消息 content：工具结果 + 后台任务通知合并到同一条消息。
    Tool results and completed background notifications are both returned to
    the model as user-side content."""
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    """将已完成的后台任务通知作为独立 user 消息注入 messages。"""
    notes = collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int):
    """调用 LLM：组装 system prompt → with_retry 包裹的真实验证调用。"""
    system = assemble_system_prompt(context)
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens),
        state)


def agent_loop(messages: list, context: dict):
    """Agent 主循环 —— 全部 s01-s19 机制归位。
    每一轮:
      1. 注入 cron 队列 + 后台任务通知
      2. todo 提醒（>=3 轮未更新时）
      3. 上下文预算管道（tool_result_budget → snip → micro → compact）
      4. assemble_tool_pool + 组装 system prompt
      5. call_llm（with_retry + max_tokens 升级 + reactive compact）
      6. 遍历 tool_use block：hooks 权限闸门 → 后台分发 → 工具执行 → PostToolUse hooks
      7. 合并 tool_result + background notification → 追加 messages
    """
    global rounds_since_todo
    tools, handlers = assemble_tool_pool()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ── 第 1 步：注入 cron 队列中的定时任务 ──
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")

        # ── 第 1b 步：注入已完成后台任务通知 ──
        inject_background_notifications(messages)

        # ── 第 2 步：todo 提醒（>= 3 轮未更新）──
        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # ── 第 3 步：上下文预算管道 ──
        prepare_context(messages)
        context = update_context(context, messages)
        tools, handlers = assemble_tool_pool()

        # ── 第 4-5 步：LLM 调用（with error recovery）──
        try:
            response = call_llm(messages, context, tools, state, max_tokens)
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        # ── 恢复路径 1：max_tokens 不足 → 升级 ──
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})

        # ── 停止条件：无 tool_use → 触发 Stop hooks → 返回 ──
        if not has_tool_use(response.content):
            trigger_hooks("Stop", messages)
            return

        # ── 第 6 步：遍历 tool_use block，执行工具 ──
        results = []
        compacted_now = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[33m> {block.name}\033[0m")  # 黄色工具名

            # compact 工具：截获后直接执行压缩（不经过权限/handler）
            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append({"role": "user",
                                 "content": "[Compacted. Continue with summarized context.]"})
                compacted_now = True
                break

            # 权限闸门（PreToolUse hooks）
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 后台分发（慢操作 → daemon 线程）
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block, handlers)
                output = (f"[Background task {bg_id} started] "
                          "Result will arrive as a task_notification.")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                continue

            # 正常工具执行
            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{str(output)[:300]}\033[0m")

            # todo_write 重置计数器
            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})

        if compacted_now:
            continue

        # ── 第 7 步：合并 tool_result + 后台通知 → 追加 messages ──
        messages.append({"role": "user", "content": build_user_content(results)})


def print_turn_assistants(messages: list, turn_start: int):
    """打印本轮新增的所有 assistant 消息中的文本内容。蓝色输出。"""
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if getattr(block, "type", None) == "text":
                terminal_print(f"\033[34m{block.text}\033[0m")


def cron_autorun_loop(history: list, context: dict):
    """Cron 自动运行守护线程：每 1s 检查 cron_queue，有任务时持锁调用 agent_loop。
    与用户输入互斥（通过 agent_lock），确保 session_history 不被并发破坏。"""
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({"role": "user",
                                "content": f"[Scheduled] {job.prompt}"})
                terminal_print(
                    f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)


if __name__ == "__main__":
    # 交互入口。流程:
    #   1. 打印欢迎信息
    #   2. 启动 cron_autorun_loop 守护线程
    #   3. 循环读取用户输入（青色提示符 s20 >>）
    #   4. 触发 UserPromptSubmit hooks
    #   5. 持锁调用 agent_loop（防止与 cron 队列处理并发）
    #   6. consume_lead_inbox 收取 teammate 结果
    #   7. q/exit/空 → 退出
    CLI_ACTIVE = True
    print("s20: comprehensive agent")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context), daemon=True).start()
    while True:
        try:
            query = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        turn_start = len(history)
        history.append({"role": "user", "content": query})
        with agent_lock:
            agent_loop(history, context)
            context = update_context(context, history)
            print_turn_assistants(history, turn_start)

        # 消费 Lead 收件箱：路由协议 + 注入历史
        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            def inbox_label(msg):
                req_id = msg.get("metadata", {}).get("request_id", "")
                suffix = f" req:{req_id}" if req_id else ""
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = "\n".join(
                f"From {m['from']} [{inbox_label(m)}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()
