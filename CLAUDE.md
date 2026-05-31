# CLAUDE.md - 代码修改规范

本项目为 AI 编程教学项目，代码需兼顾**教学可读性**和**终端可视化**。

---

## 1. 中文注释规范

**原则：** 翻译原有英文注释，补充中文注释。注释应覆盖架构设计、函数用途、关键逻辑、安全措施。代码中的英文不用动。

**必须添加注释的位置：**
- 文件头部：模块用途、核心模式（用 1-2-3 步骤说明）
- 每个函数：功能说明、参数含义、关键流程
- 安全/边界处理：黑名单、超时、截断等
- 入口 `if __name__ == "__main__"`：交互流程说明

```python
# ✅ 正确：保留英文 + 补充中文
# ── The core pattern: a while loop that calls tools until the model stops ──
def agent_loop(messages: list):
    """智能体主循环。
    流程：1. 发消息给 LLM → 2. 执行工具 → 3. 追加结果 → 循环
    """

# ❌ 错误：删除原有英文注释
def agent_loop(messages: list):
    """智能体主循环"""
```

---

## 2. 终端输出颜色规范

**原则：** 不同类型信息使用不同颜色，增强终端可读性。

| 信息类型 | 颜色 | ANSI 码 | 示例 |
|---------|------|---------|------|
| 用户提示符 | 青色 | `\033[36m` | `s01 >>` |
| 执行的命令 | 黄色 | `\033[33m` | `$ ls -la` |
| 工具结果标签 | 粗体品红 | `\033[1;35m` | `[bash 结果]` |
| 工具结果内容 | 品红 | `\033[35m` | 输出内容 |
| 模型最终回复 | 蓝色 | `\033[34m` | 模型文本 |

```python
# ✅ 正确：使用对应颜色码
print(f"\033[33m$ {command}\033[0m")                          # 命令 - 黄
print(f"\033[1;35m[{tool_name} 结果]\033[0m \033[35m{out}\033[0m")  # 结果 - 品红
print(f"\033[34m{model_response}\033[0m")                     # 回复 - 蓝

# ❌ 错误：无颜色或颜色混用
print(output[:200])
```

**注意事项：**
- 每个颜色输出后必须用 `\033[0m` 重置，避免颜色泄漏
- 使用 f-string 嵌入变量，保持格式一致

---

## 3. 工具调用显示规范

**原则：** 终端输出必须明确展示调用了**哪个工具**及**工具结果**。

```python
# ✅ 正确：标签中包含工具名
print(f"\033[1;35m[{block.name} -> Tool Calling Result]\033[0m \033[35m{output[:200]}\033[0m")
# 终端输出：[bash -> Tool Calling Result] file1.txt file2.txt ...

# ❌ 错误：无工具名
print(f"\033[1;35m[Tool Calling Result]\033[0m ...")
```

---

## 4. 修改原则

1. **不改逻辑**：注释和颜色只增强可读性，不改变代码行为
2. **不过度注释**：自明的代码（如简单赋值）不需要注释
3. **优先使用 Edit**：修改文件用 Edit 工具（精确替换），新建文件用 Write
4. **一次改全**：同类修改（如全文件加注释）一次性完成，避免反复编辑
5. **优先 mcp__ide__sourceCode**：代码、函数的定义，优先使用 sourceCode 工具读取，这样不会引入行号前缀
6. **保留原流程图**：文件头部的 ASCII 流程图/架构图是代码的教学注释，修改时不可删除，中文注释补充在图下方

---

## 5. Windows 编码兼容规范

**原则：** `Path.read_text()` / `Path.write_text()` 必须显式指定 `encoding="utf-8"`。

**原因：** Windows 中文系统的默认编码为 GBK，而项目文件统一使用 UTF-8。不指定编码时，读取含中文的 UTF-8 文件会抛出 `UnicodeDecodeError: 'gbk' codec can't decode byte ...`。

```python
# ✅ 正确：显式指定 encoding="utf-8"
text = Path("file.py").read_text(encoding="utf-8")
Path("out.txt").write_text(content, encoding="utf-8")

# ❌ 错误：依赖系统默认编码（Windows 中文系统 = GBK）
text = Path("file.py").read_text()
Path("out.txt").write_text(content)
```

---

## 6. 跨平台命令覆盖规范

**原则：** 权限检查的黑名单/危险命令列表必须同时覆盖 Linux 和 Windows 命令。

**原因：** LLM 会检测运行平台并自动选择对应的 Shell 命令。如果危险命令列表只含 Linux 命令（如 `rm`），LLM 在 Windows 上会用 `del` 执行删除，直接绕过权限闸门。

```python
# ✅ 正确：同时覆盖 Linux 和 Windows
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "format "]
DESTRUCTIVE = ["rm ", "rmdir ", "del ", "erase ", "rd ", "> /etc/", "chmod 777"]

# ❌ 错误：只写 Linux 命令，LLM 可在 Windows 上用 del/erase 绕过
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]
```

**常用跨平台对照：**

| 操作 | Linux | Windows |
|------|-------|---------|
| 删除文件 | `rm` / `rmdir` | `del` / `erase` / `rd` |
| 格式化磁盘 | `mkfs` | `format` |
| 提权 | `sudo` | `runas` |
