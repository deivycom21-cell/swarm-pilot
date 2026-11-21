"""Adapter para endpoints compatibles con OpenAI: OpenAI, OpenRouter y Ollama local.

    openai:<modelo>       OPENAI_API_KEY      (OPENAI_BASE_URL para otros compatibles)
    openrouter:<modelo>   OPENROUTER_API_KEY
    ollama[:<modelo>]     OLLAMA_HOST (por defecto http://localhost:11434), sin clave

Streaming SSE con httpx; reintentos con backoff ante 429/5xx/errores de red antes de que
empiece el stream. Para JSON se pide `response_format: json_object` y además se describe
el esquema en el system prompt; si el servidor no admite `response_format`, se reintenta
sin él.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx

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
from swarm_pilot.config import DEFAULT_OLLAMA_HOST, ProviderSpec

MAX_RETRIES = 3


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str | None
    model: str | None
    headers: dict[str, str]


def resolve_endpoint(spec: ProviderSpec, env: Any) -> Endpoint:
    if spec.provider == "openai":
        return Endpoint(
            (env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
            env.get("OPENAI_API_KEY"),
            spec.model or env.get("OPENAI_MODEL"),
            {},
        )
    if spec.provider == "openrouter":
        return Endpoint(
            (env.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/"),
            env.get("OPENROUTER_API_KEY"),
            spec.model or env.get("OPENROUTER_MODEL"),
            {"X-Title": "swarm-pilot"},
        )
    if spec.provider == "ollama":
        host = (env.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
        if not host.startswith("http"):
            host = f"http://{host}"
        return Endpoint(
            f"{host}/v1", None, spec.model or env.get("OLLAMA_MODEL") or "qwen2.5-coder", {}
        )
    raise AdapterError(f"proveedor no compatible con OpenAI: {spec.provider}", retryable=False)


class OpenAICompatAdapter(LLMAdapter):
    agentic = False

    def __init__(self, spec: ProviderSpec, ctx: AdapterContext) -> None:
        super().__init__(spec, ctx)
        self.endpoint = resolve_endpoint(spec, ctx.env)
        self.json_mode = ctx.env.get("SWARM_JSON_MODE", "1") != "0"
        #: Inyectable en tests (httpx.MockTransport).
        self.transport: httpx.AsyncBaseTransport | None = None

    @property
    def label(self) -> str:
        return f"{self.spec.provider}:{self.endpoint.model or '?'}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.endpoint.headers}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        return headers

    def _validate(self) -> None:
        if self.spec.provider in ("openai", "openrouter") and not self.endpoint.api_key:
            var = "OPENAI_API_KEY" if self.spec.provider == "openai" else "OPENROUTER_API_KEY"
            raise AdapterError(f"falta {var} en el entorno", retryable=False)
        if not self.endpoint.model:
            raise AdapterError(
                f"indica el modelo: {self.spec.provider}:<modelo> en swarm.toml o la variable "
                f"{self.spec.provider.upper()}_MODEL",
                retryable=False,
            )

    def _client(self, timeout: float | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout or 600.0, connect=15.0), transport=self.transport
        )

    def build_body(self, req: GenerateRequest, json_mode: bool) -> dict[str, Any]:
        system = req.system
        if req.json_schema is not None:
            system += schema_instructions(req.json_schema)
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": req.prompt})
        body: dict[str, Any] = {"model": self.endpoint.model, "messages": messages, "stream": True}
        if req.json_schema is not None and json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def generate(self, req: GenerateRequest, on_log: LogFn | None = None) -> GenerateResult:
        self._validate()
        json_mode = self.json_mode
        if on_log:
            on_log(f"llamando a {self.label}")
        async with self._client(req.timeout) as client:
            for attempt in range(1, MAX_RETRIES + 2):
                body = self.build_body(req, json_mode)
                try:
                    return await self._stream(client, body, req, on_log)
                except _JsonModeUnsupported:
                    json_mode = False
                    continue
                except _Retryable as exc:
                    if attempt > MAX_RETRIES:
                        raise AdapterError(str(exc), rate_limited=exc.rate_limited) from exc
                    delay = exc.retry_after or 2**attempt
                    if on_log:
                        on_log(f"reintentando en {delay:.0f}s: {exc}")
                    await asyncio.sleep(delay)
        raise AdapterError("sin respuesta del proveedor")  # pragma: no cover

    async def _stream(
        self, client: httpx.AsyncClient, body: dict[str, Any], req: GenerateRequest,
        on_log: LogFn | None,
    ) -> GenerateResult:  # fmt: skip
        url = f"{self.endpoint.base_url}/chat/completions"
        buf = LineBuffer(on_log if req.json_schema is None else None)
        parts: list[str] = []
        usage: dict[str, Any] = {}
        finish = None
        try:
            async with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")[:800]
                    self._raise_for_status(resp, text, "response_format" in body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise AdapterError(f"error del proveedor: {chunk['error']}")
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content") or ""
                        if delta:
                            parts.append(delta)
                            buf.feed(delta)
                        finish = choice.get("finish_reason") or finish
                    usage = chunk.get("usage") or usage
        except httpx.TimeoutException as exc:
            raise AdapterError(f"tiempo de espera agotado con {self.label}") from exc
        except httpx.TransportError as exc:
            raise _Retryable(f"error de red con {self.label}: {exc}") from exc
        buf.flush()
        text = "".join(parts)
        if finish == "length":
            raise AdapterError("respuesta truncada por límite de longitud del modelo")
        data = extract_json(text) if req.json_schema is not None else None
        return GenerateResult(text=text, data=data, usage=usage)

    def _raise_for_status(self, resp: httpx.Response, text: str, used_json_mode: bool) -> None:
        code = resp.status_code
        if code == 400 and used_json_mode and "response_format" in text:
            raise _JsonModeUnsupported()
        if code == 429 or code >= 500:
            retry_after = resp.headers.get("retry-after")
            raise _Retryable(
                f"HTTP {code} de {self.label}: {text}",
                rate_limited=code == 429,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        raise AdapterError(f"HTTP {code} de {self.label}: {text}", retryable=False)

    async def check(self) -> str:
        self._validate()
        async with self._client(15) as client:
            try:
                resp = await client.get(f"{self.endpoint.base_url}/models", headers=self._headers())
            except httpx.HTTPError as exc:
                raise AdapterError(f"no se pudo contactar {self.endpoint.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise AdapterError(f"HTTP {resp.status_code} en /models", retryable=False)
        return self.label


class _JsonModeUnsupported(Exception):
    pass


class _Retryable(Exception):
    def __init__(self, msg: str, rate_limited: bool = False, retry_after: float | None = None):
        super().__init__(msg)
        self.rate_limited = rate_limited
        self.retry_after = retry_after
