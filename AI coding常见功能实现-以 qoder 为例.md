# AI Coding 常见功能实现——以 Qoder 为例

> 本文是《Claude Code 实现原理拆解：一个 Agent Harness 的 20 层结构》的姊妹篇。原理篇讲的是"这类编程 Agent 内部怎么运转"，本文讲的是"这些机制落到日常使用的 AI 编程工具上，对应什么功能、怎么用"，以 Qoder 为例逐一对应。
>
> **阅读方式**：全文 20 个小节，与原理篇的 20 个章节一一对应。每节先一句话说明原理篇对应章节在解决什么问题，再介绍 Qoder 中的对应功能、触发或配置方式，并给出官方文档链接。
>
> **说明**：（1）文中截图位置以占位标记给出，后续补充实际截图。（2）Qoder 产品迭代较快，文中命令、默认值与行为以撰写时版本为准，落地前建议对照官方文档确认。

---

## 对应关系总览

| # | 原理篇章节 | Qoder 对应功能 | 一句话说明 |
|---|---|---|---|
| 01 | Agent Loop | Agent 交互模式 | 循环本身内置，用户直接用自然语言驱动 |
| 02 | Tool Use | 内置工具集 | 文件读写、搜索、终端等开箱即用，`/tools` 查看 |
| 03 | Permission | 权限控制 | allow / ask / deny 三档，模式 + 规则双层配置 |
| 04 | Hooks | Hooks | 在关键节点注入自定义逻辑，可拦截工具调用 |
| 05 | TodoWrite | 待办列表 | 复杂任务自动生成，执行中实时更新 |
| 06 | Subagent | 子代理 | 独立上下文的专职 Agent，可自定义 |
| 07 | Skill Loading | Skills | 目录常驻、内容按需加载的可复用能力 |
| 08 | Context Compact | 上下文管理 | `/compact` 压缩、`/context` 查看、`/rewind` 回退 |
| 09 | Memory | 记忆 | 项目说明文件 + 规则 + 自动记忆 |
| 10 | System Prompt | 上下文自动组装 | 规则、记忆、Skill 在运行时分层注入 |
| 11 | Error Recovery | 错误自愈 | 失败信息回灌自动重试 + 检查点恢复 |
| 12 | Task System | 任务与看板 | `/tasks` `/kanban` 管理持久化任务 |
| 13 | Background Tasks | 后台执行 | 慢命令丢后台、后台子代理 |
| 14 | Cron Scheduler | 定时任务 | 自然语言创建，cron 表达式触发 |
| 15 | Agent Teams | 多智能体协作 | Agent Teams（beta） |
| 16 | Team Protocols | （并入 15） | 通信机制内置于协作能力中 |
| 17 | Autonomous Agents | Quest 自主任务 | 设定目标后自主执行到达成 |
| 18 | Worktree Isolation | Worktree 并行 | 一任务一 worktree，改动互不覆盖 |
| 19 | MCP Plugin | MCP 与插件 | 外部工具接入同一工具池，插件打包分发 |
| 20 | 综合架构 | 全景：机制归一个循环 | CLI 与 IDE 各能力如何协同 |

---

## 01 Agent Loop：Qoder 的 Agent 交互模式

> 原理篇第 01 节：LLM 不会自己执行命令、也看不到执行结果，需要一个"帮它跑、并把结果喂回去"的循环——ReAct（思考 → 行动 → 观察）。

在 Qoder 中，这个循环就是它的**默认工作方式**，不需要任何配置。进入项目目录启动 Qoder CLI，看到的交互界面背后就是一个 agent loop：你用自然语言提出任务，模型推理后调用工具执行，把执行结果作为观察喂回模型，如此循环，直到模型判断任务完成并给出回复。

**怎么用**：

```bash
cd /path/to/your-project
qoder  # qodercli 也可以
```

看到对话提示符后直接描述任务即可，例如"帮我梳理这个项目的整体架构和主要模块"——Qoder 会自主决定搜索哪些文件、读取哪些代码，多轮循环后给出带文件位置的回答。

日常使用中可以观察到的循环痕迹：

- 执行每一步前，界面会显示它正在调用的工具（读文件、跑命令等），这就是原理篇说的 Action；
- 执行有副作用的操作前会请求确认（对应第 03 节权限）；
- 输入 `!` 可进入 Bash 模式直接运行 shell 命令，`/status` 查看版本与模型状态。

对应原理篇的一句话：**退出条件由模型决定，不由代码决定**——你在 Qoder 中看到它"停下来了"，是因为模型判断任务完成，而不是循环次数到了上限。

**官方文档**：

- 快速上手：<https://docs.qoder.com/zh/cli/quickstart>
- 运行任务（交互方式与启动参数）：<https://docs.qoder.com/zh/cli/run-tasks>

<!-- 截图：Qoder CLI 一次多轮任务执行过程（可见工具调用、确认请求、最终回复） -->

---

## 02 Tool Use：内置工具集

> 原理篇第 02 节：工具定义和处理函数分成两张表（Dispatch Map），主循环只查表——"加工具"不用"改循环"。

Qoder 安装后即自带一组内置工具，覆盖日常开发的主要操作：文件读写与编辑、代码搜索、终端命令执行、网页抓取等。这些工具都注册在同一个工具池里，走同一套权限检查（见第 03 节）——这正是原理篇"循环不动、查表加工具"的产品形态。

**怎么用**：

- 会话中输入 `/tools` 查看当前可用的内置工具；
- 工具由模型按需自动调用，你不需要指定"用哪个工具"，描述清楚目标即可；
- 扩展工具池的两条路：接入 **MCP 服务**引入外部工具（见第 19 节），或通过 **Hooks** 在工具执行前后注入自己的逻辑（见第 04 节）。

