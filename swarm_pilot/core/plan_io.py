"""PLAN.md ⇄ .swarm/state.json.

`state.json` es la fuente de verdad (estado, intentos, run_id). `PLAN.md` es la vista
legible que se regenera en cada cambio; el usuario puede editarla antes de aprobar y se
vuelve a leer. El formato de cada tarea es:

    ### [ ] T1 · Título de la tarea
    - rol: coder
    - depende: T0
    - archivos: `src/app.py`, `tests/test_app.py`
    - verificar: `python -m pytest -q`
    - detalle: texto libre; puede seguir en líneas
      indentadas debajo.

Marcas de estado: `[ ]` pendiente · `[~]` en curso · `[x]` hecha · `[!]` bloqueada · `[-]` saltada.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from swarm_pilot.core.models import Plan, Task, TaskStatus, utcnow

PLAN_FILE = "PLAN.md"
STATE_DIR = ".swarm"
STATE_FILE = "state.json"

STATUS_MARK = {
    TaskStatus.PENDING: " ",
    TaskStatus.RUNNING: "~",
    TaskStatus.VERIFYING: "~",
    TaskStatus.DONE: "x",
    TaskStatus.BLOCKED: "!",
    TaskStatus.SKIPPED: "-",
}
MARK_STATUS = {
    " ": TaskStatus.PENDING,
    "~": TaskStatus.PENDING,  # "en curso" al releer = se retoma
    "x": TaskStatus.DONE,
    "X": TaskStatus.DONE,
    "!": TaskStatus.BLOCKED,
    "-": TaskStatus.SKIPPED,
}

FIELD_ALIASES = {
    "rol": "role", "role": "role",
    "depende": "depends", "depends": "depends", "dependencias": "depends",
    "archivos": "files", "files": "files",
    "verificar": "verify", "verify": "verify",
    "detalle": "detail", "detail": "detail",
    "intentos": "attempts", "attempts": "attempts",
}  # fmt: skip

_HEADER_RE = re.compile(r"^###\s+\[(.)\]\s+([A-Za-z][A-Za-z0-9_-]*)\s*(?:·|:|-|—)\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^-\s+([A-Za-zñáéíóú]+)\s*:\s*(.*)$")
_GOAL_RE = re.compile(r"^\*\*Objetivo:\*\*\s*(.+)$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


class PlanParseError(ValueError):
    pass


# --- render ----------------------------------------------------------------------


def _code_list(items: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in items) if items else "—"


def render_plan_md(plan: Plan) -> str:
    counts = plan.counts()
    done = counts["done"] + counts["skipped"]
    lines = [
        "# PLAN",
        "",
        f"**Objetivo:** {plan.goal}",
        "",
        f"> Generado por swarm-pilot · progreso {done}/{len(plan.tasks)} · "
        "puedes editar este archivo antes de aprobar el plan; se vuelve a leer.",
        "> Estados: `[ ]` pendiente · `[~]` en curso · `[x]` hecha · `[!]` bloqueada · "
        "`[-]` saltada",
        "",
        "## Tareas",
        "",
    ]
    for t in plan.tasks:
        lines.append(f"### [{STATUS_MARK[t.status]}] {t.id} · {t.title}")
        lines.append(f"- rol: {t.role.value}")
        lines.append(f"- depende: {', '.join(t.depends) if t.depends else '—'}")
        lines.append(f"- archivos: {_code_list(t.files)}")
        lines.append(f"- verificar: {_code_list(t.verify)}")
        if t.detail:
            detail_lines = t.detail.strip().splitlines()
            lines.append(f"- detalle: {detail_lines[0]}")
            lines.extend(f"  {d}" if d.strip() else "" for d in detail_lines[1:])
        if t.attempts:
            last = t.attempts[-1]
            state = "ok" if last.ok else ("falló" if last.ok is False else "en curso")
            lines.append(f"- intentos: {len(t.attempts)} (último: {state})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- parse -----------------------------------------------------------------------


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value in ("", "—", "-", "ninguno", "ninguna", "none"):
        return []
    ticks = _BACKTICK_RE.findall(value)
    if ticks:
        return [t.strip() for t in ticks if t.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_plan_md(text: str) -> Plan:
    goal: str | None = None
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_detail = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if goal is None and (m := _GOAL_RE.match(line.strip())):
            goal = m.group(1).strip()
            continue
        if m := _HEADER_RE.match(line):
            mark, task_id, title = m.groups()
            if mark not in MARK_STATUS:
                raise PlanParseError(f"marca de estado desconocida [{mark}] en {task_id}")
            current = {"id": task_id, "title": title, "status": MARK_STATUS[mark]}
            tasks.append(current)
            in_detail = False
            continue
        if line.startswith("#"):
            current, in_detail = None, False
            continue
        if current is None:
            continue
        if m := _FIELD_RE.match(line):
            key = FIELD_ALIASES.get(m.group(1).lower())
            value = m.group(2).strip()
            in_detail = key == "detail"
            if key == "role":
                current["role"] = value.lower()
            elif key in ("depends", "files", "verify"):
                current[key] = _parse_list(value)
            elif key == "detail":
                current["detail"] = value
            continue
        if in_detail and (raw.startswith("  ") or not line.strip()):
            current["detail"] = (current.get("detail", "") + "\n" + line.strip()).rstrip()
            continue
        in_detail = False

    if goal is None:
        raise PlanParseError("PLAN.md no tiene la línea **Objetivo:**")
    if not tasks:
        raise PlanParseError("PLAN.md no contiene tareas (### [ ] T1 · ...)")
    for t in tasks:
        if "detail" in t:
            t["detail"] = t["detail"].strip()
    try:
        return Plan(goal=goal, tasks=[Task(**t) for t in tasks])
    except ValueError as exc:
        raise PlanParseError(str(exc)) from exc


# --- estado en disco ------------------------------------------------------------


@dataclass
class RunState:
    plan: Plan
    run_id: str | None = None
    updated_at: datetime = field(default_factory=utcnow)
    #: mtime_ns de PLAN.md tal como lo escribimos; si cambia, el usuario lo editó.
    plan_md_mtime_ns: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "run_id": self.run_id,
                "updated_at": self.updated_at.isoformat(),
                "plan_md_mtime_ns": self.plan_md_mtime_ns,
                "plan": json.loads(self.plan.model_dump_json()),
            },
            indent=2,
            ensure_ascii=False,
        )


def state_path(project_dir: Path) -> Path:
    return project_dir / STATE_DIR / STATE_FILE


def _atomic_write(path: Path, text: str, retries: int = 20) -> None:
    """Escritura atómica. En Windows `os.replace` falla si otro proceso tiene el archivo
    abierto (editor, antivirus, indexador): se reintenta brevemente y, como último
    recurso, se escribe en el sitio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.01 * (i + 1))
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)


