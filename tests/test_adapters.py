import json
import sys
from pathlib import Path

import pytest

from swarm_pilot.adapters.base import (
    AdapterContext,
    AdapterError,
    GenerateRequest,
    LineBuffer,
    extract_json,
)
from swarm_pilot.adapters.claude_cli import ClaudeCLIAdapter
from swarm_pilot.adapters.fake import FakeAdapter
from swarm_pilot.adapters.registry import AdapterPool, create_adapter
from swarm_pilot.config import ConfigError, ProviderSpec
from swarm_pilot.core.process_manager import ProcessManager

FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude.py"))


# --- utilidades ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'Claro, aquí está:\n```json\n{"a": 1}\n```\n',
        'prefijo {"a": 1} sufijo',
    ],
)
def test_extract_json(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_fails():
    with pytest.raises(AdapterError):
        extract_json("nada de json")


def test_line_buffer():
    out = []
    buf = LineBuffer(out.append)
    for chunk in ["hola ", "mun", "do\nsegunda\n\n", "tercera"]:
        buf.feed(chunk)
    buf.flush()
    assert out == ["hola mundo", "segunda", "tercera"]


# --- registro --------------------------------------------------------------------


def test_registry_creates_and_caches():
    pool = AdapterPool(AdapterContext())
    a = pool.get(ProviderSpec.parse("fake"))
    assert isinstance(a, FakeAdapter) and pool.get(ProviderSpec.parse("fake")) is a
    assert isinstance(
        create_adapter(ProviderSpec("claude-cli"), AdapterContext()), ClaudeCLIAdapter
    )
    with pytest.raises(ConfigError):
        create_adapter(ProviderSpec("nope"), AdapterContext())


async def test_fake_agentic_writes_files(tmp_path):
    a = create_adapter(ProviderSpec.parse("fake:landing-agentic"), AdapterContext())
    res = await a.generate(
        GenerateRequest("x", mode="edit", cwd=tmp_path, meta={"role": "architect", "task_id": "T1"})
    )
    assert (tmp_path / "index.html").exists()
    assert res.data is None and a.agentic


# --- claude-cli con simulador ---------------------------------------------------


@pytest.fixture
async def claude(tmp_path):
    pm = ProcessManager(grace=1)
    record = tmp_path / "record.json"

    def make(mode="ok", **env_extra):
        env = {
            **__import__("os").environ,
            "FAKE_CLAUDE_MODE": mode,
            "FAKE_CLAUDE_RECORD": str(record),
            **env_extra,
        }
        ctx = AdapterContext(process_manager=pm, env=env, scratch_dir=tmp_path / "scratch")
        adapter = ClaudeCLIAdapter(ProviderSpec.parse("claude-cli:sonnet"), ctx)
        adapter.command = [sys.executable, FAKE_CLAUDE]
        return adapter

    make.record = record
    yield make
    await pm.shutdown()


async def test_claude_cli_edit_mode(claude, tmp_path):
    adapter = claude(ANTHROPIC_API_KEY="sk-ant-secreta-123")
    logs = []
    res = await adapter.generate(
        GenerateRequest("Crea a.txt", system="Eres un coder.", mode="edit", cwd=tmp_path),
        logs.append,
    )
    assert res.text == "Listo." and res.cost_usd == 0.01 and res.data is None
    rec = json.loads(claude.record.read_text(encoding="utf-8"))
    argv = rec["argv"]
    assert rec["prompt"] == "Crea a.txt" and rec["system"] == "Eres un coder."
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--dangerously-skip-permissions" not in argv and "--strict-mcp-config" in argv
    assert not rec["has_api_key"], "la API key no debe llegar a claude (usa la suscripción)"
    assert logs[0].startswith("sesión claude iniciada")
    assert "🔧 Write a.txt" in logs
    assert any(line.startswith("⚠ permiso denegado") for line in logs)
    assert "💬 Dos líneas." in logs
    assert not list((tmp_path / "scratch").iterdir()), "el system prompt temporal no se borró"


async def test_claude_cli_read_mode_with_schema(claude, tmp_path):
    res = await claude().generate(
        GenerateRequest("plan", mode="read", cwd=tmp_path, json_schema={"type": "object"})
    )
    assert res.data == {"ok": True}
    argv = json.loads(claude.record.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}


async def test_claude_cli_skip_permissions_opt_in(claude, tmp_path):
    adapter = claude()
    adapter.ctx.skip_permissions = True
    args = adapter.build_args(GenerateRequest("x", mode="edit"), None)
    assert "--dangerously-skip-permissions" in args and "--tools" not in args


@pytest.mark.parametrize(
    "mode, rate_limited, match",
    [
        ("error", False, "algo salió mal"),
        ("ratelimit", True, "usage limit"),
        ("crash", False, "explotó"),
    ],
)
async def test_claude_cli_errors(claude, tmp_path, mode, rate_limited, match):
    with pytest.raises(AdapterError, match=match) as exc:
        await claude(mode).generate(GenerateRequest("x", cwd=tmp_path))
    assert exc.value.rate_limited is rate_limited


async def test_claude_cli_timeout_kills_process(claude, tmp_path):
    adapter = claude("hang")
    with pytest.raises(AdapterError, match="tiempo límite"):
        await adapter.generate(GenerateRequest("x", cwd=tmp_path, timeout=2))
    assert adapter.ctx.process_manager.processes == []


async def test_claude_cli_missing_binary(claude, tmp_path):
    adapter = claude()
    adapter.command = ["claude-que-no-existe-xyz"]
    with pytest.raises(AdapterError, match="PATH") as exc:
        await adapter.generate(GenerateRequest("x", system="s", cwd=tmp_path))
    assert not exc.value.retryable


async def test_claude_cli_check(claude):
    assert "9.9.9" in await claude().check()
