"""Adapter Anthropic API con el SDK oficial (`pip install 'swarm-pilot[anthropic]'`).

- Modelo por defecto `claude-opus-5-5` (o `anthropic:<modelo>` / `ANTHROPIC_MODEL`).
- Pensamiento adaptativo (por defecto en los modelos actuales) y `effort` explícito
  (`SWARM_ANTHROPIC_EFFORT`, por defecto `high`: tareas de código).
- Streaming con `get_final_message()` para no chocar con timeouts en respuestas largas.
- Salida estructurada con `output_config.format` cuando hay JSON Schema.
- Comprueba `stop_reason` (`refusal`, `max_tokens`) antes de leer el contenido.
- Fallbacks del servidor ante rechazos (`fallbacks: "default"`) en los modelos que los
  admiten; se desactivan con `SWARM_ANTHROPIC_FALLBACKS=0`.
- Credenciales: las resuelve el SDK (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` o un
  perfil de `ant auth login`).
"""

from __future__ import annotations

import json
from typing import Any

from swarm_pilot.adapters.base import (
    AdapterContext,
    AdapterError,
    GenerateRequest,
    GenerateResult,
    LineBuffer,
    LLMAdapter,
    LogFn,
    extract_json,
    schema_instructions,
)
from swarm_pilot.config import ProviderSpec

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
#: Modelos con fallbacks del servidor (forma "default").
FALLBACK_MODELS = frozenset({
    "claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5",
})  # fmt: skip
#: Familias que aceptan `output_config` (effort + format).
MODERN_PREFIXES = (
    "claude-opus-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-fable", "claude-mythos", "claude-haiku-5",
)  # fmt: skip
#: $ por millón de tokens (entrada, salida), para el coste estimado del panel.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-2": (8.0, 40.0),
}  # fmt: skip


class AnthropicAdapter(LLMAdapter):
    agentic = False

    def __init__(self, spec: ProviderSpec, ctx: AdapterContext) -> None:
        super().__init__(spec, ctx)
        env = ctx.env
        self.model = spec.model or env.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.effort = env.get("SWARM_ANTHROPIC_EFFORT", "high")
        self.modern = self.model.startswith(MODERN_PREFIXES)
        self.use_fallbacks = (
            env.get("SWARM_ANTHROPIC_FALLBACKS", "1") != "0"
            and self.model in FALLBACK_MODELS
            and not env.get("ANTHROPIC_BASE_URL")  # proxies / plataformas sin el parámetro
        )
        self._client: Any = None

    @property
    def label(self) -> str:
        return f"anthropic:{self.model}"

    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AdapterError(
                    "falta el SDK de Anthropic: pip install 'swarm-pilot[anthropic]'",
                    retryable=False,
                ) from exc
            key = self.ctx.env.get("ANTHROPIC_API_KEY")
            kwargs: dict[str, Any] = {"max_retries": 3}
            if key:
                kwargs["api_key"] = key
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    def build_kwargs(self, req: GenerateRequest) -> dict[str, Any]:
        system = req.system
        output_config: dict[str, Any] = {}
        if self.modern:
            output_config["effort"] = self.effort
        if req.json_schema is not None:
            if self.modern:
                output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
            else:
                system += schema_instructions(req.json_schema)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if system:
            kwargs["system"] = system
        if output_config:
            kwargs["output_config"] = output_config
        if req.timeout:
            kwargs["timeout"] = req.timeout
        return kwargs

    async def generate(self, req: GenerateRequest, on_log: LogFn | None = None) -> GenerateResult:
        import anthropic

        client = self.client()
        kwargs = self.build_kwargs(req)
        buf = LineBuffer(on_log if req.json_schema is None else None)
        if on_log:
            on_log(f"llamando a {self.label} (effort {self.effort})")
        try:
            if self.use_fallbacks:
                stream_ctx = client.beta.messages.stream(
                    **kwargs, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                stream_ctx = client.messages.stream(**kwargs)
            async with stream_ctx as stream:
                async for text in stream.text_stream:
                    buf.feed(text)
                final = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise AdapterError(f"límite de uso de la API: {exc}", rate_limited=True) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AdapterError(
                f"credenciales de Anthropic rechazadas: {exc}", retryable=False
            ) from exc
        except (anthropic.NotFoundError, anthropic.BadRequestError) as exc:
            raise AdapterError(f"petición rechazada por la API: {exc}", retryable=False) from exc
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise AdapterError(f"error de la API de Anthropic: {exc}") from exc
        buf.flush()

        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AdapterError(
                f"el modelo rechazó la petición (categoría: {category or 'n/d'})", retryable=False
            )
        if final.stop_reason == "max_tokens":
            raise AdapterError("respuesta truncada al llegar a max_tokens")
        text = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")
        data = None
        if req.json_schema is not None:
            try:
                data = json.loads(text)
            except ValueError:
                data = extract_json(text)
        usage = final.usage
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        price = PRICES.get(getattr(final, "model", self.model)) or PRICES.get(self.model)
        cost = (in_tok * price[0] + out_tok * price[1]) / 1e6 if price else None
        return GenerateResult(
            text=text, data=data, cost_usd=cost,
            usage={"input_tokens": in_tok, "output_tokens": out_tok},
        )  # fmt: skip

    async def check(self) -> str:
        import anthropic

        try:
            info = await self.client().models.retrieve(self.model)
        except anthropic.AuthenticationError as exc:
            raise AdapterError("sin credenciales válidas de Anthropic", retryable=False) from exc
        except anthropic.NotFoundError as exc:
            raise AdapterError(f"modelo desconocido: {self.model}", retryable=False) from exc
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise AdapterError(f"no se pudo contactar la API: {exc}") from exc
        return f"{self.label} ({getattr(info, 'display_name', self.model)})"
