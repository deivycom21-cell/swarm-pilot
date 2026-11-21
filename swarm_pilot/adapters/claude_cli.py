"""Adapter Claude Code CLI: ejecuta `claude -p` reutilizando la sesión Pro/Max del usuario.

Detalles verificados contra Claude Code 2.1.x:
- El prompt va por **stdin** (evita el límite de longitud de argv en Windows).
- El system prompt va con `--append-system-prompt-file` (mismo motivo).
- `--output-format stream-json --verbose` emite eventos `system` / `assistant` / `user` /
  `result`; el evento `result` trae `structured_output` cuando se pasa `--json-schema`.
- `--strict-mcp-config` evita cargar los servidores MCP del usuario en cada sub-agente.
- Herramientas por modo: lectura `Read,Glob,Grep`; edición añade `Write,Edit` con
  `--permission-mode acceptEdits`. Nunca `--dangerously-skip-permissions` salvo opt-in.

Por defecto se quita `ANTHROPIC_API_KEY` del entorno del subproceso para que Claude Code use
la suscripción del usuario y no facture a la API (`SWARM_CLAUDE_KEEP_API_KEY=1` lo evita).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from swarm_pilot.adapters.base import (
    AdapterError,
    GenerateRequest,
    GenerateResult,
    LLMAdapter,
    LogFn,
    extract_json,
)

READ_TOOLS = "Read,Glob,Grep"
EDIT_TOOLS = "Read,Glob,Grep,Write,Edit"
_RATE_WORDS = ("rate limit", "usage limit", "limit reached", "too many requests", "overloaded")


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(name: str, inp: dict[str, Any], cwd: Path | None) -> str:
    target = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command")
    if isinstance(target, str) and cwd is not None:
        with contextlib.suppress(ValueError):
            target = str(Path(target).resolve().relative_to(cwd.resolve())).replace("\\", "/")
    return f"🔧 {name} {_short(target or inp, 120)}".rstrip()


def _looks_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _RATE_WORDS)


class ClaudeCLIAdapter(LLMAdapter):
    agentic = True
    #: Comando base; los tests lo sustituyen por un simulador.
    command: list[str] = ["claude"]

    def build_args(self, req: GenerateRequest, system_file: Path | None) -> list[str]:
        args = [
            *self.command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]  # fmt: skip
        if self.spec.model:
            args += ["--model", self.spec.model]
        if req.mode == "none":
            args += ["--tools", ""]
        elif req.mode == "read":
            args += ["--tools", READ_TOOLS, "--permission-mode", "dontAsk"]
        elif self.ctx.skip_permissions:
            args += ["--dangerously-skip-permissions"]
        else:
            args += ["--tools", EDIT_TOOLS, "--permission-mode", "acceptEdits"]
        if system_file is not None:
            args += ["--append-system-prompt-file", str(system_file)]
        if req.json_schema is not None:
            args += ["--json-schema", json.dumps(req.json_schema, separators=(",", ":"))]
        return args

    def _env(self) -> dict[str, str]:
        env = dict(self.ctx.env or os.environ)
        if env.get("SWARM_CLAUDE_KEEP_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    async def generate(self, req: GenerateRequest, on_log: LogFn | None = None) -> GenerateResult:
        pm = self.ctx.process_manager
        if pm is None:
            raise AdapterError("claude-cli necesita un ProcessManager", retryable=False)
        log = on_log or (lambda _line: None)

        system_file: Path | None = None
        if req.system:
            scratch = self.ctx.scratch_dir or Path(tempfile.gettempdir())
            scratch.mkdir(parents=True, exist_ok=True)
            system_file = scratch / f"claude-system-{uuid.uuid4().hex[:12]}.txt"
            system_file.write_text(req.system, encoding="utf-8")

        try:
            mp = await pm.spawn(
                self.build_args(req, system_file),
                name="claude",
                cwd=req.cwd,
                env=self._env(),
                stdin=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            if system_file is not None:
                system_file.unlink(missing_ok=True)
            raise AdapterError(
                "no se encontró `claude` en el PATH. Instala Claude Code o elige otro proveedor.",
                retryable=False,
            ) from exc

        try:
            assert mp.proc.stdin is not None
            mp.proc.stdin.write(req.prompt.encode("utf-8"))
            with contextlib.suppress(ConnectionError):
                await mp.proc.stdin.drain()
            mp.proc.stdin.close()
            try:
                return await asyncio.wait_for(self._consume(mp, req, log), req.timeout)
            except asyncio.TimeoutError as exc:
                raise AdapterError(f"claude superó el tiempo límite ({req.timeout:.0f}s)") from exc
        finally:
            if mp.returncode is None:
                await pm.terminate(mp, grace=2)
            if system_file is not None:
                system_file.unlink(missing_ok=True)

    async def _consume(self, mp: Any, req: GenerateRequest, log: LogFn) -> GenerateResult:
        result: dict[str, Any] | None = None
        raw_tail: deque[str] = deque(maxlen=20)
        async for line in mp.lines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                raw_tail.append(line)
                log(line)
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "init":
                log(f"sesión claude iniciada ({ev.get('model', '?')})")
            elif etype == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        for text_line in block["text"].strip().splitlines():
                            if text_line.strip():
                                log(f"💬 {_short(text_line, 300)}")
                    elif block.get("type") == "tool_use":
                        log(
                            _tool_summary(block.get("name", "?"), block.get("input") or {}, req.cwd)
                        )
            elif etype == "user":
                for block in ev.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("is_error"):
                        log(f"⚠ {_short(block.get('content', ''), 200)}")
            elif etype == "result":
                result = ev
        rc = await mp.wait()

        if result is None:
            tail = "\n".join(raw_tail) or f"código de salida {rc}"
            raise AdapterError(
                f"claude terminó sin resultado: {tail}", rate_limited=_looks_rate_limited(tail)
            )
        message = str(result.get("result") or result.get("subtype") or "")
        if result.get("is_error") or result.get("subtype") != "success":
            rate = result.get("api_error_status") == 429 or _looks_rate_limited(message)
            raise AdapterError(
                f"claude devolvió un error: {_short(message, 500)}", rate_limited=rate
            )

        data = result.get("structured_output")
        if req.json_schema is not None and data is None:
            data = extract_json(message)
        return GenerateResult(
            text=message,
            data=data,
            cost_usd=result.get("total_cost_usd"),
            usage=result.get("usage") or {},
        )

    async def check(self) -> str:
        pm = self.ctx.process_manager
        try:
            res = await pm.run([*self.command, "--version"], timeout=30)
        except FileNotFoundError as exc:
            raise AdapterError("`claude` no está en el PATH", retryable=False) from exc
        if not res.ok:
            raise AdapterError(f"`claude --version` falló: {res.output[-300:]}", retryable=False)
        return f"claude-cli {res.output.strip()}"
