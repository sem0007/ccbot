"""Tests for SessionManager pure dict operations."""

from unittest.mock import AsyncMock

import pytest

from ccbot import session as session_module
from ccbot.codex_remote import make_codex_window_id
from ccbot.config import AGENT_CLAUDE, AGENT_CODEX
from ccbot.session import SessionManager, WindowState


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""
        assert mgr.window_agent("@0") == AGENT_CLAUDE

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""

    def test_window_state_serializes_agent(self) -> None:
        state = WindowState(
            agent=AGENT_CODEX,
            session_id="sid",
            cwd="/tmp/project",
            window_name="project",
        )
        restored = WindowState.from_dict(state.to_dict(), window_id="@1")
        assert restored.agent == AGENT_CODEX

    def test_missing_agent_defaults_to_claude(self, mgr: SessionManager) -> None:
        state = WindowState.from_dict(
            {"session_id": "sid", "cwd": "/tmp/project"},
            window_id="@1",
        )
        assert state.agent == ""
        mgr.window_states["@1"] = state
        assert mgr.window_agent("@1") == AGENT_CLAUDE


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"

    def test_bind_codex_thread_sets_state_and_display(
        self, mgr: SessionManager
    ) -> None:
        window_id = mgr.bind_codex_thread(
            100,
            1,
            "019e459d-c98d-7223-85b5-de7c290f859e",
            "/tmp/project",
            "project",
        )
        assert window_id == make_codex_window_id("019e459d-c98d-7223-85b5-de7c290f859e")
        assert mgr.get_window_for_thread(100, 1) == window_id
        assert mgr.get_display_name(window_id) == "project"
        assert mgr.get_window_state(window_id).cwd == "/tmp/project"
        assert mgr.get_window_state(window_id).agent == AGENT_CODEX


class TestWindowRebinding:
    def test_rebind_window_id_moves_all_persisted_state(
        self, mgr: SessionManager
    ) -> None:
        mgr.window_states["@1"] = WindowState(
            session_id="sid",
            cwd="/tmp/project",
            window_name="old",
        )
        mgr.window_display_names["@1"] = "old"
        mgr.thread_bindings[100] = {1: "@1", 2: "@2"}
        mgr.user_window_offsets[100] = {"@1": 25, "@2": 5}

        mgr.rebind_window_id("@1", "@9", "new")

        assert "@1" not in mgr.window_states
        assert mgr.window_states["@9"].session_id == "sid"
        assert mgr.window_states["@9"].cwd == "/tmp/project"
        assert mgr.window_states["@9"].window_name == "new"
        assert mgr.thread_bindings[100] == {1: "@9", 2: "@2"}
        assert mgr.user_window_offsets[100] == {"@9": 25, "@2": 5}
        assert mgr.window_display_names["@9"] == "new"

    async def test_resolve_stale_ids_preserves_missing_codex_remote_window(
        self, monkeypatch, mgr: SessionManager
    ) -> None:
        monkeypatch.setattr(
            session_module.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            mgr,
            "_cleanup_stale_session_map_entries",
            AsyncMock(),
        )
        monkeypatch.setattr(
            mgr,
            "_cleanup_old_format_session_map_keys",
            AsyncMock(),
        )
        mgr.window_states["@1"] = WindowState(
            agent=AGENT_CODEX,
            session_id="019e459d-c98d-7223-85b5-de7c290f859e",
            cwd="/tmp/project",
            window_name="project",
        )
        mgr.window_display_names["@1"] = "project"
        mgr.thread_bindings[100] = {1: "@1"}
        mgr.user_window_offsets[100] = {"@1": 25}

        await mgr.resolve_stale_ids()

        assert mgr.window_states["@1"].session_id == (
            "019e459d-c98d-7223-85b5-de7c290f859e"
        )
        assert mgr.thread_bindings[100] == {1: "@1"}
        assert mgr.user_window_offsets[100] == {"@1": 25}


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False
