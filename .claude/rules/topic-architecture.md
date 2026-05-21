# Topic-Only Architecture

The bot operates exclusively in Telegram Forum (topics) mode. There is **no** `active_sessions` mapping, **no** `/list` command, **no** General topic routing, and **no** backward-compatibility logic for older non-topic modes. Every code path assumes named topics.

## 1 Topic = 1 Window = 1 Session

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│  (Telegram) │      │ (tmux @id)  │      │  (agent)    │
└─────────────┘      └─────────────┘      └─────────────┘
     thread_bindings      session metadata
     (state.json)         (hook or remote thread)
```

Window IDs (e.g. `@0`, `@12`) are guaranteed unique within a tmux server session. Window names are stored separately as display names (`window_display_names` map).

## Mapping 1: Topic → Window ID (thread_bindings)

```python
# session.py: SessionManager
thread_bindings: dict[int, dict[int, str]]  # user_id → {thread_id → window_id}
window_display_names: dict[str, str]        # window_id → window_name (for display)
```

- Storage: memory + `state.json`
- Written when: user creates a new session via the directory browser in a topic
- Purpose: route user messages to the correct tmux window

## Mapping 2: Window ID → Session

Claude Code sessions use the hook-generated `session_map.json`. Codex remote
sessions store the Codex thread id in `WindowState.session_id` and keep a real
tmux window for the attached TUI.

```python
# session_map.json (key format: "tmux_session:window_id")
{
  "ccbot:@0": {"session_id": "uuid-xxx", "cwd": "/path/to/project", "window_name": "project"},
  "ccbot:@5": {"session_id": "uuid-yyy", "cwd": "/path/to/project", "window_name": "project-2"}
}
```

- Storage: `session_map.json` for Claude, `state.json` window state for Codex
- Written when: Claude Code's `SessionStart` hook fires, or when a Codex remote thread is created/resumed
- Property: one window maps to one session; session_id changes after `/clear`
- Purpose: SessionMonitor uses Claude mappings to decide which JSONL files to watch; Codex uses app-server notifications and rollout files

## Message Flows

**Outbound** (user → agent):
```
User sends "hello" in topic (thread_id=42)
  → thread_bindings[user_id][42] → "@0"
  → send_to_window("@0", "hello")   # Claude: tmux keys; Codex: app-server turn/start
```

**Inbound** (agent → user):
```
SessionMonitor or Codex app-server receives new message (session_id = "uuid-xxx")
  → Iterate thread_bindings, find (user, thread) whose window_id maps to this session
  → Deliver message to user in the correct topic (thread_id)
```

**New topic flow**: First message in an unbound topic → agent picker (when multiple agents are enabled) → directory browser → select directory → session picker (if existing sessions found) or create window → bind topic → forward pending message.

**Resume session flow**: When selecting a directory with existing sessions for the chosen agent, a session picker UI is shown. Claude resume runs `claude --resume <session_id>`; Codex resume calls `thread/resume` and attaches a tmux-hosted TUI with `codex resume <thread_id> --remote ...`. Note: Claude `--resume` makes the hook report a new session_id but messages continue writing to the original JSONL file; the bot overrides window_state to track the original session_id.

**Topic lifecycle**: Closing/deleting a topic auto-kills the associated tmux window and unbinds the thread. Stale bindings (window deleted externally) are cleaned up by the status polling loop.

## Session Lifecycle

**Startup cleanup**: On bot startup, stale Claude sessions not present in `session_map.json` are cleaned up. Codex remote window state is preserved, and the bot attempts to restore missing tmux-hosted Codex TUIs when the app-server starts.

**Runtime change detection**: Each polling cycle checks live windows and Claude `session_map.json` changes:
- Window's session_id changed (e.g., after `/clear`) → clean up old session
- Window deleted → clean up corresponding binding, except Codex remote state may be preserved for restore
