import asyncio
import dataclasses
import json

import pytest

from swarm_pilot.adapters.base import AdapterContext, AdapterError
from swarm_pilot.adapters.fake import LANDING_PLAN, FakeAdapter, landing_scenario
from swarm_pilot.config import ProviderSpec, load_settings
from swarm_pilot.core import plan_io
from swarm_pilot.core.events import EventBus
from swarm_pilot.core.models import EventType, Plan, Task, TaskStatus
from swarm_pilot.core.orchestrator import Orchestrator, Outcome
from swarm_pilot.core.process_manager import ProcessManager


def landing_plan() -> Plan:
    return Plan(goal="landing", tasks=[Task(**t) for t in LANDING_PLAN["tasks"]])


@pytest.fixture
async def harness(tmp_path):
    pm = ProcessManager(grace=1)
    settings = load_settings(
        tmp_path,
        env={"SWARM_CONFIG": str(tmp_path / "none.toml")},
        autodetect=lambda: ProviderSpec("fake"),
    )

    def make(responder=landing_scenario, *, agentic=False, plan=None, delay=0.0, **kw):
        adapter = FakeAdapter(
            ProviderSpec("fake"), AdapterContext(), responder, agentic=agentic, delay=delay
        )
        bus = EventBus("run-test")
        orch = Orchestrator(
            project_dir=tmp_path,
            plan=plan or landing_plan(),
            settings=settings,
            bus=bus,
            pm=pm,
            adapter_for=lambda role: adapter,
            run_id="run-test",
            **kw,
        )
        orch.adapter = adapter
        orch.events = bus.history
        return orch

    yield make
    await pm.shutdown()


def types(orch, task_id=None):
    return [(e.type, e.payload.get("status")) for e in orch.events(task_id)]


@pytest.mark.parametrize("agentic", [False, True])
async def test_landing_completes_with_self_repair(harness, tmp_path, agentic):
    orch = harness(agentic=agentic)
    summary = await orch.run()

    assert summary.outcome is Outcome.COMPLETED
    assert summary.counts["done"] == 3
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["features"]
    t3 = orch.plan.get("T3")
    assert [a.ok for a in t3.attempts] == [False, True]
    assert "JSON inválido" in t3.attempts[0].error
    assert "config.json" in orch.adapter.calls[-1].prompt  # el reintento recibe error/diff
    assert "FAILED verification" in orch.adapter.calls[-1].prompt
    assert len(orch.plan.get("T2").attempts) == 1, "T2 no debe cargar con el fallo de T3"

    failed = [e for e in orch.events("T3") if e.type is EventType.ATTEMPT_FAILED]
    assert failed[0].payload["remaining"] == 2
    md = (tmp_path / "PLAN.md").read_text(encoding="utf-8")
    assert md.count("### [x]") == 3
    assert orch.events()[-1].type is EventType.RUN_FINISHED


async def test_independent_tasks_run_in_parallel(harness):
    orch = harness(delay=0.2)
    await orch.run()
    seq = [
        (e.task_id, e.payload.get("status"))
        for e in orch.events()
        if e.type is EventType.TASK_STATUS
    ]
    first_done = min(i for i, s in enumerate(seq) if s[1] == "done" and s[0] in ("T2", "T3"))
    started = {tid for tid, st in seq[:first_done] if st == "running"}
    assert {"T2", "T3"} <= started


def broken_t3(req):
    if req.meta.get("task_id") == "T3":
        return {"files": [{"path": "config.json", "content": "{roto"}], "notes": "x"}
    return landing_scenario(req)


async def test_blocked_task_pauses_only_its_branch(harness):
    orch = harness(broken_t3, on_blocked="stop")
    summary = await orch.run()
    assert summary.outcome is Outcome.PAUSED and summary.blocked == ["T3"]
    assert orch.plan.get("T2").status is TaskStatus.DONE
    assert len(orch.plan.get("T3").attempts) == 3
    help_ev = [e for e in orch.events("T3") if e.type is EventType.NEEDS_HELP][0]
    assert "JSON inválido" in help_ev.payload["error"]


