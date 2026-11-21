import asyncio
import json
import threading

from swarm_pilot.core.events import EventBus, redact
from swarm_pilot.core.models import EventType

SECRET = "sk-test-0123456789"


def test_redact_nested():
    data = {"a": f"key={SECRET}", "b": [SECRET, 1, {"c": (SECRET,)}]}
    assert redact(data, [SECRET]) == {"a": "key=***", "b": ["***", 1, {"c": ("***",)}]}
    assert redact("x", []) == "x"


async def test_publish_reaches_all_subscribers_in_order():
    bus = EventBus("run1")
    a, b = bus.subscribe(), bus.subscribe()
    bus.emit(EventType.RUN_STARTED, goal="g")
    bus.emit(EventType.TASK_STATUS, task_id="T1", status="running")
    for sub in (a, b):
        first, second = await sub.get(), await sub.get()
        assert first.type is EventType.RUN_STARTED and first.run_id == "run1"
        assert second.task_id == "T1" and second.payload == {"status": "running"}


async def test_close_ends_async_iteration():
    bus = EventBus()
    sub = bus.subscribe()
    bus.emit(EventType.AGENT_LOG, line="hola")
    bus.close()
    received = [ev async for ev in sub]
    assert [e.payload["line"] for e in received] == ["hola"]
    assert bus.emit(EventType.AGENT_LOG, line="tarde")  # publicar tras cerrar no falla
    late = bus.subscribe()
    assert await late.get() is None


async def test_slow_subscriber_drops_oldest_without_blocking():
    bus = EventBus()
    sub = bus.subscribe(maxsize=3)
    for i in range(10):
        bus.emit(EventType.AGENT_LOG, n=i)
    assert sub.dropped == 7
    assert [(await sub.get()).payload["n"] for _ in range(3)] == [7, 8, 9]


async def test_unsubscribe_via_context_manager():
    bus = EventBus()
    async with bus.subscribe() as sub:
        pass
    bus.emit(EventType.AGENT_LOG, line="x")
    assert sub._queue.empty()


async def test_secrets_are_redacted_everywhere(tmp_path):
    log = tmp_path / "runs" / "r1" / "events.jsonl"
    bus = EventBus("r1", log_path=log, secrets=[SECRET, ""])
    sub = bus.subscribe()
    bus.emit(EventType.AGENT_LOG, task_id="T1", line=f"Authorization: Bearer {SECRET}")
    bus.close()
    assert SECRET not in (await sub.get()).payload["line"]
    assert SECRET not in bus.history()[0].payload["line"]
    text = log.read_text(encoding="utf-8")
    assert SECRET not in text
    assert json.loads(text.splitlines()[0])["payload"]["line"] == "Authorization: Bearer ***"


async def test_history_filter_by_task():
    bus = EventBus()
    bus.emit(EventType.AGENT_LOG, task_id="A", line="1")
    bus.emit(EventType.AGENT_LOG, task_id="B", line="2")
    assert [e.payload["line"] for e in bus.history("B")] == ["2"]
    assert len(bus.history()) == 2


async def test_publish_threadsafe_from_worker_thread():
    bus = EventBus()
    sub = bus.subscribe()
    bus.emit(EventType.RUN_STARTED)  # fija el loop del bus
    await sub.get()

    from swarm_pilot.core.models import Event

    t = threading.Thread(
        target=bus.publish_threadsafe, args=(Event(type=EventType.AGENT_LOG, payload={"x": 1}),)
    )
    t.start()
    t.join()
    ev = await asyncio.wait_for(sub.get(), timeout=2)
    assert ev.payload == {"x": 1}
