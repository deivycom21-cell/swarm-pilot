"""Verificación de tareas: chequeos integrados + comandos del plan.

Orden:
1. Chequeos integrados sobre los archivos cambiados (en proceso, sin subprocesos):
   - la tarea cambió algo (si no tiene comandos propios);
   - sintaxis de `.py`; JSON válido en `.json`;
   - los assets locales referenciados por `.html` existen (o los creará otra tarea).
2. `npm install` si hay `package.json` y falta `node_modules` (o cambió `package.json`).
3. Los comandos `verificar` de la tarea, en orden; se para en el primer fallo.

El informe de fallo (`report()`) es lo que se devuelve al agente para que se repare.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from swarm_pilot.core.environment import command_env
from swarm_pilot.core.models import SATISFIED, Plan, Task, paths_overlap
from swarm_pilot.core.process_manager import ProcessManager
from swarm_pilot.core.workspace import WorkspaceError, safe_path

LogFn = Callable[[str], None]
JSONC_FILES = re.compile(r"(^|/)(tsconfig[^/]*|jsconfig[^/]*|\.vscode/.*|.*\.jsonc)$")
NPM_HINTS = ("npm", "npx", "node ", "vite", "yarn", "pnpm", "tsc", "next ", "astro", "eslint")


@dataclass
class CheckResult:
    name: str
    ok: bool
    output: str = ""
    command: str | None = None
    duration: float = 0.0


@dataclass
class VerifyResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def report(self, max_chars: int = 4000) -> str:
        parts = []
        for c in self.checks:
            if c.ok:
                continue
            head = f"✗ {c.name}" + (f": `{c.command}`" if c.command else "")
            parts.append(f"{head}\n{c.output.strip()}")
        text = "\n\n".join(parts)
        if len(text) > max_chars:  # la cola suele tener el error relevante
            text = "…" + text[-max_chars:]
        return text

    def summary(self) -> list[dict[str, object]]:
        return [
            {"name": c.name, "ok": c.ok, "command": c.command, "duration": round(c.duration, 2)}
            for c in self.checks
        ]


class _AssetCollector(HTMLParser):
    ATTRS = {"script": "src", "link": "href", "img": "src", "source": "src", "video": "src",
             "audio": "src"}  # fmt: skip

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted = self.ATTRS.get(tag)
        if not wanted:
            return
        values = dict(attrs)
        if tag == "link" and (values.get("rel") or "").lower() in ("preconnect", "dns-prefetch"):
            return
        value = values.get(wanted)
        if value:
            self.refs.append(value.strip())


def _is_local_ref(ref: str) -> bool:
    low = ref.lower()
    return not (
        not ref
        or low.startswith(("http:", "https:", "//", "data:", "mailto:", "tel:", "javascript:", "#"))
        or "{{" in ref
        or "${" in ref
        or "<%" in ref
    )


def check_python(path: Path) -> str | None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:
        return f"{exc.filename}:{exc.lineno}: {exc.msg}\n{(exc.text or '').rstrip()}"
    except UnicodeDecodeError as exc:
        return f"{path}: no es UTF-8 válido ({exc})"
    return None


def check_json(path: Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return f"{path.name}: JSON inválido: {exc}"
    return None


def missing_html_assets(project_dir: Path, html_rel: str, pending_files: list[str]) -> list[str]:
    html_path = safe_path(project_dir, html_rel)
    parser = _AssetCollector()
    parser.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    missing = []
    base_dir = html_path.parent
    for ref in parser.refs:
        if not _is_local_ref(ref):
            continue
        clean = re.split(r"[?#]", ref, maxsplit=1)[0]
        if not clean:
            continue
        if clean.startswith("/"):
            candidates = [
                project_dir / clean.lstrip("/"),
                project_dir / "public" / clean.lstrip("/"),
            ]
        else:
            candidates = [base_dir / clean]
        if any(c.exists() for c in candidates):
            continue
        rels = []
        for c in candidates:
            try:
                rels.append(c.resolve().relative_to(project_dir.resolve()).as_posix())
            except ValueError:
                continue
        # Otra tarea aún pendiente lo creará: no es un error todavía.
        if any(paths_overlap(r, p) for r in rels for p in pending_files):
            continue
        missing.append(ref)
    return missing


class Verifier:
    def __init__(
        self,
        project_dir: Path,
        pm: ProcessManager,
        *,
        timeout: float = 300,
        on_log: LogFn | None = None,
        install_lock: asyncio.Lock | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.pm = pm
        self.timeout = timeout
        self.on_log = on_log or (lambda _l: None)
        #: Compartido entre verificaciones en paralelo: un solo `npm install` a la vez.
        self.install_lock = install_lock or asyncio.Lock()

    def builtin_checks(self, task: Task, changed: list[str], plan: Plan) -> list[CheckResult]:
        checks: list[CheckResult] = []
        # Archivos declarados que ya existen: un error que persiste sin cambios se sigue viendo.
        declared = [f for f in task.files if not any(ch in f for ch in "*?[")]
        existing = [f for f in declared if (self.project_dir / f).is_file()]
        if not changed and not task.verify and not existing:
            checks.append(CheckResult(
                "cambios", False,
                "La tarea no creó ni modificó ningún archivo. Implementa la tarea editando los "
                "archivos del proyecto.",
            ))  # fmt: skip
            return checks
        pending = [
            f for t in plan.tasks if t.id != task.id and t.status not in SATISFIED for f in t.files
        ]
        errors: list[str] = []
        for rel in dict.fromkeys([*changed, *existing]):
            try:
                path = safe_path(self.project_dir, rel)
            except WorkspaceError:
                continue
            if not path.is_file():
                continue
            low = rel.lower()
            if low.endswith(".py"):
                err = check_python(path)
            elif low.endswith(".json") and not JSONC_FILES.search(low):
                err = check_json(path)
            elif low.endswith((".html", ".htm")):
                missing = missing_html_assets(self.project_dir, rel, pending)
                err = (
                    f"{rel} referencia archivos que no existen: {', '.join(missing)}"
                    if missing
                    else None
                )
            else:
                err = None
            if err:
                errors.append(err)
        checks.append(CheckResult("chequeos integrados", not errors, "\n".join(errors)))
        return checks

    def needs_npm_install(self, task: Task, changed: list[str]) -> bool:
        if not (self.project_dir / "package.json").is_file():
            return False
        if "package.json" in changed:
            return True
        if (self.project_dir / "node_modules").is_dir():
            return False
        return any(h in f"{c} " for c in task.verify for h in NPM_HINTS)

    async def run_command(self, name: str, command: str) -> CheckResult:
        self.on_log(f"$ {command}")
        res = await self.pm.run(
            command,
            name=name,
            cwd=self.project_dir,
            env=command_env(self.project_dir),
            timeout=self.timeout,
        )
        output = res.output
        if res.timed_out:
            output += f"\n(tiempo límite de {self.timeout:.0f}s superado; proceso terminado)"
        return CheckResult(name, res.ok, output, command, res.duration)

    async def verify(self, task: Task, changed: list[str], plan: Plan) -> VerifyResult:
        result = VerifyResult(self.builtin_checks(task, changed, plan))
        if not result.ok:
            return result
        async with self.install_lock:
            if self.needs_npm_install(task, changed):
                check = await self.run_command("npm install", "npm install --no-audit --no-fund")
                result.checks.append(check)
                if not check.ok:
                    return result
        for i, command in enumerate(task.verify, 1):
            start = time.monotonic()
            check = await self.run_command(f"verificar {i}", command)
            check.duration = time.monotonic() - start
            result.checks.append(check)
            mark = "✓" if check.ok else "✗"
            self.on_log(f"{mark} {command} ({check.duration:.1f}s)")
            if not check.ok:
                break
        return result
