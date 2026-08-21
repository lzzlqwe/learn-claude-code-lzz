> 本文是《Claude Code 实现原理拆解：一个 Agent Harness 的 20 层结构》的姊妹篇。原理篇讲的是"这类编程 Agent 内部怎么运转"，本文讲的是"这些机制落到日常使用的 AI 编程工具上，对应什么功能、怎么用"，以 Qoder 为例逐一对应。
>
> **阅读方式**：全文与原理篇的 20 个机制逐一对应（其中 15–17 在 Qoder 侧对应同一个功能，合并为一节，正文共 18 节）。每节先一句话说明原理篇对应章节在解决什么问题，再介绍 Qoder 中的对应功能、触发或配置方式，并给出官方文档链接。
>
> **说明**：（1）Qoder 产品迭代较快，文中命令、默认值与行为以撰写时版本为准，落地前建议对照官方文档确认。（2）截图基于撰写时的实际使用环境，界面可能随版本变化。
>

---

## 对应关系总览
| # | 原理篇章节 | Qoder 对应功能 | 一句话说明 |
| --- | --- | --- | --- |
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
| 12 | Task System | 任务系统 | TaskCreate 等 4 个内置工具管理任务图，带状态与依赖 |
| 13 | Background Tasks | 后台执行 | 慢命令丢后台、后台子代理，`/tasks` 查看 |
| 14 | Cron Scheduler | 定时与循环执行 | 自然语言触发 `/loop` Skill → CronCreate 建任务 |
| 15–17 | Agent Teams / Team Protocols / Autonomous Agents | Agent Teams（beta） | main Agent + teammate、SendMessage 通信、shared Task 自主认领；附单 Agent 的 `/goal` |
| 18 | Worktree Isolation | Worktree 并行 | 一任务一 worktree，改动互不覆盖 |
| 19 | MCP Plugin | MCP 与插件 | 外部工具接入同一工具池，插件打包分发 |
| 20 | 综合架构 | 全景：机制归一个循环 | CLI 与 IDE 各能力如何协同 |


---

## 01 Agent Loop：Qoder 的 Agent 交互模式
> 原理篇第 01 节：LLM 不会自己执行命令、也看不到执行结果，需要一个"帮它跑、并把结果喂回去"的循环——ReAct（思考 → 行动 → 观察）。
>

在 Qoder 中，这个循环就是它的**默认工作方式**，不需要任何配置。进入项目目录启动 Qoder CLI，看到的交互界面背后就是一个 agent loop：你用自然语言提出任务，模型推理后调用工具执行，把执行结果作为观察喂回模型，如此循环，直到模型判断任务完成并给出回复。

**怎么用**：

```bash
cd /path/to/your-project
qoder  # qodercli 也可以
```

看到对话提示符后直接描述任务即可，例如"帮我梳理这个项目的整体架构和主要模块"——Qoder 会自主决定搜索哪些文件、读取哪些代码，多轮循环后给出带文件位置的回答。

日常使用中可以观察到的循环痕迹：

+ 执行每一步前，界面会显示它正在调用的工具（读文件、跑命令等），这就是原理篇说的 Action；
+ 执行有副作用的操作前会请求确认（对应第 03 节权限）；
+ 输入 `!` 可进入 Bash 模式直接运行 shell 命令，`/status` 查看版本与模型状态。

对应原理篇的一句话：**退出条件由模型决定，不由代码决定**——你在 Qoder 中看到它"停下来了"，是因为模型判断任务完成，而不是循环次数到了上限。

**官方文档**：

