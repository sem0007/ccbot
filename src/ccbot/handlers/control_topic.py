"""Persistent control-topic dashboard.

The control topic is a forum topic that is never bound to any tmux window and
never auto-terminates. It shows a live overview of every worker session and
offers management buttons. All actions call ControlService (the same core the
API uses), so the human's control plane and the agent's API stay in lock-step.

Key function: render_dashboard(service) -> (text, InlineKeyboardMarkup).
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .callback_data import CB_CTRL_KILL, CB_CTRL_RECONCILE, CB_CTRL_REFRESH

_STATUS_ICON = {"consistent": "🟢", "broken": "🟡", "orphaned": "🔴"}


async def render_dashboard(service: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Build the dashboard text + inline keyboard from live service state."""
    sessions = await service.list_sessions()
    bindings = await service.list_bindings()
    health = await service.health()

    bind_status: dict[str, str] = {b["window_id"]: b["status"] for b in bindings}

    lines = ["🎛 <b>Control</b> — панель управления сессиями", ""]
    lines.append(
        f"окон: <b>{health['windows']}</b> · "
        f"привязок: <b>{health['bindings']}</b> · "
        f"отслеживается: <b>{health['tracked_sessions']}</b>"
    )
    lines.append("")

    if not sessions:
        lines.append("<i>активных окон нет</i>")
    for s in sessions:
        icon = "💤" if not s.get("alive") else "▶️"
        st = bind_status.get(s["window_id"], "")
        st_icon = _STATUS_ICON.get(st, "⚪")
        sid = (s.get("session_id") or "")[:8] or "—"
        name = s.get("window_name") or s["window_id"]
        thr = s.get("thread_id")
        thr_txt = f" · тема {thr}" if thr else " · без темы"
        flags = ""
        if s.get("pending_bind"):
            flags += " ⏳"
        if s.get("orphaned"):
            flags += " 🗑"
        lines.append(
            f"{icon}{st_icon} <b>{name}</b> <code>{s['window_id']}</code> "
            f"· {sid}{thr_txt}{flags}"
        )

    text = "\n".join(lines)

    # Buttons: global refresh/reconcile + a kill button per live worker window.
    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=CB_CTRL_REFRESH),
            InlineKeyboardButton("🩺 Сверка", callback_data=CB_CTRL_RECONCILE),
        ]
    ]
    row: list[InlineKeyboardButton] = []
    for s in sessions:
        if not s.get("alive"):
            continue
        label = f"✖ {s.get('window_name') or s['window_id']}"
        row.append(
            InlineKeyboardButton(
                label[:24], callback_data=f"{CB_CTRL_KILL}{s['window_id']}"
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    return text, InlineKeyboardMarkup(keyboard)
