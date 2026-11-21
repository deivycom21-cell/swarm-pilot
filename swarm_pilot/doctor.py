"""`swarm-pilot doctor`: diagnóstico de proveedores, herramientas, puertos y restos."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from swarm_pilot.adapters.base import AdapterContext, AdapterError
from swarm_pilot.adapters.registry import create_adapter
from swarm_pilot.config import (
    DEFAULT_OLLAMA_HOST,
    PROJECT_CONFIG,
    ROLES,
    SECRET_ENV_VARS,
    ConfigError,
    load_settings,
    probe_ollama,
    user_config_path,
)
from swarm_pilot.core.environment import detect_environment
from swarm_pilot.core.preview import port_is_free
from swarm_pilot.core.process_manager import ProcessManager, pid_alive

OK, WARN, BAD = "[green]✓[/]", "[yellow]•[/]", "[red]✗[/]"


async def run_doctor(project_dir: Path, cleanup: bool, console: Console) -> int:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(width=2)
    table.add_column(style="bold")
    table.add_column()
    problems = 0

    def row(mark: str, name: str, detail: str) -> None:
        nonlocal problems
        if mark == BAD:
            problems += 1
        table.add_row(mark, name, detail)

    py = sys.version_info
    row(OK if py >= (3, 10) else BAD, "Python", f"{py.major}.{py.minor}.{py.micro}")

    # --- configuración y proveedores ---
    settings = None
    try:
        settings = load_settings(project_dir)
        cfg = [p for p in (user_config_path(), project_dir / PROJECT_CONFIG) if p.is_file()]
        row(OK, "Configuración", ", ".join(str(p) for p in cfg) or "por defecto (sin swarm.toml)")
    except ConfigError as exc:
        row(BAD, "Configuración", escape(str(exc)))

    pm = ProcessManager(grace=1)
    try:
        if settings is not None:
            checked: dict[str, str] = {}
            for role in ROLES:
                spec = settings.providers.get(role)
                if spec is None:
                    row(BAD, f"Rol {role}", "sin proveedor (ver abajo cómo configurar uno)")
                    continue
                key = str(spec)
                if key not in checked:
                    adapter = create_adapter(
                        spec, AdapterContext(process_manager=pm, env=os.environ)
                    )
                    try:
                        checked[key] = OK + " " + escape(await adapter.check())
                    except (AdapterError, ConfigError) as exc:
                        checked[key] = BAD + " " + escape(str(exc))
                    except Exception as exc:  # dependencias opcionales ausentes, etc.
                        checked[key] = BAD + " " + escape(f"{type(exc).__name__}: {exc}")
                mark, _, detail = checked[key].partition(" ")
                source = settings.provider_sources.get(role, "?")
                row(mark, f"Rol {role}", f"{escape(key)} [dim]({source})[/] — {detail}")

        for var in SECRET_ENV_VARS:
            row(OK if os.environ.get(var) else WARN, var,
                "definida" if os.environ.get(var) else "[dim]no definida[/]")  # fmt: skip
        host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        up = probe_ollama(host)
        row(OK if up else WARN, "Ollama", f"{host} {'responde' if up else '[dim]no responde[/]'}")

        # --- herramientas y extras ---
        env = detect_environment(project_dir)
        for tool in ("claude", "node", "npm", "git"):
            import shutil

            path = shutil.which(tool)
            row(OK if path else WARN, tool, escape(path) if path else "[dim]no encontrado[/]")
        for module, extra in (("anthropic", "anthropic"), ("mcp", "mcp")):
            found = importlib.util.find_spec(module) is not None
            hint = escape(f"pip install 'swarm-pilot[{extra}]'")
            row(OK if found else WARN, escape(f"extra [{extra}]"),
                "instalado" if found else f"[dim]{hint}[/]")  # fmt: skip
        row(OK, "Entorno", escape(env.describe().splitlines()[0]))

        # --- puertos ---
        ports = (settings.dashboard_port, settings.preview_port) if settings else (4000, 3000)
        for port in ports:
            free = port_is_free(port)
            row(OK if free else WARN, f"Puerto {port}",
                "libre" if free else "ocupado (se usará el siguiente libre)")  # fmt: skip

        # --- restos de runs anteriores ---
        pid_file = project_dir / ".swarm" / "pids.json"
        if pid_file.exists():
            if cleanup:
                killed = ProcessManager.cleanup_stale(pid_file)
                row(OK, "Procesos huérfanos", f"limpiados: {', '.join(killed) or 'ninguno vivo'}")
            else:
                import json

                entries = json.loads(pid_file.read_text(encoding="utf-8")).get("processes", [])
                alive = [e for e in entries if pid_alive(int(e["pid"]))]
                row(WARN if alive else OK, "Procesos huérfanos",
                    f"{len(alive)} vivos; usa --cleanup" if alive else "ninguno")  # fmt: skip
        else:
            row(OK, "Procesos huérfanos", "ninguno")
    finally:
        await pm.shutdown()

    console.print(table)
    if settings is not None and any(settings.providers.get(r) is None for r in ROLES):
        console.print(
            "\n[bold]Configura un proveedor[/] con cualquiera de estas opciones:\n"
            "  • Instala Claude Code (usa tu suscripción): https://claude.com/claude-code\n"
            "  • export ANTHROPIC_API_KEY=… · OPENAI_API_KEY=… · OPENROUTER_API_KEY=…\n"
            "  • Arranca Ollama: ollama serve\n"
            "  • O define [providers] en swarm.toml (`swarm-pilot init`)"
        )
    return 1 if problems else 0
