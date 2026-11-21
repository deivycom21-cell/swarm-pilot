import asyncio

import httpx
import pytest
import websockets

from swarm_pilot.config import ProviderSpec, load_settings
from swarm_pilot.core.models import EventType
from swarm_pilot.runtime import Session


@pytest.fixture
async def session(tmp_path):
    settings = load_settings(
        tmp_path,
        env={"SWARM_CONFIG": str(tmp_path / "none.toml")},
        autodetect=lambda: ProviderSpec.parse("fake"),
        overrides={"dashboard_port": 4610},
    )
    s = Session(settings, ui=True, preview=False, env={})
    await s.start_services()
    yield s
    await s.close()


async def test_index_injects_token_and_sets_headers(session):
    async with httpx.AsyncClient(base_url=session.web.url) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert session.web.token in r.text and "__SWARM_TOKEN__" not in r.text
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


async def test_rejects_foreign_host_header(session):
    async with httpx.AsyncClient(base_url=session.web.url) as c:
        r = await c.get("/api/state", headers={"Host": "evil.example:4610"})
    assert r.status_code == 403


async def test_actions_require_token(session):
    async with httpx.AsyncClient(base_url=session.web.url) as c:
        no_token = await c.post("/api/approve", json={"approve": True})
        assert no_token.status_code == 403
        headers = {"X-Swarm-Token": session.web.token}
        nothing_to_approve = await c.post("/api/approve", json={"approve": True}, headers=headers)
        assert nothing_to_approve.status_code == 409
        no_run = await c.post(
            "/api/answer", json={"task_id": "T1", "action": "skip"}, headers=headers
        )
        assert no_run.status_code == 400


async def test_approve_through_api_resolves_wait(session):
    await session.make_plan("Crea una landing")
    waiter = asyncio.create_task(session.wait_approval())
    await asyncio.sleep(0.05)
    async with httpx.AsyncClient(base_url=session.web.url) as c:
        r = await c.post(
            "/api/approve", json={"approve": True}, headers={"X-Swarm-Token": session.web.token}
        )
    assert r.status_code == 200
    assert await asyncio.wait_for(waiter, 5) is True


async def test_state_snapshot(session):
    await session.make_plan("Crea una landing")
    async with httpx.AsyncClient(base_url=session.web.url) as c:
        data = (await c.get("/api/state")).json()
    assert data["goal"] == "Crea una landing"
    assert [t["id"] for t in data["plan"]["tasks"]] == ["T1", "T2", "T3"]
    assert data["providers"]["planner"] == "fake"
    assert any(e["payload"].get("source") == "planner" for e in data["history"])


async def test_websocket_streams_snapshot_then_events(session):
    url = session.web.url.replace("http", "ws") + "ws"
    async with websockets.connect(url, origin=session.web.url.rstrip("/")) as ws:
        first = await asyncio.wait_for(ws.recv(), 5)
        assert '"type":"snapshot"' in first.replace(" ", "")
        session.bus.emit(EventType.AGENT_LOG, task_id="T1", line="hola panel")
        msg = await asyncio.wait_for(ws.recv(), 5)
        assert "hola panel" in msg


async def test_websocket_rejects_foreign_origin(session):
    url = session.web.url.replace("http", "ws") + "ws"
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(url, origin="https://evil.example") as ws:
            await ws.recv()


async def test_full_run_through_session(session, tmp_path):
    await session.make_plan("Crea una landing")
    summary = await session.execute()
    assert summary.outcome.value == "completed"
    assert (tmp_path / "index.html").exists()
    assert session.snapshot()["summary"]["outcome"] == "completed"
