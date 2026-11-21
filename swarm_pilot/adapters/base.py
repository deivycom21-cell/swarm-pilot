"""Protocolo común de los adapters LLM.

Todos los proveedores exponen una sola operación, `generate()`. Hay dos familias:

- **agénticos** (`agentic = True`, p. ej. `claude -p`): reciben `cwd` y un `mode`
  ("read" / "edit") y leen o editan archivos ellos mismos con sus herramientas.
- **de completado** (APIs): solo devuelven texto o JSON; quien llama aplica los cambios.

Si la petición trae `json_schema`, el resultado lleva el objeto ya parseado en `data`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from swarm_pilot.config import ProviderSpec

LogFn = Callable[[str], None]
Mode = Literal["none", "read", "edit"]


class AdapterError(RuntimeError):
    """Fallo del proveedor. `rate_limited` indica límite de uso (no tiene sentido reintentar ya)."""

    def __init__(self, message: str, *, retryable: bool = True, rate_limited: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


@dataclass
class GenerateRequest:
    prompt: str
    system: str = ""
    mode: Mode = "none"
    cwd: Path | None = None
    json_schema: dict[str, Any] | None = None
    timeout: float | None = None
    max_tokens: int = 32_000
    #: Contexto libre para logs y para el adapter falso (rol, task_id, intento...).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerateResult:
    text: str
    data: Any = None
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterContext:
    """Dependencias que algunos adapters necesitan (procesos, entorno, permisos)."""

    process_manager: Any = None  # ProcessManager; Any para evitar import circular
    env: Mapping[str, str] = field(default_factory=dict)
    skip_permissions: bool = False
    scratch_dir: Path | None = None


class LLMAdapter(ABC):
    #: Si True, el adapter edita archivos por su cuenta en modo "edit".
    agentic: bool = False

    def __init__(self, spec: ProviderSpec, ctx: AdapterContext) -> None:
        self.spec = spec
        self.ctx = ctx

    @property
    def label(self) -> str:
        return str(self.spec)

    @abstractmethod
    async def generate(self, req: GenerateRequest, on_log: LogFn | None = None) -> GenerateResult:
        """Ejecuta la petición. Lanza `AdapterError` ante fallos del proveedor."""

    async def check(self) -> str:
        """Comprobación rápida para `doctor`. Devuelve un texto descriptivo o lanza AdapterError."""
        return self.label


# --- utilidades para adapters de completado -----------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Saca un objeto JSON de una respuesta de modelo, tolerando cercas ``` y prosa alrededor."""
    text = text.strip()
    candidates = [text]
    candidates += [m.group(1) for m in _FENCE_RE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            continue
    raise AdapterError("la respuesta del modelo no contiene JSON válido")


def schema_instructions(schema: dict[str, Any]) -> str:
    """Texto para el system prompt de modelos sin salida estructurada nativa."""
    return (
        "\n\nRespond ONLY with a single JSON object (no prose, no markdown fences) that "
        "validates against this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    )


class LineBuffer:
    """Convierte un flujo de fragmentos de texto en líneas completas para `on_log`."""

    def __init__(self, on_log: LogFn | None) -> None:
        self.on_log = on_log
        self._buf = ""

    def feed(self, chunk: str) -> None:
        if self.on_log is None:
            return
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.on_log(line)

    def flush(self) -> None:
        if self.on_log is not None and self._buf.strip():
            self.on_log(self._buf)
        self._buf = ""
