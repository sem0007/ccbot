#!/usr/bin/env bash
#
# check-secrets.sh — страж утечки секретов и приватных данных.
# Запускай ПЕРЕД каждым пушем. Выход 0 = чисто, 1 = найдено, пушить нельзя.
#
#   scripts/check-secrets.sh          # проверить staged-изменения (после git add)
#   scripts/check-secrets.sh --all    # проверить весь трекаемый код
#
# Скрипт НЕ содержит секретов: реальные значения читаются из ~/.ccbot/.env
# в момент запуска (этот файл в репозиторий не попадает) и ищутся в изменениях.
#
# Две линии защиты:
#   1) СИЛЬНАЯ — динамический поиск реальных значений из .env по ВСЕМУ изменению
#      (включая tests/ и доки): если реальный токен/id где-то просочился — ловим.
#   2) ЭВРИСТИКА — типовые шаблоны секретов; НЕ гоняется по tests/*.example/*.md,
#      чтобы не срабатывать на заглушках и примерах.
#
set -u

fail=0
red() { printf '\033[31m%s\033[0m\n' "$1"; }
grn() { printf '\033[32m%s\033[0m\n' "$1"; }
yel() { printf '\033[33m%s\033[0m\n' "$1"; }

MODE="${1:-}"

# --- тела для проверок ------------------------------------------------------
if [ "$MODE" = "--all" ]; then
  NAMES="$(git ls-files)"
  BODY_ALL="$(git ls-files -z | xargs -0 cat 2>/dev/null)"
  BODY_HEUR="$(git ls-files -z ':(exclude)tests/**' ':(exclude)*.example' ':(exclude)*.sample' ':(exclude)*.template' ':(exclude)*.md' | xargs -0 cat 2>/dev/null)"
  SRC="весь трекаемый код"
else
  NAMES="$(git diff --cached --name-only)"
  BODY_ALL="$(git diff --cached)"
  BODY_HEUR="$(git diff --cached -- . ':(exclude)tests/**' ':(exclude)*.example' ':(exclude)*.sample' ':(exclude)*.template' ':(exclude)*.md')"
  SRC="staged-изменения"
fi

# --- 1. секретные/приватные файлы не должны попадать в git ------------------
# (шаблоны .env.example/.sample/.template — разрешены)
BAD_FILES="$(printf '%s\n' "$NAMES" \
  | grep -E '(^|/)\.env(\.|$)|(^|/)\.ccbot/|run\.log$|state\.json$|session_map\.json$|monitor_state\.json$|\.pem$|(^|/)id_(rsa|ed25519|ecdsa)' \
  | grep -vE '\.(example|sample|template)$' || true)"
if [ -n "$BAD_FILES" ]; then
  red "✗ В индекс попал секретный/приватный файл:"
  printf '%s\n' "$BAD_FILES" | sed 's/^/    /'
  fail=1
fi

# --- 2. СИЛЬНАЯ: реальные значения из ~/.ccbot/.env не должны утекать --------
ENVF="${CCBOT_DIR:-$HOME/.ccbot}/.env"
if [ -f "$ENVF" ]; then
  while IFS='=' read -r key val; do
    case "$key" in
      TELEGRAM_BOT_TOKEN|CCBOT_API_TOKEN|OPENAI_API_KEY|ALLOWED_USERS|CCBOT_CONTROL_CHAT_ID) ;;
      *) continue ;;
    esac
    val="${val%$'\r'}"
    [ -z "$val" ] && continue
    IFS=',' read -ra parts <<< "$val"   # ALLOWED_USERS может быть списком
    for one in "${parts[@]}"; do
      one="$(printf '%s' "$one" | tr -d '[:space:]')"
      [ "${#one}" -lt 6 ] && continue
      if printf '%s' "$BODY_ALL" | grep -qF -- "$one"; then
        red "✗ В $SRC найдено РЕАЛЬНОЕ значение \$$key — это утечка! Убери в ~/.ccbot/.env и читай из окружения."
        fail=1
      fi
    done
  done < "$ENVF"
else
  yel "⚠ $ENVF не найден — сильная проверка значений пропущена (эвристика ниже работает)."
fi

# --- 3. ЭВРИСТИКА: типовые шаблоны секретов (без tests/*.example/*.md) -------
check() { # <regex> <человеческое-имя>
  if printf '%s' "$BODY_HEUR" | grep -qE -- "$1"; then
    red "✗ Похоже на секрет ($2) — проверь diff вручную."
    fail=1
  fi
}
check '[0-9]{8,10}:[A-Za-z0-9_-]{35}'      'Telegram bot token'
check '(sk|rk)-[A-Za-z0-9]{20,}'          'OpenAI/подобный API-ключ'
check '-----BEGIN [A-Z ]*PRIVATE KEY-----' 'приватный ключ'
check '\b-100[0-9]{7,}\b'                  'Telegram chat_id супергруппы'
check '\bAKIA[0-9A-Z]{16}\b'              'AWS access key'

# --- итог ------------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
  grn "✓ Секретов и приватных данных в изменениях не найдено — можно пушить."
else
  red "✗ ПУШ ЗАПРЕЩЁН: найдены секреты/приватные данные (см. выше)."
fi
exit "$fail"