+ 快速上手：[https://docs.qoder.com/zh/cli/quickstart](https://docs.qoder.com/zh/cli/quickstart)
+ 运行任务（交互方式与启动参数）：[https://docs.qoder.com/zh/cli/run-tasks](https://docs.qoder.com/zh/cli/run-tasks)

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787057535582-2fcc9169-9d63-4ccc-be63-05e25bfe4905.png" width="901" title="" crop="0,0,1,1" id="udcdd9b5f" class="ne-image">

---

## 02 Tool Use：内置工具集
> 原理篇第 02 节：工具定义和处理函数分成两张表（Dispatch Map），主循环只查表——"加工具"不用"改循环"。
>

Qoder 安装后即自带一组内置工具（目前32个），覆盖日常开发的主要操作：文件读写与编辑、代码搜索、终端命令执行、网页抓取。这些工具都注册在同一个工具池里，走同一套权限检查（见第 03 节）

**怎么用**：

+ 会话中输入 `/tools` 查看当前可用的内置工具；

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787058480722-09ab404f-14b3-45b3-b790-0e4aaaca0b7a.png" width="1494" title="" crop="0,0,1,1" id="ufb8e4c51" class="ne-image">

+ 工具由模型按需自动调用，你不需要指定"用哪个工具"，描述清楚目标即可；
+ 扩展工具池的两条路：接入 **MCP 服务**引入外部工具（见第 19 节），或通过 **Hooks** 在工具执行前后注入自己的逻辑（见第 04 节）。

与原理篇对应的两个观察点：

+ **结构化参数**：每个工具都有明确的输入参数定义，模型产出的是结构化调用而不是一行需要解析的字符串——这是工具调用可靠性的来源；
+ **并行执行**：彼此独立、无顺序依赖的工具调用会被并发执行（例如同时读多个文件），你可以在执行过程中观察到多个工具同时进行。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787058329601-0ecd4d97-71fb-4e76-b130-306a09172181.png" width="853" title="" crop="0,0,1,1" id="meNoG" class="ne-image">

**官方文档**：

+ 内置能力总览（斜杠命令、子代理、Skills、工作流）：[https://docs.qoder.com/zh/cli/built-ins](https://docs.qoder.com/zh/cli/built-ins)
+ 斜杠命令完整清单：[https://docs.qoder.com/zh/cli/slash-reference](https://docs.qoder.com/zh/cli/slash-reference)

---

## 03 Permission：权限控制
> 原理篇第 03 节：每次工具调用前过一道权限闸门，默认拒绝，规则可配置。
>

Qoder 的权限系统在**每次工具调用前**做一次检查，结果只有三种：`allow`（直接执行）、`ask`（请求确认）、`deny`（拒绝）。适用于文件读写、Bash 命令、网页抓取、MCP 工具、子代理等所有工具类型。

### 权限模式
权限模式决定整体策略，在交互会话中按 **Shift+Tab** 循环切换：

| 模式 | 行为 |
| --- | --- |
| `default` | 安全读取类操作自动执行，敏感操作请求确认 |
| `accept_edits` | 自动批准工作目录内的文件编辑，Shell 命令仍需确认 |
| `auto` | 零弹窗，安全操作自动批准，风险动作拒绝或交由判定 |
| `bypass_permissions（yolo）` | 跳过所有确认（仅用于可信本地实验） |
| `dont_ask` | 从不弹窗，原本需要确认的动作直接拒绝（适合必须不弹窗的 headless 流程） |


### 权限规则
模式之上还可以配置精确规则，判定顺序为：**deny 优先 → 工具自身的安全检查（如危险命令检测、敏感路径检测） → ask 规则 → allow 规则**。

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

+ `~/.qoder/settings.json`——用户级，所有项目生效；
+ `<project>/.qoder/settings.json`——项目级，随仓库提交、团队共享；
+ `<project>/.qoder/settings.local.json`——本机私有，建议加入 `.gitignore`。

使用规范工具名：Read、Edit、Write、Bash、Grep、Glob、WebFetch、WebSearch、Agent，以及 mcp__github__create_issue 这类 MCP 工具名。格式为：

```json
ToolName(content)：作用于某个路径、命令、agent 类型，或工具支持的其他特定内容。
```

会话中也可以用 `/permissions` 查看与调整规则。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787107042213-f3aca7de-d232-4b58-a5ed-fd03c319a02b.png" width="484" title="" crop="0,0,1,1" id="u3d703b6e" class="ne-image">

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787106985345-a84bd5cc-6fad-4314-a96e-33a86b83cef6.png" width="638" title="" crop="0,0,1,1" id="u1d4ff2fd" class="ne-image">

注意如果是自己显式执行`!git diff`则不会被拦截！

### 一个必须知道的行为差异
**非交互（headless）场景下，**`ask`** 会被自动当作拒绝**。所以想让 Qoder 在脚本或流水线里自动跑，必须提前把权限规则配好（或用 `--allowed-tools` 显式放行指定工具）。

**官方文档**：[https://docs.qoder.com/zh/cli/permissions](https://docs.qoder.com/zh/cli/permissions)

---

## 04 Hooks：把自定义逻辑插进关键节点
> 原理篇第 04 节：扩展点从主循环里搬出去——不改循环代码，在工具调用前后等时机挂载自己的逻辑。
>

Hooks 允许在 Qoder 的关键节点介入执行流，且与 CLI 本身解耦：通过 JSON 配置定义，不用改任何代码。典型用途：工具执行前拦截危险操作、写文件后自动跑 lint、任务完成后发通知。

### 常用事件
事件清单较长（完整清单见官方文档），日常最常用的是这几个：

| 事件 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `PreToolUse` | 工具调用前 | 权限检查、日志记录 |
| `PermissionRequest` | 权限判定为 ask、弹窗确认前 | 可自动批准/拒绝，钉钉提示 |
| `PostToolUse` | 工具调用成功后 | 副作用（自动 git add 等）、输出检查 |
| `SessionStart` | 会话开始 | 输入验证、注入上下文 |
| `Stop` | 主 Agent 停止响应时 | 收尾清理 |


### Hook 与权限决策
Hook 有两个直接参与权限链路的注入点（ `PreToolUse` 和 `PermissionRequest` ）

整体执行顺序：

1. Hook `PreToolUse` —— 返回 allow/deny 则短路；
2. 权限管道（规则 + 模式 + 安全检查，见第 03 节）；
3. 结果为 `ask` 时 → Hook `PermissionRequest` —— 返回 allow/deny 则短路；
4. 最终由运行环境消费 `ask`（弹窗确认 / headless 下拒绝 / 回调）。

关键点：**Hook 的权限决策优先级高于权限模式**——即使在 `bypass_permissions（yolo）` 模式下，`PreToolUse` 返回 `deny` 依然会阻止执行。

### Hook 脚本编写
Hook 脚本通过 stdin 接收 JSON 输入，通过 exit code 和 stdout 控制行为。退出码有：

+ `exit 0`：放行；
+ `exit 2`：**阻塞**本次操作，stderr 内容会作为反馈返回给 Agent——写清"为什么被拦 + 应该怎么做"，Agent 看到后会自己换做法；
+ 其他退出码：非阻塞错误，记录但不中断。

详见官方文档：[Hook 脚本编写](https://docs.qoder.com/zh/cli/hooks#hook-%E8%84%9A%E6%9C%AC%E7%BC%96%E5%86%99)

### 示例：拦截危险命令（PreToolUse）
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

### 示例：权限弹窗前播放提示音（PermissionRequest）
日常使用中的真实痛点：把 Qoder 挂在后台干活，自己去处理别的事，等回来才发现它早就停在权限确认弹窗上等了半天。用 `PermissionRequest` hook 可以在每次需要授权时播放一段系统提示音：

第一步，创建脚本 `~/.qoder/hooks/notify-ask.sh`：

```bash
#!/bin/bash
cat > /dev/null                     # 读掉 stdin，防止管道阻塞
afplay /System/Library/Sounds/Glass.aiff   # macOS 系统自带提示音
exit 0                              # 不输出 JSON，ask 继续走正常弹窗流程
```

第二步，在 `~/.qoder/settings.json` 中注册：

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "hooks": [
          { "type": "command", "command": "~/.qoder/hooks/notify-ask.sh" }
        ]
      }
    ]
  }
}
```

第三步，启动 Qoder CLI，让它执行一条会触发权限确认的操作（如安装依赖），听到提示音回到终端处理弹窗即可。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787118044410-dd1001ec-fe7c-4b0d-9616-639ac7b678f5.png" width="704" title="" crop="0,0,1,1" id="u90a44f10" class="ne-image">

这个脚本故意不输出任何 JSON：这里 hook 不做决策，ask 照常弹窗，只起提醒作用。想换成语音播报，把 `afplay` 一行换成 `say "Qoder 需要你的确认"` 即可。

### 使用提醒
+ Hook 脚本要快、要幂等，建议设置 `timeout`；`async: true` 适合格式化这类不阻塞主流程的副作用；
+ 除 shell 命令外，hook 还支持 HTTP 请求、单次模型调用判定、子 Agent 校验等类型，进阶用法见官方文档；
+ 当前目录未被信任时 Hooks 不会加载——排查不生效问题先查这一条；
+ 会话内可用 `/hooks` 管理已配置的 Hooks。

**官方文档**：

1. [https://docs.qoder.com/zh/cli/hooks](https://docs.qoder.com/zh/cli/hooks)
2. [Hook 与权限](https://docs.qoder.com/zh/cli/permissions#hook-%E4%B8%8E%E6%9D%83%E9%99%90)

---

## 05 TodoWrite：自动生成的待办列表
> 原理篇第 05 节：复杂任务中 Agent 容易丢步骤、跑偏方向，需要一个外挂的"工作记忆"——把计划显式写成清单，做一项勾一项。
>

这个机制在 Qoder 中是**内置且自动的**，不需要任何配置：当你给出一个多步骤的复杂任务时，Qoder 会先自己创建一份待办列表（Todo List），把任务拆成若干条目，然后逐条执行并实时更新状态（待处理 / 进行中 / 已完成）。

**怎么触发**：

+ 在实现一个**复杂的需求**或给一个**多步骤任务**，例如："帮我重构 xxx 模块，补齐单元测试，并更新对应文档"。
+ 简单的单步请求（如"这个函数是干什么的"）通常不会出现待办列表；
+ 执行过程中可以直接在界面上看到列表的创建与逐项勾选。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787121183356-1a90d846-010f-4b48-aed6-4debf3bcf232.png" width="641" title="" crop="0,0,1,1" id="ue7a91d7b" class="ne-image">

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787121241359-2831a91c-f86a-439c-8e05-2e0924aa3bdd.png" width="639" title="" crop="0,0,1,1" id="uae092573" class="ne-image">

**日常使用价值**：待办列表是可观察的"执行计划"——任务跑偏时你能第一时间看到它打算做什么、做到哪一步，及时打断纠正；同时它也是长任务在上下文压缩后仍能继续推进的原因之一（上下文压缩见第 08 节）。

**注意区分：**这里的待办列表是当前会话内的执行清单（保存在当前进程内存中，关掉会话即消失）；跨会话、带依赖的项目级任务管理是另一层能力，见第 12 节任务系统（持久化，非内存列表）。

---

## 06 Subagent：子代理
> 原理篇第 06 节：子 Agent 用独立上下文执行子任务，只把结果带回主会话——隔离的是上下文，不是权限。
>

Qoder 的子代理（Subagent）是专门处理某类任务的 Agent，可以拥有自己的系统提示词、工具集合、模型配置、权限模式、运行限制和 Hook。适合把代码探索、方案设计、接口审查这类工作拆给更聚焦的执行者，避免大量中间搜索过程塞进主会话上下文。

### qoder 内置子代理
开箱即用的常用内置子代理（输入`/agents` 即可查看）：

| 名称 | 能力 |
| --- | --- |
| `general-purpose` | 通用研究：复杂搜索、多文件分析、多步骤任务；未显式指定时的默认选择 |
| `Explore` | 只读代码探索：查文件、搜符号、理解现有实现 |
| `Plan` | 只读方案设计：梳理实现路径、关键文件与依赖顺序 |


### 怎么自定义 subagent
1. 推荐用 AI 辅助生成：`/agents` 打开配置面板 → 切换到 User 或 Project 标签页 → Create new agent → 用自然语言描述需求，自动生成配置文件后再微调。
2. 也可以手动编写 Markdown 配置，YAML frontmatter 声明配置、正文就是系统提示词：

```markdown
<!-- 项目级：.qoder/agents/api-reviewer.md（随仓库提交）
     用户级：~/.qoder/agents/api-reviewer.md（所有项目生效） -->
---
name: api-reviewer
description: Review API designs, endpoint naming, status codes, and versioning.
tools: [Read, Grep, Glob]
permissionMode: default
maxTurns: 8
---

You are an API design reviewer.

Focus on resource naming, request method semantics, status code
consistency, and versioning. Return findings grouped by severity.
```

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787125347930-e4def06c-3dbd-4dc9-9afa-4e117dc15f6b.png" width="561" title="" crop="0,0,1,1" id="u418f0c54" class="ne-image">

修改配置后可用 `/agents reload` 重新加载。需要子代理在独立工作目录中改动代码时，frontmatter 加一行 `isolation: worktree`（worktree 隔离见第 18 节）。

+ **更多更专业的 subagent 的定义**可以参考这个很火的GitHub项目：[https://github.com/affaan-m/ECC/tree/main/agents](https://github.com/affaan-m/ECC/tree/main/agents)

### 怎么调用
+ **显式调用**（最稳定）：直接写出名称，例如 `使用 api-reviewer subagent 审查这个接口设计`，或 `@api-reviewer 审查这个接口设计`；

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787124356190-b9e516ab-f09c-4b80-9b35-24896b7a8037.png" width="505" title="" crop="0,0,1,1" id="u7e5cd738" class="ne-image">

+ **隐式调用**：直接描述任务（如"帮我做一次接口设计审查"），Qoder 根据子代理的 `description` 自动选择；
+ **作为主 Agent**：`qoder --agent api-reviewer` 启动时把某个子代理作为本次会话的主 Agent；
+ **编排多个**：`先使用 general-purpose subagent 检查实现方案，再使用 api-reviewer subagent 审查 API 设计`；彼此独立的任务可以要求并行调度。

**官方文档**：[https://docs.qoder.com/zh/cli/subagent](https://docs.qoder.com/zh/cli/subagent)

---

## 07 Skill Loading：Skills 按需加载
> 原理篇第 07 节：知识不能全塞进上下文——名称和描述常驻，正文按需加载，越用越深。
>

Qoder 的技能（Skill）是大伙平时工作用的最多了：每个 Skill 是一个包含 `SKILL.md` 的目录，把专业知识和工作流打包成可复用能力。

**加载机制**：

1. 启动时只加载每个 Skill 的**名称和描述**，不占多少上下文；（和 tools 类似，各个Skill的元信息拼接到 System prompt 里面）sd
2. 用户请求与某个描述匹配时，模型决定加载该 Skill 的**完整 SKILL.md**；

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787125853016-37a25ae2-035a-40ab-96bf-c7488cf10168.png" width="1371" title="" crop="0,0,1,1" id="uc97f1c35" class="ne-image">

3. 执行中再按需读取 Skill 目录里的引用文件、运行脚本——渐进式披露，越用越深。

**怎么触发**：模型根据请求内容自动判断（`description` 里写清用户常用的关键词能显著提高命中率），也可以输入 `/skill-name` 显式加载。内置 Skills 如 `/simplify`（代码简化审查）、`/debug`（调试助手）、`/security-scan`（安全扫描）等可直接使用。

### 怎么创建
第一步，建目录。用户级对所有项目生效，项目级随仓库共享给团队：

```bash
# 用户级：~/.qoder/skills/{skill-name}/SKILL.md
# 项目级：.qoder/skills/{skill-name}/SKILL.md
mkdir -p ~/.qoder/skills/api-doc-generator
```

第二步，编写 `SKILL.md`——YAML 元数据（`name` 和 `description` 必填）+ Markdown 指令：

+ 推荐用 GitHub很火的`supperpower`插件里面的`writing-skills` 来写。

```markdown
---
name: xxx
description: xxx
---

xxx
```

第三步，验证。新会话启动时自动加载；已在运行的会话用 `/skills reload` 刷新，输入 `/skills` 查看可用列表。

目录里还可以放辅助文件（参考文档、脚本、模板），在 `SKILL.md` 中引用即可。集团内部同学也可以在 **Aone 开放平台**（[https://open.aone.alibaba-inc.com/market](https://open.aone.alibaba-inc.com/market)，内网）上查找现成的 Skill 与工具资源直接安装使用。

**官方文档**：[https://docs.qoder.com/zh/cli/Skills](https://docs.qoder.com/zh/cli/Skills)

---

## 08 Context Compact：上下文管理
> 原理篇第 08 节：上下文窗口有限，需要分层压缩策略——便宜的先跑，尽量保住关键信息。
>

Qoder 把上下文管理做成了几个日常可用的操作：

| 操作 | 作用 |
| --- | --- |
| `/compact` | 主动压缩当前对话上下文，释放窗口空间 |
| `/context` | 查看当前上下文的构成（各部分占比） |
| `/rewind` | 回退到检查点，恢复对话和/或文件改动 |
| `/resume` | 恢复历史会话，接着上次进度继续 |


**自动与手动**：上下文接近上限时 Qoder 会自动触发压缩（生成摘要、腾出空间），通常无感知；长会话中也可以主动 `/compact` 提前压缩，或先 `/context` 看一眼空间还剩多少。

`/rewind`** 检查点恢复**：Qoder 以对话中的每条用户消息为检查点，记录其后的文件编辑历史。输入 `/rewind` 打开回退界面，选择检查点后确认时可选三种恢复范围：

+ 恢复对话和文件（默认）；
+ 仅恢复对话（文件保持现状）；
+ 仅恢复文件（对话保持现状）。

两个使用提醒：回退只覆盖 Qoder 通过文件编辑工具产生的改动，手动编辑或 Shell 命令的改动可能不在管辖范围；重要改动配合 Git 提交更稳妥。

`/resume`** 恢复历史会话**：会话按项目隔离落盘，随时可以恢复。会话内输入 `/resume` 打开会话浏览器选择；命令行 `qoder -r <session-id>` 恢复指定会话，`qoder -c` 直接继续最近一次。

**小技巧：用 HANDOFF.md 交接长任务**。长任务快占满 context 时，可以让它先写一份 `HANDOFF.md`（包括：目标、已经完成什么、什么有效什么无效、下一步做什么）。新会话只需要引用这个文件就能接上。这比压缩可控：压缩丢什么不由你决定，交接文档丢什么由你决定。

**官方文档**：

+ 管理会话：[https://docs.qoder.com/zh/cli/sessions](https://docs.qoder.com/zh/cli/sessions)
+ 撤销与恢复：[https://docs.qoder.com/zh/cli/undo-restore](https://docs.qoder.com/zh/cli/undo-restore)

---

## 09 Memory：让它记住项目约定
> 原理篇第 09 节：压缩会丢细节，需要一层不丢的——把跨会话有效的知识落到磁盘，下次会话重新加载。
>

Qoder 每次会话都会重新构造上下文，跨会话保留的知识来自两类记忆：

| 机制 | 谁来写 | 适合内容 | 查看入口 |
| --- | --- | --- | --- |
| 静态记忆 | 用户或团队 | 包括 AGENTS.md 和 rules。明确、稳定的约定：构建命令、目录结构、代码风格、协作流程 | `/memory` |
| 自动记忆 | Qoder CLI | 从对话中学到的偏好、反馈、项目背景 | `/memory`打开 auto-memory folder；`/memory manage`管理主题文件 |


### 静态记忆：AGENTS.md 与 rules
四个常用位置（注意是否适合 git 提交）：

+ `~/.qoder/AGENTS.md`——个人跨项目偏好，不提交；
+ `<project>/AGENTS.md`——团队共享的项目规则与架构说明，提交；
+ `<project>/AGENTS.local.md`——本机私有（本地服务地址等），不提交；
+ `<project>/.qoder/rules/**/*.md`——按主题或文件范围拆分的 Markdown 规则文件，适合替代单个臃肿的 AGENTS.md，提交。

新项目可以直接 `/init` 生成 AGENTS.md 初始版本。

### 自动记忆
Qoder 会把值得跨会话复用的信息保存为本机 Markdown（记忆比如偏好、反馈、项目背景、外部资料位置）。它是本机文件、不随代码提交、也可能过期。

+ Qoder IDE 和 JetBrains 插件默认开启自动记忆。它会自动记忆一些信息。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787144243005-c0d50a1d-d782-4c54-939e-90185c4659aa.png" width="1894" title="" crop="0,0,1,1" id="CgbMS" class="ne-image">

+ qoderCli默认关闭自动记忆。可以通过`/settings` 搜索 Auto Memory，或 settings.json 中 `"autoMemoryEnabled": true`。

### 自动记忆存储位置
项目级自动记忆保存在当前项目对应的 Qoder 配置目录下：

```latex
~/.qoder/projects/<project>/memory/
```

启用用户级自动记忆后，还会使用：

```latex
~/.qoder/memory/
```

每个自动记忆目录包含一个 `MEMORY.md` 索引和若干主题文件：

```latex
memory/
├── MEMORY.md
├── user-preferences.md
├── feedback-testing.md
└── project-release-context.md
```

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787145246375-62ef23a6-6b10-4c0e-a8ec-e0a5436f74c0.png" width="1392" title="" crop="0,0,1,1" id="unN6B" class="ne-image">

`MEMORY.md` 是索引，用于加载进 System prompt。Qoder CLI 启动时会读取每个活跃自动记忆根的 `MEMORY.md`，最多读取前 200 行或约 25KB。更详细的内容应放在单独的主题文件中，由索引指向。

注意：Qoder IDE 和 JetBrains 的自动记忆存储位置不在上述路径，具体路径如下：

```latex
~/.qoder/memories/xxx/global  # 用户级目录
~/.qoder/memories/xxx/projects/<project>/  # 项目级目录
```

**官方文档**：

1. [https://docs.qoder.com/zh/cli/memory](https://docs.qoder.com/zh/cli/memory)
2. [https://docs.qoder.com/zh/cli/how-memory-works](https://docs.qoder.com/zh/cli/how-memory-works)

---

## 10 System Prompt：上下文是运行时组装的
> 原理篇第 10 节：系统提示词不是写死的常量，而是每次会话按当前环境运行时拼装。
>

Qoder 同样如此——每次会话重新构造上下文，把多层来源按当前环境拼装注入：基础指令（角色，职责这些）、记忆与规则（AGENTS.md、按触发方式加载的 rules）、工具定义与 Skills 元信息、MCP 工具、对话历史。前面几节的机制在这里合流：

+ 第 02 节的工具、第 07 节的 Skill 元信息——常驻拼接；
+ 第 09 节的记忆与规则——按触发方式分层注入；
+ 第 08 节的压缩——窗口吃紧时对历史做摘要。

用户侧的作用：**你能控制的不是提示词本身，而是注入它的原材料**——写好 AGENTS.md、规则和 Skill 描述，组装结果自然变好。几个观察/调试入口：

+ `/context`：查看当前上下文的构成与占比；
+ 规则改完下一轮即生效，不用重启——组装是每轮动态的。
+ 每轮循环开头拿一次 system prompt。context 变了就重新组装，没变就返回缓存。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787193330718-e477386b-6d07-4d01-a906-662fffbb70ac.png" width="788" title="" crop="0,0,1,1" id="VP2qQ" class="ne-image">

这里当前项目下没有记忆文件，那就不需要动态拼接。

---

## 11 Error Recovery：出错之后怎么办
> 原理篇第 11 节：错误分三类（工具失败、模型输出损坏、外部故障），各有恢复路径；核心是"把错误信息喂回去，让模型自己纠正"。
>

Qoder 的日常使用中有三个能直接感知到的恢复机制：

+ **工具失败自动重试**：命令报错、测试失败、路径不存在时，错误输出会作为观察回灌给模型，它通常自己分析原因换一种做法重试——你看到的"它发现报错 → 读日志 → 再改一版"就是这个机制在工作；对应的扩展点是 `PostToolUseFailure` hook（工具失败后触发）；
+ **检查点回退**：反复重试仍走错方向时，用 `/rewind` 回到分岔前重新描述需求（见第 08 节），比在错误路径上继续追加上下文更省成本也更有效；
+ **会话分支**：想保留当前会话另试一条路，用 `/branch` 岔出新会话。

使用建议：让它跑测试/构建命令验证自己的改动（错误信息越完整，自愈越有效）；卡住超过几轮时主动介入——回退或补充信息，而不是任它反复重试烧上下文。

---

## 12 Task System：项目级任务与看板
> 原理篇第 12 节：TodoWrite 是进程内清单，退出就没了；项目级需要落盘的任务图——有状态、有依赖、跨会话跨 Agent。
>

+ Qoder 的 task (任务列表) 功能用到 4 个内置工具：

| 工具 | 用途 | 典型操作 |
| :--- | :--- | :--- |
| TaskCreate | 创建任务 | 传入 subject (标题)、description (详情)、可选 activeForm (进行中时的 spinner 文案)，新任务默认为 pending |
| TaskList | 列出全部任务 | 查看所有任务的 id、标题、状态、依赖 (blockedBy) |
| TaskGet | 获取单个任务详情 | 读取完整描述、评论、blocks/blockedBy 依赖关系 |
| TaskUpdate | 更新任务 | 改状态 (pending → in_progress → completed)、设置依赖 (addBlocks/addBlockedBy)、认领 owner、删除 (status: deleted) |


**状态流转：**

pending → in_progress → completed。

+ 开始处理某任务时置为 `in_progress`，验证通过后立即置为 `completed`
+ 通过 `addBlockedBy` 声明依赖：被阻塞的任务在前置任务完成前不可认领

**存储位置：**

按会话隔离存储，会话结束即失效 (`/resume` 恢复会话时一并恢复)：

```plain
~/.qoder/tasks/<session-id>/
├── 1.json              # 每个任务一个 JSON 文件 (id/subject/description/status/blocks/blockedBy...)
├── .highwatermark      # 任务 ID 自增水位
├── .lock               # 会话级锁
└── .task-locks/        # 任务级锁：<taskId>.lease
```

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787210910520-e3ed4dd6-cc34-468f-a74b-ae650fc0d1ab.png" width="1348" title="" crop="0,0,1,1" id="TYIAw" class="ne-image">

会话的完整对话日志另存于 `~/.qoder/projects/<项目路径编码>/<session-id>.jsonl`，任务工具的每次调用也记录在其中。

**会话锁和任务锁：**

+ 会话锁 `.lock`：保证同一会话的任务存储不会被并发写入破坏
+ 任务锁 `.task-locks/<id>.lease`：租约式认领锁。多 agent 协作（如 subagent 团队）时，agent 认领任务会写入 lease 文件，防止两个 agent 同时处理同一任务；任务完成后锁释放

**使用示例：**

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787211404894-77b1d23f-3e02-45f9-85ef-14b50a9c82d4.png" width="1068" title="" crop="0,0,1,1" id="jIcfR" class="ne-image">

任务 json 长这样：

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787211654061-4610efbf-8dd3-423c-b1fa-f832dc756881.png" width="1600" title="" crop="0,0,1,1" id="B2WHj" class="ne-image">

---
## 13 Background Tasks：慢操作丢后台
> 原理篇第 13 节：`npm install` 跑三分钟，同步等待会让 Agent 白白闲着；判断是慢操作就丢后台线程，完成后把结果作为通知注入下一轮。
>

Qoder 侧有三种后台执行形态：

| 形态 | 说明 |
| --- | --- |
| 后台命令 | 长时间运行的命令（安装依赖、构建、启动服务、跟踪日志）放到后台执行，主对话继续往下做 |
| 后台 Subagent | 启用后台 Subagent 能力时，把独立子任务放到后台跑，完成后再通知主会话（见第 06 节） |
| 动态工作流 | `/workflows` 编排的多 Agent 流程始终作为后台任务运行，启动后返回运行 ID |


**怎么用**：

+ 直接说明意图即可，例如"在后台运行 pip3 list 命令，并查找当前目录下的所有 Python 文件。"——它会把命令丢后台并立刻继续干活；

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787214610227-aa9b0b53-5348-400b-bc45-78f86d9d9cbf.png" width="1134" title="" crop="0,0,1,1" id="CYxWq" class="ne-image">

+ 当任务明显大于一次 Agent 调用时，可以使用动态工作流：仓库审计、深度研究、迁移规划、发版检查、跨文件扫描，或需要多个独立视角后再汇总的评审流程。（动态工作流会把编排计划放到一段 JavaScript 脚本里。脚本决定启动哪些子 Agent、如何划分阶段、如何合并中间结果，以及最终把什么结果返回到当前会话。）

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787214986020-e4963e6e-50b0-445a-8d04-3d2816b9b326.png" width="1494" title="" crop="0,0,1,1" id="iRAhB" class="ne-image">

+ `/tasks` 打开后台任务面板，查看当前正在运行的后台任务及其状态；

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787215073416-d64b1578-eb17-4813-bd4f-90bdea303e2f.png" width="1524" title="" crop="0,0,1,1" id="Ci7q6" class="ne-image">

+ 你可以在当前会话中继续对话。当后台任务完成后，结果会作为通知注入下一轮对话，模型在同一轮里同时看到"当轮工具结果"和"后台完成通知"，可以综合决策。

**官方文档**：[https://docs.qoder.com/zh/cli/workflows](https://docs.qoder.com/zh/cli/workflows)

---

## 14 Cron Scheduler：定时与循环执行
> 原理篇第 14 节：需要一个不依赖人推的触发源——调度器只管"到点没"，队列处理器只管"Agent 空不空"，四层解耦。
>

创建方式很简单——**直接用自然语言描述时间和要做的事**即可：

```plain
每 2min 打印一下当前时间
每天早上 9 点检查一次 CI 状态并汇总
每周一上午 10 点生成上周的提交摘要
```

实测下的完整链路：自然语言描述周期性任务时，模型会**自动触发内置的 **`/loop`** Skill**，再由它调用 `CronCreate` 工具把描述转成 cron 表达式建任务：

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787224950840-77f22f43-2f1c-48e5-81bf-657085b7bca7.png" width="1620" title="" crop="0,0,1,1" id="kWxgk" class="ne-image">

创建成功后会直接告诉你任务 ID、cron 表达式、生效范围与过期时间，且**不等第一个周期、当场先跑一次**。取消用 `CronDelete`（直接说"列出所有定时任务"、"删除定时任务 "即可）。

### 生效范围与限制
**默认仅当前会话有效，进程退出即失效**——关了终端，定时任务就不存在了。需要跨会话保留要显式要求落盘（`/loop` 的 `--durable`），写入 `<project>/.qoder/scheduled_tasks.json`。

<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787226728333-afa62a35-0594-4966-b8e6-04939b55bd33.png" width="1116" title="" crop="0,0,1,1" id="DYhMp" class="ne-image"><img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787226551619-26e07b10-cb7b-411e-a028-78f174988cde.png" width="1136" title="" crop="0,0,1,1" id="gIHBH" class="ne-image">

三条必须知道的限制：

+ 任务数量上限 **50 个**；
+ 周期任务创建后 **7 天自动过期**——需要长期跑就得重建，否则会悄无声息地失效；
+ 最小粒度 **1 分钟**，且触发时间带抖动（周期任务最多后延 15 分钟，避免大量任务集中触发），别用它做要求精确到秒的事。

### 显式用 /loop
想精确控制间隔与预算时，直接调 `/loop`：

```plain
/loop 5m check the deploy
/loop 10m --max-turns 6 check the deploy    # 跑满 6 次后停止
/loop 30m --durable check the deploy       # 落盘，重启后仍在且不自动过期
/loop monitor CI pipeline                 # 不给间隔，由 Qoder 自行决定节奏（60s–3600s）
```

两个防超支的上限值得默认加上：`--max-turns N`（跑满 N 次停止）、`--max-credits N`（累计消耗 N 积分停止），两者先到者停止；轮次按任务整个生命周期累计，重启 CLI 不会清零。`/crontab` 可以打开定时任务面板查看每个循环的用量。

**官方文档**：

+ 定时执行任务：[https://docs.qoder.com/zh/cli/scheduled-tasks](https://docs.qoder.com/zh/cli/scheduled-tasks)
+ 重复执行任务：[https://docs.qoder.com/zh/cli/loop](https://docs.qoder.com/zh/cli/loop)

---

## 15–17 Agent Teams：多智能体协作与自主执行
> 原理篇第 15–17 节：子 Agent 是一次性的、用完即销毁，大项目需要"派出去以后还能继续对话"的持久队友（15）；异步通信需要请求-响应约定，否则消息发出去没人管（16）；任务不用逐个分配，Agent 自己从看板认领、持续推进（17）。
>
> 这三层机制在 Qoder 侧对应的是**同一个功能** —— Agent Teams：持久队友、SendMessage 通信、shared Task 自主认领。
>

Agent Teams 让一个交互式会话变成小型 Agent 团队。交互入口不变——你还是在当前会话里描述目标，main Agent 根据需要创建 teammate，让不同成员并行研究、实现、验证或互相交接。

> **Beta**：当前默认不启用，启动前需打开环境变量开关，执行命令启动 `QODER_AGENT_TEAMS=1 qoder`；也可写入 `~/.qoder/.env` 让后续启动自动启用（改后需重启）。
>

### 两个角色
| 角色 | 职责 |
| --- | --- |
| main Agent | 你直接对话的 Agent：理解目标、拆分任务、汇总结果、报告进展 |
| teammate | main Agent 创建的成员（如 researcher、coder、reviewer），有自己的上下文，可持续接收 Task 和 SendMessage |


### 怎么用
不需要手动建团队，但**最稳定的方式是在请求里明确说“使用 Agent Teams”**并给出分工——只说"并行处理"或"几个agent处理一下"，Qoder 可能会选择普通 Subagent、background task 或 Workflow：

```plain
使用 Agent Teams 处理这个重构。
创建 researcher、coder、reviewer 三个 teammate：
1. researcher 先梳理认证模块的调用链。
2. coder 根据 researcher 的结论修改代码。
3. reviewer 复核改动风险和缺失测试。
最终请给出改动摘要、涉及文件和验证结果。
```

TUI 中在会话底部可以看到 agents 列表，按向下键进入后用上下键选择 main conversation 或某个 teammate，回车切换视图、Esc 返回。

### 协作约定：SendMessage 与 shared Task
这套约定内置在 Agent Teams 里，用户不需要自己实现，但理解它有助于把任务描述写对。

**SendMessage（成员间通信）**：main Agent 与 teammate、teammate 与 teammate 之间靠它沟通问题、发现和结果。关键一点：**teammate 的普通文本输出不会自动广播给其他成员**——需要传给特定成员的信息才通过 SendMessage 走。界面上通常显示为 `Message from @researcher` 这样的提示。

**shared Task list（自主认领）**：记录谁在做什么、进展如何、哪些任务依赖其他任务——正是第 12 节任务工具的 `owner` / `status` / `blockedBy` 字段在多 Agent 场景下的用途，任务锁（`.task-locks/<id>.lease`）也是为此存在，防止两个成员认领同一任务。

```plain
shared Task list
├── [in progress] 梳理旧接口调用点   owner=@researcher
├── [pending]     实现新接口适配     owner=@coder
└── [pending]     补充迁移测试       owner=@tester
```

任务完成不等于 teammate 退出——成员完成后仍留在团队中等下一步。任务超过两三步时，明确要求它建 shared Task，协作状态会清楚很多：

```plain
使用 Agent Teams 和 shared Task list 推进这个迁移。
先创建以下 Task：1. 梳理旧接口调用点 2. 实现新接口适配 3. 补充迁移测试
然后创建 researcher、coder、tester 三个 teammate 分别领取 Task。
请在最终结果里说明每个 Task 的完成情况、关键改动和验证结果。
```

### 生命周期（重要）
**团队只存在于当前 TUI 会话**。teammate 在同一次 TUI 中可以反复 `running → idle → running`，`idle` 只表示暂时没活干、不代表退出。退出 TUI 时 teammate 会被结束、团队状态被清理；之后 `resume` 只恢复主会话历史，**teammate 不会一起回来**，需要让 main Agent 重新创建。

### 使用案例
<img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787290919373-724bdeed-9b6b-4f6e-8298-76dd6063cf3f.png" width="1404" title="" crop="0,0,1,1" id="kGCe8" class="ne-image"><img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787291057554-e9e270c2-ab4e-4d53-b6fd-34052f9ad11a.png" width="1752" title="" crop="0,0,1,1" id="e6T9E" class="ne-image"><img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787291372540-89091c68-1112-4160-8841-5956d4453cb7.png" width="1836" title="" crop="0,0,1,1" id="PgLdy" class="ne-image"><img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787291602021-1698e0ae-9519-4f87-8920-57283db214ab.png" width="1622" title="" crop="0,0,1,1" id="mUKHY" class="ne-image"><img src="https://intranetproxy.alipay.com/skylark/lark/0/2026/png/187156762/1787291754150-a8165cc2-d666-46f6-9ba9-52528f1d83cd.png" width="1500" title="" crop="0,0,1,1" id="UKnYS" class="ne-image">

### 与 Subagent 的区别
| 对比项 | Subagent | Agent Teams |
| --- | --- | --- |
| 适合任务 | 单个聚焦子任务 | 多成员持续协作的复杂任务 |
| 生命周期 | 完成一次任务即返回结果 | teammate 可 idle，在同一会话里继续接活 |
| 通信方式 | 结果主要返回给 main Agent | 成员之间可通过 SendMessage 互相沟通 |
| 成员身份 | 以 Subagent 类型为主 | 以运行时名字为主，如 `@coder`、`@reviewer` |


**控制并发规模**：同时开太多 teammate 会显著增加 token 与信息合并成本，多数场景从 2–4 个就行。

### 补充：自主执行：Goal
Agent Teams 是多 Agent 版的自主协作；如果只是想让 **Qoder 持续跑到目标达成**，用 `/goal` 更轻。给一个明确的完成条件，它会把目标拆成步骤持续执行，尽量不中途打扰——适合"把测试全部修绿""重构到构建通过"这类有清晰终点的任务：

```plain
/goal 修复所有失败的单元测试，直到 npm test 全部通过
/goal 重构支付模块并保证测试通过 --turns 30
```

| 命令 | 作用 |
| --- | --- |
| `/goal <描述> [--turns <n>]` | 创建或更新目标，`--turns` 限制最大轮数 |
| `/goal status` | 查看状态：Active / Paused、已用轮数、耗时 |
| `/goal pause` / `/goal resume` | 暂停 / 恢复 |
| `/goal take` | 目标由其他会话持有时，接管所有权 |
| `/goal clear` | 清除目标（判定达成时会自动清除） |


使用的时候需要知道：

+ **Goal 不改变权限模式**：执行期间的工具授权仍按当前权限模式走。想真正无人值守，得先自行切到 `auto` 或 `yolo`——否则它会停在确认弹窗上（第 03 节提到：非交互下 `ask` 等于拒绝）；
+ **目标跨会话持久化**：由另一个会话创建的目标，当前会话默认不能直接更新，需要 `/goal take` 认领。

配合 `/plan` 更可控：先只读分析、确认方案，再进 Goal 让它自主执行到完成。IDE 侧的 Quest 是同一理念的图形化形态——"Define the goal. Review the result."，描述目标后 Agent 自主执行，执行完在审查面板看 Diff 后直接提交。

**官方文档**：

+ Agent Teams：[https://docs.qoder.com/zh/cli/agent-teams](https://docs.qoder.com/zh/cli/agent-teams)
+ 持续完成目标（Goal）：[https://docs.qoder.com/zh/cli/goal](https://docs.qoder.com/zh/cli/goal)
+ 先规划再执行（Plan）：[https://docs.qoder.com/zh/cli/plan-mode](https://docs.qoder.com/zh/cli/plan-mode)
+ Quest 概览：[https://docs.qoder.com/zh/user-guide/quest/overview](https://docs.qoder.com/zh/user-guide/quest/overview)

---
