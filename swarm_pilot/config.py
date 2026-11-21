"""Configuración: swarm.toml + variables de entorno → Settings.

Prioridad (de menor a mayor):
    autodetección < config de usuario < ./swarm.toml < variables de entorno < flags de CLI

Las claves de API nunca se guardan en Settings: los adapters las leen del entorno.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PROJECT_CONFIG = "swarm.toml"

PROVIDERS = ("claude-cli", "anthropic", "openai", "openrouter", "ollama")
ROLES = ("planner", "architect", "coder", "tester")
WORKER_ROLES = ("architect", "coder", "tester")
#: Proveedores no documentados: `fake[:escenario]` sirve para tests y demos sin LLM.
HIDDEN_PROVIDERS = ("fake",)

#: Variables de entorno cuyo valor es secreto (se ocultan en logs y estado).
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")

DEFAULT_OLLAMA_HOST = "http://localhost:11434"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    """Proveedor y modelo opcional. `"ollama:qwen2.5-coder:14b"` → ollama / qwen2.5-coder:14b."""

    provider: str
    model: str | None = None

    @classmethod
    def parse(cls, raw: str) -> ProviderSpec:
        raw = raw.strip()
        name, sep, model = raw.partition(":")
        name = name.strip().lower()
        if name not in PROVIDERS and name not in HIDDEN_PROVIDERS:
            raise ConfigError(
                f"proveedor desconocido {name!r} en {raw!r}; opciones: {', '.join(PROVIDERS)}"
            )
        model = model.strip() if sep else ""
        return cls(name, model or None)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


class Settings(BaseModel):
    project_dir: Path
    providers: dict[str, ProviderSpec | None] = Field(default_factory=lambda: dict.fromkeys(ROLES))
    #: De qué capa salió cada proveedor ("autodetect", "env", ...), útil para `doctor`.
    provider_sources: dict[str, str] = Field(default_factory=dict)
    max_parallel: int = Field(default=2, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=10)
    require_approval: bool = True
    dashboard_port: int = Field(default=4000, ge=1, le=65535)
    preview_port: int = Field(default=3000, ge=1, le=65535)
    claude_skip_permissions: bool = False
    agent_timeout: float = Field(default=1800, gt=0)
    verify_timeout: float = Field(default=300, gt=0)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def state_dir(self) -> Path:
        return self.project_dir / ".swarm"

    def provider_for(self, role: str) -> ProviderSpec:
        if role not in ROLES:
            raise ConfigError(f"rol desconocido: {role!r}")
        spec = self.providers.get(role)
        if spec is None:
            raise ConfigError(
                f"No hay proveedor para el rol '{role}'. Instala Claude Code, define "
                "ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY, arranca Ollama, "
                "o configura swarm.toml. Ejecuta `swarm-pilot doctor` para más detalles."
            )
        return spec


# --- capas -----------------------------------------------------------------


def user_config_path(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("SWARM_CONFIG"):
        return Path(env["SWARM_CONFIG"])
    if sys.platform == "win32" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "swarm-pilot" / "config.toml"
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "swarm-pilot" / "config.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML inválido: {exc}") from exc


def _layer_from_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Convierte el TOML a un dict plano de claves de Settings."""
    out: dict[str, Any] = {}
    providers = data.get("providers", {})
    if "worker" in providers:
        for role in WORKER_ROLES:
            out[f"provider.{role}"] = providers["worker"]
    for role in ROLES:
        if role in providers:
            out[f"provider.{role}"] = providers[role]
    run = data.get("run", {})
    for key in ("max_parallel", "max_attempts", "require_approval"):
        if key in run:
            out[key] = run[key]
    ui = data.get("ui", {})
    for key in ("dashboard_port", "preview_port"):
        if key in ui:
            out[key] = ui[key]
    for key in ("agent_timeout", "verify_timeout"):
        if key in run:
            out[key] = run[key]
    if "skip_permissions" in data.get("claude", {}):
        out["claude_skip_permissions"] = data["claude"]["skip_permissions"]
    return out


def _layer_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if env.get("SWARM_WORKER"):
        for role in WORKER_ROLES:
            out[f"provider.{role}"] = env["SWARM_WORKER"]
    for role in ROLES:
        val = env.get(f"SWARM_{role.upper()}")
        if val:
            out[f"provider.{role}"] = val
    for key in ("max_parallel", "max_attempts", "dashboard_port", "preview_port"):
        val = env.get(f"SWARM_{key.upper()}")
        if val:
            try:
                out[key] = int(val)
            except ValueError as exc:
                raise ConfigError(f"SWARM_{key.upper()} debe ser un entero: {val!r}") from exc
    return out


# --- autodetección ---------------------------------------------------------


def probe_ollama(host: str, timeout: float = 0.5) -> bool:
    import httpx

    try:
        return httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def detect_default_provider(
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    ollama_probe: Callable[[str], bool] = probe_ollama,
) -> ProviderSpec | None:
    """Primer proveedor disponible, del más barato para el usuario al más caro de configurar."""
    if which("claude"):
        return ProviderSpec("claude-cli")
    if env.get("ANTHROPIC_API_KEY"):
        return ProviderSpec("anthropic")
    if env.get("OPENAI_API_KEY"):
        return ProviderSpec("openai")
    if env.get("OPENROUTER_API_KEY"):
        return ProviderSpec("openrouter")
    if ollama_probe(env.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)):
        return ProviderSpec("ollama")
    return None


# --- carga -----------------------------------------------------------------


def load_settings(
    project_dir: Path | str = ".",
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    autodetect: Callable[[], ProviderSpec | None] | None = None,
) -> Settings:
    """Carga Settings combinando todas las capas.

    `overrides` usa las mismas claves planas que las capas (p. ej. `"provider.coder"`,
    `"max_parallel"`); los valores `None` se ignoran, así la CLI puede pasar sus flags tal cual.
    """
    env = os.environ if env is None else env
    project = Path(project_dir).resolve()

    layers: list[tuple[str, dict[str, Any]]] = [
        ("user", _layer_from_toml(_read_toml(user_config_path(env)))),
        ("project", _layer_from_toml(_read_toml(project / PROJECT_CONFIG))),
        ("env", _layer_from_env(env)),
        ("cli", {k: v for k, v in (overrides or {}).items() if v is not None}),
    ]

    values: dict[str, Any] = {}
    providers: dict[str, ProviderSpec | None] = dict.fromkeys(ROLES)
    sources: dict[str, str] = {}
    for source, layer in layers:
        for key, val in layer.items():
            if key.startswith("provider."):
                role = key.split(".", 1)[1]
                if role not in ROLES:
                    raise ConfigError(f"rol desconocido en la configuración: {role!r}")
                providers[role] = val if isinstance(val, ProviderSpec) else ProviderSpec.parse(val)
                sources[role] = source
            else:
                values[key] = val

    missing = [r for r in ROLES if providers[r] is None]
    if missing:
        detect = autodetect or (lambda: detect_default_provider(env))
        found = detect()
        if found is not None:
            for role in missing:
                providers[role] = found
                sources[role] = "autodetect"

    try:
        return Settings(
            project_dir=project, providers=providers, provider_sources=sources, **values
        )
    except ValueError as exc:  # pydantic.ValidationError hereda de ValueError
        raise ConfigError(str(exc)) from exc


def secret_values(env: Mapping[str, str] = os.environ) -> list[str]:
    """Valores de secretos presentes en el entorno, para ocultarlos en logs."""
    return [env[k] for k in SECRET_ENV_VARS if len(env.get(k, "")) >= 8]
