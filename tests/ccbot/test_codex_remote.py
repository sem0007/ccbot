"""Tests for Codex remote transport helpers and notification conversion."""

import asyncio
import shlex

from ccbot import codex_remote
from ccbot.codex_remote import (
    CodexRemoteManager,
    codex_thread_id_from_window_id,
    is_codex_window_id,
    make_codex_window_id,
)


class FakeCodexClient:
    """Small request recorder for CodexRemoteManager unit tests."""

    def __init__(self, result=None) -> None:
        self.result = result or {}
        self.calls = []

    async def request(self, method, params, timeout=60.0):
        self.calls.append((method, params, timeout))
        return self.result


def test_codex_window_id_roundtrip() -> None:
    thread_id = "019e459d-c98d-7223-85b5-de7c290f859e"
    window_id = make_codex_window_id(thread_id)
    assert is_codex_window_id(window_id)
    assert codex_thread_id_from_window_id(window_id) == thread_id


def test_agent_message_completed_notification() -> None:
    manager = CodexRemoteManager()
    msg = manager._message_from_notification(
        "item/completed",
        {
            "threadId": "t1",
            "turnId": "turn1",
            "item": {
                "type": "agentMessage",
                "id": "item1",
                "text": "done",
                "phase": None,
                "memoryCitation": None,
            },
        },
    )
    assert msg is not None
    assert msg.session_id == "t1"
    assert msg.text == "done"
    assert msg.content_type == "text"


def test_command_execution_tool_pair() -> None:
    manager = CodexRemoteManager()
    params = {
        "threadId": "t1",
        "turnId": "turn1",
        "item": {
            "type": "commandExecution",
            "id": "cmd1",
            "command": "ls",
            "status": "completed",
            "aggregatedOutput": "a.py",
            "exitCode": 0,
        },
    }
    started = manager._message_from_notification("item/started", params)
    completed = manager._message_from_notification("item/completed", params)

    assert started is not None
    assert started.content_type == "tool_use"
    assert started.tool_use_id == "cmd1"
    assert started.tool_name == "Bash"
    assert started.text == "**Bash**(ls)"
    assert completed is not None
    assert completed.content_type == "tool_result"
    assert completed.tool_use_id == "cmd1"
    assert completed.tool_name == "Bash"
    assert "**Bash**(ls)" in completed.text
    assert "Output 1 lines" in completed.text
    assert "a.py" in completed.text


def test_command_execution_failure_uses_error_style() -> None:
    manager = CodexRemoteManager()
    params = {
        "threadId": "t1",
        "item": {
            "type": "commandExecution",
            "id": "cmd1",
            "command": "false",
            "status": "completed",
            "aggregatedOutput": "",
            "exitCode": 1,
        },
    }

    msg = manager._message_from_notification("item/completed", params)

    assert msg is not None
    assert msg.content_type == "tool_result"
    assert msg.tool_use_id == "cmd1"
    assert msg.text == "**Bash**(false)\n  ⎿  Error: exit code 1"


def test_build_tui_command_uses_remote_resume(tmp_path) -> None:
    manager = CodexRemoteManager()
    manager.client.command = "codex"
    manager.client._remote_url = "ws://127.0.0.1:12345"

    command = manager.build_tui_command("thread-1", str(tmp_path))
    parts = shlex.split(command)

    assert parts[:5] == [
        "codex",
        "-c",
        "check_for_update_on_startup=false",
        "--dangerously-bypass-approvals-and-sandbox",
        "resume",
    ]
    assert parts[5] == "thread-1"
    assert "--remote" in parts
    assert parts[parts.index("--remote") + 1] == "ws://127.0.0.1:12345"
    assert "--cd" in parts
    assert parts[parts.index("--cd") + 1] == str(tmp_path.resolve())
    assert "--no-alt-screen" in parts
    assert "--ask-for-approval" not in parts
    assert "--sandbox" not in parts