def save(project_dir: Path, plan: Plan, run_id: str | None = None) -> RunState:
    """Escribe PLAN.md y state.json de forma atómica."""
    plan_md = project_dir / PLAN_FILE
    _atomic_write(plan_md, render_plan_md(plan))
    state = RunState(plan=plan, run_id=run_id, plan_md_mtime_ns=plan_md.stat().st_mtime_ns)
    _atomic_write(state_path(project_dir), state.to_json())
    return state


def load_state(project_dir: Path) -> RunState | None:
    path = state_path(project_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return RunState(
        plan=Plan.model_validate(data["plan"]),
        run_id=data.get("run_id"),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        plan_md_mtime_ns=data.get("plan_md_mtime_ns"),
    )


def load(project_dir: Path) -> RunState | None:
    """Estado actual incorporando ediciones manuales de PLAN.md.

    - Si solo existe PLAN.md (p. ej. escrito a mano), se parsea.
    - Si PLAN.md cambió desde que lo escribimos, se usa su contenido y se conservan los
      intentos registrados de las tareas cuyo id sigue existiendo.
    """
    plan_md = project_dir / PLAN_FILE
    state = load_state(project_dir)
    if not plan_md.is_file():
        return state
    if state is not None and plan_md.stat().st_mtime_ns == state.plan_md_mtime_ns:
        return state
    edited = parse_plan_md(plan_md.read_text(encoding="utf-8"))
    if state is None:
        return RunState(plan=edited)
    for task in edited.tasks:
        try:
            previous = state.plan.get(task.id)
        except KeyError:
            continue
        task.attempts = previous.attempts
    return RunState(plan=edited, run_id=state.run_id)
