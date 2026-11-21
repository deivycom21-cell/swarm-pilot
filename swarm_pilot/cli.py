"""CLI de swarm-pilot.

swarm-pilot run "Crea una landing page moderna"   planifica, pide aprobación y ejecuta
swarm-pilot plan "Arma una API en FastAPI"        solo genera PLAN.md
swarm-pilot resume                                retoma el plan de .swarm/state.json
swarm-pilot status | doctor [--cleanup] | init | mcp
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from swarm_pilot import __version__
from swarm_pilot.config import ConfigError, Settings, load_settings
from swarm_pilot.core import plan_io
from swarm_pilot.core.models import Event, EventType, Plan, TaskStatus
from swarm_pilot.core.planner import PlannerError
from swarm_pilot.core.process_manager import IS_WINDOWS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Orquestador local de sub-agentes de IA: planifica, ejecuta, verifica y se auto-repara.",
)

# En Windows, si la salida va a un pipe/archivo, Python usa la codificación ANSI (cp1252):
# forzamos UTF-8 para que acentos e iconos lleguen bien. En una consola real no hace falta.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError, OSError):
        if _stream.isatty():
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        else:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

EXIT_OK, EXIT_ERROR, EXIT_PAUSED, EXIT_INTERRUPTED = 0, 1, 2, 130

STATUS_STYLE = {
    TaskStatus.PENDING: ("○", "dim"),
    TaskStatus.RUNNING: ("◐", "cyan"),
    TaskStatus.VERIFYING: ("◑", "cyan"),
    TaskStatus.DONE: ("●", "green"),
    TaskStatus.BLOCKED: ("✕", "red"),
    TaskStatus.SKIPPED: ("–", "dim"),
}

SWARM_TOML_TEMPLATE = """\
# Configuración de swarm-pilot para este proyecto.
# Formato de proveedor: "proveedor[:modelo]" — claude-cli, anthropic, openai, openrouter, ollama.

[providers]
# planner = "claude-cli"              # el modelo potente planifica
# worker  = "ollama:qwen2.5-coder"    # todos los roles de ejecución...
# coder   = "openrouter:<modelo>"     # ...o uno por rol: architect / coder / tester

[run]
# max_parallel = 2       # tareas en paralelo
# max_attempts = 3       # intentos de auto-reparación por tarea
# require_approval = true
# agent_timeout = 1800   # segundos por llamada a un agente
# verify_timeout = 300   # segundos por comando de verificación

[ui]
# dashboard_port = 4000
# preview_port = 3000

[claude]
# skip_permissions = false   # true = --dangerously-skip-permissions (solo en sandboxes)
"""


# --- utilidades ---------------------------------------------------------------


def _settings(project_dir: Path, **overrides: Any) -> Settings:
    try:
        return load_settings(project_dir, overrides=overrides)
    except ConfigError as exc:
        err.print(f"[red]Error de configuración:[/] {escape(str(exc))}")
        raise typer.Exit(EXIT_ERROR) from exc


def plan_table(plan: Plan, title: str | None = None) -> Table:
    table = Table(title=title, title_justify="left", show_lines=False, expand=False, pad_edge=False)
    table.add_column("", width=1)
    table.add_column("ID", style="bold")
    table.add_column("Rol", style="magenta")
    table.add_column("Tarea")
    table.add_column("Depende", style="dim")
    table.add_column("Archivos", style="dim", overflow="fold")
    for t in plan.tasks:
        icon, style = STATUS_STYLE[t.status]
        attempts = f" [dim]({t.attempts_used} int.)[/]" if t.attempts_used else ""
        table.add_row(
            f"[{style}]{icon}[/]", t.id, t.role.value, escape(t.title) + attempts,
            ", ".join(t.depends) or "—", escape(", ".join(t.files)) or "—",
        )  # fmt: skip
    return table


def print_commands(plan: Plan) -> None:
    commands = [(t.id, c) for t in plan.tasks for c in t.verify]
    console.print("\n[bold]Comandos que se ejecutarán para verificar:[/]")
    if not commands:
        console.print("  [dim](ninguno; solo chequeos integrados)[/]")
    for tid, cmd in commands:
        console.print(f"  [dim]{tid}[/]  $ {escape(cmd)}")
    console.print("  [dim]+ `npm install` automático si hay package.json y faltan dependencias[/]")


def stdin_is_terminal() -> bool:
    """¿Hay una persona al teclado? En Windows `NUL` dice ser TTY: se confirma con la consola."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        if not stream.isatty():
            return False
        fileno = stream.fileno()
    except (AttributeError, ValueError, OSError):
        return False
    if not IS_WINDOWS:
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(fileno)
    except OSError:
        return False
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined]


