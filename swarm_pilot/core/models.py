"""Modelos de dominio: Task, Plan, Attempt, Event.

El `Plan` es un DAG de tareas. Se valida al construirse (ids únicos, dependencias
existentes, sin ciclos) para que el resto del sistema pueda asumir un grafo sano.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    ARCHITECT = "architect"
    CODER = "coder"
    TESTER = "tester"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


#: Estados que satisfacen a las tareas dependientes.
SATISFIED = frozenset({TaskStatus.DONE, TaskStatus.SKIPPED})
#: Estados en los que una tarea está ocupando un worker.
ACTIVE = frozenset({TaskStatus.RUNNING, TaskStatus.VERIFYING})


class Attempt(BaseModel):
    """Un intento de un worker sobre una tarea, con el resultado de su verificación."""

    number: int = Field(ge=1)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ok: bool | None = None
    error: str = ""
    changed_files: list[str] = Field(default_factory=list)
    hint: str | None = None


def normalize_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") if p not in ("", "/") else p


def paths_overlap(a: str, b: str) -> bool:
    """True si dos rutas (o patrones glob) declarados pueden tocar el mismo archivo."""
    a, b = normalize_path(a), normalize_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    # Una es directorio de la otra: "src" solapa con "src/app.py".
    return a.startswith(b + "/") or b.startswith(a + "/")


class Task(BaseModel):
    id: str
    title: str
    role: Role = Role.CODER
    depends: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    verify: list[str] = Field(default_factory=list)
    detail: str = ""
    status: TaskStatus = TaskStatus.PENDING
    attempts: list[Attempt] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(f"id de tarea inválido: {v!r}")
        return v

    @field_validator("title")
    @classmethod
    def _non_empty_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("el título de la tarea no puede estar vacío")
        return v

    @field_validator("depends")
    @classmethod
    def _dedupe_depends(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @field_validator("files")
    @classmethod
    def _normalize_files(cls, v: list[str]) -> list[str]:
        return [p for p in dict.fromkeys(normalize_path(x) for x in v) if p]

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    @property
    def last_attempt(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    def overlaps(self, other: Task) -> bool:
        """True si ambas tareas declaran archivos que pueden coincidir."""
        return any(paths_overlap(a, b) for a in self.files for b in other.files)


class PlanValidationError(ValueError):
    pass


class Plan(BaseModel):
    goal: str
    tasks: list[Task]
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_dag(self) -> Plan:
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise PlanValidationError(f"ids de tarea duplicados: {', '.join(dupes)}")
        known = set(ids)
        for t in self.tasks:
            if t.id in t.depends:
                raise PlanValidationError(f"{t.id} depende de sí misma")
            missing = [d for d in t.depends if d not in known]
            if missing:
                raise PlanValidationError(
                    f"{t.id} depende de tareas inexistentes: {', '.join(missing)}"
                )
        self.topological_order()  # lanza si hay ciclo
        return self

    # --- consultas ---------------------------------------------------------

    def get(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def topological_order(self) -> list[Task]:
        """Orden topológico estable (respeta el orden original entre tareas libres)."""
        pending = {t.id: set(t.depends) for t in self.tasks}
        order: list[Task] = []
        while pending:
            free = [t for t in self.tasks if t.id in pending and not pending[t.id]]
            if not free:
                cycle = ", ".join(sorted(pending))
                raise PlanValidationError(f"ciclo de dependencias entre: {cycle}")
            for t in free:
                order.append(t)
                del pending[t.id]
            for deps in pending.values():
                deps.difference_update(t.id for t in free)
        return order

    def ready_tasks(self) -> list[Task]:
        """Tareas pendientes cuyas dependencias ya están satisfechas."""
        satisfied = {t.id for t in self.tasks if t.status in SATISFIED}
        return [
            t
            for t in self.topological_order()
            if t.status is TaskStatus.PENDING and set(t.depends) <= satisfied
        ]

    def descendants(self, task_id: str) -> set[str]:
        """Todas las tareas que dependen (directa o indirectamente) de `task_id`."""
        children: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for d in t.depends:
                children[d].append(t.id)
        out: set[str] = set()
        stack = list(children[task_id])
        while stack:
            cur = stack.pop()
            if cur not in out:
                out.add(cur)
                stack.extend(children[cur])
        return out

    def stalled_by_blocked(self) -> set[str]:
        """Tareas que no pueden avanzar porque un ancestro está bloqueado."""
        out: set[str] = set()
        for t in self.tasks:
            if t.status is TaskStatus.BLOCKED:
                out |= self.descendants(t.id)
        return out

    @property
    def is_finished(self) -> bool:
        return all(t.status in SATISFIED for t in self.tasks)

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in TaskStatus}
        for t in self.tasks:
            out[t.status.value] += 1
        return out


class EventType(str, Enum):
    PHASE = "phase"  # planning / awaiting_approval / running / finished
    RUN_STARTED = "run_started"
    PLAN_UPDATED = "plan_updated"
    TASK_STATUS = "task_status"
    AGENT_LOG = "agent_log"
    VERIFY_RESULT = "verify_result"
    ATTEMPT_FAILED = "attempt_failed"
    NEEDS_HELP = "needs_help"
    PREVIEW_STATUS = "preview_status"
    RUN_FINISHED = "run_finished"


class Event(BaseModel):
    type: EventType
    ts: datetime = Field(default_factory=utcnow)
    run_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def tasks_conflict(task: Task, active: Iterable[Task]) -> bool:
    """True si `task` no puede correr en paralelo con alguna de `active`."""
    return any(task.overlaps(a) for a in active)
