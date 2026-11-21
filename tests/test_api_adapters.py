import json
from types import SimpleNamespace

import httpx
import pytest

from swarm_pilot.adapters import openai_compat
from swarm_pilot.adapters.anthropic_api import FALLBACK_BETA, AnthropicAdapter
from swarm_pilot.adapters.base import AdapterContext, AdapterError, GenerateRequest
from swarm_pilot.adapters.openai_compat import OpenAICompatAdapter, resolve_endpoint
from swarm_pilot.config import ProviderSpec

# --- OpenAI-compatible ----------------------------------------------------------


def sse(*contents, finish="stop"):
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": c}, "finish_reason": None}]})
        for c in contents
    ]
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}],
                                        "usage": {"total_tokens": 7}}))  # fmt: skip
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def make_openai(handler, provider="openai:gpt-test", **env):
    env = {"OPENAI_API_KEY": "sk-test", "OPENROUTER_API_KEY": "or-test", **env}
    adapter = OpenAICompatAdapter(ProviderSpec.parse(provider), AdapterContext(env=env))
    adapter.transport = httpx.MockTransport(handler)
    return adapter


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(openai_compat.asyncio, "sleep", no_sleep)


async def test_streams_text_and_logs_lines():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, text=sse("Hola ", "mundo\nsegunda", " línea"))

    logs = []
    res = await make_openai(handler).generate(GenerateRequest("hi", system="sys"), logs.append)
    assert res.text == "Hola mundo\nsegunda línea"
    assert res.usage == {"total_tokens": 7}
    assert "Hola mundo" in logs and "segunda línea" in logs
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["stream"] is True and "response_format" not in seen["body"]


async def test_json_mode_and_fallback_when_unsupported():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, text=sse('{"files": [], ', '"notes": "ok"}'))

    req = GenerateRequest("x", json_schema={"type": "object"})
    res = await make_openai(handler).generate(req)
    assert res.data == {"files": [], "notes": "ok"}
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in bodies[0]["messages"][0]["content"]
    assert "response_format" not in bodies[1]


async def test_retries_then_succeeds_and_rate_limit_surfaces():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, text=sse("ok"))

    assert (await make_openai(flaky).generate(GenerateRequest("x"))).text == "ok"

    always_429 = make_openai(lambda r: httpx.Response(429, headers={"retry-after": "1"}))
    with pytest.raises(AdapterError) as exc:
        await always_429.generate(GenerateRequest("x"))
    assert exc.value.rate_limited


@pytest.mark.parametrize("status", [401, 404])
async def test_client_errors_are_not_retried(status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, text="nope")

    with pytest.raises(AdapterError) as exc:
        await make_openai(handler).generate(GenerateRequest("x"))
    assert not exc.value.retryable and calls["n"] == 1


async def test_truncated_response_is_an_error():
    adapter = make_openai(lambda r: httpx.Response(200, text=sse("abc", finish="length")))
    with pytest.raises(AdapterError, match="truncada"):
        await adapter.generate(GenerateRequest("x"))


async def test_validation_messages():
    no_key = OpenAICompatAdapter(ProviderSpec.parse("openai:m"), AdapterContext(env={}))
    with pytest.raises(AdapterError, match="OPENAI_API_KEY"):
        await no_key.generate(GenerateRequest("x"))
    no_model = OpenAICompatAdapter(ProviderSpec.parse("openrouter"),
                                   AdapterContext(env={"OPENROUTER_API_KEY": "k"}))  # fmt: skip
    with pytest.raises(AdapterError, match="indica el modelo"):
        await no_model.generate(GenerateRequest("x"))


