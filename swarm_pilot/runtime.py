"""Session: un run completo de swarm-pilot, compartido por la CLI y el servidor MCP.

    planner → aprobación (CLI o panel) → orchestrator ─┬→ panel :4000 (WebSocket)
                                                       └→ vista previa :3000

`close()` apaga todo en orden: orquestador → vista previa → panel → subprocesos (árbol
completo) → bus de eventos. Es idempotente.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from datetime import datetime
from typing import Any, Literal

from swarm_pilot.adapters.base import AdapterContext, LLMAdapter
from swarm_pilot.adapters.registry import AdapterPool
from swarm_pilot.config import Settings, secret_values
from swarm_pilot.core import plan_io
from swarm_pilot.core.environment import detect_environment
from swarm_pilot.core.events import EventBus
from swarm_pilot.core.models import EventType, Plan, Task, TaskStatus
from swarm_pilot.core.orchestrator import HelpAction, Orchestrator, Outcome, RunSummary
from swarm_pilot.core.planner import make_plan
from swarm_pilot.core.preview import PreviewManager, find_free_port
from swarm_pilot.core.process_manager import ProcessManager

Phase = Literal["idle", "planning", "awaiting_approval", "running", "finished"]


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


class Session:
    def __init__(
        self,
        settings: Settings,
        *,
        ui: bool = True,
        preview: bool = True,
        on_blocked: Literal["wait", "stop"] = "wait",
        env: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.project_dir = settings.project_dir
        self.run_id = new_run_id()
        self.state_dir = settings.state_dir
        env = dict(os.environ if env is None else env)
        self.pm = ProcessManager(pid_file=self.state_dir / "pids.json")
        self.bus = EventBus(
            self.run_id,
            log_path=self.state_dir / "runs" / self.run_id / "events.jsonl",
            secrets=secret_values(env),
        )
        self.pool = AdapterPool(
            AdapterContext(
                process_manager=self.pm,
                env=env,
                skip_permissions=settings.claude_skip_permissions,
                scratch_dir=self.state_dir / "tmp",
            )
        )
        self.env = detect_environment(self.project_dir)
        self.ui_enabled = ui
        self.preview_enabled = preview
        self.on_blocked = on_blocked
        self.phase: Phase = "idle"
        self.plan: Plan | None = None
        self.orchestrator: Orchestrator | None = None
        self.summary: RunSummary | None = None
        self.web: Any = None
        self.preview: PreviewManager | None = None
        self.started_at = time.time()
        self._approval: asyncio.Future[bool] | None = None
        self._closed = False
        self.stale_killed: list[str] = []

    # --- servicios --------------------------------------------------------------------

    async def start_services(self) -> None:
        """Limpia restos de un run caído y levanta panel y vista previa (si procede)."""
        pid_file = self.state_dir / "pids.json"
        if pid_file.exists():
            self.stale_killed = await asyncio.to_thread(ProcessManager.cleanup_stale, pid_file)
        if self.preview_enabled:
            self.preview = PreviewManager(
                self.project_dir, self.pm, self.bus, self.settings.preview_port
            )
            await self.preview.ensure()
        if self.ui_enabled:
            from swarm_pilot.web.server import WebServer

            self.web = WebServer(self, find_free_port(self.settings.dashboard_port))
            await self.web.start()

    @property
    def dashboard_url(self) -> str | None:
        return self.web.url if self.web else None

    def adapter_for(self, role: str) -> LLMAdapter:
        return self.pool.get(self.settings.provider_for(role))

    # --- fases ------------------------------------------------------------------------

    def _set_phase(self, phase: Phase) -> None:
        self.phase = phase
        self.bus.emit(EventType.PHASE, phase=phase,
                      plan=self.plan.model_dump(mode="json") if self.plan else None)  # fmt: skip

    async def make_plan(self, goal: str) -> Plan:
        self._set_phase("planning")
        adapter = self.adapter_for("planner")

        def log(line: str) -> None:
            self.bus.emit(EventType.AGENT_LOG, task_id="planner", line=line, source="planner")

        self.plan = await make_plan(
            goal, adapter, self.project_dir, env=self.env, on_log=log,
            timeout=self.settings.agent_timeout,
        )  # fmt: skip
        plan_io.save(self.project_dir, self.plan, self.run_id)
        self._set_phase("idle")
        return self.plan

    def load_existing(self) -> Plan | None:
        state = plan_io.load(self.project_dir)
        if state is None:
            return None
        self.plan = state.plan
        return self.plan

    async def wait_approval(self) -> bool:
        """Espera a que el usuario apruebe el plan (CLI con `approve()` o el panel)."""
        self._approval = asyncio.get_running_loop().create_future()
        self._set_phase("awaiting_approval")
        try:
            return await self._approval
        finally:
            self._approval = None

    def approve(self, approved: bool) -> str | None:
        fut = self._approval
        if fut is None or fut.done():
            return "no hay ningún plan esperando aprobación"
        fut.get_loop().call_soon_threadsafe(lambda: fut.done() or fut.set_result(approved))
        return None

    async def execute(self, plan: Plan | None = None) -> RunSummary:
        plan = plan or self.plan
        if plan is None:
            raise RuntimeError("no hay plan que ejecutar")
        self.plan = plan
        self.orchestrator = Orchestrator(
            project_dir=self.project_dir,
            plan=plan,
            settings=self.settings,
            bus=self.bus,
            pm=self.pm,
            adapter_for=self.adapter_for,
            run_id=self.run_id,
            env=self.env,
            on_blocked=self.on_blocked,
            on_task_done=self._on_task_done,
        )
        self._set_phase("running")
        try:
            self.summary = await self.orchestrator.run()
        finally:
            self._set_phase("finished")
        return self.summary

    async def _on_task_done(self, task: Task, changed: list[str]) -> None:
        if self.preview is not None:
            await self.preview.ensure(changed)

    def answer(self, task_id: str, action: str, hint: str | None = None) -> str | None:
        if self.orchestrator is None:
            return "no hay ninguna ejecución en curso"
        return self.orchestrator.answer(task_id, action, hint)

    # --- estado ----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        orch = self.orchestrator
        return {
            "run_id": self.run_id,
            "phase": self.phase,
            "project": self.project_dir.name,
            "goal": self.plan.goal if self.plan else None,
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "pending_help": list(orch.pending_help.values()) if orch else [],
            "preview": self.preview.info() if self.preview else {"state": "off"},
            "cost_usd": round(orch.cost_usd, 4) if orch else 0.0,
            "started_at": self.started_at,
            "summary": (
                {
                    "outcome": self.summary.outcome.value,
                    "counts": self.summary.counts,
                    "duration": self.summary.duration,
                }
                if self.summary
                else None
            ),  # fmt: skip
            "providers": {
                role: str(spec) if spec else None for role, spec in self.settings.providers.items()
            },
            "history": [
                e.model_dump(mode="json")
                for e in self.bus.history()[-600:]
                if e.type
                in (
                    EventType.AGENT_LOG,
                    EventType.ATTEMPT_FAILED,
                    EventType.VERIFY_RESULT,
                    EventType.NEEDS_HELP,
                )
            ],  # fmt: skip
        }

    def blocked_tasks(self) -> list[Task]:
        return [t for t in (self.plan.tasks if self.plan else []) if t.status is TaskStatus.BLOCKED]

    # --- cierre ----------------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._approval is not None and not self._approval.done():
            self._approval.cancel()
        if self.preview is not None:
            with contextlib.suppress(Exception):
                await self.preview.stop()
        if self.web is not None:
            with contextlib.suppress(Exception):
                await self.web.stop()
        await self.pm.shutdown()
        self.bus.close()


__all__ = ["HelpAction", "Outcome", "Session"]
