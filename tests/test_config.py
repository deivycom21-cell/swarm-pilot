from pathlib import Path

import pytest

from swarm_pilot.config import (
    ConfigError,
    ProviderSpec,
    detect_default_provider,
    load_settings,
    secret_values,
    user_config_path,
)

NO_DETECT = lambda: None  # noqa: E731


@pytest.fixture
def env(tmp_path):
    # Config de usuario apuntando a un archivo inexistente: aísla del equipo real.
    return {"SWARM_CONFIG": str(tmp_path / "no-user-config.toml")}


@pytest.mark.parametrize(
    "raw, provider, model",
    [
        ("claude-cli", "claude-cli", None),
        ("ollama:qwen2.5-coder:14b", "ollama", "qwen2.5-coder:14b"),
        (" OpenRouter : vendor/model ", "openrouter", "vendor/model"),
        ("anthropic:", "anthropic", None),
    ],
)
def test_provider_spec_parse(raw, provider, model):
    spec = ProviderSpec.parse(raw)
    assert (spec.provider, spec.model) == (provider, model)


def test_provider_spec_rejects_unknown():
    with pytest.raises(ConfigError, match="desconocido"):
        ProviderSpec.parse("gemini:pro")


def test_defaults_with_autodetect(tmp_path, env):
    s = load_settings(tmp_path, env=env, autodetect=lambda: ProviderSpec("claude-cli"))
    assert s.max_parallel == 2 and s.max_attempts == 3 and s.require_approval
    assert all(str(s.provider_for(r)) == "claude-cli" for r in ("planner", "coder"))
    assert s.provider_sources["planner"] == "autodetect"


def test_missing_provider_raises_helpful_error(tmp_path, env):
    s = load_settings(tmp_path, env=env, autodetect=NO_DETECT)
    with pytest.raises(ConfigError, match="doctor"):
        s.provider_for("planner")


def test_precedence_user_project_env_cli(tmp_path, env):
    user = Path(env["SWARM_CONFIG"])
    user.write_text('[providers]\nworker = "ollama"\nplanner = "ollama"\n[run]\nmax_parallel = 4\n')
    (tmp_path / "swarm.toml").write_text(
        '[providers]\nplanner = "claude-cli"\ncoder = "openrouter:vendor/m"\n'
        "[run]\nmax_attempts = 2\n[ui]\ndashboard_port = 4100\n"
    )
    env = {**env, "SWARM_TESTER": "anthropic:claude-haiku-5-5", "SWARM_MAX_PARALLEL": "3"}
    s = load_settings(
        tmp_path,
        env=env,
        overrides={"provider.coder": "openai:gpt-x", "max_attempts": None},
        autodetect=NO_DETECT,
    )
    assert str(s.providers["planner"]) == "claude-cli"  # proyecto > usuario
    assert str(s.providers["architect"]) == "ollama"  # worker del usuario
    assert str(s.providers["coder"]) == "openai:gpt-x"  # cli > proyecto
    assert str(s.providers["tester"]) == "anthropic:claude-haiku-5-5"  # env
    assert s.max_parallel == 3  # env > usuario
    assert s.max_attempts == 2  # override None no pisa
    assert s.dashboard_port == 4100
    assert s.provider_sources == {
        "planner": "project",
        "architect": "user",
        "coder": "cli",
        "tester": "env",
    }


def test_swarm_worker_env_and_role_override(tmp_path, env):
    env = {**env, "SWARM_WORKER": "ollama", "SWARM_CODER": "openai"}
    s = load_settings(tmp_path, env=env, autodetect=lambda: ProviderSpec("claude-cli"))
    assert str(s.providers["architect"]) == "ollama"
    assert str(s.providers["coder"]) == "openai"
    assert str(s.providers["planner"]) == "claude-cli"  # sin configurar → autodetección


def test_invalid_values(tmp_path, env):
    with pytest.raises(ConfigError):
        load_settings(tmp_path, env={**env, "SWARM_MAX_PARALLEL": "muchos"}, autodetect=NO_DETECT)
    with pytest.raises(ConfigError):
        load_settings(tmp_path, env=env, overrides={"max_attempts": 0}, autodetect=NO_DETECT)
    (tmp_path / "swarm.toml").write_text("[providers\n")
    with pytest.raises(ConfigError, match="TOML"):
        load_settings(tmp_path, env=env, autodetect=NO_DETECT)


def test_detect_default_provider_order():
    no_ollama = lambda host: False  # noqa: E731
    found = lambda name: "/bin/claude" if name == "claude" else None  # noqa: E731
    missing = lambda name: None  # noqa: E731
    assert detect_default_provider({}, found, no_ollama) == ProviderSpec("claude-cli")
    keys = {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}
    assert detect_default_provider(keys, missing, no_ollama) == ProviderSpec("anthropic")
    assert detect_default_provider({"OPENROUTER_API_KEY": "x"}, missing, no_ollama).provider == (
        "openrouter"
    )
    assert detect_default_provider({}, missing, lambda h: True) == ProviderSpec("ollama")
    assert detect_default_provider({}, missing, no_ollama) is None


def test_user_config_path_override():
    assert user_config_path({"SWARM_CONFIG": "/x/c.toml"}) == Path("/x/c.toml")


def test_secret_values_skips_short_or_missing():
    env = {"ANTHROPIC_API_KEY": "sk-ant-1234567890", "OPENAI_API_KEY": "short"}
    assert secret_values(env) == ["sk-ant-1234567890"]