def test_build_tui_command_can_use_explicit_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_remote.config, "codex_disable_update_check", False)
    monkeypatch.setattr(
        codex_remote.config,
        "codex_dangerously_bypass_approvals_and_sandbox",
        False,
    )
    monkeypatch.setattr(codex_remote.config, "codex_approval_policy", "on-request")
    monkeypatch.setattr(codex_remote.config, "codex_sandbox", "read-only")

    manager = CodexRemoteManager()
    manager.client.command = "codex"
    manager.client._remote_url = "ws://127.0.0.1:12345"

    command = manager.build_tui_command("thread-1", str(tmp_path))
    parts = shlex.split(command)

    assert parts[:3] == ["codex", "resume", "thread-1"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in parts
    assert "-c" not in parts
    assert parts[parts.index("--ask-for-approval") + 1] == "on-request"
    assert parts[parts.index("--sandbox") + 1] == "read-only"


async def test_wait_until_resumable_waits_for_session_file(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(codex_remote.config, "codex_sessions_path", tmp_path)

    thread_id = "thread-1"
    session_file = (
        tmp_path
        / "2026"
        / "05"
        / "20"
        / f"rollout-2026-05-20T00-00-00-{thread_id}.jsonl"
    )

    async def persist_session_file() -> None:
        await asyncio.sleep(0.01)
        session_file.parent.mkdir(parents=True)
        session_file.write_text('{"type":"session_meta"}\n', encoding="utf-8")

    task = asyncio.create_task(persist_session_file())
    try:
        manager = CodexRemoteManager()
        assert await manager.wait_until_resumable(thread_id, timeout=1.0, interval=0.01)
    finally:
        await task


async def test_wait_until_resumable_times_out(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_remote.config, "codex_sessions_path", tmp_path)

    manager = CodexRemoteManager()
    assert not await manager.wait_until_resumable(
        "missing", timeout=0.01, interval=0.001
    )


async def test_send_to_thread_tracks_turn_id_from_result() -> None:
    manager = CodexRemoteManager()
    fake = FakeCodexClient(result={"turn": {"id": "turn1"}})
    manager.client = fake

    ok, _message = await manager.send_to_thread("thread1", "hello")

    assert ok
    assert manager._active_turn_ids["thread1"] == "turn1"
    assert fake.calls[0][0] == "turn/start"


async def test_interrupt_thread_sends_active_turn_id() -> None:
    manager = CodexRemoteManager()
    fake = FakeCodexClient()
    manager.client = fake
    manager._active_turn_ids["thread1"] = "turn1"

    ok, _message = await manager.interrupt_thread("thread1")

    assert ok
    assert fake.calls == [
        ("turn/interrupt", {"threadId": "thread1", "turnId": "turn1"}, 10.0)
    ]


async def test_interrupt_thread_requires_active_turn_id() -> None:
    manager = CodexRemoteManager()
    fake = FakeCodexClient()
    manager.client = fake

    ok, message = await manager.interrupt_thread("thread1")

    assert not ok
    assert message == "No active Codex turn to interrupt"
    assert fake.calls == [
        ("thread/read", {"threadId": "thread1", "includeTurns": True}, 10.0)
    ]


async def test_interrupt_thread_reads_active_turn_when_not_tracked() -> None:
    manager = CodexRemoteManager()
    fake = FakeCodexClient(
        result={
            "thread": {
                "turns": [
                    {"id": "old", "status": "completed"},
                    {"id": "turn1", "status": "inProgress"},
                ]
            }
        }
    )
    manager.client = fake

    ok, _message = await manager.interrupt_thread("thread1")

    assert ok
    assert fake.calls == [
        ("thread/read", {"threadId": "thread1", "includeTurns": True}, 10.0),
        ("turn/interrupt", {"threadId": "thread1", "turnId": "turn1"}, 10.0),
    ]


async def test_turn_notifications_track_active_turn() -> None:
    manager = CodexRemoteManager()

    await manager._handle_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "thread1", "turn": {"id": "turn1"}},
        }
    )
    assert manager._active_turn_ids["thread1"] == "turn1"

    await manager._handle_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread1", "turn": {"id": "turn1"}},
        }
    )
    assert "thread1" not in manager._active_turn_ids
