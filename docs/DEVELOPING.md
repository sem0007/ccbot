# Как править этот форк

Короткая памятка для сопровождения `sem0007/ccbot`. Подробности правок — в
`../CHANGELOG.md`.

## Где что живёт

- **Разработка** — на Маке: `~/ConnectedSpace/Projects/ccbot` (origin = sem0007,
  upstream = six-ddc/ccbot).
- **Работает** — на сервере `ap-brooklyn`: клон в `~/ConnectedSpace/Projects/
  ccbot`, поставлен как editable uv-tool (`uv tool install -e .` → `~/.local/bin/
  ccbot`), крутится под **systemd user-service** `ccbot`. Воркеры-`claude` — окна
  в tmux-сессии `ccbot`.
- **Конфиг и секреты** — ТОЛЬКО в `~/.ccbot/.env` на сервере, вне репозитория.
  Состояние — `~/.ccbot/*.json`, лог — `~/.ccbot/run.log`.

## Цикл правки (edit → push → deploy)

```bash
# 1. правишь .py на Маке, проверяешь синтаксис
python3 -m py_compile src/ccbot/<файл>.py

# 2. ОБЯЗАТЕЛЬНО: проверка на утечку секретов (см. ниже)
scripts/check-secrets.sh

# 3. запись в CHANGELOG.md (дата, что, зачем) — без этого не пушим
$EDITOR CHANGELOG.md

# 4. коммит + пуш в GitHub
git add -A && git commit -m "…" && git push origin main

# 5. на сервере: подтянуть и перезапустить
ssh ap-brooklyn 'cd ~/ConnectedSpace/Projects/ccbot && git pull --ff-only \
  && systemctl --user restart ccbot'
# если менялись ЗАВИСИМОСТИ (pyproject) — один раз:
#   uv tool install -e . --reinstall
```

**Правило:** каждое изменение уходит на GitHub **с записью в `CHANGELOG.md`**.
Правки только на сервере (без пуша) запрещены — теряются и расходятся.

## Проверка на утечку секретов — ОБЯЗАТЕЛЬНО перед пушем

Репозиторий **публичный**. Никогда не коммить:

- `.env`, любые токены (`TELEGRAM_BOT_TOKEN`, `CCBOT_API_TOKEN`, `OPENAI_API_KEY`);
- приватные идентификаторы: Telegram-id пользователя, `CCBOT_CONTROL_CHAT_ID`,
  chat_id групп;
- приватные ключи, IP/адреса серверов, содержимое `~/.ccbot/` (state, логи,
  `session_map.json`, `monitor_state.json`).

Перед каждым пушем гоняй страж — он читает реальные значения из `~/.ccbot/.env`
(они в репозиторий не попадают) и ищет их в изменениях, плюс проверяет типовые
шаблоны секретов:

```bash
scripts/check-secrets.sh          # проверить staged-изменения (после git add)
scripts/check-secrets.sh --all    # проверить весь трекаемый код
```

Выход `0` — чисто, можно пушить. Выход `1` — найден секрет/приватные данные,
пуш делать НЕЛЬЗЯ, убери значение в `~/.ccbot/.env` и читай из окружения.

Удобно повесить как git-хук:

```bash
ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
```

## Отладка

- Логи: `ssh ap-brooklyn 'tail -f ~/.ccbot/run.log'` или ручка API `/logs`.
- Управление ботом по API (токен — в `~/.ccbot/.env`, НЕ печатать в чат/историю):
  ```bash
  ssh ap-brooklyn
  T=$(grep ^CCBOT_API_TOKEN= ~/.ccbot/.env | cut -d= -f2-)
  curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8787/health
  ```
- Статус сервиса: `systemctl --user status ccbot`; рестарт: `systemctl --user
  restart ccbot`.

## Синхронизация с upstream

```bash
git fetch upstream && git log --oneline HEAD..upstream/main   # что нового у них
git merge upstream/main   # аккуратно, наши правки в service.py/api.py/bot.py
```
