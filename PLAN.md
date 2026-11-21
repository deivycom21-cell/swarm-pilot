# PLAN.md — swarm-pilot v0.1

> **Estado:** v0.1.0 completa (M1–M4); distribuida directamente desde GitHub · **Actualizado:** 2026-11-20
> Este archivo usa el mismo formato que `swarm-pilot` generará para tus proyectos: cada tarea tiene rol, dependencias, archivos y comando de verificación.

---

## 1. Objetivo

Un orquestador local que convierte una instrucción genérica ("Arma una API en FastAPI") en un plan de tareas atómicas, lo ejecuta con sub-agentes por rol y **no aprueba ninguna tarea sin verificarla**. Cero servidores del autor: cada usuario trae su proveedor (sesión de Claude Code, API key propia u Ollama local).

**Definición de MVP terminado:**
En una carpeta vacía, `swarm-pilot run "Crea una landing page moderna"`, usando solo la sesión de Claude Code del usuario:
1. genera y muestra `PLAN.md`, y espera aprobación;
2. ejecuta las tareas y repara solo los fallos de verificación;
3. muestra el progreso en `http://localhost:4000` y la web en vivo en `http://localhost:3000`;
4. al pulsar `Ctrl+C` no deja ningún proceso huérfano ni puerto ocupado.

---

## 2. Arquitectura

```
                    ┌────────────┐
 instrucción ─────▶ │  Planner   │──▶ PLAN.md  ⇄  .swarm/state.json
                    └─────┬──────┘
                          │ DAG de tareas (aprobado por el usuario)
                    ┌─────▼────────┐   eventos   ┌──────────┐  WS   ┌──────────────┐
                    │ Orchestrator │────────────▶│ EventBus │──────▶│ :4000 panel  │
                    └─────┬────────┘             └──────────┘       └──────┬───────┘
           ┌──────────────┼──────────────┐                                 │ iframe
      ┌────▼─────┐  ┌─────▼────┐  ┌──────▼───┐                      ┌──────▼───────┐
      │ Worker   │  │ Worker   │  │ Worker   │  architect/coder/    │ :3000 vista  │
      │ (rol)    │  │ (rol)    │  │ (rol)    │  tester              │ previa       │
      └────┬─────┘  └────┬─────┘  └────┬─────┘                      └──────────────┘
           └─────────────┼─────────────┘
                    ┌────▼─────┐   falla → error al worker (máx. 3) → pausa y pide ayuda
                    │ Verifier │
                    └──────────┘

  Adapters: claude-cli · anthropic (SDK oficial) · openai-compat (OpenAI / OpenRouter / Ollama)
  ProcessManager: registra TODO subproceso y mata el árbol completo al salir
```

### Estructura de carpetas

```
swarm-pilot/
├── pyproject.toml · README.md · PLAN.md · LICENSE · .env.example
├── swarm_pilot/
│   ├── cli.py                # Typer: init, plan, run, resume, status, doctor, mcp
│   ├── mcp_server.py         # servidor MCP (stdio) para Claude Code / Cursor
│   ├── config.py             # swarm.toml + variables de entorno → Settings
│   ├── adapters/
│   │   ├── base.py           # protocolo LLMAdapter + capacidades
│   │   ├── registry.py       # "proveedor:modelo" → instancia de adapter
│   │   ├── claude_cli.py     # subproceso `claude -p`
│   │   ├── anthropic_api.py  # SDK oficial `anthropic`
│   │   └── openai_compat.py  # OpenAI / OpenRouter / Ollama vía httpx
│   ├── core/
│   │   ├── models.py         # Task, Plan, Attempt, Event (pydantic)
│   │   ├── events.py         # EventBus asyncio (pub/sub + log JSONL)
│   │   ├── plan_io.py        # PLAN.md ⇄ state.json
│   │   ├── planner.py
│   │   ├── worker.py
│   │   ├── workspace.py      # aplica cambios de archivos dentro del proyecto
│   │   ├── verifier.py
│   │   ├── orchestrator.py   # planificador DAG + reintentos + pausa
│   │   ├── preview.py        # detecta y levanta el dev server
│   │   └── process_manager.py
│   └── web/
│       ├── server.py         # FastAPI + WebSocket
│       └── static/index.html # panel vanilla, sin build
└── tests/
```