class StdinReader:
    """Lee stdin en un hilo daemon (input() no se puede cancelar y bloquearía la salida)."""

    def __init__(self) -> None:
        self.interactive = stdin_is_terminal()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        for line in sys.stdin:
            self._lines.put(line.rstrip("\r\n"))
        self._lines.put(None)

    async def ask(self, prompt: str) -> str | None:
        if not self.interactive:
            return None
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="stdin")
            self._thread.start()
        while not self._lines.empty():  # descarta respuestas viejas
            self._lines.get_nowait()
        console.print(prompt, end="")
        while True:
            try:
                return self._lines.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)


class EventPrinter:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def __call__(self, ev: Event) -> None:
        p = ev.payload
        tid = ev.task_id or ""
        if ev.type is EventType.PHASE and p.get("phase") == "planning":
            console.print("[bold cyan]🧠 Planificando…[/]")
        elif ev.type is EventType.TASK_STATUS:
            status = TaskStatus(p["status"])
            icon, style = STATUS_STYLE[status]
            extra = (
                f" [dim](intento {p['attempt']}, {p.get('agent', '')})[/]"
                if status is TaskStatus.RUNNING
                else ""
            )
            if status is TaskStatus.VERIFYING:
                if not self.quiet:
                    console.print(f"  [dim]{tid} │ verificando…[/]")
                return
            if status is TaskStatus.DONE and p.get("notes"):
                extra = f" [dim]— {escape(str(p['notes'])[:160])}[/]"
            console.print(f"[{style}]{icon} {tid}[/] {escape(p.get('title', ''))}{extra}")
        elif ev.type is EventType.AGENT_LOG and not self.quiet:
            source = tid or p.get("source", "")
            line = str(p.get("line", ""))
            width = max(40, console.width - len(source) - 6)
            console.print(f"  [dim]{escape(source)} │ {escape(line[:width])}[/]")
        elif ev.type is EventType.ATTEMPT_FAILED:
            remaining = p.get("remaining", 0)
            what = f"reintentando (quedan {remaining})" if remaining else "sin intentos restantes"
            console.print(f"[yellow]↻ {tid} intento {p.get('attempt')} falló — {what}[/]")
            for line in str(p.get("error", "")).splitlines()[:8]:
                console.print(f"  [yellow dim]{escape(line[: console.width - 4])}[/]")
        elif ev.type is EventType.PREVIEW_STATUS:
            if p.get("state") == "ready" and not p.get("reload"):
                console.print(f"[green]🌐 Vista previa en vivo:[/] {p.get('url')}")
            elif p.get("state") == "failed":
                console.print(f"[yellow]⚠ La vista previa falló:[/] {escape(str(p.get('error')))}")


async def _print_events(session: Any, printer: EventPrinter) -> None:
    async for ev in session.bus.subscribe(maxsize=10_000):
        printer(ev)