async def wait_for_help(orch, task_id):
    for _ in range(200):
        if task_id in orch.pending_help:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no se pidió ayuda")


async def test_retry_with_hint(harness):
    def responder(req):
        if req.meta.get("task_id") == "T3" and "usa JSON válido" not in req.prompt:
            return broken_t3(req)
        # Con la pista, T3 se comporta como un segundo intento (JSON correcto).
        return landing_scenario(dataclasses.replace(req, meta={**req.meta, "attempt": 2}))

    orch = harness(responder)
    run = asyncio.create_task(orch.run())
    await wait_for_help(orch, "T3")
    assert orch.answer("T3", "retry", "  usa JSON válido  ") is None
    summary = await asyncio.wait_for(run, 30)
    assert summary.outcome is Outcome.COMPLETED
    assert orch.plan.get("T3").attempts[-1].hint == "usa JSON válido"


async def test_manual_fix_and_skip_and_abort(harness, tmp_path):
    orch = harness(broken_t3)
    run = asyncio.create_task(orch.run())
    await wait_for_help(orch, "T3")
    assert orch.answer("T2", "skip") == "T2 no está bloqueada"
    assert orch.answer("T3", "bailar") is not None
    (tmp_path / "config.json").write_text('{"features": []}')
    orch.answer("T3", "manual")
    summary = await asyncio.wait_for(run, 30)
    assert summary.outcome is Outcome.COMPLETED
    assert orch.plan.get("T3").status is TaskStatus.DONE

    orch2 = harness(broken_t3)
    run = asyncio.create_task(orch2.run())
    await wait_for_help(orch2, "T3")
    orch2.answer("T3", "skip")
    assert (await asyncio.wait_for(run, 30)).counts["skipped"] == 1


async def test_abort(harness):
    orch = harness(broken_t3)
    run = asyncio.create_task(orch.run())
    await wait_for_help(orch, "T3")
    orch.answer("", "abort")
    assert (await asyncio.wait_for(run, 30)).outcome is Outcome.ABORTED


async def test_rate_limit_blocks_immediately(harness):
    def responder(req):
        if req.meta.get("task_id") == "T1":
            return AdapterError("usage limit reached", rate_limited=True)
        return landing_scenario(req)

    summary = await harness(responder, on_blocked="stop").run()
    assert summary.blocked == ["T1"]
    assert summary.counts["pending"] == 2  # T2 y T3 dependen de T1


async def test_agent_errors_consume_attempts(harness):
    calls = {"n": 0}

    def responder(req):
        if req.meta.get("task_id") == "T1" and calls["n"] < 2:
            calls["n"] += 1
            return AdapterError("respuesta truncada")
        return landing_scenario(req)

    orch = harness(responder)
    summary = await orch.run()
    assert summary.outcome is Outcome.COMPLETED
    t1 = orch.plan.get("T1")
    assert [a.ok for a in t1.attempts] == [False, False, True]
    assert "respuesta truncada" in t1.attempts[0].error
    assert calls["n"] == 2


async def test_cancel_resets_running_tasks_and_resume_continues(harness, tmp_path):
    orch = harness(delay=5)
    run = asyncio.create_task(orch.run())
    await asyncio.sleep(0.5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    state = plan_io.load(tmp_path)
    assert state.plan.get("T1").status is TaskStatus.PENDING

    # Simula un run previo que ya completó T1: el nuevo solo ejecuta T2 y T3.
    state.plan.get("T1").status = TaskStatus.DONE
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css"><script src="app.js"></script>'
    )
    orch2 = harness(plan=state.plan)
    summary = await orch2.run()
    assert summary.outcome is Outcome.COMPLETED
    assert {c.meta["task_id"] for c in orch2.adapter.calls} == {"T2", "T3"}


async def test_task_done_hook_errors_do_not_break_run(harness):
    seen = []

    async def hook(task, changed):
        seen.append((task.id, changed))
        raise RuntimeError("preview roto")

    summary = await harness(on_task_done=hook).run()
    assert summary.outcome is Outcome.COMPLETED
    assert ("T1", ["index.html"]) in seen
