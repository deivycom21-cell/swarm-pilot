"""Planner: analiza el directorio y convierte un objetivo en un `Plan` (DAG de tareas)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from swarm_pilot.adapters.base import AdapterError, GenerateRequest, LLMAdapter, LogFn
from swarm_pilot.core.environment import Environment, detect_environment
from swarm_pilot.core.models import Plan, Task
from swarm_pilot.core.prompts import PLAN_SCHEMA, PLANNER_SYSTEM, planner_prompt
from swarm_pilot.core.workspace import tree

MANIFESTS = (
    "README.md", "CLAUDE.md", "AGENTS.md", "package.json", "pyproject.toml",
    "requirements.txt", "setup.py", "tsconfig.json", "vite.config.ts", "vite.config.js",
    "next.config.js", "next.config.mjs", "astro.config.mjs", "Cargo.toml", "go.mod",
    "Makefile", "index.html",
)  # fmt: skip
MAX_MANIFEST_CHARS = 4000
MAX_TOTAL_MANIFEST_CHARS = 20_000
MAX_TRIES = 3


class PlannerError(RuntimeError):
    pass


def read_manifests(project_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    total = 0
    for name in MANIFESTS:
        path = project_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > MAX_MANIFEST_CHARS:
            text = text[:MAX_MANIFEST_CHARS] + "\n… (recortado)"
        if total + len(text) > MAX_TOTAL_MANIFEST_CHARS:
            break
        out[name] = text
        total += len(text)
    return out


async def make_plan(
    goal: str,
    adapter: LLMAdapter,
    project_dir: Path,
    *,
    env: Environment | None = None,
    on_log: LogFn | None = None,
    timeout: float | None = None,
) -> Plan:
    """Pide el plan al modelo y lo valida; reintenta con el error si el JSON no es válido."""
    goal = goal.strip()
    if not goal:
        raise PlannerError("el objetivo está vacío")
    env = env or detect_environment(project_dir)
    prompt = planner_prompt(goal, env.describe(), tree(project_dir), read_manifests(project_dir))
    log = on_log or (lambda _l: None)
    last_error = ""
    for attempt in range(1, MAX_TRIES + 1):
        full_prompt = prompt
        if last_error:
            full_prompt += (
                f"\n\n# Your previous answer was invalid\n{last_error}\nReturn a corrected plan."
            )
        log(f"planificando (intento {attempt}/{MAX_TRIES}) con {adapter.label}")
        try:
            res = await adapter.generate(
                GenerateRequest(
                    full_prompt,
                    system=PLANNER_SYSTEM,
                    mode="read" if adapter.agentic else "none",
                    cwd=project_dir,
                    json_schema=PLAN_SCHEMA,
                    timeout=timeout,
                    meta={"role": "planner"},
                ),
                on_log,
            )
        except AdapterError as exc:
            if not exc.retryable or exc.rate_limited or attempt == MAX_TRIES:
                raise PlannerError(str(exc)) from exc
            last_error = str(exc)
            continue
        data = res.data
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            last_error = "The answer must be a JSON object with a `tasks` array."
            continue
        try:
            return Plan(goal=goal, tasks=[Task(**t) for t in data["tasks"]])
        except (ValidationError, TypeError) as exc:
            last_error = str(exc)
            log(f"plan inválido: {last_error.splitlines()[0]}")
    raise PlannerError(
        f"el modelo no produjo un plan válido tras {MAX_TRIES} intentos: {last_error}"
    )
