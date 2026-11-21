"""Adapter falso y determinista, para tests y demos sin gastar tokens.

`fake` o `fake:landing` simula un proyecto de landing page completo: el planner devuelve
un plan fijo y cada tarea devuelve sus archivos. La tarea T3 falla a propósito en el
primer intento (JSON inválido) para ejercitar el bucle de auto-reparación.
`fake:landing-agentic` hace lo mismo pero escribiendo los archivos él mismo, como `claude -p`.

Variables: `SWARM_FAKE_DELAY` (segundos de espera por llamada, para ver el panel en vivo).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from swarm_pilot.adapters.base import (
    AdapterContext,
    GenerateRequest,
    GenerateResult,
    LLMAdapter,
    LogFn,
    extract_json,
)
from swarm_pilot.config import ProviderSpec

Responder = Callable[[GenerateRequest], Any]


class FakeAdapter(LLMAdapter):
    def __init__(
        self,
        spec: ProviderSpec,
        ctx: AdapterContext,
        responder: Responder | None = None,
        *,
        agentic: bool = False,
        delay: float = 0.0,
    ) -> None:
        super().__init__(spec, ctx)
        self.responder = responder or landing_scenario
        self.agentic = agentic
        self.delay = delay
        self.calls: list[GenerateRequest] = []

    @classmethod
    def from_spec(cls, spec: ProviderSpec, ctx: AdapterContext) -> FakeAdapter:
        scenario = spec.model or "landing"
        if scenario not in ("landing", "landing-agentic"):
            raise ValueError(f"escenario fake desconocido: {scenario!r}")
        delay = float(ctx.env.get("SWARM_FAKE_DELAY", "0") or 0)
        return cls(spec, ctx, landing_scenario, agentic=scenario.endswith("agentic"), delay=delay)

    async def generate(self, req: GenerateRequest, on_log: LogFn | None = None) -> GenerateResult:
        self.calls.append(req)
        if on_log:
            on_log(f"[fake] {req.meta.get('role', '?')} {req.meta.get('task_id', '')}".rstrip())
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.responder(req)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, GenerateResult):
            result = out
        elif isinstance(out, str):
            result = GenerateResult(text=out, data=extract_json(out) if req.json_schema else None)
        else:
            result = GenerateResult(text=json.dumps(out, ensure_ascii=False), data=out)

        if self.agentic and req.mode == "edit" and isinstance(result.data, dict):
            # Simula a un agente que edita archivos con sus herramientas.
            assert req.cwd is not None
            for f in result.data.get("files", []):
                target = req.cwd / f["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")
            result = GenerateResult(text=result.data.get("notes", "hecho"))
        return result


# --- escenario "landing" -------------------------------------------------------

LANDING_PLAN = {
    "tasks": [
        {
            "id": "T1",
            "title": "Estructura HTML de la landing",
            "role": "architect",
            "files": ["index.html"],
            "detail": "Hero, features y footer; enlaza styles.css y app.js.",
        },
        {
            "id": "T2",
            "title": "Estilos oscuros y responsivos",
            "role": "coder",
            "depends": ["T1"],
            "files": ["styles.css"],
        },
        {
            "id": "T3",
            "title": "Contenido configurable e interacción",
            "role": "coder",
            "depends": ["T1"],
            "files": ["app.js", "config.json"],
        },
    ]
}

INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nimbus — Despliega en segundos</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="hero">
    <h1 id="headline">Despliega en segundos</h1>
    <p id="tagline">Infraestructura sin fricción para equipos pequeños.</p>
    <a class="cta" href="#features">Empezar</a>
  </header>
  <section id="features" class="features"></section>
  <footer>Hecho con swarm-pilot</footer>
  <script src="app.js"></script>
</body>
</html>
"""

STYLES_CSS = """:root { --bg: #0b0d12; --fg: #e6e8ee; --muted: #8a90a2; --accent: #7c9cff; }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.6 system-ui, sans-serif; background: var(--bg); color: var(--fg); }
.hero { min-height: 70vh; display: grid; place-content: center; text-align: center; padding: 2rem; }
.hero h1 { font-size: clamp(2.2rem, 6vw, 4rem); margin: 0; letter-spacing: -0.03em; }
.hero p { color: var(--muted); }
.cta { justify-self: center; margin-top: 1rem; padding: .7rem 1.4rem; border-radius: 999px;
       background: var(--accent); color: #0b0d12; text-decoration: none; font-weight: 600; }
.features { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            max-width: 960px; margin: 0 auto; padding: 2rem 1rem; }
.feature { border: 1px solid #1d2230; border-radius: 12px; padding: 1.2rem; }
footer { text-align: center; color: var(--muted); padding: 2rem; }
"""

APP_JS = """fetch("config.json")
  .then((r) => r.json())
  .then((cfg) => {
    const box = document.getElementById("features");
    for (const f of cfg.features) {
      const el = document.createElement("article");
      el.className = "feature";
      el.innerHTML = `<h3>${f.title}</h3><p>${f.text}</p>`;
      box.appendChild(el);
    }
  });
"""

CONFIG_JSON = {
    "features": [
        {"title": "Un comando", "text": "De tu repo a producción con un solo push."},
        {"title": "Previews", "text": "Cada rama obtiene su propia URL."},
        {"title": "Rollback", "text": "Vuelve atrás en un clic."},
    ]
}


def landing_scenario(req: GenerateRequest) -> Any:
    role = req.meta.get("role")
    if role == "planner":
        return LANDING_PLAN
    task_id = req.meta.get("task_id")
    attempt = int(req.meta.get("attempt", 1))
    if task_id == "T1":
        files = [{"path": "index.html", "content": INDEX_HTML}]
    elif task_id == "T2":
        files = [{"path": "styles.css", "content": STYLES_CSS}]
    elif task_id == "T3":
        config = json.dumps(CONFIG_JSON, indent=2, ensure_ascii=False)
        if attempt == 1:
            config = config.rstrip("}\n")  # error deliberado: JSON truncado
        files = [
            {"path": "app.js", "content": APP_JS},
            {"path": "config.json", "content": config + "\n"},
        ]
    else:
        files = []
    return {"files": files, "notes": f"{task_id} intento {attempt}"}
