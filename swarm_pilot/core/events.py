"""EventBus asyncio: pub/sub de eventos del run + historial + log JSONL.

- `publish()` nunca bloquea: si un suscriptor va lento se descartan sus eventos más
  antiguos (el orquestador no debe frenarse por un navegador lento).
- Todos los secretos conocidos se ocultan *antes* de guardar o difundir el evento.
- El historial en memoria permite enviar un snapshot a clientes que se conectan tarde.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from swarm_pilot.core.models import Event, EventType

REDACTED = "***"


def redact(value: Any, secrets: Iterable[str]) -> Any:
    """Sustituye recursivamente cada secreto dentro de strings, listas y dicts."""
    secrets = [s for s in secrets if s]
    if not secrets:
        return value
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, secrets) for v in value)
    return value


_CLOSED = object()


class Subscription:
    """Cola de eventos de un suscriptor. Úsala con `async for` o `async with`."""

    def __init__(self, bus: EventBus, maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _offer(self, item: Any) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - carrera imposible en un hilo
                    pass

    async def get(self) -> Event | None:
        """Siguiente evento, o None si el bus se cerró."""
        item = await self._queue.get()
        return None if item is _CLOSED else item

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while (ev := await self.get()) is not None:
            yield ev

    def close(self) -> None:
        self._bus._unsubscribe(self)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(
        self,
        run_id: str | None = None,
        *,
        log_path: Path | None = None,
        secrets: Iterable[str] = (),
        history_limit: int = 5000,
    ) -> None:
        self.run_id = run_id
        self._secrets = [s for s in secrets if s]
        self._subs: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history_limit)
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open("a", encoding="utf-8")

    # --- publicación --------------------------------------------------------

    def publish(self, event: Event) -> Event:
        """Publica desde el hilo del event loop. Devuelve el evento ya redactado."""
        if self._closed:
            return event
        if self._loop is None:
            self._loop = _running_loop()
        if event.run_id is None and self.run_id is not None:
            event = event.model_copy(update={"run_id": self.run_id})
        if self._secrets:
            event = event.model_copy(update={"payload": redact(event.payload, self._secrets)})
        self._history.append(event)
        if self._log is not None:
            self._log.write(event.model_dump_json() + "\n")
            self._log.flush()
        for sub in list(self._subs):
            sub._offer(event)
        return event

    def emit(self, type: EventType, task_id: str | None = None, **payload: Any) -> Event:
        return self.publish(Event(type=type, task_id=task_id, payload=payload))

    def publish_threadsafe(self, event: Event) -> None:
        """Publica desde otro hilo (p. ej. un callback de una librería síncrona)."""
        loop = self._loop
        if loop is None or _running_loop() is loop:
            self.publish(event)
        else:
            loop.call_soon_threadsafe(self.publish, event)

    # --- suscripción --------------------------------------------------------

    def subscribe(self, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, maxsize)
        if self._closed:
            sub._offer(_CLOSED)
        else:
            self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def history(self, task_id: str | None = None) -> list[Event]:
        if task_id is None:
            return list(self._history)
        return [e for e in self._history if e.task_id == task_id]

    # --- cierre -------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in self._subs:
            sub._offer(_CLOSED)
        self._subs.clear()
        if self._log is not None:
            self._log.close()
            self._log = None

    @property
    def closed(self) -> bool:
        return self._closed


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
