"""Centro de mando: FastAPI + WebSocket en 127.0.0.1.

Seguridad (un servidor en localhost es alcanzable desde cualquier web abierta):
- Solo se aceptan peticiones cuyo `Host` sea 127.0.0.1/localhost:<puerto> (anti DNS rebinding).
- Las acciones (POST) exigen la cabecera `X-Swarm-Token` con un token aleatorio por sesión;
  una cabecera personalizada fuerza un preflight CORS que no autorizamos, así que otra web
  no puede disparar acciones.
- El WebSocket solo acepta `Origin` del propio panel (los WS no están sujetos a CORS).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from swarm_pilot.core.events import EventBus

STATIC_DIR = Path(__file__).with_name("static")


class SessionLike(Protocol):
    bus: EventBus

    def snapshot(self) -> dict[str, Any]: ...
    def answer(self, task_id: str, action: str, hint: str | None) -> str | None: ...
    def approve(self, approved: bool) -> str | None: ...


class AnswerBody(BaseModel):
    task_id: str = ""
    action: str
    hint: str | None = None


class ApproveBody(BaseModel):
    approve: bool


def create_app(session: SessionLike, token: str, port_getter: Callable[[], int]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def allowed_hosts() -> set[str]:
        port = port_getter()
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def check_host(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.headers.get("host") not in allowed_hosts():
            return JSONResponse({"detail": "host no permitido"}, status_code=403)
        return await call_next(request)

    def require_token(request: Request) -> None:
        if not secrets.compare_digest(request.headers.get("x-swarm-token", ""), token):
            raise HTTPException(403, "token inválido")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__SWARM_TOKEN__", token),
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                     "Referrer-Policy": "no-referrer"},
        )  # fmt: skip

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return session.snapshot()

    @app.post("/api/answer")
    async def answer(body: AnswerBody, request: Request) -> dict[str, bool]:
        require_token(request)
        error = session.answer(body.task_id, body.action, body.hint)
        if error:
            raise HTTPException(400, error)
        return {"ok": True}

    @app.post("/api/approve")
    async def approve(body: ApproveBody, request: Request) -> dict[str, bool]:
        require_token(request)
        error = session.approve(body.approve)
        if error:
            raise HTTPException(409, error)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        host_ok = websocket.headers.get("host") in allowed_hosts()
        origin_ok = origin is None or origin.split("://", 1)[-1] in allowed_hosts()
        if not (host_ok and origin_ok):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        sub = session.bus.subscribe(maxsize=5000)
        try:
            await websocket.send_json({"type": "snapshot", "data": session.snapshot()})
            async for event in sub:
                await websocket.send_text(event.model_dump_json())
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            sub.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    return app


class _QuietServer(uvicorn.Server):
    """uvicorn sin manejadores de señales propios: Ctrl+C lo gestiona swarm-pilot."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        return

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
        yield


class WebServer:
    def __init__(self, session: SessionLike, port: int, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.app = create_app(session, self.token, lambda: self.port)
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="warning", lifespan="off",
            timeout_graceful_shutdown=2, ws_ping_interval=20,
        )  # fmt: skip
        self._server = _QuietServer(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"no se pudo iniciar el panel en {self.url}: {exc}")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 5)
        except (asyncio.TimeoutError, Exception):
            self._server.force_exit = True
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._server = None
