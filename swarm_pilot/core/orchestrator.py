"""Orchestrator: planificador DAG, auto-reparación y pausa con petición de ayuda.

Ciclo de cada tarea (`_run_task`):
    worker → snapshot/diff → verifier → DONE
                              └─ falla → reintento con error + diff (hasta max_attempts)
                                          └─ agotado → BLOCKED + evento `needs_help`

Una tarea bloqueada solo frena su rama del DAG; las independientes siguen. El usuario
responde con `answer()` (CLI, panel web o MCP): retry [+pista], manual, skip o abort.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from swarm_pilot.adapters.base import AdapterError, LLMAdapter
from swarm_pilot.config import Settings
from swarm_pilot.core import plan_io
from swarm_pilot.core.environment import Environment, detect_environment
from swarm_pilot.core.events import EventBus
from swarm_pilot.core.models import (
    ACTIVE,
    Attempt,
    EventType,
    Plan,
    Task,
    TaskStatus,
    paths_overlap,
    tasks_conflict,
    utcnow,
)
from swarm_pilot.core.process_manager import ProcessManager
from swarm_pilot.core.verifier import Verifier
from swarm_pilot.core.worker import RetryContext, Worker
from swarm_pilot.core.workspace import diff, snapshot, unified_diff

AdapterFor = Callable[[str], LLMAdapter]
TaskDoneHook = Callable[[Task, list[str]], Awaitable[None]]


class HelpAction(str, Enum):
    RETRY = "retry"
    MANUAL = "manual"
    SKIP = "skip"
    ABORT = "abort"


class Outcome(str, Enum):
    COMPLETED = "completed"
    PAUSED = "paused"  # quedan tareas bloqueadas y on_blocked="stop"
    ABORTED = "aborted"


@dataclass
class RunSummary:
    outcome: Outcome
    counts: dict[str, int]
    cost_usd: float
    duration: float
    blocked: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        *,
        project_dir: Path,
        plan: Plan,
        settings: Settings,
        bus: EventBus,
        pm: ProcessManager,
        adapter_for: AdapterFor,
        run_id: str | None = None,
        env: Environment | None = None,
        on_blocked: Literal["wait", "stop"] = "wait",
        on_task_done: TaskDoneHook | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.plan = plan
        self.settings = settings
        self.bus = bus
        self.pm = pm
        self.adapter_for = adapter_for
        self.run_id = run_id
        self.env = env or detect_environment(project_dir)
        self.on_blocked = on_blocked
        self.on_task_done = on_task_done
        self.worker = Worker(project_dir, self.env, timeout=settings.agent_timeout)
        self.cost_usd = 0.0
        self.pending_help: dict[str, dict[str, object]] = {}
        self._answers: asyncio.Queue[tuple[str, HelpAction, str | None]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hints: dict[str, str] = {}
        self._last_diff: dict[str, str] = {}
        self._active: dict[str, asyncio.Task[None]] = {}
        #: Para cada intento en curso, qué otras tareas corrieron a la vez.
        self._concurrent: dict[str, set[str]] = {}
        self._install_lock = asyncio.Lock()
        self._abort = False

    # --- API pública ---------------------------------------------------------------

    def answer(self, task_id: str, action: HelpAction | str, hint: str | None = None) -> str | None:
        """Responde a una tarea bloqueada. Devuelve un mensaje de error o None si se aceptó.

        Seguro de llamar desde otro hilo (p. ej. el lector de stdin de la CLI)."""
        try:
            action = HelpAction(action)
        except ValueError:
            return f"acción desconocida: {action!r}"
        if action is not HelpAction.ABORT:
            try:
                task = self.plan.get(task_id)
            except KeyError:
                return f"tarea desconocida: {task_id!r}"
            if task.status is not TaskStatus.BLOCKED:
                return f"{task_id} no está bloqueada"
        item = (task_id, action, (hint or "").strip() or None)
        loop = self._loop
        running = None
        with contextlib.suppress(RuntimeError):
            running = asyncio.get_running_loop()
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._answers.put_nowait, item)
        else:
            self._answers.put_nowait(item)
        return None

    async def run(self) -> RunSummary:
        self._loop = asyncio.get_running_loop()
        start = time.monotonic()
        for t in self.plan.tasks:  # reanudación: lo que estaba en curso vuelve a la cola
            if t.status in ACTIVE:
                t.status = TaskStatus.PENDING
        self._save()
        self.bus.emit(EventType.RUN_STARTED, goal=self.plan.goal, tasks=len(self.plan.tasks))
        for t in self.plan.tasks:
            if t.status is TaskStatus.BLOCKED:
                last = t.last_attempt
                self._request_help(t, last.error if last else "bloqueada en un run anterior")

        outcome = Outcome.COMPLETED
        answer_waiter: asyncio.Task[tuple[str, HelpAction, str | None]] | None = None
        try:
            while not self._abort:
                self._schedule()
                if not self._active:
                    if self.plan.is_finished:
                        break
                    if not self.pending_help or self.on_blocked == "stop":
                        outcome = Outcome.PAUSED
                        break
                if answer_waiter is None:
                    answer_waiter = asyncio.create_task(self._answers.get())
                waiters: set[asyncio.Future[object]] = {*self._active.values(), answer_waiter}
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for tid, job in list(self._active.items()):
                    if job in done:
                        del self._active[tid]
                if answer_waiter in done:
                    await self._apply_answer(*answer_waiter.result())
                    answer_waiter = None
            if self._abort:
                outcome = Outcome.ABORTED
                await self._cancel_active()
        except asyncio.CancelledError:
            await self._cancel_active()
            self._finish(Outcome.ABORTED, start)
            raise
        finally:
            if answer_waiter is not None:
                answer_waiter.cancel()
        return self._finish(outcome, start)

    # --- planificación ----------------------------------------------------------------

    def _conflicts(self, task: Task) -> bool:
        running = [self.plan.get(i) for i in self._active]
        if not running:
            return False
        # Una tarea sin archivos declarados puede tocar cualquier cosa: corre sola.
        if not task.files or any(not r.files for r in running):
            return True
        return tasks_conflict(task, running)

    def _schedule(self) -> None:
        for task in self.plan.ready_tasks():
            if len(self._active) >= self.settings.max_parallel:
                return
            if task.id in self._active or self._conflicts(task):
                continue
            self._start(task.id, self._run_task(task))

    def _start(self, task_id: str, coro: Awaitable[None]) -> None:
        for others in self._concurrent.values():
            others.add(task_id)
        self._concurrent[task_id] = set(self._active)
        self._active[task_id] = asyncio.create_task(coro)  # type: ignore[arg-type]

    async def _cancel_active(self) -> None:
        jobs = list(self._active.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._active.clear()
        for t in self.plan.tasks:
            if t.status in ACTIVE:
                t.status = TaskStatus.PENDING
        self._save()

    # --- ejecución de una tarea ---------------------------------------------------------

    def _logger(self, task_id: str, source: str = "agent") -> Callable[[str], None]:
        def log(line: str) -> None:
            self.bus.emit(EventType.AGENT_LOG, task_id=task_id, line=line, source=source)

        return log

    def _attributed(self, task: Task, changed: list[str]) -> list[str]:
        """Cambios atribuibles a `task`: descarta los archivos de tareas que corrían a la vez."""
        others = self._concurrent.get(task.id, set())
        foreign = [f for o in others for f in self.plan.get(o).files]
        own = task.files
        return [
            c for c in changed
            if any(paths_overlap(c, f) for f in own) or not any(paths_overlap(c, f) for f in foreign)
        ]  # fmt: skip

    async def _run_task(self, task: Task) -> None:
        adapter = self.adapter_for(task.role.value)
        limit = task.attempts_used + self.settings.max_attempts
        hint = self._hints.pop(task.id, None)
        log = self._logger(task.id)
        attempt: Attempt | None = None
        try:
            while task.attempts_used < limit:
                number = task.attempts_used + 1
                prev = task.last_attempt
                retry = RetryContext(
                    attempt=number,
                    error=prev.error if prev is not None and prev.ok is False else "",
                    diff=self._last_diff.get(task.id, ""),
                    hint=hint,
                )
                attempt = Attempt(number=number, hint=hint)
                task.attempts.append(attempt)
                self._concurrent[task.id] = set(self._active) - {task.id}
                self._set_status(task, TaskStatus.RUNNING, attempt=number, agent=adapter.label)

                before = await asyncio.to_thread(snapshot, self.project_dir)
                notes = ""
                try:
                    out = await self.worker.execute(adapter, self.plan, task, retry, log)
                    notes = out.notes
                    if out.cost_usd:
                        self.cost_usd += out.cost_usd
                except AdapterError as exc:
                    attempt.finished_at = utcnow()
                    attempt.ok = False
                    attempt.error = f"Error del agente ({adapter.label}): {exc}"
                    if exc.rate_limited or not exc.retryable:
                        self._block(task, attempt.error, rate_limited=exc.rate_limited)
                        return
                    self._attempt_failed(task, attempt, limit)
                    hint = None
                    continue
                after = await asyncio.to_thread(snapshot, self.project_dir)
                changed = self._attributed(task, diff(before, after).all)
                attempt.changed_files = changed
                self._last_diff[task.id] = unified_diff(before, after, only=changed)
                if task.files:
                    undeclared = [
                        c for c in changed if not any(paths_overlap(c, f) for f in task.files)
                    ]
                    if undeclared:
                        log(f"⚠ archivos fuera de lo declarado: {', '.join(undeclared)}")

                self._set_status(task, TaskStatus.VERIFYING, attempt=number)
                result = await self._verifier(task.id).verify(task, changed, self.plan)
                attempt.finished_at = utcnow()
                attempt.ok = result.ok
                self.bus.emit(
                    EventType.VERIFY_RESULT, task_id=task.id, ok=result.ok, attempt=number,
                    checks=result.summary(),
                )  # fmt: skip
                if result.ok:
                    self._set_status(task, TaskStatus.DONE, attempt=number, notes=notes,
                                     changed=changed)  # fmt: skip
                    await self._task_done(task, changed)
                    return
                attempt.error = result.report()
                self._attempt_failed(task, attempt, limit)
                hint = None
            last = task.last_attempt
            self._block(task, last.error if last else "sin intentos")
        except asyncio.CancelledError:
            if attempt is not None and attempt.ok is None:
                attempt.finished_at = utcnow()
                attempt.error = "cancelado"
            raise
        except Exception as exc:  # error interno: no tumba el run, bloquea la tarea
            self._block(task, f"error interno de swarm-pilot: {exc!r}")
        finally:
            self._concurrent.pop(task.id, None)

    def _verifier(self, task_id: str) -> Verifier:
        return Verifier(
            self.project_dir,
            self.pm,
            timeout=self.settings.verify_timeout,
            on_log=self._logger(task_id, "verifier"),
            install_lock=self._install_lock,
        )

    async def _task_done(self, task: Task, changed: list[str]) -> None:
        if self.on_task_done is None:
            return
        try:
            await self.on_task_done(task, changed)
        except Exception as exc:  # el preview no debe romper el run
            self._logger(task.id, "system")(f"⚠ hook tras completar la tarea falló: {exc!r}")

    async def _reverify(self, task: Task) -> None:
        """Acción 'manual': el usuario lo arregló a mano; solo se vuelve a verificar."""
        self._set_status(task, TaskStatus.VERIFYING)
        changed = [f for f in task.files if (self.project_dir / f).is_file()]
        result = await self._verifier(task.id).verify(task, changed or task.files, self.plan)
        self.bus.emit(EventType.VERIFY_RESULT, task_id=task.id, ok=result.ok, manual=True,
                      checks=result.summary())  # fmt: skip
        if result.ok:
            self._set_status(task, TaskStatus.DONE, manual=True)
            await self._task_done(task, changed)
        else:
            self._block(task, result.report())

    # --- estados y ayuda ------------------------------------------------------------------

    def _attempt_failed(self, task: Task, attempt: Attempt, limit: int) -> None:
        self.bus.emit(
            EventType.ATTEMPT_FAILED, task_id=task.id, attempt=attempt.number,
            remaining=max(0, limit - task.attempts_used), error=attempt.error,
        )  # fmt: skip
        self._save()

    def _block(self, task: Task, error: str, rate_limited: bool = False) -> None:
        self._set_status(task, TaskStatus.BLOCKED, error=error)
        self._request_help(task, error, rate_limited)

    def _request_help(self, task: Task, error: str, rate_limited: bool = False) -> None:
        info: dict[str, object] = {
            "task_id": task.id,
            "title": task.title,
            "error": error,
            "attempts": task.attempts_used,
            "rate_limited": rate_limited,
            "actions": [a.value for a in HelpAction],
        }
        self.pending_help[task.id] = info
        self.bus.emit(EventType.NEEDS_HELP, task_id=task.id, **{k: v for k, v in info.items()
                                                                 if k != "task_id"})  # fmt: skip

    async def _apply_answer(self, task_id: str, action: HelpAction, hint: str | None) -> None:
        if action is HelpAction.ABORT:
            self._abort = True
            return
        task = self.plan.get(task_id)
        if task.status is not TaskStatus.BLOCKED:  # respuesta tardía (p. ej. CLI y panel a la vez)
            return
        self.pending_help.pop(task_id, None)
        if action is HelpAction.RETRY:
            if hint:
                self._hints[task_id] = hint
            self._set_status(task, TaskStatus.PENDING, hint=hint)
        elif action is HelpAction.SKIP:
            self._set_status(task, TaskStatus.SKIPPED)
        elif action is HelpAction.MANUAL:
            self._start(task_id, self._reverify(task))

    def _set_status(self, task: Task, status: TaskStatus, **payload: object) -> None:
        task.status = status
        self.bus.emit(EventType.TASK_STATUS, task_id=task.id, status=status.value,
                      title=task.title, **payload)  # fmt: skip
        self._save()

    def _save(self) -> None:
        try:
            plan_io.save(self.project_dir, self.plan, self.run_id)
        except OSError as exc:  # no perder el run por un archivo bloqueado
            self.bus.emit(EventType.AGENT_LOG, line=f"⚠ no se pudo guardar el estado: {exc}",
                          source="system")  # fmt: skip
        self.bus.emit(EventType.PLAN_UPDATED, plan=self.plan.model_dump(mode="json"))

    def _finish(self, outcome: Outcome, start: float) -> RunSummary:
        summary = RunSummary(
            outcome=outcome,
            counts=self.plan.counts(),
            cost_usd=round(self.cost_usd, 4),
            duration=round(time.monotonic() - start, 1),
            blocked=[t.id for t in self.plan.tasks if t.status is TaskStatus.BLOCKED],
        )
        self.bus.emit(
            EventType.RUN_FINISHED, outcome=outcome.value, counts=summary.counts,
            cost_usd=summary.cost_usd, duration=summary.duration, blocked=summary.blocked,
        )  # fmt: skip
        return summary