与原理篇对应的两个观察点：

- **结构化参数**：每个工具都有明确的输入参数定义，模型产出的是结构化调用而不是一行需要解析的字符串——这是工具调用可靠性的来源；
- **并行执行**：彼此独立、无顺序依赖的工具调用会被并发执行（例如同时读多个文件），你可以在执行过程中观察到多个工具同时进行。

**官方文档**：

- 内置能力总览（斜杠命令、子代理、Skills、工作流）：<https://docs.qoder.com/zh/cli/built-ins>
- 斜杠命令完整清单：<https://docs.qoder.com/zh/cli/slash-reference>

<!-- 截图：/tools 命令输出的内置工具列表 -->
<!-- 截图：一次并行工具调用的执行过程（如同时检索多个文件） -->

---

## 03 Permission：权限控制

> 原理篇第 03 节：每次工具调用前过一道权限闸门，默认拒绝，规则可配置。

Qoder 的权限系统在**每次工具调用前**做一次检查，结果只有三种：`allow`（直接执行）、`ask`（请求确认）、`deny`（拒绝）。适用于文件读写、Bash 命令、网页抓取、MCP 工具、子代理等所有工具类型。

### 权限模式

权限模式决定整体策略，在交互会话中按 **Shift+Tab** 循环切换：

| 模式 | 行为 |
|---|---|
| `default` | 安全读取类操作自动执行，敏感操作请求确认 |
| `accept_edits` | 自动批准工作目录内的文件编辑，Shell 命令仍需确认 |
| `auto` | 零弹窗，安全操作自动批准，风险动作拒绝或交由判定 |
| `bypass_permissions` | 跳过所有确认（仅用于可信本地实验） |
| `dont_ask` | 从不弹窗，原本需要确认的动作直接拒绝（适合无人值守流程） |

### 权限规则

模式之上还可以配置精确规则，判定顺序为：**deny 优先 → 工具安全检查 → ask 规则 → allow 规则**。

持久化配置写在 `settings.json` 的 `permissions` 分组，三个位置按优先级从低到高：

```json
{
  "permissions": {
    "allow": ["Bash(git status)", "Bash(git diff:*)"],
    "ask": ["Bash(npm install:*)"],
    "deny": ["Bash(rm -rf:*)"]
  }
}
```

- `~/.qoder/settings.json`——用户级，所有项目生效；
- `<project>/.qoder/settings.json`——项目级，随仓库提交、团队共享；
- `<project>/.qoder/settings.local.json`——本机私有，建议加入 `.gitignore`。

会话中也可以用 `/permissions` 查看与调整规则，或用 `/allow`、`/deny` 快速增删。

### 一个必须知道的行为差异

**非交互（headless）场景下，`ask` 会被自动当作拒绝**。所以想让 Qoder 在脚本或流水线里自动跑，必须提前把权限规则配好（或用 `--allowed-tools` 显式放行），否则表现会是"什么都没做就结束了"。

**官方文档**：<https://docs.qoder.com/zh/cli/permissions>

<!-- 截图：一次 ask 弹窗确认的实际样子 -->
<!-- 截图：/permissions 面板 -->

---

## 04 Hooks：把自定义逻辑插进关键节点

> 原理篇第 04 节：扩展点从主循环里搬出去——不改循环代码，在工具调用前后等时机挂载自己的逻辑。

Hooks 允许在 Qoder 的关键节点介入执行流，且与 CLI 本身解耦：通过 JSON 配置定义，不用改任何代码。典型用途：工具执行前拦截危险操作、写文件后自动跑 lint、任务完成后发通知。

### 常用事件

事件清单较长（完整清单见官方文档），日常最常用的是这几个：

| 事件 | 触发时机 | 可否阻塞 |
|---|---|---|
| `PreToolUse` | 工具调用前 | ✅ 可拦截 |
| `PostToolUse` | 工具调用成功后 | —（做副作用） |
| `SessionStart` | 会话开始 | —（注入上下文） |
| `Stop` | 主 Agent 停止响应时 | — |

### 退出码契约（关键知识）

Hook 脚本通过 stdin 接收 JSON 输入，通过退出码控制行为：

- `exit 0`：放行；
- `exit 2`：**阻塞**本次操作，stderr 内容会作为反馈返回给 Agent——写清"为什么被拦 + 应该怎么做"，Agent 看到后会自己换做法；
- 其他退出码：非阻塞错误，记录但不中断。

### 示例：拦截危险命令

第一步，创建脚本 `~/.qoder/hooks/block-rm.sh`：

```bash
#!/bin/bash
input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command')

if echo "$command" | grep -q 'rm -rf'; then
  echo "危险命令已被阻止: $command" >&2
  exit 2
fi

exit 0
```

第二步，在 `~/.qoder/settings.json` 中注册：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "~/.qoder/hooks/block-rm.sh" }
        ]
      }
    ]
  }
}
```

第三步，启动 Qoder CLI，让它执行一条包含 `rm -rf` 的命令，验证被拦截且 Agent 收到了原因说明。

### 使用提醒

- Hook 脚本要快、要幂等，建议设置 `timeout`；`async: true` 适合格式化这类不阻塞主流程的副作用；
- 除 shell 命令外，hook 还支持 HTTP 请求、单次模型调用判定、子 Agent 校验等类型，进阶用法见官方文档；
- 当前目录未被信任时 Hooks 不会加载——排查不生效问题先查这一条；
- 会话内可用 `/hooks` 管理已配置的 Hooks。

**官方文档**：<https://docs.qoder.com/zh/cli/hooks>

<!-- 截图：hook 拦截生效的会话现场（exit 2 + stderr 反馈回到对话） -->

---

<!-- 后续小节（05–20）将按批次补充 -->