> `cli.py` y `mcp_server.py` viven dentro del paquete `swarm_pilot/` para que `pip install git+https://github.com/deivycom21-cell/swarm-pilot.git` funcione y exponga el comando `swarm-pilot`.

---

## 3. Decisiones de diseño

**D1 · Dos tipos de adapter.**
- *Agénticos* (`claude-cli`): el propio agente lee y edita archivos con sus herramientas.
- *De completado* (`anthropic`, `openai-compat`): el modelo devuelve los cambios como JSON `{"files":[{"path","content"}],"notes"}`. Si un modelo pequeño no sigue el formato, se usan bloques ```` ```file:ruta ```` como respaldo. `workspace.py` aplica los cambios.

El worker oculta esta diferencia; el resto del sistema no la conoce.

**D2 · Fuente de verdad.**
`.swarm/state.json` guarda el estado y `PLAN.md` es la vista legible, regenerada en cada cambio. Antes de `run`, el usuario puede editar `PLAN.md` a mano y se vuelve a leer, así que el plan es editable antes de aprobarlo.

**D3 · Paralelismo seguro.**
Una tarea corre en paralelo solo si se cumplen sus dependencias **y** sus `archivos` declarados no se solapan con los de otra tarea activa. `max_parallel` vale 2 por defecto. Después de cada intento, un diff del workspace detecta las ediciones fuera de lo declarado, y esas tareas se re-verifican.

**D4 · Verificación.**
Se ejecutan los comandos `verificar` de la tarea más los chequeos que se detectan según el stack:
- Python: `ruff`, `py_compile`, `pytest` (si existen).
- Node: los scripts `lint`/`build`/`test` de `package.json`.
- Estático: el HTML parsea y los assets referenciados existen.

Cada comando tiene timeout y corre solo dentro del proyecto. Al worker se le devuelve solo la cola de la salida (unos 4 KB).

**D5 · Auto-reparación.**
Si la verificación de un intento falla, el mismo worker recibe el error y el diff de su intento, hasta 3 intentos en total. Después la tarea pasa a `blocked`: se pausa solo esa rama del DAG (las tareas independientes siguen) y se emite `needs_help`. Desde la CLI o el panel, el usuario puede:
- dar una pista y reintentar;
- marcarla como "arreglada a mano" para re-verificarla;
- saltarla;
- abortar la ejecución.

**D6 · Seguridad por defecto.**
- Después de planificar se pide aprobación y se muestran todos los comandos a ejecutar (`--yes` la omite).
- La escritura de archivos queda confinada a la raíz del proyecto (rutas resueltas).
- `claude -p` corre con `--permission-mode acceptEdits` y herramientas restringidas; nunca con `--dangerously-skip-permissions` salvo que se active explícitamente en la config.
- Las claves solo se leen de variables de entorno y se ocultan en logs y estado.
- Sin telemetría.

**D7 · Procesos.**
Todo subproceso pasa por `ProcessManager`:
- Windows: `CREATE_NEW_PROCESS_GROUP`, kill de árbol con `psutil` y un **Job Object** con `KILL_ON_JOB_CLOSE`, para que el sistema mate a los descendientes incluso si `swarm-pilot` muere de golpe.
- POSIX: `start_new_session=True` y `killpg`. Al apagar se barren también los grupos creados en la sesión, para alcanzar a nietos que quedaron huérfanos.

Orden de apagado: parar el scheduler → cancelar workers → `terminate` del árbol (5 s de gracia) → `kill` → cerrar uvicorn. Se engancha a señales y a `atexit`. `.swarm/pids.json` permite limpiar los procesos de una ejecución previa que se cayó (`swarm-pilot doctor --cleanup`).

**D8 · Configuración (BYOK, modo híbrido).**
Prioridad: flags de CLI > variables de entorno > `./swarm.toml` > config de usuario > autodetección.
La config de usuario está en `~/.config/swarm-pilot/config.toml` (Linux/macOS) o `%APPDATA%\swarm-pilot\config.toml` (Windows), y se puede cambiar con `SWARM_CONFIG`.

```toml
[providers]                      # formato "proveedor[:modelo]"
planner   = "claude-cli"         # potente para planificar
architect = "claude-cli"
coder     = "openrouter:<modelo>"
tester    = "ollama:qwen2.5-coder"

