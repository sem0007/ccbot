# CCBot

通过 Telegram 远程控制 Claude Code 和 Codex 会话 — 监控、交互、管理运行在 tmux 中的 AI 编程会话。

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## 为什么做 CCBot？

Claude Code 和 Codex 都是本地 agent CLI。当你离开电脑 — 通勤路上、躺在沙发上、或者只是不在工位 — 会话仍在继续，但你失去了查看和控制的能力。

CCBot 让你**通过 Telegram 无缝接管同一个会话**。核心设计思路是：保留一个以终端为事实来源的 agent 会话。Claude Code 直接运行在 tmux 窗口中，并通过 hook 追踪；Codex 通过 app-server remote 协议运行，并保留一个连接到同一 thread 的 tmux TUI。这意味着：

- **从电脑无缝切换到手机** — Claude 或 Codex 正在执行重构？走开就是了，继续在 Telegram 上监控和回复。
- **随时切换回电脑** — tmux 会话从未中断，直接 `tmux attach` 就能回到终端，完整的滚动历史和上下文都在。
- **并行运行多个会话** — 每个 Telegram 话题对应一个独立的 tmux 窗口，一个聊天组里就能管理多个项目。

市面上其他编程 agent Telegram Bot 通常创建独立的 API 会话，这些会话是隔离的 — 你无法在终端里恢复它们。CCBot 采取了不同的方式：Claude Code 通过 tmux + hook 支持，Codex 通过 remote app-server thread 支持，同时保留可以随时切回的终端 UI。

实际上，CCBot 自身就是用这种方式开发的 — 通过 CCBot 在 Telegram 上监控和驱动 agent 会话来迭代自身。

## 功能特性

- **支持 Claude Code 和 Codex** — 可使用任一 agent，也可同时启用并在每个话题中选择
- **基于话题的会话** — 每个 Telegram 话题 1:1 映射到一个 tmux 窗口和 agent 会话
- **实时通知** — 接收 agent 回复、思考过程、工具调用/结果、本地命令输出的 Telegram 消息
- **交互式 UI** — 通过内联键盘操作 AskUserQuestion、ExitPlanMode 和权限提示
- **语音消息** — 语音消息通过 OpenAI 转录为文字并转发
- **发送消息** — 将文字转发给当前 agent（Claude 通过 tmux，Codex 通过 remote app-server）
- **斜杠命令转发** — 任何 `/command` 直接发送给当前 agent（如 `/clear`、`/compact`、`/cost`）
- **创建新会话** — 通过目录浏览器从 Telegram 启动 Claude Code 或 Codex 会话
- **恢复会话** — 选择目录中已有的 Claude 或 Codex 会话继续上次的工作
- **关闭会话** — 关闭话题自动终止关联的 tmux 窗口
- **消息历史** — 分页浏览对话历史（默认显示最新）
- **会话追踪** — Claude 会话通过 `SessionStart` hook 追踪；Codex 会话通过 remote thread 元数据追踪
- **持久化状态** — 话题绑定和读取偏移量在重启后保持

## 前置要求

- **tmux** — 需要安装并在 PATH 中可用
- **至少一个 agent CLI** — 需要安装 Claude Code（`claude`）和/或 Codex（`codex`）

## 安装

### 方式一：从 GitHub 安装（推荐）

```bash
# 使用 uv（推荐）
uv tool install git+https://github.com/six-ddc/ccmux.git

# 或使用 pipx
pipx install git+https://github.com/six-ddc/ccmux.git
```

### 方式二：从源码安装

```bash
git clone https://github.com/six-ddc/ccmux.git
cd ccmux
uv sync
```

## 配置

**1. 创建 Telegram Bot 并启用话题模式：**

