"""Vista previa en vivo: detecta el servidor de desarrollo del proyecto y lo levanta.

Detección (en orden):
1. `package.json` con script `dev` o `start` → `npm run <script>` (con el flag de puerto
   adecuado para vite / next / astro; `PORT` en el entorno para el resto).
2. `vite.config.*` sin scripts → `npx vite`.
3. `index.html` en la raíz, `public/` o `dist/` → `python -m http.server`.

El servidor arranca en segundo plano (no frena al orquestador), escucha solo en 127.0.0.1
y se reinicia si cambian `package.json` o la config de vite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from swarm_pilot.core.environment import command_env
from swarm_pilot.core.events import EventBus
from swarm_pilot.core.models import EventType
from swarm_pilot.core.process_manager import ManagedProcess, ProcessManager

RESTART_TRIGGERS = ("package.json", "vite.config.js", "vite.config.ts", "vite.config.mjs",
                    "next.config.js", "next.config.mjs", "astro.config.mjs")  # fmt: skip
READY_TIMEOUT = 120.0


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int, host: str = "127.0.0.1", attempts: int = 50) -> int:
    for port in range(preferred, preferred + attempts):
        if port_is_free(port, host):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


@dataclass
class PreviewCommand:
    kind: str
    args: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    needs_install: bool = False


def detect_preview(project_dir: Path, port: int) -> PreviewCommand | None:
    p = str(port)
    pkg = project_dir / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except ValueError:
            scripts = {}
        needs_install = not (project_dir / "node_modules").is_dir()
        env = {"PORT": p, "HOST": "127.0.0.1", "BROWSER": "none"}
        for name in ("dev", "start"):
            script = scripts.get(name)
            if not isinstance(script, str) or not script.strip():
                continue
            low = script.lower()
            if "vite" in low:
                kind, extra = "vite", ["--port", p, "--strictPort", "--host", "127.0.0.1"]
            elif "next" in low:
                kind, extra = "next", ["-p", p, "-H", "127.0.0.1"]
            elif "astro" in low:
                kind, extra = "astro", ["--port", p, "--host", "127.0.0.1"]
            else:
                kind, extra = f"npm {name}", []
            args = ["npm", "run", name, *(["--", *extra] if extra else [])]
            return PreviewCommand(kind, args, project_dir, env, needs_install)
        if any((project_dir / f"vite.config.{ext}").is_file() for ext in ("js", "ts", "mjs")):
            args = ["npx", "vite", "--port", p, "--strictPort", "--host", "127.0.0.1"]
            return PreviewCommand("vite", args, project_dir, env, needs_install)
    for sub in ("", "public", "dist"):
        directory = project_dir / sub if sub else project_dir
        if (directory / "index.html").is_file():
            args = [sys.executable, "-m", "http.server", p, "--bind", "127.0.0.1",
                    "--directory", str(directory)]  # fmt: skip
            return PreviewCommand("static", args, project_dir)
    return None


class PreviewManager:
    def __init__(
        self, project_dir: Path, pm: ProcessManager, bus: EventBus, preferred_port: int = 3000
    ) -> None:
        self.project_dir = project_dir
        self.pm = pm
        self.bus = bus
        self.preferred_port = preferred_port
        self.state = "off"  # off | starting | ready | failed | stopped
        self.url: str | None = None
        self.kind: str | None = None
        self.error: str | None = None
        self._proc: ManagedProcess | None = None
        self._job: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def info(self) -> dict[str, object]:
        return {"state": self.state, "url": self.url, "kind": self.kind, "error": self.error}

    def _emit(self) -> None:
        self.bus.emit(EventType.PREVIEW_STATUS, **self.info())

    def _log(self, line: str) -> None:
        self.bus.emit(EventType.AGENT_LOG, task_id=None, line=line, source="preview")

    async def ensure(self, changed: list[str] | None = None) -> None:
        """Arranca (o reinicia) la vista previa en segundo plano si el proyecto la admite."""
        if self._closed:
            return
        restart = self.state in ("ready", "starting") and any(
            c in RESTART_TRIGGERS for c in (changed or [])
        )
        if self.state in ("ready", "starting") and not restart:
            if self.state == "ready":  # cambio de archivos: el panel recarga el iframe
                self.bus.emit(EventType.PREVIEW_STATUS, **self.info(), reload=True)
            return
        if self._job is not None and not self._job.done():
            return
        self._job = asyncio.create_task(self._start(restart))

    async def wait_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        if self._job is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._job), timeout)
        return self.state == "ready"

    async def _start(self, restart: bool) -> None:
        async with self._lock:
            if restart:
                await self._stop_proc()
            port = find_free_port(self.preferred_port)
            cmd = detect_preview(self.project_dir, port)
            if cmd is None:
                return
            self.kind, self.state, self.error, self.url = cmd.kind, "starting", None, None
            self._emit()
            env = command_env(self.project_dir, cmd.env)
            env.pop("CI", None)  # algunos dev servers cambian de comportamiento con CI=1
            try:
                if cmd.needs_install:
                    self._log("npm install (vista previa)…")
                    res = await self.pm.run(["npm", "install", "--no-audit", "--no-fund"],
                                            name="npm install", cwd=cmd.cwd, env=env,
                                            timeout=600, on_line=self._log)  # fmt: skip
                    if not res.ok:
                        raise RuntimeError(f"npm install falló:\n{res.output[-1500:]}")
                self._proc = await self.pm.spawn(cmd.args, name=f"preview:{cmd.kind}",
                                                 cwd=cmd.cwd, env=env)  # fmt: skip
                pump = asyncio.create_task(self._pump(self._proc))
                url = f"http://127.0.0.1:{port}/"
                if await self._wait_http(url, self._proc):
                    self.state, self.url = "ready", url
                    self._log(f"vista previa lista en {url}")
                else:
                    pump.cancel()
                    raise RuntimeError("el servidor de desarrollo no respondió a tiempo")
            except (RuntimeError, OSError) as exc:
                self.state, self.error = "failed", str(exc)
                self._log(f"⚠ vista previa: {exc}")
                await self._stop_proc()
            self._emit()

    async def _pump(self, proc: ManagedProcess) -> None:
        async for line in proc.lines():
            if line.strip():
                self._log(line)
        if not self._closed and self.state == "ready" and proc is self._proc:
            self.state, self.error = "failed", f"el servidor terminó (código {proc.returncode})"
            self._emit()

    async def _wait_http(self, url: str, proc: ManagedProcess) -> bool:
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                if proc.returncode is not None:
                    return False
                try:
                    r = await client.get(url)
                    if r.status_code < 500:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)
        return False

    async def _stop_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            await self.pm.terminate(proc, grace=3)

    async def stop(self) -> None:
        self._closed = True
        if self._job is not None and not self._job.done():
            self._job.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._job
        await self._stop_proc()
        if self.state != "off":
            self.state = "stopped"
