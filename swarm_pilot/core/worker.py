"""Sub-agentes por rol (architect / coder / tester).

El worker oculta la diferencia entre adapters agénticos (editan archivos solos) y de
completado (devuelven JSON con los archivos, que aplicamos con `workspace.apply_edits`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from swarm_pilot.adapters.base import AdapterError, GenerateRequest, LLMAdapter, LogFn
from swarm_pilot.core.environment import Environment
from swarm_pilot.core.models import Plan, Task
from swarm_pilot.core.prompts import FILES_SCHEMA, worker_system
from swarm_pilot.core.workspace import (
    FileEdit,
    WorkspaceError,
    apply_edits,
    iter_files,
    rel_posix,
    safe_path,
    tree,
)

MAX_CONTEXT_CHARS = 120_000
MAX_FILE_CHARS = 40_000


@dataclass
class RetryContext:
    attempt: int
    error: str = ""
    diff: str = ""
    hint: str | None = None


@dataclass
class WorkerOutput:
    notes: str
    cost_usd: float | None = None
    applied: list[str] = field(default_factory=list)


def plan_overview(plan: Plan, current: Task) -> str:
    lines = []
    for t in plan.tasks:
        marker = "→" if t.id == current.id else " "
        files = f" [{', '.join(t.files)}]" if t.files else ""
        deps = f" (after {', '.join(t.depends)})" if t.depends else ""
        lines.append(f"{marker} {t.id} [{t.status.value}] {t.role.value}: {t.title}{deps}{files}")
    return "\n".join(lines)


def context_files(project_dir: Path, plan: Plan, task: Task) -> dict[str, str]:
    """Contenido actual de los archivos relevantes, para adapters que no pueden leer solos.

    Prioridad: archivos de la tarea, luego los de sus dependencias, luego el resto pequeño.
    """
    wanted: list[str] = list(task.files)
    for dep in task.depends:
        wanted += plan.get(dep).files
    seen: set[str] = set()
    out: dict[str, str] = {}
    total = 0

    def add(rel: str) -> None:
        nonlocal total
        if rel in seen:
            return
        seen.add(rel)
        try:
            path = safe_path(project_dir, rel)
        except WorkspaceError:
            return
        if not path.is_file():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if len(text) > MAX_FILE_CHARS or total + len(text) > MAX_CONTEXT_CHARS:
            return
        out[rel] = text
        total += len(text)

    for rel in wanted:
        add(rel)
    for path in iter_files(project_dir):
        if total >= MAX_CONTEXT_CHARS:
            break
        if path.stat().st_size <= 8_000:
            add(rel_posix(project_dir, path))
    return out


def task_prompt(
    plan: Plan,
    task: Task,
    project_dir: Path,
    env: Environment,
    retry: RetryContext | None,
    include_files: bool,
) -> str:
    parts = [
        f"# Overall goal\n{plan.goal}",
        f"# Plan (→ marks your task)\n{plan_overview(plan, task)}",
        f"# Your task: {task.id} — {task.title}\n{task.detail or '(no extra detail)'}",
        "Files assigned to this task: " + (", ".join(task.files) if task.files else "(not fixed)"),
        "Verification commands that will run afterwards: "
        + ("; ".join(f"`{c}`" for c in task.verify) if task.verify else "(built-in checks only)"),
        f"# Environment\n{env.describe()}",
        f"# Project tree\n{tree(project_dir)}",
    ]
    if include_files:
        for rel, text in context_files(project_dir, plan, task).items():
            parts.append(f"# Current content of {rel}\n```\n{text}\n```")
    if retry is not None and retry.attempt > 1 and retry.error:
        parts.append(
            f"# Attempt {retry.attempt - 1} FAILED verification\n"
            f"Error output:\n```\n{retry.error}\n```"
        )
        if retry.diff:
            parts.append(f"Changes made in that attempt:\n```diff\n{retry.diff}\n```")
        parts.append("Fix the cause of the failure.")
    if retry is not None and retry.hint:
        parts.append(f"# Guidance from the user (follow it)\n{retry.hint}")
    return "\n\n".join(parts)


class Worker:
    def __init__(self, project_dir: Path, env: Environment, timeout: float | None = None):
        self.project_dir = project_dir
        self.env = env
        self.timeout = timeout

    async def execute(
        self,
        adapter: LLMAdapter,
        plan: Plan,
        task: Task,
        retry: RetryContext | None = None,
        on_log: LogFn | None = None,
    ) -> WorkerOutput:
        attempt = retry.attempt if retry else 1
        prompt = task_prompt(plan, task, self.project_dir, self.env, retry, not adapter.agentic)
        req = GenerateRequest(
            prompt,
            system=worker_system(task.role.value, adapter.agentic),
            mode="edit" if adapter.agentic else "none",
            cwd=self.project_dir,
            json_schema=None if adapter.agentic else FILES_SCHEMA,
            timeout=self.timeout,
            meta={"role": task.role.value, "task_id": task.id, "attempt": attempt},
        )
        res = await adapter.generate(req, on_log)
        if adapter.agentic:
            return WorkerOutput(notes=res.text.strip(), cost_usd=res.cost_usd)

        data = res.data
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise AdapterError("la respuesta no tiene el formato {files: [...], notes}")
        try:
            edits = [FileEdit(str(f["path"]), str(f["content"])) for f in data["files"]]
            applied = apply_edits(self.project_dir, edits, [str(d) for d in data.get("delete", [])])
        except (KeyError, TypeError) as exc:
            raise AdapterError(f"archivo mal formado en la respuesta: {exc}") from exc
        except WorkspaceError as exc:
            raise AdapterError(f"ruta rechazada: {exc}") from exc
        if on_log:
            for rel in applied:
                on_log(f"✎ {rel}")
        return WorkerOutput(notes=str(data.get("notes", "")).strip(), cost_usd=res.cost_usd,
                            applied=applied)  # fmt: skip