async def _help_loop(session: Any, stdin: StdinReader) -> None:
    """Pregunta en la terminal qué hacer con cada tarea bloqueada."""
    async for ev in session.bus.subscribe(maxsize=10_000):
        if ev.type is not EventType.NEEDS_HELP or not stdin.interactive:
            continue
        task_id = ev.task_id
        p = ev.payload
        console.rule(f"[red]{task_id} necesita ayuda[/]")
        if p.get("rate_limited"):
            console.print("[yellow]Se alcanzó el límite de uso del proveedor.[/]")
        console.print(escape(str(p.get("error", ""))[-2500:]), style="red dim")
        console.print(
            "[bold]Opciones:[/] [cyan]r[/] reintentar (puedes añadir una pista: "
            "[dim]r usa flexbox[/]) · [cyan]m[/] lo arreglé a mano · [cyan]s[/] saltar · "
            "[cyan]a[/] abortar  [dim](o responde en el panel)[/]"
        )
        while True:
            task = session.plan.get(task_id) if session.plan else None
            if task is None or task.status is not TaskStatus.BLOCKED:
                break  # ya se respondió desde el panel
            answer = await stdin.ask(f"{task_id} › ")
            if answer is None:
                break
            cmd, _, hint = answer.strip().partition(" ")
            action = {"r": "retry", "m": "manual", "s": "skip", "a": "abort"}.get(cmd.lower()[:1])
            if action is None:
                console.print("[dim]Escribe r, m, s o a.[/]")
                continue
            problem = session.answer(task_id, action, hint or None)
            if problem:
                console.print(f"[dim]{escape(problem)}[/]")
            break


async def _approval(session: Any, stdin: StdinReader, yes: bool) -> bool:
    plan = session.plan
    console.print(plan_table(plan, title=f"Plan: {plan.goal}"))
    print_commands(plan)
    if yes or not session.settings.require_approval:
        return True
    if not stdin.interactive and not session.dashboard_url:
        err.print("[red]Hace falta aprobar el plan:[/] usa --yes en modo no interactivo.")
        return False
    where = f" o apruébalo en {session.dashboard_url}" if session.dashboard_url else ""
    console.print(f"\nPuedes editar [bold]PLAN.md[/] antes de aprobar{where}.")

    async def from_terminal() -> None:
        answer = await stdin.ask("¿Ejecutar este plan? [S/n] ")
        if answer is not None:
            session.approve(answer.strip().lower() in ("", "s", "si", "sí", "y", "yes"))

    asker = asyncio.create_task(from_terminal())
    try:
        approved = await session.wait_approval()
    finally:
        asker.cancel()
    if approved:  # incorpora ediciones manuales de PLAN.md hechas mientras se revisaba
        edited = plan_io.load(session.project_dir)
        if edited is not None and edited.plan != plan:
            session.plan = edited.plan
            console.print("[cyan]Se usará el PLAN.md editado.[/]")
    return approved


def _install_signals(session: Any, main_task: asyncio.Task[Any]) -> None:
    state = {"count": 0}

    def on_signal() -> None:
        state["count"] += 1
        if state["count"] == 1:
            console.print("\n[yellow]Deteniendo… cerrando agentes, panel y vista previa.[/]")
            main_task.cancel()
        else:  # segundo Ctrl+C: salida inmediata, matando lo que quede
            session.pm.shutdown_sync()
            os._exit(EXIT_INTERRUPTED)

    session.pm.install_signal_handlers(on_signal)


