"""Codex app-server remote transport over the official JSON-RPC protocol.

Starts `codex app-server --listen ws://...`, initializes the protocol over a
WebSocket client, and exposes high-level thread operations used by the Telegram bot:
  - create/resume a Codex thread for a working directory.
  - start a turn with user text or local image references.
  - build a tmux-hosted Codex TUI command attached to the same app-server.
  - convert structured app-server notifications into NewMessage events.

This module intentionally does not import bot.py to keep the transport layer
independent from Telegram concerns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect

from .config import config
from .session_monitor import NewMessage
from .transcript_parser import PendingToolInfo, TranscriptParser

logger = logging.getLogger(__name__)

CODEX_REMOTE_PREFIX = "codex:"
CODEX_UPDATE_CHECK_CONFIG = "check_for_update_on_startup=false"
CODEX_DANGEROUS_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
LISTENING_RE = re.compile(r"listening on:\s+(wss?://\S+)")


def make_codex_window_id(thread_id: str) -> str:
    """Encode a Codex thread id as a routing key compatible with window bindings."""
    return f"{CODEX_REMOTE_PREFIX}{thread_id}"


def is_codex_window_id(window_id: str) -> bool:
    """Return True if this routing key points to a Codex remote thread."""
    return window_id.startswith(CODEX_REMOTE_PREFIX)


def codex_thread_id_from_window_id(window_id: str) -> str:
    """Decode a Codex remote routing key into a thread id."""
    return window_id.removeprefix(CODEX_REMOTE_PREFIX)


@dataclass
class CodexThread:
    """Codex thread metadata returned by app-server."""

    thread_id: str
    session_id: str
    cwd: str
    path: str
    name: str


@dataclass
class FormattedToolItem:
    """Display-ready representation of a Codex app-server tool item."""

    tool_name: str
    summary: str
    detail: str = ""
    input_data: Any = None


class CodexAppServerClient:
    """Small async JSON-RPC client for `codex app-server --listen ws://...`."""

    def __init__(self, command: str | None = None) -> None:
        self.command = command or config.codex_command
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._start_lock = asyncio.Lock()
        self._ws: ClientConnection | None = None
        self._remote_url: str | None = None
        self._remote_url_future: asyncio.Future[str] | None = None
        self._ws_reader_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._notification_callback: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None

    def set_notification_callback(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Set callback for app-server notifications."""
        self._notification_callback = callback

    @property
    def is_running(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._ws is not None
        )

    @property
    def remote_url(self) -> str | None:
        """Return the WebSocket URL exposed by the running app-server."""
        return self._remote_url

    async def start(self) -> None:
        """Start app-server and perform protocol initialization."""
        if self.is_running:
            return

        async with self._start_lock:
            if self.is_running:
                return

            if self._proc or self._ws:
                await self.stop()

            cmd = shlex.split(self.command)
            if not cmd:
                raise RuntimeError("CODEX_COMMAND is empty")
            if config.codex_disable_update_check:
                cmd.extend(["-c", CODEX_UPDATE_CHECK_CONFIG])
            if config.codex_dangerously_bypass_approvals_and_sandbox:
                cmd.append(CODEX_DANGEROUS_BYPASS_FLAG)

            listen_url = config.codex_app_server_listen
            if not listen_url.startswith(("ws://", "wss://")):
                raise RuntimeError(
                    "CODEX_APP_SERVER_LISTEN must be a ws:// or wss:// URL "
                    "so tmux Codex TUI clients can attach with --remote"
                )

            env = os.environ.copy()
            env["CODEX_HOME"] = str(config.codex_home)
            if config.openai_api_key:
                env["OPENAI_API_KEY"] = config.openai_api_key
            if config.openai_base_url:
                env["OPENAI_BASE_URL"] = config.openai_base_url

            loop = asyncio.get_running_loop()
            self._remote_url_future = loop.create_future()
            self._remote_url = None

            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                "app-server",
                "--listen",
                listen_url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._stdout_task = asyncio.create_task(self._read_process_stream("stdout"))
            self._stderr_task = asyncio.create_task(self._read_process_stream("stderr"))
            self._exit_task = asyncio.create_task(self._watch_process_exit())

            try:
                self._remote_url = await asyncio.wait_for(
                    self._remote_url_future, timeout=30.0
                )
            except Exception:
                await self.stop()
                raise

            try:
                self._ws = await connect(self._remote_url)
                self._ws_reader_task = asyncio.create_task(self._read_ws_messages())

                await self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "ccbot",
                            "title": "CCBot",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=30.0,
                )
            except Exception:
                await self.stop()
                raise

    async def stop(self) -> None:
        """Stop app-server and cancel background readers."""
        self._fail_pending(RuntimeError("Codex app-server stopped"))

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Failed to close Codex WebSocket", exc_info=True)
        self._ws = None

        tasks = [
            self._ws_reader_task,
            self._stdout_task,
            self._stderr_task,
            self._exit_task,
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)

        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        self._remote_url = None
        self._remote_url_future = None
        self._ws_reader_task = None
        self._stdout_task = None
        self._stderr_task = None
        self._exit_task = None

    async def request(
        self, method: str, params: dict[str, Any] | None, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await its result."""
        if not self.is_running:
            await self.start()
        if not self._ws:
            raise RuntimeError("Codex app-server WebSocket is not connected")

        request_id = str(self._next_id)
        self._next_id += 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        try:
            payload = {"id": request_id, "method": method, "params": params}
            await self._send_message(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _send_message(self, payload: dict[str, Any]) -> None:
        if not self._ws:
            raise RuntimeError("Codex app-server WebSocket is not connected")
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    async def _read_process_stream(self, stream_name: str) -> None:
        assert self._proc
        stream = self._proc.stdout if stream_name == "stdout" else self._proc.stderr
        assert stream
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            match = LISTENING_RE.search(line)
            if match and self._remote_url_future and not self._remote_url_future.done():
                self._remote_url_future.set_result(match.group(1))
            logger.debug("codex app-server %s: %s", stream_name, line)

    async def _watch_process_exit(self) -> None:
        assert self._proc
        returncode = await self._proc.wait()
        if self._remote_url_future and not self._remote_url_future.done():
            self._remote_url_future.set_exception(
                RuntimeError(f"Codex app-server exited before listening: {returncode}")
            )
        self._fail_pending(RuntimeError(f"Codex app-server exited: {returncode}"))

    async def _read_ws_messages(self) -> None:
        assert self._ws
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace").strip()
                else:
                    line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON app-server websocket message: %s", line)
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail_pending(RuntimeError(f"Codex app-server websocket closed: {e}"))
            logger.debug("Codex WebSocket reader stopped: %s", e)

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if msg_id is not None and ("result" in message or "error" in message):
            future = self._pending.get(str(msg_id))
            if not future or future.done():
                return
            if "error" in message:
                err = message["error"]
                if isinstance(err, dict):
                    text = err.get("message", str(err))
                else:
                    text = str(err)
                future.set_exception(RuntimeError(text))
            else:
                result = message.get("result")
                future.set_result(result if isinstance(result, dict) else {})
            return

        # Requests initiated by app-server. The Telegram approval UI is not
        # wired yet, so decline/cancel by default to avoid hanging the agent.
        if msg_id is not None and "method" in message:
            await self._respond_to_server_request(message)
            return

        if "method" in message and self._notification_callback:
            try:
                await self._notification_callback(message)
            except Exception:
                logger.exception("Codex notification callback failed")

    async def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        if not self._ws:
            return
        method = message.get("method", "")
        response: dict[str, Any]
        if method == "item/commandExecution/requestApproval":
            response = {"decision": "decline"}
        elif method == "item/fileChange/requestApproval":
            response = {"decision": "decline"}
        elif method == "applyPatchApproval":
            response = {"decision": "denied"}
        elif method == "execCommandApproval":
            response = {"decision": "denied"}
        elif method == "item/tool/call":
            response = {"contentItems": [], "success": False}
        elif method == "item/tool/requestUserInput":
            response = {"answers": {}}
        else:
            error_payload = {
                "id": message.get("id"),
                "error": {
                    "code": -32601,
                    "message": f"Unsupported server request: {method}",
                },
            }
            await self._send_message(error_payload)
            return

        payload = {"id": message.get("id"), "result": response}
        await self._send_message(payload)


class CodexRemoteManager:
    """High-level Codex remote session manager used by bot.py."""

    def __init__(self) -> None:
        self.client = CodexAppServerClient()
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._started_tool_ids: set[tuple[str, str]] = set()
        self._pending_tools: dict[tuple[str, str], PendingToolInfo] = {}
        self._active_turn_ids: dict[str, str] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        """Set callback for converted Codex messages."""
        self._message_callback = callback

    async def start(self) -> None:
        self.client.set_notification_callback(self._handle_notification)
        await self.client.start()

    async def stop(self) -> None:
        await self.client.stop()
        self._active_turn_ids.clear()

    @staticmethod
    def _has_local_session_file(thread_id: str) -> bool:
        """Return True once Codex has persisted the session file used by `resume`."""
        if not thread_id or not config.codex_sessions_path.exists():
            return False
        pattern = f"rollout-*-{thread_id}.jsonl"
        try:
            return any(
                path.is_file() and path.stat().st_size > 0
                for path in config.codex_sessions_path.rglob(pattern)
            )
        except OSError:
            return False

    async def wait_until_resumable(
        self,
        thread_id: str,
        timeout: float | None = None,
        interval: float = 0.2,
    ) -> bool:
        """Wait until `codex resume <thread_id>` can see the local session file."""
        deadline = asyncio.get_running_loop().time() + (
            config.codex_session_persist_timeout if timeout is None else timeout
        )
        while True:
            if await asyncio.to_thread(self._has_local_session_file, thread_id):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(interval)

    @staticmethod
    def _approval_policy() -> str:
        """Return the effective approval policy for Codex remote turns."""
        if config.codex_dangerously_bypass_approvals_and_sandbox:
            return "never"
        return config.codex_approval_policy

    @staticmethod
    def _sandbox() -> str:
        """Return the effective sandbox mode for Codex remote turns."""
        if config.codex_dangerously_bypass_approvals_and_sandbox:
            return "danger-full-access"
        return config.codex_sandbox

    @staticmethod
    def _codex_command_parts(command: str) -> list[str]:
        """Return Codex command parts with automation-safe global overrides."""
        parts = shlex.split(command)
        if not parts:
            raise RuntimeError("CODEX_COMMAND is empty")
        if config.codex_disable_update_check:
            parts.extend(["-c", CODEX_UPDATE_CHECK_CONFIG])
        if config.codex_dangerously_bypass_approvals_and_sandbox:
            parts.append(CODEX_DANGEROUS_BYPASS_FLAG)
        return parts

    def build_tui_command(self, thread_id: str, cwd: str) -> str:
        """Build a Codex TUI command attached to the managed app-server."""
        remote_url = self.client.remote_url
        if not remote_url:
            raise RuntimeError("Codex app-server has not exposed a remote URL")

        parts = self._codex_command_parts(self.client.command)
        parts.extend(
            [
                "resume",
                thread_id,
                "--remote",
                remote_url,
                "--cd",
                str(Path(cwd).expanduser().resolve()),
                "--no-alt-screen",
            ]
        )
        if not config.codex_dangerously_bypass_approvals_and_sandbox:
            parts.extend(
                [
                    "--ask-for-approval",
                    config.codex_approval_policy,
                    "--sandbox",
                    config.codex_sandbox,
                ]
            )
        if config.codex_model:
            parts.extend(["--model", config.codex_model])
        return shlex.join(parts)

    async def create_thread(
        self, cwd: str, resume_thread_id: str | None = None
    ) -> CodexThread:
        """Create or resume a Codex thread via app-server."""
        path = str(Path(cwd).expanduser().resolve())
        common: dict[str, Any] = {
            "cwd": path,
            "approvalPolicy": self._approval_policy(),
            "sandbox": self._sandbox(),
            "threadSource": "user",
        }
        if config.codex_model:
            common["model"] = config.codex_model

        if resume_thread_id:
            params = dict(common)
            params["threadId"] = resume_thread_id
            result = await self.client.request("thread/resume", params, timeout=60.0)
        else:
            result = await self.client.request("thread/start", common, timeout=60.0)

        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("Codex app-server did not return thread metadata")

        thread_id = str(thread.get("id", ""))
        if not thread_id:
            raise RuntimeError("Codex app-server returned an empty thread id")
        name = thread.get("name") or thread.get("preview") or Path(path).name
        return CodexThread(
            thread_id=thread_id,
            session_id=str(thread.get("sessionId") or thread_id),
            cwd=str(thread.get("cwd") or path),
            path=str(thread.get("path") or ""),
            name=str(name),
        )

    async def send_to_thread(self, thread_id: str, text: str) -> tuple[bool, str]:
        """Start a Codex turn with plain user text."""
        if not thread_id:
            return False, "Missing Codex thread id"
        try:
            result = await self.client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {"type": "text", "text": text, "text_elements": []},
                    ],
                },
                timeout=30.0,
            )
            turn_id = self._turn_id_from_result(result)
            if turn_id:
                self._active_turn_ids[thread_id] = turn_id
        except Exception as e:
            logger.exception("Failed to start Codex turn")
            return False, str(e)
        return True, f"Sent to Codex thread {thread_id}"

    async def interrupt_thread(self, thread_id: str) -> tuple[bool, str]:
        """Interrupt the active turn for a Codex thread."""
        turn_id = await self._resolve_active_turn_id(thread_id)
        if not turn_id:
            return False, "No active Codex turn to interrupt"
        try:
            await self.client.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=10.0,
            )
        except Exception as e:
            return False, str(e)
        return True, "Interrupted"

    async def _resolve_active_turn_id(self, thread_id: str) -> str:
        turn_id = self._active_turn_ids.get(thread_id)
        if turn_id:
            return turn_id

        try:
            result = await self.client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
                timeout=10.0,
            )
        except Exception:
            logger.debug("Failed to read Codex thread %s for interrupt", thread_id)
            return ""

        turn_id = self._active_turn_id_from_thread(result.get("thread"))
        if turn_id:
            self._active_turn_ids[thread_id] = turn_id
        return turn_id

    @staticmethod
    def _active_turn_id_from_thread(thread: Any) -> str:
        if not isinstance(thread, dict):
            return ""
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return ""
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            if turn.get("status") == "inProgress":
                return str(turn.get("id") or "")
        return ""

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params", {})
        if not isinstance(params, dict):
            return

        self._track_turn_lifecycle(method, params)
        new_message = self._message_from_notification(method, params)
        if new_message and self._message_callback:
            await self._message_callback(new_message)

    @staticmethod
    def _turn_id_from_result(result: dict[str, Any]) -> str:
        turn = result.get("turn")
        if isinstance(turn, dict):
            return str(turn.get("id") or "")
        return ""

    def _track_turn_lifecycle(self, method: str, params: dict[str, Any]) -> None:
        if method not in {"turn/started", "turn/completed"}:
            return
        thread_id = str(params.get("threadId") or "")
        turn = params.get("turn")
        if not thread_id or not isinstance(turn, dict):
            return
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            return
        if method == "turn/started":
            self._active_turn_ids[thread_id] = turn_id
        elif self._active_turn_ids.get(thread_id) == turn_id:
            self._active_turn_ids.pop(thread_id, None)

    def _message_from_notification(
        self, method: str, params: dict[str, Any]
    ) -> NewMessage | None:
        if method == "item/started":
            return self._tool_message_from_item(params, complete=False)
        if method == "item/completed":
            return self._item_completed_message(params)
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("status") == "failed":
                error = turn.get("error")
                if isinstance(error, dict):
                    text = error.get("message") or str(error)
                else:
                    text = str(error or "Turn failed")
                return NewMessage(
                    session_id=str(params.get("threadId", "")),
                    text=f"Codex turn failed: {text}",
                    is_complete=True,
                    content_type="text",
                    role="assistant",
                )
        return None

    def _item_completed_message(self, params: dict[str, Any]) -> NewMessage | None:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        thread_id = str(params.get("threadId", ""))
        item_type = item.get("type")

        if item_type == "agentMessage":
            text = str(item.get("text") or "")
            if not text.strip():
                return None
            return NewMessage(
                session_id=thread_id,
                text=text,
                is_complete=True,
                content_type="text",
                role="assistant",
            )

        if item_type == "reasoning":
            parts = []
            for key in ("summary", "content"):
                value = item.get(key)
                if isinstance(value, list):
                    parts.extend(str(v) for v in value if v)
            text = "\n".join(parts).strip()
            if not text:
                return None
            return NewMessage(
                session_id=thread_id,
                text=text,
                is_complete=True,
                content_type="thinking",
                role="assistant",
            )

        if item_type == "userMessage":
            content = item.get("content")
            texts = []
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text", "")))
            text = "\n".join(t for t in texts if t).strip()
            if not text:
                return None
            return NewMessage(
                session_id=thread_id,
                text=text,
                is_complete=True,
                content_type="text",
                role="user",
            )

        tool_result = self._tool_message_from_item(params, complete=True)
        return tool_result

    def _tool_message_from_item(
        self, params: dict[str, Any], *, complete: bool
    ) -> NewMessage | None:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        thread_id = str(params.get("threadId", ""))
        item_id = self._tool_item_id(params, item)
        if not thread_id or not item_id:
            return None

        formatted = self._format_tool_item(item)
        if not formatted:
            return None

        key = (thread_id, item_id)
        if not complete:
            if key in self._started_tool_ids:
                return None
            self._started_tool_ids.add(key)
            self._pending_tools[key] = PendingToolInfo(
                summary=formatted.summary,
                tool_name=formatted.tool_name,
                input_data=formatted.input_data,
            )
            return NewMessage(
                session_id=thread_id,
                text=formatted.summary,
                is_complete=True,
                content_type="tool_use",
                tool_use_id=item_id,
                role="assistant",
                tool_name=formatted.tool_name,
            )

        self._started_tool_ids.discard(key)
        pending = self._pending_tools.pop(key, None)
        summary = pending.summary if pending else formatted.summary
        tool_name = pending.tool_name if pending else formatted.tool_name
        detail = formatted.detail
        text = summary if not detail else f"{summary}\n{detail}"
        return NewMessage(
            session_id=thread_id,
            text=text,
            is_complete=True,
            content_type="tool_result",
            tool_use_id=item_id,
            role="assistant",
            tool_name=tool_name,
        )

    @staticmethod
    def _tool_item_id(params: dict[str, Any], item: dict[str, Any]) -> str:
        for key in ("id", "itemId", "item_id", "callId", "call_id", "processId"):
            value = item.get(key)
            if value:
                return str(value)
        for key in ("itemId", "item_id", "callId", "call_id"):
            value = params.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _stringify_result(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("text")
                        or item.get("output_text")
                        or item.get("input_text")
                        or item.get("content")
                    )
                    if text:
                        parts.append(str(text))
                    else:
                        parts.append(json.dumps(item, ensure_ascii=False, indent=2))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("text", "output", "content", "message"):
                text = value.get(key)
                if isinstance(text, str):
                    return text
                if isinstance(text, list):
                    return CodexRemoteManager._stringify_result(text)
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _command_output(item: dict[str, Any]) -> str:
        output = item.get("aggregatedOutput", item.get("aggregated_output"))
        if output:
            return str(output)

        parts = []
        stdout = item.get("stdout")
        stderr = item.get("stderr")
        if stdout:
            parts.append(str(stdout))
        if stderr:
            parts.append(str(stderr))
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _status_detail(status: str, exit_code: Any = None) -> str:
        if exit_code not in (None, 0, "0"):
            return f"  ⎿  Exit code {exit_code}"
        if status and status not in {"completed", "success", "succeeded"}:
            return f"  ⎿  {status}"
        if exit_code in (0, "0"):
            return "  ⎿  Completed"
        return ""

    @staticmethod
    def _is_failed_status(status: str, exit_code: Any = None) -> bool:
        if exit_code not in (None, 0, "0"):
            return True
        return bool(status and status not in {"completed", "success", "succeeded"})

    @staticmethod
    def _format_error_detail(output: str, fallback: str) -> str:
        text = output.strip()
        if not text:
            return f"  ⎿  Error: {fallback}" if fallback else "  ⎿  Error"

        first = text.splitlines()[0]
        if len(first) > 100:
            first = first[:100] + "…"
        detail = f"  ⎿  Error: {first}"
        if "\n" in text:
            detail += "\n" + TranscriptParser._format_expandable_quote(text)
        return detail

    @staticmethod
    def _format_tool_item(item: dict[str, Any]) -> FormattedToolItem | None:
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            status = str(item.get("status") or "")
            output = CodexRemoteManager._command_output(item)
            exit_code = item.get("exitCode", item.get("exit_code"))
            input_data = {"command": command}
            summary = TranscriptParser.format_tool_use_summary("Bash", input_data)
            if CodexRemoteManager._is_failed_status(status, exit_code):
                fallback = (
                    f"exit code {exit_code}"
                    if exit_code not in (None, 0, "0")
                    else status
                )
                detail = CodexRemoteManager._format_error_detail(output, fallback)
            else:
                detail = TranscriptParser._format_tool_result_text(
                    output, "Bash", input_data
                )
                if not detail:
                    detail = CodexRemoteManager._status_detail(status, exit_code)
            return FormattedToolItem("Bash", summary, detail, input_data)

        if item_type == "fileChange":
            changes = item.get("changes")
            count = len(changes) if isinstance(changes, list) else 0
            status = str(item.get("status") or "")
            stdout = CodexRemoteManager._stringify_result(item.get("stdout")).strip()
            stderr = CodexRemoteManager._stringify_result(item.get("stderr")).strip()
            summary = f"**FileChange**({count} file(s))"
            detail_parts = []
            status_detail = CodexRemoteManager._status_detail(status)
            if status_detail:
                detail_parts.append(status_detail)
            output = "\n".join(part for part in (stdout, stderr) if part)
            if output:
                detail_parts.append(TranscriptParser._format_expandable_quote(output))
            return FormattedToolItem("FileChange", summary, "\n".join(detail_parts))

        if item_type == "mcpToolCall":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "tool")
            status = str(item.get("status") or "")
            summary = f"**{server}/{tool}**" if server else f"**{tool}**"
            result = CodexRemoteManager._stringify_result(item.get("result")).strip()
            error = CodexRemoteManager._stringify_result(item.get("error")).strip()
            detail_parts = []
            status_detail = CodexRemoteManager._status_detail(status)
            if error:
                first = error.splitlines()[0]
                detail_parts.append(f"  ⎿  Error: {first}")
                if "\n" in error:
                    detail_parts.append(
                        TranscriptParser._format_expandable_quote(error)
                    )
            elif result:
                detail_parts.append(TranscriptParser._format_expandable_quote(result))
            elif status_detail:
                detail_parts.append(status_detail)
            return FormattedToolItem(tool, summary, "\n".join(detail_parts))

        if item_type == "dynamicToolCall":
            namespace = item.get("namespace")
            tool = str(item.get("tool") or "tool")
            name = f"{namespace}.{tool}" if namespace else tool
            status = str(item.get("status") or "")
            success = item.get("success")
            arguments = item.get("arguments")
            input_data = arguments if isinstance(arguments, dict) else {}
            summary = TranscriptParser.format_tool_use_summary(name, input_data)
            detail_parts = []
            content = CodexRemoteManager._stringify_result(
                item.get("contentItems", item.get("content_items"))
            ).strip()
            if content:
                detail_parts.append(TranscriptParser._format_expandable_quote(content))
            elif success is False:
                detail_parts.append("  ⎿  Failed")
            else:
                status_detail = CodexRemoteManager._status_detail(status)
                if status_detail:
                    detail_parts.append(status_detail)
            return FormattedToolItem(tool, summary, "\n".join(detail_parts), input_data)

        if item_type == "webSearch":
            query = str(item.get("query") or "")
            input_data = {"query": query}
            summary = TranscriptParser.format_tool_use_summary("WebSearch", input_data)
            return FormattedToolItem("WebSearch", summary)

        return None


codex_remote_manager = CodexRemoteManager()
