"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from ccbot.config import AGENT_CLAUDE, AGENT_CODEX, Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.delenv("CCBOT_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("CCBOT_ENABLED_AGENTS", raising=False)


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False

    def test_default_agent(self):
        cfg = Config()
        assert cfg.default_agent == AGENT_CLAUDE
        assert cfg.enabled_agents == (AGENT_CLAUDE,)

    def test_codex_agent_config(self, monkeypatch):
        monkeypatch.setenv("CCBOT_ENABLED_AGENTS", "claude,codex")
        monkeypatch.setenv("CCBOT_DEFAULT_AGENT", AGENT_CODEX)
        monkeypatch.setenv("CCBOT_CODEX_HOME", "/tmp/codex-home")
        cfg = Config()
        assert cfg.default_agent == AGENT_CODEX
        assert cfg.enabled_agents == (AGENT_CLAUDE, AGENT_CODEX)
        assert cfg.is_agent_enabled(AGENT_CODEX) is True
        assert cfg.codex_home == Path("/tmp/codex-home")
        assert cfg.codex_sessions_path == Path("/tmp/codex-home/sessions")
        assert cfg.codex_app_server_listen == "ws://127.0.0.1:0"
        assert cfg.codex_disable_update_check is True
        assert cfg.codex_dangerously_bypass_approvals_and_sandbox is True
        assert cfg.codex_session_persist_timeout == 10.0

    def test_codex_remote_can_disable_automation_overrides(self, monkeypatch):
        monkeypatch.setenv("CCBOT_CODEX_DISABLE_UPDATE_CHECK", "false")
        monkeypatch.setenv("CODEX_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX", "false")
        monkeypatch.setenv("CCBOT_CODEX_SESSION_PERSIST_TIMEOUT", "2.5")
        cfg = Config()
        assert cfg.codex_disable_update_check is False
        assert cfg.codex_dangerously_bypass_approvals_and_sandbox is False
        assert cfg.codex_session_persist_timeout == 2.5

    def test_invalid_default_agent(self, monkeypatch):
        monkeypatch.setenv("CCBOT_DEFAULT_AGENT", "bad")
        with pytest.raises(ValueError, match="CCBOT_DEFAULT_AGENT"):
            Config()

    def test_default_agent_must_be_enabled(self, monkeypatch):
        monkeypatch.setenv("CCBOT_ENABLED_AGENTS", "claude")
        monkeypatch.setenv("CCBOT_DEFAULT_AGENT", "codex")
        with pytest.raises(ValueError, match="CCBOT_DEFAULT_AGENT"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigClaudeProjectsPath:
    def test_default_claude_projects_path(self, monkeypatch):
        """Default path is ~/.claude/projects when no env vars are set."""
        # Ensure no custom path env vars are set
        monkeypatch.delenv("CCBOT_CLAUDE_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = Config()
        assert cfg.claude_projects_path == Path.home() / ".claude" / "projects"

    def test_custom_claude_projects_path(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH overrides the default path."""
        custom_path = "/custom/projects/path"
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", custom_path)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_path)

    def test_claude_config_dir_projects_path(self, monkeypatch):
        """CLAUDE_CONFIG_DIR sets path to $CLAUDE_CONFIG_DIR/projects."""
        custom_config_dir = "/custom/claude/config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", custom_config_dir)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_config_dir) / "projects"

    def test_ccbot_projects_path_takes_priority(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH takes priority over CLAUDE_CONFIG_DIR."""
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", "/priority/path")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/lower/priority")
        cfg = Config()
        assert cfg.claude_projects_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None