1. 与 [@BotFather](https://t.me/BotFather) 对话创建新 Bot 并获取 Token
2. 打开 @BotFather 的个人页面，点击 **Open App** 启动小程序
3. 选择你的 Bot，进入 **Settings** > **Bot Settings**
4. 启用 **Threaded Mode**（话题模式）

**2. 配置环境变量：**

创建 `~/.ccbot/.env`：

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**必填项：**

| 变量 | 说明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 从 @BotFather 获取的 Bot Token |
| `ALLOWED_USERS` | 逗号分隔的 Telegram 用户 ID |

**可选项：**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CCBOT_DIR` | `~/.ccbot` | 配置/状态目录（`.env` 从此目录加载） |
| `TMUX_SESSION_NAME` | `ccbot` | tmux 会话名称 |
| `CCBOT_ENABLED_AGENTS` | 自动检测 | 逗号分隔的 agent（`claude`, `codex`）；未配置时检测本地命令 |
| `CCBOT_DEFAULT_AGENT` | 优先 `claude` | 单 agent 流程使用的默认 agent |
| `CLAUDE_COMMAND` | `claude` | 新窗口中运行的命令 |
| `CODEX_COMMAND` | `codex` | Codex remote session 使用的命令 |
| `MONITOR_POLL_INTERVAL` | `2.0` | 轮询间隔（秒） |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | 在目录浏览器中显示隐藏（点开头）目录 |
| `OPENAI_API_KEY` | _(无)_ | OpenAI API 密钥，用于语音消息转录 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API 基础 URL（用于代理或兼容 API） |

未配置 `CCBOT_ENABLED_AGENTS` 时，CCBot 会自动检测已安装的 `claude` 和 `codex` 命令。如果两者都可用，新话题会先显示 agent 选择器，再进入目录浏览器。

消息格式化目前固定为 HTML，使用 `chatgpt-md-converter`（`chatgpt_md_converter` 包）。
不再提供运行时切换到 MarkdownV2 的开关。

> 如果在 VPS 上运行且没有交互终端来批准权限，可以考虑：
> ```
> CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
> ```

## Claude Code Hook 设置（推荐）

此 hook 用于 Claude Code tmux 会话。Codex remote 会话通过 Codex app-server thread 元数据追踪，不需要 Claude hook。

通过 CLI 自动安装：

```bash
ccbot hook --install
```

或手动添加到 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

Hook 会将窗口-会话映射写入 `$CCBOT_DIR/session_map.json`（默认 `~/.ccbot/`），这样 Bot 就能自动追踪每个 tmux 窗口中运行的 Claude 会话 — 即使在 `/clear` 或会话重启后也能保持关联。

## 使用方法

```bash
# 通过 uv tool / pipx 安装的
ccbot

# 从源码安装的
uv run ccbot
```

### 命令

**Bot 命令：**

| 命令 | 说明 |
|---|---|
| `/start` | 显示欢迎消息 |
| `/history` | 当前话题的消息历史 |
| `/screenshot` | 截取终端屏幕 |
| `/esc` | 中断当前 agent |

**Agent 斜杠命令（转发到当前会话）：**

| 命令 | 说明 |
|---|---|
| `/clear` | 清除对话历史 |
| `/compact` | 压缩对话上下文 |
| `/cost` | 显示 Token/费用统计 |
| `/help` | 显示 agent 帮助 |
| `/memory` | 编辑 CLAUDE.md（Claude Code） |

其他未识别的 `/command` 也会原样转发给当前会话（如 `/review`、`/doctor`、`/init`）。

### 话题工作流

**1 话题 = 1 窗口 = 1 会话。** Bot 在 Telegram 论坛（话题）模式下运行。

**创建新会话：**

1. 在 Telegram 群组中创建新话题
2. 在话题中发送任意消息
3. 弹出目录浏览器 — 选择项目目录
4. 如果启用了多个 agent，先选择 Claude Code 或 Codex
5. 如果该目录下已有对应 agent 的会话，会弹出会话选择器 — 选择恢复已有会话或创建新会话
6. 自动创建 tmux 窗口，启动所选 agent，并转发待处理的消息

**发送消息：**

话题绑定会话后，直接在话题中发送文字或语音消息即可 — 文字会转发给当前 agent，语音消息会自动转录为文字后转发。

**关闭会话：**

在 Telegram 中关闭（或删除）话题，关联的 tmux 窗口会自动终止，绑定也会被移除。

### 消息历史

使用内联按钮导航：

```
📋 [项目名称] Messages (42 total)

───── 14:32 ─────

👤 修复登录 bug

───── 14:33 ─────

我来排查这个登录 bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### 通知

监控器每 2 秒轮询会话 JSONL 文件，并发送以下通知：
- **助手回复** — agent 的文字回复
- **思考过程** — 以可展开引用块显示
- **工具调用/结果** — 带统计摘要（如 "Read 42 lines"、"Found 5 matches"）
- **本地命令输出** — 命令的标准输出（如 `git status`），前缀为 `❯ command_name`

通知发送到绑定了该会话窗口的话题中。

格式说明：
- Telegram 消息使用 `HTML` parse mode
- 通过 `chatgpt-md-converter` 做 Markdown→HTML 转换与 HTML 标签感知拆分，保证长代码块拆分稳定

## 在 tmux 中运行 agent

### 方式一：通过 Telegram 创建（推荐）

1. 在 Telegram 群组中创建新话题
2. 发送任意消息
3. 从浏览器中选择项目目录

### 方式二：手动创建

```bash
tmux attach -t ccbot
tmux new-window -n myproject -c ~/Code/myproject
# 在新窗口中启动 Claude Code
claude
```

窗口必须在 `ccbot` tmux 会话中（可通过 `TMUX_SESSION_NAME` 配置）。Claude 启动时 Hook 会自动将其注册到 `session_map.json`。Codex remote 会话建议从 Telegram 创建，这样 CCBot 可以创建 app-server thread 并将 tmux TUI 连接到它。

## 架构概览

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│  (Telegram) │      │ (tmux @id)  │      │  (agent)    │
└─────────────┘      └─────────────┘      └─────────────┘
     thread_bindings      session metadata
     (state.json)         (hook 或 remote thread)
```

**核心设计思路：**
- **话题为中心** — 每个 Telegram 话题绑定一个 tmux 窗口和一个 agent 会话，话题就是会话列表
- **窗口 ID 为中心** — 所有内部状态以 tmux 窗口 ID（如 `@0`、`@12`）为键，而非窗口名称。窗口名称仅作为显示名称保留。同一目录可有多个窗口
- **会话追踪** — Claude Code 的 `SessionStart` Hook 写入 `session_map.json`；Codex remote 会话通过 app-server thread 元数据追踪
- **工具调用配对** — `tool_use_id` 跨轮询周期追踪；工具结果直接编辑原始的工具调用 Telegram 消息
- **HTML + 降级** — 所有消息通过 `chatgpt-md-converter` 转换为 Telegram HTML，解析失败时降级为纯文本
- **解析层不截断** — 完整保留内容；发送层按 Telegram 4096 字符限制拆分

## 数据存储

| 路径 | 说明 |
|---|---|
| `$CCBOT_DIR/state.json` | 话题绑定、窗口状态、显示名称、每用户读取偏移量 |
| `$CCBOT_DIR/session_map.json` | Hook 生成的 `{tmux_session:window_id: {session_id, cwd, window_name}}` 映射 |
| `$CCBOT_DIR/monitor_state.json` | 每会话的监控字节偏移量（防止重复通知） |
| `~/.claude/projects/` | Claude Code 会话数据（只读） |
| `~/.codex/sessions/` | Codex rollout/session 数据（只读） |
| `~/.codex/session_index.jsonl` | Codex 可恢复会话索引 |

## 文件结构

```
src/ccbot/
├── __init__.py            # 包入口
├── main.py                # CLI 调度器（hook 子命令 + bot 启动）
├── hook.py                # Hook 子命令，用于会话追踪（+ --install）
├── config.py              # 环境变量配置
├── bot.py                 # Telegram Bot 设置、命令处理、话题路由
├── codex_remote.py        # Codex app-server remote transport 和 TUI 命令构建
├── session.py             # 会话管理、状态持久化、消息历史
├── session_monitor.py     # JSONL 文件监控（轮询 + 变更检测）
├── monitor_state.py       # 监控状态持久化（字节偏移量）
├── transcript_parser.py   # Claude Code 和 Codex JSONL 对话记录解析
├── terminal_parser.py     # 终端面板解析（交互式 UI + 状态行）
├── html_converter.py      # Markdown → Telegram HTML 转换 + HTML 感知拆分
├── screenshot.py          # 终端文字 → PNG 图片（支持 ANSI 颜色）
├── transcribe.py          # 通过 OpenAI API 进行语音转文字
├── utils.py               # 通用工具（原子 JSON 写入、JSONL 辅助函数）
├── tmux_manager.py        # tmux 窗口管理（列出、创建、发送按键、终止）
├── fonts/                 # 截图渲染用字体
└── handlers/
    ├── __init__.py        # Handler 模块导出
    ├── callback_data.py   # 回调数据常量（CB_* 前缀）
    ├── directory_browser.py # 目录浏览器内联键盘 UI
    ├── history.py         # 消息历史分页
    ├── interactive_ui.py  # 交互式 UI 处理（AskUser、ExitPlan、权限）
    ├── message_queue.py   # 每用户消息队列 + worker（合并、限流）
    ├── message_sender.py  # safe_reply / safe_edit / safe_send 辅助函数
    ├── response_builder.py # 响应消息构建（格式化 tool_use、思考等）
    └── status_polling.py  # 终端状态行轮询
```

## 贡献者

感谢所有贡献者！我们鼓励使用 Claude Code 或 Codex 协同参与项目贡献。

<a href="https://github.com/six-ddc/ccmux/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=six-ddc/ccmux" />
</a>
