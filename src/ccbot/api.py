"""Local debug/control HTTP API — the automation surface for ccbot.

An aiohttp server bound to loopback (config.api_host, default 127.0.0.1). Every
route is a thin adapter over one ControlService method. Because ALLOWED_USERS
restricts Telegram to a human, an agent drives ccbot through this API instead:
it can create/resume/send/kill sessions and read Claude's output (ring buffer)
without going through Telegram.

SECURITY: /send injects keystrokes and POST /sessions spawns Claude — these are
privilege-equivalent to running commands as the user. Therefore:
  - fail-closed: without CCBOT_API_TOKEN the server refuses to start;
  - Bearer token compared in constant time (hmac.compare_digest);
  - loopback bind only;
  - optional cwd allowlist for POST /sessions;
  - every request is audit-logged.

Key entry point: run_api(service).
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import config
from .service import ControlService

logger = logging.getLogger(__name__)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    presented = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer ") :].strip()
    # config.api_token is guaranteed non-empty here (run_api refuses to start
    # otherwise), so an empty/absent header can never match.
    if not hmac.compare_digest(presented, config.api_token):
        logger.warning("API auth failed: %s %s", request.method, request.path)
        return _json({"error": "unauthorized"}, status=401)
    return await handler(request)


@web.middleware
async def _audit_middleware(request: web.Request, handler):
    logger.info(
        "API %s %s from %s",
        request.method,
        request.rel_url,
        request.remote,
    )
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # never leak a stack trace to the client
        logger.exception("API handler error")
        return _json({"error": str(e)}, status=500)


def _validate_cwd(cwd: str) -> str | None:
    """Return an error string if cwd is unusable, else None."""
    if not cwd or not Path(cwd).is_absolute():
        return "cwd must be an absolute path"
    p = Path(cwd)
    if not p.is_dir():
        return f"cwd is not a directory: {cwd}"
    if config.api_allowed_cwds:
        rp = str(p.resolve())
        if not any(
            rp == root or rp.startswith(root.rstrip("/") + "/")
            for root in config.api_allowed_cwds
        ):
            return f"cwd not in CCBOT_API_ALLOWED_CWDS: {cwd}"
    return None


class Api:
    """Route handlers bound to a ControlService instance."""

    def __init__(self, service: ControlService) -> None:
        self.svc = service

    async def health(self, request: web.Request) -> web.Response:
        return _json(await self.svc.health())

    async def list_sessions(self, request: web.Request) -> web.Response:
        return _json(await self.svc.list_sessions())

    async def get_session(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        for s in await self.svc.list_sessions():
            if s["window_id"] == wid:
                return _json(s)
        return _json({"error": "window not found"}, status=404)

    async def create_session(self, request: web.Request) -> web.Response:
        body = await request.json()
        cwd = body.get("cwd", "")
        err = _validate_cwd(cwd)
        if err:
            return _json({"error": err}, status=400)
        result = await self.svc.create_session(
            cwd,
            resume_session_id=body.get("resume_session_id"),
            bind_thread_id=body.get("bind_thread_id"),
            user_id=body.get("user_id"),
            chat_id=body.get("chat_id"),
            pending_text=body.get("pending_text"),
            window_name=body.get("window_name"),
        )
        status = 200 if result.success else 400
        return _json(result.__dict__, status=status)

    async def resume_session(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        body = await request.json()
        cwd = body.get("cwd", "")
        session_id = body.get("session_id", "")
        err = _validate_cwd(cwd)
        if err:
            return _json({"error": err}, status=400)
        # Preserve the existing thread binding, if any, then re-open resumed.
        binding = next(
            (
                (u, t)
                for u, t, w in self.svc.sm.iter_thread_bindings()
                if w == wid
            ),
            None,
        )
        await self.svc.kill_session(wid)
        result = await self.svc.create_session(
            cwd,
            resume_session_id=session_id,
            bind_thread_id=binding[1] if binding else None,
            user_id=binding[0] if binding else None,
        )
        return _json(result.__dict__, status=200 if result.success else 400)

    async def send(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        body = await request.json()
        text = body.get("text", "")
        if not text:
            return _json({"error": "text is required"}, status=400)
        ok, msg = await self.svc.send_text(wid, text)
        return _json({"ok": ok, "message": msg}, status=200 if ok else 400)

    async def output(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        lines = int(request.query.get("lines", "60"))
        ansi = request.query.get("ansi", "").lower() in ("1", "true", "yes")
        text = await self.svc.capture_output(wid, lines=lines, with_ansi=ansi)
        if text is None:
            return _json({"error": "capture failed"}, status=404)
        return _json({"window_id": wid, "output": text})

    async def screenshot(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        png = await self.svc.screenshot(wid)
        if png is None:
            return _json({"error": "screenshot failed"}, status=404)
        return web.Response(body=png, content_type="image/png")

    async def messages(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        since = int(request.query.get("since", "0"))
        ws = self.svc.sm.window_states.get(wid)
        if not ws or not ws.session_id:
            return _json({"window_id": wid, "session_id": None, "messages": []})
        return _json(
            {
                "window_id": wid,
                "session_id": ws.session_id,
                "messages": self.svc.bus.tail(ws.session_id, since),
            }
        )

    async def restart(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        result = await self.svc.restart_session(wid)
        return _json(result.__dict__, status=200 if result.success else 400)

    async def heal(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        result = await self.svc.heal(wid)
        return _json(result.__dict__, status=200 if result.success else 400)

    async def kill(self, request: web.Request) -> web.Response:
        wid = request.match_info["wid"]
        ok = await self.svc.kill_session(wid)
        return _json({"ok": ok, "window_id": wid})

    async def bindings(self, request: web.Request) -> web.Response:
        return _json(await self.svc.list_bindings())

    async def monitor(self, request: web.Request) -> web.Response:
        return _json(self.svc.get_monitor_state())

    async def resumable(self, request: web.Request) -> web.Response:
        cwd = request.query.get("cwd", "")
        if not cwd:
            return _json({"error": "cwd query param required"}, status=400)
        return _json({"cwd": cwd, "sessions": await self.svc.list_resumable_sessions(cwd)})

    async def reconcile(self, request: web.Request) -> web.Response:
        return _json(await self.svc.reconcile())

    async def control_topic(self, request: web.Request) -> web.Response:
        return _json({"control_topic": self.svc.sm.control_topic})

    async def logs(self, request: web.Request) -> web.Response:
        lines = int(request.query.get("lines", "80"))
        log_path = config.config_dir / "run.log"
        if not log_path.exists():
            return _json({"error": "run.log not found"}, status=404)
        try:
            content = log_path.read_text(errors="replace").splitlines()[-lines:]
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        return _json({"lines": content})


def build_app(service: ControlService) -> web.Application:
    api = Api(service)
    app = web.Application(middlewares=[_audit_middleware, _auth_middleware])
    app.add_routes(
        [
            web.get("/health", api.health),
            web.get("/sessions", api.list_sessions),
            web.post("/sessions", api.create_session),
            web.get("/sessions/{wid}", api.get_session),
            web.post("/sessions/{wid}/resume", api.resume_session),
            web.post("/sessions/{wid}/send", api.send),
            web.get("/sessions/{wid}/output", api.output),
            web.get("/sessions/{wid}/screenshot", api.screenshot),
            web.get("/sessions/{wid}/messages", api.messages),
            web.post("/sessions/{wid}/restart", api.restart),
            web.post("/sessions/{wid}/heal", api.heal),
            web.delete("/sessions/{wid}", api.kill),
            web.get("/bindings", api.bindings),
            web.get("/monitor", api.monitor),
            web.get("/resumable", api.resumable),
            web.post("/reconcile", api.reconcile),
            web.get("/control-topic", api.control_topic),
            web.get("/logs", api.logs),
        ]
    )
    return app


async def run_api(service: ControlService) -> None:
    """Start the API server. Fail-closed: no token → no server.

    Runs until cancelled (call as a supervised background task).
    """
    if not config.api_token:
        logger.warning(
            "API disabled: CCBOT_API_TOKEN not set (fail-closed, no server bound)"
        )
        return
    app = build_app(service)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.api_host, config.api_port)
    await site.start()
    logger.info("Control API listening on %s:%s", config.api_host, config.api_port)
    try:
        import asyncio

        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        logger.info("Control API stopped")