async def _run_session(
    settings: Settings,
    goal: str | None,
    *,
    yes: bool,
    ui: bool,
    preview: bool,
    quiet: bool,
    keep_alive: bool,
) -> int:
    from swarm_pilot.runtime import Outcome, Session

    stdin = StdinReader()
    session = Session(settings, ui=ui, preview=preview,
                      on_blocked="wait" if (stdin.interactive or ui) else "stop")  # fmt: skip
    _install_signals(session, asyncio.current_task())  # type: ignore[arg-type]
    printer_task = asyncio.create_task(_print_events(session, EventPrinter(quiet)))
    help_task = asyncio.create_task(_help_loop(session, stdin))
    code = EXIT_OK
    try:
        await session.start_services()
        if session.stale_killed:
            console.print(
                f"[dim]Limpiados procesos de un run anterior: {', '.join(session.stale_killed)}[/]"
            )
        if session.dashboard_url:
            console.print(f"[bold]🛰  Centro de mando:[/] {session.dashboard_url}")

        if goal:
            try:
                await session.make_plan(goal)
            except PlannerError as exc:
                err.print(f"[red]No se pudo planificar:[/] {escape(str(exc))}")
                return EXIT_ERROR
        elif session.load_existing() is None:
            err.print('[red]No hay plan.[/] Pasa un objetivo: swarm-pilot run "..."')
            return EXIT_ERROR
        else:
            console.print("[cyan]Retomando el plan existente.[/]")
            if session.plan.is_finished:
                console.print(plan_table(session.plan))
                console.print("[green]El plan ya está completo.[/]")
                return EXIT_OK

        if not await _approval(session, stdin, yes):
            console.print(
                "Plan no aprobado. Está guardado en PLAN.md; ejecútalo con `swarm-pilot resume`."
            )
            return EXIT_OK

        summary = await session.execute()
        console.print()
        console.print(plan_table(session.plan, title="Resultado"))
        c = summary.counts
        cost = f" · coste eq. API ${summary.cost_usd:.3f}" if summary.cost_usd else ""
        line = f"{c['done']} hechas · {c['blocked']} bloqueadas · {c['skipped']} saltadas · {summary.duration:.0f}s{cost}"
        if summary.outcome is Outcome.COMPLETED:
            console.print(f"[bold green]✓ Proyecto terminado[/] — {line}")
        elif summary.outcome is Outcome.PAUSED:
            console.print(f"[bold yellow]⏸ En pausa[/] — {line}. Retoma con `swarm-pilot resume`.")
            code = EXIT_PAUSED
        else:
            console.print(f"[bold red]■ Abortado[/] — {line}")
            code = EXIT_ERROR

        has_preview = session.preview is not None and session.preview.state == "ready"
        if keep_alive and (session.dashboard_url or has_preview):
            console.print("[dim]Panel y vista previa siguen activos. Pulsa Ctrl+C para salir.[/]")
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        code = EXIT_INTERRUPTED
    finally:
        for task in (printer_task, help_task):
            task.cancel()
        await session.close()
        if code == EXIT_INTERRUPTED:
            console.print("[dim]Todo cerrado. Retoma con `swarm-pilot resume`.[/]")
    return code


def _run_async(coro: Any) -> int:
    try:
        return asyncio.run(coro)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return EXIT_INTERRUPTED


# --- comandos -----------------------------------------------------------------

DirOpt = Annotated[Path, typer.Option("--dir", "-d", help="Directorio del proyecto.",
                                      file_okay=False, resolve_path=True)]  # fmt: skip


def _version(value: bool) -> None:
    if value:
        console.print(f"swarm-pilot {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Muestra la versión."),
    ] = False,  # fmt: skip
) -> None:
    pass


@app.command()
def run(
    goal: Annotated[
        str | None, typer.Argument(help="Qué construir. Sin objetivo, retoma el plan.")
    ] = None,
    project_dir: DirOpt = Path("."),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No pedir aprobación del plan.")] = False,
    no_ui: Annotated[bool, typer.Option("--no-ui", help="Sin centro de mando web.")] = False,
    no_preview: Annotated[bool, typer.Option("--no-preview", help="Sin vista previa.")] = False,
    planner: Annotated[
        str | None, typer.Option(help="Proveedor del planner, p. ej. claude-cli.")
    ] = None,
    worker: Annotated[str | None, typer.Option(help="Proveedor de todos los workers.")] = None,
    max_parallel: Annotated[int | None, typer.Option(help="Tareas en paralelo.")] = None,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Oculta los logs de los agentes.")
    ] = False,
    exit_when_done: Annotated[
        bool, typer.Option("--exit", help="Salir al terminar (no mantener panel/preview).")
    ] = False,
) -> None:
    """Planifica (si das un objetivo), pide aprobación y ejecuta con auto-reparación."""
    project_dir.mkdir(parents=True, exist_ok=True)
    overrides: dict[str, Any] = {"max_parallel": max_parallel, "provider.planner": planner}
    if worker:
        overrides.update({f"provider.{r}": worker for r in ("architect", "coder", "tester")})
    settings = _settings(project_dir, **overrides)
    code = _run_async(_run_session(
        settings, goal, yes=yes, ui=not no_ui, preview=not no_preview, quiet=quiet,
        keep_alive=not exit_when_done,
    ))  # fmt: skip
    raise typer.Exit(code)


