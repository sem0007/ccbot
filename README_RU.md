# CCBot

[English README](README.md)
[中文文档](README_CN.md)

Удалённое управление сессиями Claude Code и Codex через Telegram — мониторинг, интерактивное управление и работа с AI-сессиями в tmux.

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Зачем CCBot?

Claude Code и Codex работают как локальные agent CLI. Когда вы отходите от компьютера — в дороге, дома или просто не за рабочим местом — сессия продолжает выполняться, но вы теряете видимость и контроль.

CCBot позволяет **бесшовно продолжать ту же самую сессию через Telegram**. Ключевая идея: терминальная сессия остаётся источником истины. Claude Code запускается прямо в tmux-окне и отслеживается через hook; Codex работает через app-server remote protocol, а tmux-hosted TUI подключается к тому же thread. Это означает:

- **Переключение с десктопа на телефон в середине работы** — Claude или Codex делает рефакторинг? Можно отойти и продолжать наблюдать/отвечать из Telegram.
- **Мгновенное возвращение к десктопу** — tmux-сессия не прерывается; `tmux attach` возвращает вас в тот же терминал с полной историей и контекстом.
- **Параллельная работа с несколькими сессиями** — каждый Telegram topic соответствует отдельному tmux-окну.

Большинство других Telegram-ботов для coding agents создают отдельные API-сессии. Такие сессии изолированы и не продолжаются в вашем терминале. CCBot работает иначе: Claude Code поддерживается через tmux + hook, Codex — через remote app-server threads, при этом сохраняется терминальный UI, к которому можно вернуться.

## Возможности

- **Поддержка Claude Code и Codex** — используйте любой agent или включите оба и выбирайте для каждого topic
- **Сессии по темам** — каждый Telegram topic 1:1 связан с tmux-окном и agent-сессией
- **Уведомления в реальном времени** — ответы agent, thinking-контент, tool use/result, вывод локальных команд
- **Интерактивный UI** — управление AskUserQuestion, ExitPlanMode и Permission Prompt через inline-клавиатуру
- **Голосовые сообщения** — голосовые сообщения транскрибируются через OpenAI и пересылаются как текст
- **Отправка сообщений** — проброс текста в активный agent (Claude через tmux, Codex через remote app-server)
- **Проброс slash-команд** — любая `/command` уходит напрямую в активный agent (например, `/clear`, `/compact`, `/cost`)
- **Создание новых сессий** — запуск Claude Code или Codex из Telegram через браузер директорий
- **Возобновление сессий** — выберите существующую Claude или Codex сессию в директории, чтобы продолжить с того места, где остановились
- **Завершение сессий** — закрытие topic автоматически завершает связанное tmux-окно
- **История сообщений** — пагинация истории диалога (сначала новые)
- **Трекинг сессий** — Claude-сессии отслеживаются через `SessionStart` hook; Codex-сессии — через remote thread metadata
- **Персистентное состояние** — привязки topic/window и read-offset сохраняются после перезапуска

## Требования

- **tmux** — должен быть установлен и доступен в PATH
- **Минимум один agent CLI** — должен быть установлен Claude Code (`claude`) и/или Codex (`codex`)

## Установка

### Вариант 1: установка из GitHub (рекомендуется)

```bash
# Через uv (рекомендуется)
uv tool install git+https://github.com/six-ddc/ccmux.git

# Или через pipx
pipx install git+https://github.com/six-ddc/ccmux.git
```

### Вариант 2: установка из исходников

```bash
git clone https://github.com/six-ddc/ccmux.git
cd ccmux
uv sync
```

## Конфигурация

**1. Создайте Telegram-бота и включите Threaded Mode:**