[run]
max_parallel = 2
max_attempts = 3
```

Variables de entorno: `SWARM_PLANNER`, `SWARM_WORKER` (todos los roles), `SWARM_CODER`/`SWARM_TESTER`/`SWARM_ARCHITECT`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENROUTER_API_KEY`, `OLLAMA_HOST`.

Si no hay nada configurado, se usa el primer proveedor disponible en este orden: `claude` en el PATH → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`/`OPENROUTER_API_KEY` → Ollama accesible. Si no hay ninguno, `doctor` explica cómo configurar uno.

**D9 · Panel y vista previa.**
Ambos escuchan solo en `127.0.0.1`. Si un puerto está ocupado, se usa el siguiente libre.

El panel recibe por WebSocket un snapshot inicial y luego deltas, con estos eventos: `run_started`, `plan_updated`, `task_status`, `agent_log`, `verify_result`, `attempt_failed`, `needs_help`, `preview_status`, `run_finished`.

Para la vista previa se detecta el servidor en este orden:
1. `package.json` → `npm run dev -- --port N`;
2. `vite.config.*` → `npx vite --port N`;
3. `index.html` → servidor estático de Python en `127.0.0.1:N` (como `http.server`, sin la resolución DNS inversa que bloquea en macOS);
4. si no hay nada, no hay vista previa.

La vista previa se levanta tras la primera tarea web verificada y se considera lista cuando responde HTTP 200. Se reinicia si cambia `package.json`.

---

## 4. Tareas

Formato: `rol` · `depende` · `archivos` · `verificar`. Estado: `[ ]` pendiente · `[x]` hecha.

> **Nota de implementación:** algunos tests se agruparon respecto a lo planeado. `test_registry`, `test_claude_cli` → `tests/test_adapters.py`; `test_openai_compat`, `test_anthropic_api` → `tests/test_api_adapters.py`; `test_worker` → `tests/test_orchestrator.py`; `test_cli` → `tests/e2e/test_cli_e2e.py`. Los prompts viven en `core/prompts.py` y el `FakeAdapter` en `adapters/fake.py` (también usable como proveedor `fake` para demos). Estado verificado: 162 tests en verde, `ruff` limpio, `python -m build` + `twine check` OK.

### Fase 0 — Fundaciones

#### [x] T01 · Estructura del proyecto, pyproject, README, PLAN
- rol: architect
- depende: —
- archivos: `pyproject.toml`, `README.md`, `PLAN.md`, `swarm_pilot/**/__init__.py`
- verificar: `python -c "import tomllib;tomllib.load(open('pyproject.toml','rb'))"`

#### [x] T02 · Modelos de dominio
- rol: coder
- depende: T01
- archivos: `swarm_pilot/core/models.py`, `tests/test_models.py`
- detalle: `Task` (id, título, rol, depende, archivos, verificar, estado, intentos), `Plan` (validación DAG: ciclos, dependencias inexistentes, orden topológico), `Attempt`, `Event`.
- verificar: `pytest tests/test_models.py`

#### [x] T03 · Configuración y autodetección de proveedores
- rol: coder
- depende: T01
- archivos: `swarm_pilot/config.py`, `tests/test_config.py`
- verificar: `pytest tests/test_config.py`

#### [x] T04 · EventBus asíncrono
- rol: coder
- depende: T02
- archivos: `swarm_pilot/core/events.py`, `tests/test_events.py`
- detalle: pub/sub con colas por suscriptor y persistencia en `.swarm/runs/<id>/events.jsonl`.
- verificar: `pytest tests/test_events.py`

#### [x] T05 · ProcessManager multiplataforma
- rol: coder
- depende: T01
- archivos: `swarm_pilot/core/process_manager.py`, `tests/test_process_manager.py`
- verificar: `pytest tests/test_process_manager.py` (lanza un hijo que lanza un nieto, apaga el gestor y comprueba que no queda ninguno vivo)

### Fase 1 — Adapters

#### [x] T06 · Protocolo base y registro
- rol: architect
- depende: T03
- archivos: `swarm_pilot/adapters/base.py`, `swarm_pilot/adapters/registry.py`, `tests/fakes.py`
- detalle: `complete()` y `run_agentic()`, capacidades (`agentic`, `streaming`), callback de log y un `FakeAdapter` determinista para los tests.
- verificar: `pytest tests/test_registry.py`

#### [x] T07 · Adapter Claude Code CLI
- rol: coder
- depende: T05, T06
- archivos: `swarm_pilot/adapters/claude_cli.py`
- detalle:
  - `claude -p … --output-format stream-json --verbose` con `cwd` en el proyecto.
  - El planner corre solo con herramientas de lectura.
  - El parseo de eventos se convierte en logs en vivo.
  - Los límites de uso se manejan con backoff y luego pausa.
  - Los flags se validan contra `claude --help` de la versión instalada y quedan aislados en un único lugar.
- verificar: `pytest tests/test_claude_cli.py` (binario simulado) y una prueba real opcional con `SWARM_E2E=1`

#### [x] T08 · Adapter OpenAI-compatible (OpenAI / OpenRouter / Ollama)
- rol: coder
- depende: T06
- archivos: `swarm_pilot/adapters/openai_compat.py`
- detalle: `httpx` con streaming SSE, `base_url` por proveedor y reintentos ante 429/5xx.
- verificar: `pytest tests/test_openai_compat.py` (con `httpx.MockTransport`)

#### [x] T09 · Adapter Anthropic API
- rol: coder
- depende: T06
- archivos: `swarm_pilot/adapters/anthropic_api.py`
- detalle:
  - SDK oficial `anthropic` como extra opcional.
  - Modelo por defecto `claude-opus-5-5`, pensamiento adaptativo y `effort` explícito.
  - Streaming y `get_final_message()`.
  - Maneja `stop_reason == "refusal"`.
- verificar: `pytest tests/test_anthropic_api.py` (cliente simulado)

### Fase 2 — Núcleo

#### [x] T10 · PLAN.md ⇄ state.json
- rol: coder
- depende: T02
- archivos: `swarm_pilot/core/plan_io.py`, `tests/test_plan_io.py`
- verificar: `pytest tests/test_plan_io.py` (ida y vuelta sin pérdida, también tras ediciones manuales)

#### [x] T11 · Planner
- rol: coder
- depende: T06, T10
- archivos: `swarm_pilot/core/planner.py`
- detalle:
  - Escanea el directorio: árbol, manifiestos (`package.json`, `pyproject.toml`) y README, con tope de tamaño.
  - Pide al modelo un plan en JSON, lo valida con pydantic y reintenta hasta 2 veces si no es válido.
  - Escribe `PLAN.md`.
- verificar: `pytest tests/test_planner.py` (FakeAdapter)

#### [x] T12 · Workspace con sandbox de rutas
- rol: coder
- depende: T01
- archivos: `swarm_pilot/core/workspace.py`, `tests/test_workspace.py`
- detalle: aplica bloques de archivos, rechaza rutas fuera de la raíz y hace un snapshot y un diff por intento.
- verificar: `pytest tests/test_workspace.py`

#### [x] T13 · Worker por roles
- rol: coder
- depende: T06, T12
- archivos: `swarm_pilot/core/worker.py`, `swarm_pilot/core/prompts/`
- detalle: prompts de `architect`, `coder` y `tester`, rama agéntica o de completado, y en los reintentos incluye el error anterior y el diff.
- verificar: `pytest tests/test_worker.py`

#### [x] T14 · Verifier
- rol: coder
- depende: T05
- archivos: `swarm_pilot/core/verifier.py`
- verificar: `pytest tests/test_verifier.py` (proyectos de prueba en Python, Node y estático)

#### [x] T15 · Orchestrator
- rol: architect
- depende: T04, T11, T13, T14
- archivos: `swarm_pilot/core/orchestrator.py`
- detalle: scheduler DAG con bloqueo por solapamiento de archivos, máximo 3 intentos por tarea, `blocked`/`needs_help`, reanudación desde `state.json` y cancelación limpia.
- verificar: `pytest tests/test_orchestrator.py` (escenario con un fallo que se repara en el intento 2 y otro que se bloquea en el 3)

### Fase 3 — CLI

#### [x] T16 · CLI
- rol: coder
- depende: T15
- archivos: `swarm_pilot/cli.py`
- detalle:
  - Comandos `init`, `plan`, `run`, `resume`, `status` y `doctor` (con `--cleanup`).
  - Flags: `--yes`, `--no-ui`, `--planner`, `--worker` y `--max-parallel`.
  - La ayuda para tareas bloqueadas se pide de forma interactiva.
- verificar: `swarm-pilot --help` · `pytest tests/test_cli.py`

**🏁 Hito M1:** ejecución completa solo desde la terminal con `claude-cli`.

### Fase 4 — Experiencia visual

#### [x] T17 · Servidor del panel
- rol: coder
- depende: T04
- archivos: `swarm_pilot/web/server.py`
- detalle: FastAPI con `/ws`, `/api/state`, `/api/answer` y estáticos; uvicorn en el mismo proceso, en `127.0.0.1:4000` o el siguiente puerto libre.
- verificar: `pytest tests/test_web.py` (`TestClient` y WebSocket)

#### [x] T18 · Panel `index.html`
- rol: coder
- depende: T17
- archivos: `swarm_pilot/web/static/index.html`
- detalle:
  - Un solo archivo de HTML, CSS y JS vanilla, tema oscuro minimalista, de unos 40 KB.
  - Paneles: plan con estados, agentes activos, logs en vivo con filtro por tarea, línea de tiempo de reparaciones y caja de "necesita ayuda".
  - Vista previa en pantalla dividida (iframe) y reconexión automática.
- verificar: revisión manual con un run simulado y `pytest tests/test_web.py::test_static`

#### [x] T19 · Vista previa en vivo
- rol: coder
- depende: T05
- archivos: `swarm_pilot/core/preview.py`
- verificar: `pytest tests/test_preview.py` (detección sobre fixtures y servidor estático real)

#### [x] T20 · Integración y apagado limpio
- rol: tester
- depende: T16, T18, T19
- archivos: `swarm_pilot/cli.py`, `tests/test_shutdown.py`
- verificar: `pytest tests/test_shutdown.py` (envía `SIGINT`/`CTRL_BREAK` y comprueba que no quedan PIDs vivos y que los puertos 4000/3000 quedan libres)

**🏁 Hito M2:** MVP completo según la sección 1.

### Fase 5 — MCP

#### [x] T21 · Servidor MCP
- rol: coder
- depende: T15
- archivos: `swarm_pilot/mcp_server.py`
- detalle:
  - Usa el SDK oficial `mcp` (FastMCP) con transporte stdio.
  - Herramientas:
    - `swarm_plan(goal, path)` devuelve el `PLAN.md`;
    - `swarm_run(path, yes)` lanza la ejecución en segundo plano y devuelve `run_id` y la URL del panel;
    - `swarm_status(run_id)` devuelve el estado;
    - `swarm_answer(run_id, task_id, action, hint)` responde a una tarea bloqueada.
  - Las ejecuciones largas son asíncronas y se consultan con polling, para evitar los timeouts de MCP.
  - Incluye fragmentos de config para `claude mcp add` y `.cursor/mcp.json`.
- verificar: `pytest tests/test_mcp.py` (cliente MCP en memoria)

**🏁 Hito M3:** todos los adapters más MCP.

### Fase 6 — Calidad y lanzamiento

#### [x] T22 · E2E determinista
- rol: tester
- depende: T20
- archivos: `tests/e2e/`
- detalle: escenario "landing page" con FakeAdapter y una prueba real opcional (`SWARM_E2E=1`).
- verificar: `pytest tests/e2e`

#### [x] T23 · CI
- rol: coder
- depende: T22
- archivos: `.github/workflows/ci.yml`
- detalle: matriz Ubuntu/Windows/macOS × Python 3.10–3.13, con `ruff` y `pytest`.
- verificar: CI en verde

#### [x] T24 · Documentación y publicación
- rol: architect
- depende: T23
- archivos: `README.md`
- detalle: guía rápida, proveedores y seguridad; distribuido directamente desde GitHub (no se publica en PyPI).
- verificar: `pip install git+https://github.com/deivycom21-cell/swarm-pilot.git` en un entorno limpio
- progreso:
  - [x] README con guía rápida, comandos, proveedores, seguridad y MCP (todo en README; no hizo falta `docs/`)
  - [x] instalación desde GitHub documentada en ambos idiomas (requiere `git`)
  - [x] el paquete construye, pasa `twine check` y se instala y ejecuta en un entorno limpio
  - [x] repositorio público en `https://github.com/deivycom21-cell/swarm-pilot`