@app.command()
def resume(
    project_dir: DirOpt = Path("."),
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    no_ui: Annotated[bool, typer.Option("--no-ui")] = False,
    no_preview: Annotated[bool, typer.Option("--no-preview")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    exit_when_done: Annotated[bool, typer.Option("--exit")] = False,
) -> None:
    """Retoma el plan guardado en .swarm/state.json (incluye ediciones de PLAN.md)."""
    settings = _settings(project_dir)
    code = _run_async(_run_session(
        settings, None, yes=yes, ui=not no_ui, preview=not no_preview, quiet=quiet,
        keep_alive=not exit_when_done,
    ))  # fmt: skip
    raise typer.Exit(code)


@app.command()
def plan(
    goal: Annotated[str, typer.Argument(help="Qué construir.")],
    project_dir: DirOpt = Path("."),
    planner: Annotated[str | None, typer.Option(help="Proveedor del planner.")] = None,
) -> None:
    """Genera PLAN.md sin ejecutar nada."""
    project_dir.mkdir(parents=True, exist_ok=True)
    settings = _settings(project_dir, **{"provider.planner": planner})

    async def go() -> int:
        from swarm_pilot.runtime import Session

        session = Session(settings, ui=False, preview=False)
        printer = asyncio.create_task(_print_events(session, EventPrinter()))
        try:
            plan_ = await session.make_plan(goal)
        except PlannerError as exc:
            err.print(f"[red]No se pudo planificar:[/] {escape(str(exc))}")
            return EXIT_ERROR
        finally:
            printer.cancel()
            await session.close()
        console.print(plan_table(plan_, title=f"Plan: {plan_.goal}"))
        print_commands(plan_)
        console.print(f"\nGuardado en [bold]{project_dir / plan_io.PLAN_FILE}[/]. "
                      "Edítalo si quieres y ejecútalo con [bold]swarm-pilot resume[/].")  # fmt: skip
        return EXIT_OK

    raise typer.Exit(_run_async(go()))


@app.command()
def status(project_dir: DirOpt = Path(".")) -> None:
    """Muestra el estado del plan actual."""
    try:
        state = plan_io.load(project_dir)
    except plan_io.PlanParseError as exc:
        err.print(f"[red]PLAN.md no es válido:[/] {escape(str(exc))}")
        raise typer.Exit(EXIT_ERROR) from exc
    if state is None:
        console.print('No hay plan en este directorio. Crea uno con `swarm-pilot plan "..."`.')
        raise typer.Exit(EXIT_OK)
    console.print(plan_table(state.plan, title=f"Plan: {state.plan.goal}"))
    c = state.plan.counts()
    console.print(f"{c['done']} hechas · {c['pending']} pendientes · {c['blocked']} bloqueadas · "
                  f"{c['skipped']} saltadas")  # fmt: skip
    for t in state.plan.tasks:
        if t.status is TaskStatus.BLOCKED and t.last_attempt:
            console.print(f"\n[red]{t.id} bloqueada:[/]")
            console.print(escape(t.last_attempt.error[-1500:]), style="dim")


@app.command()
def doctor(
    project_dir: DirOpt = Path("."),
    cleanup: Annotated[
        bool, typer.Option("--cleanup", help="Mata procesos huérfanos de runs anteriores.")
    ] = False,
) -> None:
    """Comprueba proveedores, herramientas y puertos."""
    from swarm_pilot.doctor import run_doctor

    raise typer.Exit(_run_async(run_doctor(project_dir, cleanup, console)))


@app.command()
def init(project_dir: DirOpt = Path(".")) -> None:
    """Crea un swarm.toml comentado en el proyecto."""
    target = project_dir / "swarm.toml"
    if target.exists():
        console.print(f"Ya existe {target}.")
        raise typer.Exit(EXIT_OK)
    project_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(SWARM_TOML_TEMPLATE, encoding="utf-8")
    console.print(f"Creado {target}. Ejecuta `swarm-pilot doctor` para comprobar los proveedores.")


@app.command()
def mcp() -> None:
    """Arranca el servidor MCP (stdio) para Claude Code, Cursor y otros clientes."""
    try:
        from swarm_pilot.mcp_server import main as mcp_main
    except ImportError as exc:
        err.print("Falta el extra MCP: pip install 'swarm-pilot[mcp]'")
        raise typer.Exit(EXIT_ERROR) from exc
    mcp_main()


if __name__ == "__main__":  # pragma: no cover
    app()


__all__ = ["app", "IS_WINDOWS"]
