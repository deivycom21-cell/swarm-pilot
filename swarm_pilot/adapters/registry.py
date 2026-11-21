"""Resuelve un `ProviderSpec` ("proveedor:modelo") a una instancia de adapter."""

from __future__ import annotations

from swarm_pilot.adapters.base import AdapterContext, LLMAdapter
from swarm_pilot.config import ConfigError, ProviderSpec


def create_adapter(spec: ProviderSpec, ctx: AdapterContext) -> LLMAdapter:
    # Imports perezosos: cada adapter carga solo sus dependencias.
    if spec.provider == "claude-cli":
        from swarm_pilot.adapters.claude_cli import ClaudeCLIAdapter

        return ClaudeCLIAdapter(spec, ctx)
    if spec.provider == "anthropic":
        from swarm_pilot.adapters.anthropic_api import AnthropicAdapter

        return AnthropicAdapter(spec, ctx)
    if spec.provider in ("openai", "openrouter", "ollama"):
        from swarm_pilot.adapters.openai_compat import OpenAICompatAdapter

        return OpenAICompatAdapter(spec, ctx)
    if spec.provider == "fake":
        from swarm_pilot.adapters.fake import FakeAdapter

        return FakeAdapter.from_spec(spec, ctx)
    raise ConfigError(f"proveedor sin adapter: {spec.provider!r}")


class AdapterPool:
    """Una instancia por proveedor distinto, compartida entre roles."""

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self._cache: dict[ProviderSpec, LLMAdapter] = {}

    def get(self, spec: ProviderSpec) -> LLMAdapter:
        if spec not in self._cache:
            self._cache[spec] = create_adapter(spec, self.ctx)
        return self._cache[spec]
