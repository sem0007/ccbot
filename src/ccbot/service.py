"""ControlService — the transport-independent core of ccbot.

Every management operation (create/send/resume/kill/list/reconcile/…) is
implemented here EXACTLY ONCE. Both front-ends are thin adapters over it:
  - the Telegram handlers (bot.py) parse an Update → call a method → format a reply;
  - the HTTP API (api.py) parses a request → call the same method → JSON.

Because ALLOWED_USERS restricts Telegram to a single human, an automated agent
cannot drive the bot through Telegram — it drives it through the HTTP API, which
is why the core must live outside the Telegram layer.

Outbound delivery of Claude's replies is fanned out through MessageBus so both
the human (Telegram) and the agent (API ring buffer) see the same stream.

Key classes: MessageBus, ControlService.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config
from .monitor_state import TrackedSession
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor
from .tmux_manager import _UUID_RE, tmux_manager
from .utils import read_cwd_from_jsonl

logger = logging.getLogger(__name__)

# Shell commands that indicate a pane is NOT running Claude (window is idle/dead).
_SHELL_COMMANDS = {"zsh", "-zsh", "bash", "-bash", "sh", "-sh", "fish", "-fish"}


class MessageBus:
    """Fan-out for Claude replies + a per-session ring buffer.

    The monitor used to push each NewMessage to a single callback (Telegram
    delivery). That single sink is replaced by this bus: Telegram delivery is
    registered as one subscriber, and every message is also appended to a ring
    buffer so the API (GET /sessions/{id}/messages) can read Claude's output
    without Telegram. Extending the single callback into a registry — rather
    than replacing it — is what keeps human delivery working.
    """

    def __init__(self, ring_size: int = 200) -> None:
        self._subscribers: list[Callable[[NewMessage], Awaitable[None]]] = []
        self._ring: dict[str, deque[dict[str, Any]]] = {}
        self._ring_size = ring_size
        self._seq = 0

    def subscribe(self, cb: Callable[[NewMessage], Awaitable[None]]) -> None:
        self._subscribers.append(cb)

    async def publish(self, msg: NewMessage) -> None:
        # Ring buffer first (never fails), so the API sees output even if a
        # Telegram subscriber raises.
        self._seq += 1
        entry = {
            "seq": self._seq,
            "session_id": msg.session_id,
            "role": msg.role,
            "content_type": msg.content_type,
            "tool_name": msg.tool_name,
            "text": msg.text,
            "is_complete": msg.is_complete,
            "ts": time.time(),
        }
        ring = self._ring.get(msg.session_id)
        if ring is None:
            ring = deque(maxlen=self._ring_size)
            self._ring[msg.session_id] = ring
        ring.append(entry)

        for cb in self._subscribers:
            try:
                await cb(msg)
            except Exception as e:  # one bad subscriber must not stall the bus
                logger.error("MessageBus subscriber error: %s", e)

    def tail(self, session_id: str, since: int = 0) -> list[dict[str, Any]]:
        ring = self._ring.get(session_id)
        if not ring:
            return []
        return [e for e in ring if e["seq"] > since]


@dataclass
class CreateResult:
    """Outcome of create_session, consumed by both front-ends."""

    success: bool
    message: str
    window_id: str = ""
    window_name: str = ""
    session_id: str = ""
    resumed: bool = False


class ControlService:
    """Single owner of all management logic; front-ends are thin adapters."""

    def __init__(self, monitor: SessionMonitor, bot: Any = None) -> None:
        self.sm = session_manager
        self.tmux = tmux_manager
        self.monitor = monitor
        self.bot = bot
        self.bus = MessageBus()
        self.last_reconcile: float = 0.0

    async def on_new_message(self, msg: NewMessage) -> None:
        """Monitor callback: fan a Claude reply out to all subscribers."""
        await self.bus.publish(msg)

    # --- helpers -----------------------------------------------------------

    def _pane_alive(self, pane_cmd: str) -> bool:
        """Heuristic: a window is 'alive' when its pane isn't a bare shell."""
        return bool(pane_cmd) and pane_cmd not in _SHELL_COMMANDS

    def _newest_session_file(
        self, cwd: str, since_mtime: float
    ) -> tuple[str, Path, int] | None:
        """Find the freshest session JSONL for cwd created/grown after start.

        Used to recover the REAL session_id when the hook is slow or when
        Claude forks a new id on --resume. Tries the encoded project dir first,
        then a cwd-matched glob across all project dirs.
        """
        best: tuple[float, str, Path, int] | None = None

        def _consider(f: Path) -> None:
            nonlocal best
            if f.stem == "sessions-index":
                return
            try:
                st = f.stat()
            except OSError:
                return
            if st.st_mtime + 0.5 < since_mtime:
                return  # untouched since we started the window
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, f.stem, f, st.st_size)

        proj = config.claude_projects_path / self.sm._encode_cwd(cwd)
        if proj.is_dir():
            for f in proj.glob("*.jsonl"):
                _consider(f)

        if best is None and config.claude_projects_path.is_dir():
            try:
                target = str(Path(cwd).resolve())
            except (OSError, ValueError):
                target = cwd
            for f in config.claude_projects_path.glob("*/*.jsonl"):
                if f.stem == "sessions-index":
                    continue
                real = read_cwd_from_jsonl(f)
                if not real:
                    continue
                try:
                    real_norm = str(Path(real).resolve())
                except (OSError, ValueError):
                    real_norm = real
                if real_norm == target:
                    _consider(f)

        if best is None:
            return None
        return best[1], best[2], best[3]

    def _prewatch(self, session_id: str, file_path: str, offset: int) -> None:
        """Pre-register a session with the monitor at a chosen start offset.

        NEW session → offset 0 (deliver the intro reply; file is near-empty).
        RESUME → offset = size at create time (don't replay the whole history).
        Without this, the monitor's first-sight branch sets offset=EOF and the
        first reply is never delivered.
        """
        self.monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=file_path,
                last_byte_offset=offset,
            )
        )
        # Force a re-read next cycle regardless of the mtime cache.
        self.monitor._file_mtimes.pop(session_id, None)
        self.monitor.state.save_if_dirty()

    # --- create / resume ---------------------------------------------------

    async def create_session(
        self,
        cwd: str,
        *,
        resume_session_id: str | None = None,
        bind_thread_id: int | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        pending_text: str | None = None,
        window_name: str | None = None,
    ) -> CreateResult:
        """Create a tmux window, resolve the REAL session_id, bind, pre-watch.

        thread_id/user_id/chat_id are EXPLICIT parameters (never read from a
        clobberable per-user context) — this is what structurally fixes the
        "new session doesn't bind to its topic" bug.
        """
        if resume_session_id and not _UUID_RE.fullmatch(resume_session_id):
            return CreateResult(False, "Invalid resume_session_id (not a UUID)")

        start = time.time()
        ok, message, wname, wid = await self.tmux.create_window(
            cwd, window_name=window_name, resume_session_id=resume_session_id
        )
        if not ok:
            return CreateResult(False, message)

        # Give Claude's SessionStart hook time to publish window→session_id.
        hook_timeout = 15.0 if resume_session_id else 6.0
        hook_ok = await self.sm.wait_for_session_map_entry(wid, timeout=hook_timeout)

        ws = self.sm.get_window_state(wid)
        ws.cwd = cwd
        ws.window_name = wname
        real_id = ws.session_id  # synced from session_map by wait_for_...

        # Recover the real session_id when the hook was slow, or when Claude
        # forked a new id on --resume (hook may still report the requested one).
        if not real_id or (resume_session_id and real_id == resume_session_id):
            picked = self._newest_session_file(cwd, start)
            if picked:
                picked_id, picked_path, picked_size = picked
                if picked_id != real_id:
                    real_id = picked_id
                    await self.sm.override_session_map_entry(
                        wid, real_id, cwd=cwd, window_name=wname
                    )
                    ws.session_id = real_id

        if resume_session_id:
            ws.requested_resume_id = resume_session_id

        # Pre-watch so the monitor uses the right start offset.
        if real_id:
            fp = self.sm._build_session_file_path(real_id, cwd)
            if fp is not None:
                if resume_session_id:
                    # Don't replay history: start at the file's current size.
                    try:
                        offset = fp.stat().st_size if fp.exists() else 0
                    except OSError:
                        offset = 0
                else:
                    offset = 0  # new session — deliver the intro
                self._prewatch(real_id, str(fp), offset)
            ws.pending_bind = False
        else:
            # Hook still hasn't published; protect the empty window from the
            # stale cleanup and let reconcile() adopt the id shortly.
            ws.pending_bind = True
            logger.warning("create_session: no session_id yet for %s (hook slow)", wid)

        # Bind + seed group chat_id + persist as one atomic batch.
        async with self.sm._transaction():
            if bind_thread_id is not None and user_id is not None:
                self.sm.bind_thread(user_id, bind_thread_id, wid, window_name=wname)
                if chat_id is not None:
                    self.sm.set_group_chat_id(user_id, bind_thread_id, chat_id)
            self.sm._save_state()

        if pending_text:
            sok, smsg = await self.sm.send_to_window(wid, pending_text)
            if not sok:
                logger.warning("create_session: pending text not sent: %s", smsg)

        return CreateResult(
            success=True,
            message=message,
            window_id=wid,
            window_name=wname,
            session_id=real_id,
            resumed=bool(resume_session_id),
        )

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        return await self.sm.send_to_window(window_id, text)

    async def capture_output(
        self, window_id: str, lines: int = 60, with_ansi: bool = False
    ) -> str | None:
        text = await self.tmux.capture_pane(window_id, with_ansi=with_ansi)
        if text is None:
            return None
        if lines > 0:
            text = "\n".join(text.splitlines()[-lines:])
        return text

    async def screenshot(self, window_id: str) -> bytes | None:
        text = await self.tmux.capture_pane(window_id, with_ansi=True)
        if text is None:
            return None
        from .screenshot import text_to_image

        return await text_to_image(text, with_ansi=True)

    async def kill_session(
        self, window_id: str
    ) -> bool:
        """Kill a window and drop every binding/topic-state that referenced it."""
        killed = await self.tmux.kill_window(window_id)
        async with self.sm._transaction():
            for uid, tid, wid in list(self.sm.iter_thread_bindings()):
                if wid == window_id:
                    self.sm.unbind_thread(uid, tid)
            self.sm.window_states.pop(window_id, None)
            self.sm.window_display_names.pop(window_id, None)
            self.sm._save_state()
        return killed

    async def restart_session(self, window_id: str) -> CreateResult:
        """Kill a dead/stuck window and re-open it with --resume on its session."""
        ws = self.sm.window_states.get(window_id)
        if not ws or not ws.session_id or not ws.cwd:
            return CreateResult(False, "No known session_id/cwd for this window")
        sid, cwd, wname = ws.session_id, ws.cwd, ws.window_name
        # Preserve the thread binding so the new window keeps the same topic.
        binding = next(
            ((u, t) for u, t, w in self.sm.iter_thread_bindings() if w == window_id),
            None,
        )
        await self.kill_session(window_id)
        return await self.create_session(
            cwd,
            resume_session_id=sid,
            bind_thread_id=binding[1] if binding else None,
            user_id=binding[0] if binding else None,
            window_name=wname or None,
        )

    async def heal(self, window_id: str) -> CreateResult:
        """Alias for restart_session (revive a window whose Claude died)."""
        return await self.restart_session(window_id)

    # --- read model --------------------------------------------------------

    async def list_sessions(self) -> list[dict[str, Any]]:
        windows = await self.tmux.list_windows()
        by_id = {w.window_id: w for w in windows}
        # thread binding lookup
        bind_by_wid: dict[str, tuple[int, int]] = {}
        for uid, tid, wid in self.sm.iter_thread_bindings():
            bind_by_wid[wid] = (uid, tid)

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for wid, w in by_id.items():
            seen.add(wid)
            ws = self.sm.window_states.get(wid)
            sid = ws.session_id if ws else ""
            tracked = self.monitor.state.get_session(sid) if sid else None
            bind = bind_by_wid.get(wid)
            out.append(
                {
                    "window_id": wid,
                    "window_name": w.window_name,
                    "cwd": w.cwd,
                    "session_id": sid,
                    "alive": self._pane_alive(w.pane_current_command),
                    "pane_cmd": w.pane_current_command,
                    "user_id": bind[0] if bind else None,
                    "thread_id": bind[1] if bind else None,
                    "monitor_offset": tracked.last_byte_offset if tracked else None,
                    "pending_bind": bool(ws and ws.pending_bind),
                    "requested_resume_id": ws.requested_resume_id if ws else None,
                }
            )
        # window_states with no live tmux window = orphaned
        for wid, ws in self.sm.window_states.items():
            if wid not in seen:
                bind = bind_by_wid.get(wid)
                out.append(
                    {
                        "window_id": wid,
                        "window_name": ws.window_name,
                        "cwd": ws.cwd,
                        "session_id": ws.session_id,
                        "alive": False,
                        "orphaned": True,
                        "user_id": bind[0] if bind else None,
                        "thread_id": bind[1] if bind else None,
                    }
                )
        return out

    async def list_bindings(self) -> list[dict[str, Any]]:
        windows = await self.tmux.list_windows()
        live_ids = {w.window_id for w in windows}
        out: list[dict[str, Any]] = []
        for uid, tid, wid in self.sm.iter_thread_bindings():
            ws = self.sm.window_states.get(wid)
            sid = ws.session_id if ws else ""
            has_chat = f"{uid}:{tid}" in self.sm.group_chat_ids
            if wid not in live_ids:
                status = "orphaned"  # thread points at a dead window
            elif not sid:
                status = "broken"  # window alive but no session_id yet
            else:
                status = "consistent"
            out.append(
                {
                    "user_id": uid,
                    "thread_id": tid,
                    "window_id": wid,
                    "session_id": sid,
                    "has_group_chat_id": has_chat,
                    "status": status,
                }
            )
        return out

    def get_monitor_state(self) -> dict[str, Any]:
        return {
            "tracked_sessions": {
                sid: {
                    "file_path": ts.file_path,
                    "last_byte_offset": ts.last_byte_offset,
                }
                for sid, ts in self.monitor.state.tracked_sessions.items()
            },
            "watching": sorted(self.monitor._last_session_map.values()),
        }

    async def list_resumable_sessions(self, cwd: str) -> list[dict[str, Any]]:
        sessions = await self.sm.list_sessions_for_directory(cwd)
        return [
            {
                "session_id": s.session_id,
                "summary": s.summary,
                "message_count": s.message_count,
                "file_path": s.file_path,
            }
            for s in sessions
        ]

    async def health(self) -> dict[str, Any]:
        tmux_ok = self.tmux.get_session() is not None
        windows = await self.tmux.list_windows()
        return {
            "ok": tmux_ok,
            "tmux_session": config.tmux_session_name,
            "windows": len(windows),
            "bindings": sum(1 for _ in self.sm.iter_thread_bindings()),
            "tracked_sessions": len(self.monitor.state.tracked_sessions),
            "monitor_running": self.monitor._running,
            "control_topic": self.sm.control_topic,
            "last_reconcile": self.last_reconcile,
        }

    # --- reconcile ---------------------------------------------------------

    async def reconcile(self) -> dict[str, Any]:
        """Idempotently re-align tmux ↔ session_map ↔ state ↔ monitor_state.

        Generalizes the startup-only resolve_stale_ids into something callable
        at runtime (periodically, on tmux-server restart, via POST /reconcile).
        """
        report: dict[str, Any] = {
            "remapped": [],
            "healed": [],
            "orphaned_bindings": [],
            "dropped_monitor": [],
        }

        # 1. Remap stale @ids by window_name / drop truly gone ones (startup logic).
        await self.sm.resolve_stale_ids()

        windows = await self.tmux.list_windows()
        live_ids = {w.window_id for w in windows}
        cwd_by_id = {w.window_id: w.cwd for w in windows}

        # 2. Self-heal live windows that still have an empty session_id.
        await self.sm.load_session_map()
        for wid in live_ids:
            ws = self.sm.window_states.get(wid)
            if ws and not ws.session_id:
                cwd = ws.cwd or cwd_by_id.get(wid, "")
                if cwd:
                    picked = self._newest_session_file(cwd, 0.0)
                    if picked:
                        pid, ppath, _ = picked
                        await self.sm.override_session_map_entry(wid, pid, cwd=cwd)
                        ws.session_id = pid
                        ws.cwd = cwd
                        ws.pending_bind = False
                        report["healed"].append({"window_id": wid, "session_id": pid})

        # 3. Flag bindings pointing at dead windows (don't delete silently).
        for uid, tid, wid in self.sm.iter_thread_bindings():
            if wid not in live_ids:
                report["orphaned_bindings"].append(
                    {"user_id": uid, "thread_id": tid, "window_id": wid}
                )

        # 4. Drop monitor entries for sessions no windows reference anymore.
        referenced = {
            ws.session_id for ws in self.sm.window_states.values() if ws.session_id
        }
        for sid in list(self.monitor.state.tracked_sessions.keys()):
            if sid not in referenced:
                self.monitor.state.remove_session(sid)
                report["dropped_monitor"].append(sid)
        self.monitor.state.save_if_dirty()

        self.sm._save_state()
        self.last_reconcile = time.time()
        report["at"] = self.last_reconcile
        return report

    # --- control topic -----------------------------------------------------

    async def ensure_control_topic(self) -> dict[str, int] | None:
        """Create (or re-use) the persistent, never-bound control topic.

        Requires config.control_chat_id (the bot must be admin of that
        supergroup with topic-management rights). Returns the topic dict.
        """
        if config.control_chat_id is None or self.bot is None:
            return None
        existing = self.sm.control_topic
        if existing and existing.get("chat_id") == config.control_chat_id:
            # Trust the persisted topic; a liveness probe would need extra perms.
            return existing
        try:
            topic = await self.bot.create_forum_topic(
                chat_id=config.control_chat_id,
                name=config.control_topic_name,
            )
            info = {
                "chat_id": config.control_chat_id,
                "thread_id": topic.message_thread_id,
            }
            self.sm.control_topic = info
            # Seed chat_id for outbound routing to this topic (per allowed user).
            for uid in config.allowed_users:
                self.sm.set_group_chat_id(
                    uid, topic.message_thread_id, config.control_chat_id
                )
            self.sm._save_state()
            logger.info("Control topic created: %s", info)
            return info
        except Exception as e:
            logger.error("Failed to ensure control topic: %s", e)
            return None


# Set by bot.post_init once the monitor/bot exist. api.py reads it.
service: ControlService | None = None


def set_service(svc: ControlService) -> None:
    global service
    service = svc