def test_endpoints():
    ep = resolve_endpoint(ProviderSpec.parse("ollama"), {"OLLAMA_HOST": "gpu-box:11434"})
    assert (
        ep.base_url == "http://gpu-box:11434/v1" and ep.model == "qwen2.5-coder" and not ep.api_key
    )
    ep = resolve_endpoint(
        ProviderSpec.parse("openrouter:vendor/model"), {"OPENROUTER_API_KEY": "k"}
    )
    assert ep.base_url == "https://openrouter.ai/api/v1" and ep.headers == {
        "X-Title": "swarm-pilot"
    }
    ep = resolve_endpoint(ProviderSpec.parse("openai"), {"OPENAI_BASE_URL": "http://lm:1234/v1/",
                                                         "OPENAI_MODEL": "local"})  # fmt: skip
    assert ep.base_url == "http://lm:1234/v1" and ep.model == "local"


async def test_check_lists_models():
    adapter = make_openai(lambda r: httpx.Response(200, json={"data": []}))
    assert await adapter.check() == "openai:gpt-test"


# --- Anthropic (cliente simulado) --------------------------------------------------


class FakeStream:
    """Imita el gestor de `client.messages.stream(...)` del SDK."""

    def __init__(self, text, final):
        self.text, self.final = text, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for line in self.text.splitlines(keepends=True):
                yield line

        return gen()

    async def get_final_message(self):
        return self.final


def fake_client(text, stop_reason="end_turn", model="claude-opus-5-5"):
    calls = []
    final = SimpleNamespace(
        stop_reason=stop_reason, stop_details=SimpleNamespace(category="cyber"), model=model,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000),
    )  # fmt: skip

    def stream(**kwargs):
        calls.append(kwargs)
        return FakeStream(text, final)

    client = SimpleNamespace(
        messages=SimpleNamespace(stream=stream),
        beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)),
    )
    return client, calls


def anthropic_adapter(spec="anthropic", **env):
    return AnthropicAdapter(ProviderSpec.parse(spec), AdapterContext(env=env))


async def test_anthropic_defaults_structured_output_and_fallbacks():
    adapter = anthropic_adapter()
    adapter._client, calls = fake_client('{"tasks": []}')
    res = await adapter.generate(
        GenerateRequest("plan", system="sys", json_schema={"type": "object"})
    )
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"] == {"effort": "high",
                                   "format": {"type": "json_schema", "schema": {"type": "object"}}}  # fmt: skip
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "thinking" not in kw  # adaptativo por defecto en Opus 5.5
    assert res.data == {"tasks": []}
    assert res.cost_usd == pytest.approx(4.0 + 2.0)  # 1M in × $4 + 100k out × $20


async def test_anthropic_streams_prose_to_log_and_respects_opt_outs():
    adapter = anthropic_adapter("anthropic:claude-haiku-4-5", SWARM_ANTHROPIC_FALLBACKS="0")
    adapter._client, calls = fake_client("línea 1\nlínea 2\n", model="claude-haiku-4-5")
    logs = []
    await adapter.generate(GenerateRequest("x"), logs.append)  # texto libre: se loguea
    kw = calls[0]
    assert "betas" not in kw and "output_config" not in kw  # modelo antiguo, sin fallbacks
    assert "línea 1" in logs and "línea 2" in logs


async def test_anthropic_legacy_model_gets_schema_in_system():
    adapter = anthropic_adapter("anthropic:claude-haiku-4-5")
    adapter._client, calls = fake_client('{"a": 1}')
    res = await adapter.generate(GenerateRequest("x", system="s", json_schema={"type": "object"}))
    assert "JSON Schema" in calls[0]["system"] and res.data == {"a": 1}


@pytest.mark.parametrize("stop, match", [("refusal", "rechazó"), ("max_tokens", "truncada")])
async def test_anthropic_stop_reasons(stop, match):
    adapter = anthropic_adapter()
    adapter._client, _ = fake_client("x", stop_reason=stop)
    with pytest.raises(AdapterError, match=match):
        await adapter.generate(GenerateRequest("x"))


def test_anthropic_model_from_env_and_proxy_disables_fallbacks():
    a = anthropic_adapter(ANTHROPIC_MODEL="claude-sonnet-5-5", ANTHROPIC_BASE_URL="http://proxy")
    assert a.model == "claude-sonnet-5-5" and not a.use_fallbacks
    assert a.label == "anthropic:claude-sonnet-5-5"
