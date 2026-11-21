"""Servidor MCP (stdio): expone swarm-pilot como herramientas para Claude Code, Cursor, etc.

    claude mcp add swarm-pilot -- swarm-pilot mcp
    # Cursor (.cursor/mcp.json): {"mcpServers": {"swarm-pilot": {"command": "swarm-pilot", "args": ["mcp"]}}}

Herramientas:
- `swarm_plan(goal, path)`   genera PLAN.md y lo devuelve (para revisarlo con el usuario).
- `swarm_run(path, goal?)`   ejecuta en segundo plano; devuelve `run_id` y la URL del panel.
- `swarm_status(run_id?)`    progreso, tareas bloqueadas con su error, vista previa, logs.
- `swarm_answer(...)`        responde a una tarea bloqueada: retry [+hint] / manual / skip / abort.
- `swarm_stop(run_id)`       detiene un run y cierra sus procesos.

Las ejecuciones son asíncronas (evitan los timeouts de las llamadas MCP): se lanzan con
`swarm_run` y se consultan con `swarm_status`. Nada se escribe en stdout salvo el
protocolo MCP. Al cerrarse el cliente, se apagan todos los runs y sus subprocesos.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from swarm_pilot.config import ConfigError, load_settings
from swarm_pilot.core import plan_io
from swarm_pilot.core.models import EventType, TaskStatus
from swarm_pilot.core.planner import PlannerError
from swarm_pilot.runtime import Session

INSTRUCTIONS = """\
swarm-pilot orchestrates AI sub-agents that plan, build, verify and self-repair a project.
Recommended flow: call swarm_plan, show the plan to the user and get their approval, then
call swarm_run and poll swarm_status every 20-60 seconds. When a task is blocked, explain
its error to the user and call swarm_answer with their decision. Share the dashboard URL.
"""


@dataclass
class RunHandle:
    session: Session
    task: asyncio.Task[Any]
    path: Path


RUNS: dict[str, RunHandle] = {}


@contextlib.asynccontextmanager
async def lifespan(_server: Any) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await stop_all()


server = _Server("swarm-pilot", instructions=INSTRUCTIONS, lifespan=lifespan)


def _resolve(path: str) -> Path:
    project = Path(path).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    return project


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _task_rows(session: Session) -> list[dict[str, Any]]:
    if session.plan is None:
        return []
    return [
        {"id": t.id, "title": t.title, "role": t.role.value, "status": t.status.value,
         "attempts": t.attempts_used, "depends": t.depends}
        for t in session.plan.tasks
    ]  # fmt: skip


@server.tool()
async def swarm_plan(goal: str, path: str = ".") -> dict[str, Any]:
    """Create PLAN.md for `goal` in the project directory `path` without executing anything.
    Show the returned plan to the user before calling swarm_run."""
    try:
        settings = load_settings(_resolve(path))
    except ConfigError as exc:
        return _error(str(exc))
    session = Session(settings, ui=False, preview=False)
    try:
        plan = await session.make_plan(goal)
    except (PlannerError, ConfigError) as exc:
        return _error(str(exc))
    finally:
        await session.close()
    return {"ok": True, "path": str(settings.project_dir), "plan_md": plan_io.render_plan_md(plan),
            "tasks": len(plan.tasks)}  # fmt: skip


@server.tool()
async def swarm_run(
    path: str = ".", goal: str | None = None, dashboard: bool = True, preview: bool = True
) -> dict[str, Any]:
    """Execute the plan in `path` in the background (plans `goal` first if given, otherwise
    resumes PLAN.md). Returns a run_id and the live dashboard URL; poll swarm_status."""
    project = _resolve(path)
    for run_id, handle in RUNS.items():
        if handle.path == project and not handle.task.done():
            return {"ok": True, "run_id": run_id, "already_running": True,
                    "dashboard_url": handle.session.dashboard_url}  # fmt: skip
    try:
        settings = load_settings(project)
    except ConfigError as exc:
        return _error(str(exc))
    session = Session(settings, ui=dashboard, preview=preview, on_blocked="wait")
    try:
        await session.start_services()
        if goal:
            await session.make_plan(goal)
        elif session.load_existing() is None:
            await session.close()
            return _error(
                "no hay PLAN.md en ese directorio: pasa `goal` o llama antes a swarm_plan"
            )
    except (PlannerError, ConfigError, RuntimeError) as exc:
        await session.close()
        return _error(str(exc))
    task = asyncio.create_task(session.execute())
    RUNS[session.run_id] = RunHandle(session, task, project)
    return {
        "ok": True,
        "run_id": session.run_id,
        "dashboard_url": session.dashboard_url,
        "tasks": _task_rows(session),
        "message": "Ejecución iniciada. Consulta el progreso con swarm_status.",
    }


@server.tool()
async def swarm_status(run_id: str | None = None) -> dict[str, Any]:
    """Progress of a run (the most recent one if run_id is omitted): phase, tasks, blocked
    tasks with their errors, preview URL and the latest log lines."""
    if not RUNS:
        return _error("no hay ejecuciones en este servidor")
    handle = RUNS.get(run_id) if run_id else list(RUNS.values())[-1]
    if handle is None:
        return _error(f"run_id desconocido: {run_id}")
    s = handle.session
    logs = [
        f"{e.task_id or e.payload.get('source', '')}: {e.payload.get('line', '')}"
        for e in s.bus.history()[-400:]
        if e.type is EventType.AGENT_LOG
    ][-20:]
    result: dict[str, Any] = {
        "ok": True,
        "run_id": s.run_id,
        "phase": s.phase,
        "running": not handle.task.done(),
        "tasks": _task_rows(s),
        "blocked": list(s.orchestrator.pending_help.values()) if s.orchestrator else [],
        "dashboard_url": s.dashboard_url,
        "preview": s.preview.info() if s.preview else None,
        "recent_logs": logs,
    }
    if handle.task.done():
        exc = handle.task.exception() if not handle.task.cancelled() else None
        if exc is not None:
            result["error"] = repr(exc)
        elif s.summary is not None:
            result["summary"] = {"outcome": s.summary.outcome.value, "counts": s.summary.counts,
                                 "duration": s.summary.duration, "cost_usd": s.summary.cost_usd}  # fmt: skip
    return result


@server.tool()
async def swarm_answer(
    run_id: str, task_id: str, action: str, hint: str | None = None
) -> dict[str, Any]:
    """Resolve a blocked task. action: "retry" (optionally with a `hint` for the agent),
    "manual" (the user fixed it; re-verify), "skip", or "abort" (stop the whole run)."""
    handle = RUNS.get(run_id)
    if handle is None:
        return _error(f"run_id desconocido: {run_id}")
    problem = handle.session.answer(task_id, action, hint)
    return _error(problem) if problem else {"ok": True}


@server.tool()
async def swarm_stop(run_id: str) -> dict[str, Any]:
    """Stop a run: cancels its agents and closes its dashboard, preview and processes."""
    handle = RUNS.pop(run_id, None)
    if handle is None:
        return _error(f"run_id desconocido: {run_id}")
    await _stop(handle)
    pending = [t.id for t in (handle.session.plan.tasks if handle.session.plan else [])
               if t.status is not TaskStatus.DONE]  # fmt: skip
    return {"ok": True, "stopped": run_id, "unfinished_tasks": pending}


async def _stop(handle: RunHandle) -> None:
    if not handle.task.done():
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handle.task
    await handle.session.close()


async def stop_all() -> None:
    handles = list(RUNS.values())
    RUNS.clear()
    await asyncio.gather(*(_stop(h) for h in handles), return_exceptions=True)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