- opcional, fuera de v0.1: GIF de demo (`SWARM_PLANNER=fake SWARM_WORKER=fake SWARM_FAKE_DELAY=1 swarm-pilot run "demo" --yes`)

**🏁 Hito M4:** v0.1.0 publicada en GitHub.

---

## 5. Orden de implementación

```
M1:  T02 T03 T05 ─▶ T04 T06 T10 T12 ─▶ T07 T11 T13 T14 ─▶ T15 ─▶ T16
M2:  T17 ─▶ T18 · T19 ─▶ T20
M3:  T08 · T09 · T21
M4:  T22 ─▶ T23 ─▶ T24
```

`claude-cli` se implementa primero porque es el camino sin coste para el usuario. Los adapters por API (T08, T09) pueden avanzar en paralelo en cuanto exista T06.

---

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| Los flags de `claude -p` cambian entre versiones | Aislados en un único módulo; `doctor` comprueba la versión |
| Los modelos pequeños (Ollama) no siguen el formato de edición | Parser de respaldo con bloques; el verifier atrapa el error; recomendar el modo híbrido |
| Un agente agéntico edita archivos no declarados en paralelo | Diff por intento, re-verificación y `max_parallel` conservador |
| Árboles de procesos en Windows (npm → node → esbuild) | Kill de árbol con `psutil`, probado en CI con Windows |
| Límites de uso de Claude Pro/Max | Backoff y luego pausa con `needs_help`; el planner prioriza menos tareas y más grandes |
| Timeouts de llamadas MCP en ejecuciones largas | `swarm_run` asíncrono más `swarm_status` |
| Comandos de verificación generados por el LLM | Puerta de aprobación, timeout y `cwd` restringido |

---

## 7. Decisiones a confirmar antes de T02

1. **Layout:** paquete `swarm_pilot/` con `cli.py` y `mcp_server.py` dentro (recomendado para `pip install`).
2. **Licencia:** MIT.
3. **Python:** ≥ 3.10.
4. **Puerta de aprobación** tras el plan activada por defecto (`--yes` para omitirla).
