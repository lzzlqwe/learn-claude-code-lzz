#!/usr/bin/env python3
"""
s01_agent_loop.py - 智能体循环（Agent Loop）

一个 AI 编程智能体的全部秘密，浓缩为一个模式：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)   # 将对话和工具发给大模型
        execute tools                      # 执行模型要调用的工具
        append results                     # 把工具结果追加回对话

    +----------+      +-------+      +---------+
    |   用户    | ---> |  LLM  | ---> |  工具    |
    |   提示词  |      |       |      |  执行    |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   工具结果     |
                          +---------------+
                          （循环继续）

这是核心循环：把工具执行结果不断喂回给模型，直到模型决定停止。
生产级智能体在此基础上叠加了策略层、钩子层和生命周期控制。

用法：
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/code.py
"""

import os
import subprocess

# 尝试启用 readline，以支持终端中的行编辑功能（如方向键翻历史命令）
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass  # Windows 上通常没有 readline，忽略即可

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量，override=True 表示用 .env 的值覆盖已有的环境变量
load_dotenv(override=True)

# 如果配置了自定义 BASE_URL（例如代理/中转服务），则清除 AUTH_TOKEN，避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 创建 Anthropic 客户端，支持自定义 API 地址
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型它是一个编码智能体，用 bash 解决问题，直接行动不要啰嗦
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── 工具定义：只提供一个 bash 工具 ────────────────────────────
TOOLS = [{
    "name": "bash",
    "description": "执行 Shell 命令。",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# ── 工具执行函数 ────────────────────────────────────────
def run_bash(command: str) -> str:
    """在子进程中执行 bash 命令，并返回执行结果。

    安全措施：
    - 拦截危险命令（如 rm -rf /、sudo 等）
    - 超时限制 120 秒
    - 输出截断至 50000 字符
    """
    # 危险命令黑名单，防止误操作破坏系统
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        # shell=True 允许执行管道、重定向等 Shell 语法
        # capture_output=True 捕获 stdout 和 stderr
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ── 核心模式：一个 while 循环，反复调工具，直到模型说停 ──
def agent_loop(messages: list):
    """智能体主循环。

    流程：
    1. 将对话消息 + 工具定义发给 LLM
    2. 如果 LLM 返回 stop_reason == "tool_use"，说明它想调工具
    3. 执行工具，把结果追加到对话历史中
    4. 回到步骤 1，继续循环
    5. 如果 stop_reason != "tool_use"（通常为 "end_turn"），循环结束
    """
    while True:
        # 调用 Anthropic API，传入对话历史、系统提示词、工具定义
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # 将模型的回复追加到对话历史中（assistant 角色）
        messages.append({"role": "assistant", "content": response.content})

        # 如果模型没有请求工具调用，说明对话结束，退出循环
        if response.stop_reason != "tool_use":
            return

        # 遍历模型回复中的每个内容块，找出工具调用并逐个执行
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 黄色打印命令本身，方便观察模型在做什么
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                # 品红色加粗标签 + 品红色内容，突出显示工具执行结果
                print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        # 将工具执行结果以 user 角色追加回对话，让模型能看到执行结果
        messages.append({"role": "user", "content": results})


# ── 入口：交互式命令行 ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []  # 对话历史，贯穿整个交互会话
    while True:
        try:
            # 青色提示符，等待用户输入
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break  # Ctrl+C 或 Ctrl+D 退出

        # 输入 q / exit / 空行 均可退出
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 将用户消息追加到对话历史
        history.append({"role": "user", "content": query})

        # 启动智能体循环，模型会自动调用工具直到完成
        agent_loop(history)

        # 循环结束后，蓝色打印模型的最终文本回复
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(f"\033[34m{block.text}\033[0m")
        print()
