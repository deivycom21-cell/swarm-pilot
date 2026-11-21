import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.client.client import Client  # noqa: E402

from swarm_pilot import mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
async def fake_providers(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("SWARM_PLANNER", "fake")
    monkeypatch.setenv("SWARM_WORKER", "fake")
    yield
    await mcp_server.stop_all()  # por si un test deja un run vivo


def payload(result):
    if getattr(result, "structured_content", None):
        data = result.structured_content
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def test_lists_tools():
    async with Client(mcp_server.server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"swarm_plan", "swarm_run", "swarm_status", "swarm_answer", "swarm_stop"}


async def test_plan_then_run_until_done(tmp_path):
    project = tmp_path / "landing"
    async with Client(mcp_server.server) as client:
        plan = payload(await client.call_tool("swarm_plan", {"goal": "Crea una landing",
                                                              "path": str(project)}))  # fmt: skip
        assert plan["ok"] and plan["tasks"] == 3 and "### [ ] T1" in plan["plan_md"]

        run = payload(await client.call_tool("swarm_run", {"path": str(project),
                                                            "dashboard": False, "preview": False}))  # fmt: skip
        assert run["ok"] and run["dashboard_url"] is None
        again = payload(
            await client.call_tool("swarm_run", {"path": str(project), "dashboard": False})
        )
        assert again.get("already_running") or again["run_id"] != run["run_id"]

        status = {}
        for _ in range(100):
            status = payload(await client.call_tool("swarm_status", {"run_id": run["run_id"]}))
            if not status["running"]:
                break
            await asyncio.sleep(0.1)
        assert status["summary"]["outcome"] == "completed"
        assert {t["status"] for t in status["tasks"]} == {"done"}
        assert status["recent_logs"]

        bad = payload(await client.call_tool("swarm_answer", {"run_id": run["run_id"],
                                                               "task_id": "T1", "action": "skip"}))  # fmt: skip
        assert not bad["ok"] and "no está bloqueada" in bad["error"]
        stop = payload(await client.call_tool("swarm_stop", {"run_id": run["run_id"]}))
        assert stop["ok"] and stop["unfinished_tasks"] == []
    assert (project / "index.html").exists()


async def test_run_without_plan_reports_error(tmp_path):
    async with Client(mcp_server.server) as client:
        res = payload(await client.call_tool("swarm_run", {"path": str(tmp_path / "vacio"),
                                                            "dashboard": False, "preview": False}))  # fmt: skip
        assert not res["ok"] and "swarm_plan" in res["error"]
        status = payload(await client.call_tool("swarm_status", {"run_id": "nope"}))
        assert not status["ok"]