1. Напишите [@BotFather](https://t.me/BotFather), создайте бота и получите токен
2. Откройте профиль @BotFather и нажмите **Open App**
3. Выберите вашего бота, затем **Settings** > **Bot Settings**
4. Включите **Threaded Mode**

**2. Настройте переменные окружения:**

Создайте `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**Обязательные:**

| Переменная | Описание |
| ---------- | -------- |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `ALLOWED_USERS` | Список Telegram user ID через запятую |

**Опциональные:**

| Переменная | По умолчанию | Описание |
| ---------- | ------------ | -------- |
| `CCBOT_DIR` | `~/.ccbot` | Каталог конфигурации/состояния (`.env` грузится отсюда) |
| `TMUX_SESSION_NAME` | `ccbot` | Имя tmux-сессии |
| `CCBOT_ENABLED_AGENTS` | автообнаружение | Агенты через запятую (`claude`, `codex`); если не задано, проверяются локальные команды |
| `CCBOT_DEFAULT_AGENT` | `claude`, если доступен | Агент по умолчанию для сценариев с одним агентом |
| `CLAUDE_COMMAND` | `claude` | Команда запуска в новых окнах |
| `CODEX_COMMAND` | `codex` | Команда Codex для remote-сессий |
| `MONITOR_POLL_INTERVAL` | `2.0` | Интервал опроса в секундах |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | Показывать скрытые (dot) директории в браузере каталогов |
| `OPENAI_API_KEY` | _(нет)_ | API-ключ OpenAI для транскрипции голосовых сообщений |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Базовый URL OpenAI API (для прокси или совместимых API) |

Если `CCBOT_ENABLED_AGENTS` не задан, CCBot автоматически ищет установленные команды `claude` и `codex`. Если доступны обе, новые topic сначала показывают выбор agent, затем браузер директорий.

Форматирование сообщений всегда HTML через `chatgpt-md-converter` (`chatgpt_md_converter`).
Переключателя формата на MarkdownV2 во время выполнения нет.

> Если бот запущен на VPS без интерактивного терминала для подтверждений, можно использовать:
>
> ```
> CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
> ```

## Настройка Claude Code Hook (рекомендуется)

Этот hook нужен для Claude Code tmux-сессий. Codex remote sessions отслеживаются через Codex app-server thread metadata и не требуют Claude hook.

Авто-установка через CLI:

```bash
ccbot hook --install
```

Или вручную добавьте в `~/.claude/settings.json`:

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

Это записывает отображение window-session в `$CCBOT_DIR/session_map.json` (по умолчанию `~/.ccbot/`), чтобы бот автоматически отслеживал, какая Claude-сессия работает в каждом tmux-окне — даже после `/clear` или рестарта сессии.

## Использование

```bash
# Если установлено через uv tool / pipx
ccbot

# Если запуск из исходников
uv run ccbot
```

### Команды

**Команды бота:**

| Команда | Описание |
| ------- | -------- |
| `/start` | Показать приветственное сообщение |
| `/history` | История сообщений для текущего topic |
| `/screenshot` | Снимок терминала |
| `/esc` | Прервать активный agent |

**Slash-команды agent (пробрасываются в активную сессию):**

| Команда | Описание |
| ------- | -------- |
| `/clear` | Очистить историю диалога |
| `/compact` | Уплотнить контекст диалога |
| `/cost` | Показать статистику токенов/стоимости |
| `/help` | Справка agent |
| `/memory` | Редактировать CLAUDE.md (Claude Code) |

Любая неизвестная `/command` также пробрасывается в активную сессию как есть (например, `/review`, `/doctor`, `/init`).

### Workflow по topic

**1 topic = 1 window = 1 session.** Бот работает в режиме Telegram Forum Topics.

**Создание новой сессии:**

1. Создайте новый topic в Telegram-группе
2. Отправьте любое сообщение в topic
3. Появится браузер директорий — выберите каталог проекта
4. Если включено несколько agents, выберите Claude Code или Codex
5. Если в каталоге есть существующие сессии выбранного agent, появится выбор сессий — возобновите существующую или начните новую
6. Будет создано tmux-окно, запустится выбранный agent, и ваше отложенное сообщение отправится в сессию

**Отправка сообщений:**

После привязки topic к сессии отправляйте текст или голосовые сообщения в topic — текст уходит в активный agent, голосовые сообщения автоматически транскрибируются и пересылаются как текст.

**Завершение сессии:**

Закройте (или удалите) topic в Telegram. Связанное tmux-окно будет автоматически завершено, привязка удалена.

### История сообщений

Навигация через inline-кнопки:

```
📋 [project-name] Messages (42 total)

───── 14:32 ─────

👤 fix the login bug

───── 14:33 ─────

I'll look into the login bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### Уведомления

Монитор опрашивает session JSONL-файлы каждые 2 секунды и отправляет уведомления о:

- **Ответах ассистента** — текстовые ответы agent
- **Thinking-контенте** — отображается как раскрываемые цитаты
- **Tool use/result** — краткие сводки (например, `Read 42 lines`, `Found 5 matches`)
- **Выводе локальных команд** — stdout команд вроде `git status`, префикс `❯ command_name`

Уведомления отправляются в topic, привязанный к окну сессии.

Примечание по форматированию:
- Telegram-сообщения рендерятся с parse mode `HTML` через `chatgpt-md-converter`
- Длинные сообщения делятся с учётом HTML-тегов, чтобы сохранять код-блоки и форматирование

## Запуск agents в tmux

### Вариант 1: создать через Telegram (рекомендуется)

1. Создайте новый topic в Telegram-группе
2. Отправьте любое сообщение
3. Выберите каталог проекта в браузере

### Вариант 2: создать вручную

```bash
tmux attach -t ccbot
tmux new-window -n myproject -c ~/Code/myproject
# Затем запустите Claude Code в новом окне
claude
```

Окно должно находиться в tmux-сессии `ccbot` (настраивается через `TMUX_SESSION_NAME`). Hook автоматически зарегистрирует его в `session_map.json` при запуске Claude. Codex remote sessions лучше создавать из Telegram, чтобы CCBot создал app-server thread и подключил к нему tmux TUI.

## Обзор архитектуры

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│ (Telegram)  │      │ (tmux @id)  │      │  (agent)    │
└─────────────┘      └─────────────┘      └─────────────┘
   thread_bindings       session metadata
   (state.json)          (hook или remote thread)
```

## Хранение данных

| Путь | Описание |
| ---- | -------- |
| `$CCBOT_DIR/state.json` | Привязки topic, состояния окон, display names и read-offset на пользователя |
| `$CCBOT_DIR/session_map.json` | Hook-таблица `{tmux_session:window_id: {session_id, cwd, window_name}}` |
| `$CCBOT_DIR/monitor_state.json` | Byte-offset монитора по сессиям (предотвращает дубли) |
| `~/.claude/projects/` | Данные сессий Claude Code (только чтение) |
| `~/.codex/sessions/` | Данные rollout/session Codex (только чтение) |
| `~/.codex/session_index.jsonl` | Индекс возобновляемых Codex-сессий |

## Структура файлов

```
src/ccbot/
├── __init__.py            # Точка входа пакета
├── main.py                # CLI-диспетчер (hook подкоманда + запуск бота)
├── hook.py                # Hook-подкоманда для трекинга сессий (+ --install)
├── config.py              # Конфигурация из переменных окружения
├── bot.py                 # Настройка Telegram-бота, обработчики команд, topic routing
├── codex_remote.py        # Codex app-server remote transport и команда TUI
├── session.py             # Управление сессиями, persist состояния, история сообщений
├── session_monitor.py     # Мониторинг JSONL-файлов (polling + обнаружение изменений)
├── monitor_state.py       # Persist состояния монитора (byte-offset)
├── transcript_parser.py   # Парсинг JSONL-транскриптов Claude Code и Codex
├── terminal_parser.py     # Парсинг terminal pane (interactive UI + status line)
├── html_converter.py      # Markdown -> Telegram HTML + HTML-aware splitting
├── screenshot.py          # Terminal text -> PNG с поддержкой ANSI-цветов
├── transcribe.py          # Транскрипция голоса в текст через OpenAI API
├── utils.py               # Общие утилиты (atomic JSON writes, JSONL helpers)
├── tmux_manager.py        # Управление tmux-окнами (list, create, send keys, kill)
├── fonts/                 # Встроенные шрифты для рендера скриншотов
└── handlers/
    ├── __init__.py        # Экспорты handler-модулей
    ├── callback_data.py   # Константы callback data (префиксы CB_*)
    ├── directory_browser.py # Inline UI браузера директорий
    ├── history.py         # Пагинация истории сообщений
    ├── interactive_ui.py  # Обработка interactive UI (AskUser, ExitPlan, Permissions)
    ├── message_queue.py   # Очередь сообщений на пользователя + worker (merge, rate limit)
    ├── message_sender.py  # safe_reply / safe_edit / safe_send helpers
    ├── response_builder.py # Сборка ответных сообщений (tool_use, thinking и т.д.)
    └── status_polling.py  # Polling terminal status line
```

## Участники

Спасибо всем, кто вносит вклад! Мы поощряем использование Claude Code или Codex для совместной разработки.

<a href="https://github.com/six-ddc/ccmux/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=six-ddc/ccmux" />
</a>
