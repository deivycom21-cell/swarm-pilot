import json

import pytest

from swarm_pilot.adapters.base import AdapterContext, AdapterError
from swarm_pilot.adapters.fake import LANDING_PLAN, FakeAdapter
from swarm_pilot.config import ProviderSpec
from swarm_pilot.core.planner import PlannerError, make_plan, read_manifests


def fake(responder, agentic=False):
    return FakeAdapter(ProviderSpec("fake"), AdapterContext(), responder, agentic=agentic)


async def test_plan_from_fake_landing(tmp_path):
    adapter = fake(lambda req: LANDING_PLAN)
    plan = await make_plan("  Crea una landing  ", adapter, tmp_path)
    assert plan.goal == "Crea una landing"
    assert [t.id for t in plan.tasks] == ["T1", "T2", "T3"]
    req = adapter.calls[0]
    assert req.meta == {"role": "planner"} and req.mode == "none"
    assert req.json_schema["required"] == ["tasks"]
    assert "Crea una landing" in req.prompt and "(directorio vacío)" in req.prompt


async def test_agentic_planner_uses_read_mode(tmp_path):
    adapter = fake(lambda req: LANDING_PLAN, agentic=True)
    await make_plan("x", adapter, tmp_path)
    assert adapter.calls[0].mode == "read" and adapter.calls[0].cwd == tmp_path


async def test_invalid_plan_is_retried_with_error(tmp_path):
    answers = iter([
        {"tasks": [{"id": "T1", "title": "a", "depends": ["T2"]},
                   {"id": "T2", "title": "b", "depends": ["T1"]}]},
        "no es json",
        LANDING_PLAN,
    ])  # fmt: skip
    adapter = fake(lambda req: next(answers))
    plan = await make_plan("x", adapter, tmp_path)
    assert len(plan.tasks) == 3
    assert "ciclo" in adapter.calls[1].prompt
    assert len(adapter.calls) == 3


async def test_planner_gives_up(tmp_path):
    with pytest.raises(PlannerError, match="plan válido"):
        await make_plan("x", fake(lambda req: {"nope": 1}), tmp_path)
    with pytest.raises(PlannerError, match="vacío"):
        await make_plan("  ", fake(lambda req: LANDING_PLAN), tmp_path)


async def test_rate_limit_is_not_retried(tmp_path):
    adapter = fake(lambda req: AdapterError("usage limit", rate_limited=True))
    with pytest.raises(PlannerError, match="usage limit"):
        await make_plan("x", adapter, tmp_path)
    assert len(adapter.calls) == 1


def test_read_manifests(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}))
    (tmp_path / "README.md").write_text("x" * 10_000)
    found = read_manifests(tmp_path)
    assert set(found) == {"package.json", "README.md"}
    assert found["README.md"].endswith("(recortado)")
