# swarm-pilot

**[Español](#español) · [English](#english)**

> Alpha (v0.1). Funciona de punta a punta; espera cambios en la API. · Works end to end; expect API changes.

---

## Español

Orquestador local y autónomo de sub-agentes de IA. Le das una instrucción, genera un plan de tareas atómicas, las ejecuta con agentes especializados y **verifica cada una antes de aprobarla**. Si una verificación falla, el agente la repara solo (hasta 3 intentos).

```bash
pip install "swarm-pilot[all] @ git+https://github.com/deivycom21-cell/swarm-pilot.git"
swarm-pilot run "Crea una landing page moderna"
```

- **Planner → Workers → Verifier.** `PLAN.md` editable, roles Arquitecto, Coder y Tester, y ejecución en paralelo cuando es seguro (sin solapamiento de archivos).
- **Tu propio modelo (BYOK).** Tu sesión de Claude Code (`claude -p`), la API de Anthropic, OpenAI, OpenRouter u Ollama local. Sin servidores intermedios ni costes ocultos.
- **Modo híbrido.** Un modelo potente para planificar y otros rápidos o gratuitos para ejecutar.
- **Centro de mando** en `localhost:4000` y **vista previa en vivo** de tu web en `localhost:3000`.
- **Servidor MCP.** Úsalo desde Claude Code o Cursor.
- **Apagado limpio.** `Ctrl+C` no deja procesos huérfanos ni puertos ocupados (también en Windows).

### Instalación

Requiere Python ≥ 3.10 y [git](https://git-scm.com) (la instalación se hace desde GitHub). Y al menos un proveedor: [Claude Code](https://claude.com/claude-code) en el PATH, una API key o [Ollama](https://ollama.com).

```bash
pip install git+https://github.com/deivycom21-cell/swarm-pilot.git             # núcleo: claude-cli, OpenAI, OpenRouter, Ollama
pip install "swarm-pilot[all] @ git+https://github.com/deivycom21-cell/swarm-pilot.git"      # + SDK de Anthropic + servidor MCP
swarm-pilot doctor                  # comprueba proveedores, herramientas y puertos
```

Desde el código: `git clone https://github.com/deivycom21-cell/swarm-pilot.git && cd swarm-pilot && pip install -e ".[dev,all]"`.

### Cómo funciona

1. **Planifica.** El planner lee el directorio (árbol, `package.json`, `pyproject.toml`, README) y escribe `PLAN.md` con tareas que tienen rol, dependencias, archivos y comandos de verificación.
2. **Pide aprobación.** Muestra el plan y **todos los comandos que se ejecutarán**. Puedes editar `PLAN.md` a mano antes de aprobar; se vuelve a leer. `--yes` omite la pregunta.
3. **Ejecuta.** Las tareas corren en orden de dependencias; dos tareas van en paralelo solo si sus archivos no se solapan.
4. **Verifica.** Chequeos integrados (sintaxis Python, JSON válido, assets del HTML existentes) + los comandos `verificar` de la tarea, con timeout y dentro del proyecto.
5. **Se repara.** Si falla, el mismo agente recibe el error y su diff. Tras 3 intentos la tarea queda **bloqueada**: solo esa rama del plan se pausa y te pregunta (terminal o panel): reintentar con una pista, "lo arreglé a mano", saltar o abortar.

El estado vive en `.swarm/state.json`; `PLAN.md` es su vista legible. Si cortas un run, `swarm-pilot resume` sigue donde quedó.

### Comandos

| Comando | Qué hace |
|---|---|
| `swarm-pilot run "objetivo"` | Planifica, pide aprobación y ejecuta. Sin objetivo, retoma el plan. |
| `swarm-pilot plan "objetivo"` | Solo genera `PLAN.md`. |
| `swarm-pilot resume` | Retoma `.swarm/state.json` (incluye tus ediciones de `PLAN.md`). |
| `swarm-pilot status` | Estado del plan y errores de las tareas bloqueadas. |
| `swarm-pilot doctor [--cleanup]` | Diagnóstico; `--cleanup` mata procesos de un run anterior que se cayó. |
| `swarm-pilot init` | Crea un `swarm.toml` comentado. |
| `swarm-pilot mcp` | Servidor MCP por stdio. |

Opciones de `run`: `--yes`, `--no-ui`, `--no-preview`, `--planner`, `--worker`, `--max-parallel`, `--quiet`, `--exit` (salir al terminar en vez de mantener panel y vista previa), `--dir`.

Códigos de salida: `0` terminado · `1` error/abortado · `2` en pausa (hay tareas bloqueadas) · `130` interrumpido.

### Proveedores y configuración

Formato `proveedor[:modelo]`:

| Proveedor | Tipo | Requiere |
|---|---|---|
| `claude-cli` | agéntico (edita archivos él mismo) | `claude` en el PATH (usa tu suscripción) |
| `anthropic[:modelo]` | completado | `ANTHROPIC_API_KEY` y el extra `[anthropic]` |
| `openai[:modelo]` | completado | `OPENAI_API_KEY` (`OPENAI_BASE_URL` opcional) |
| `openrouter:modelo` | completado | `OPENROUTER_API_KEY` |
| `ollama:modelo` | completado | Ollama accesible en `OLLAMA_HOST` |

```toml
# swarm.toml  (crea uno con `swarm-pilot init`)
[providers]
planner = "claude-cli"              # modelo potente para planificar
worker  = "ollama:qwen2.5-coder"    # todos los roles de ejecución…
# coder = "openrouter:<modelo>"     # …o uno por rol: architect / coder / tester

[run]
max_parallel = 2
max_attempts = 3
```

Prioridad: flags de la CLI > variables de entorno > `./swarm.toml` > config de usuario (`~/.config/swarm-pilot/config.toml` o `%APPDATA%\swarm-pilot\config.toml`, o `SWARM_CONFIG`) > autodetección (`claude` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`/`OPENROUTER_API_KEY` → Ollama).

Variables: `SWARM_PLANNER`, `SWARM_WORKER`, `SWARM_ARCHITECT`, `SWARM_CODER`, `SWARM_TESTER`, `SWARM_MAX_PARALLEL`, `SWARM_MAX_ATTEMPTS`, `SWARM_DASHBOARD_PORT`, `SWARM_PREVIEW_PORT`. Ver [`.env.example`](.env.example).

### Seguridad

- Aprobación del plan **activada por defecto**, mostrando cada comando antes de ejecutarlo.
- La escritura de archivos está confinada a la raíz del proyecto.
- `claude -p` corre con `--permission-mode acceptEdits` y herramientas restringidas. Nunca con `--dangerously-skip-permissions` salvo que pongas `[claude] skip_permissions = true` (solo en sandboxes).
- Las API keys solo se leen de variables de entorno y se ocultan en logs y estado.
- Panel y vista previa escuchan solo en `127.0.0.1`. Sin telemetría.

### MCP (Claude Code / Cursor)

```bash
claude mcp add swarm-pilot -- swarm-pilot mcp
```

```json
// .cursor/mcp.json
{ "mcpServers": { "swarm-pilot": { "command": "swarm-pilot", "args": ["mcp"] } } }
```

Herramientas: `swarm_plan`, `swarm_run` (asíncrono: devuelve `run_id` y la URL del panel), `swarm_status`, `swarm_answer` y `swarm_stop`.

### Desarrollo

```bash
pip install -e ".[dev,all]"
ruff check . && ruff format --check .
pytest                       # determinista, sin tokens (usa un proveedor simulado)
SWARM_E2E=1 pytest tests/e2e # además, una prueba real con Claude Code (consume cuota)
```

Para ver el panel sin gastar tokens: `SWARM_PLANNER=fake SWARM_WORKER=fake SWARM_FAKE_DELAY=1 swarm-pilot run "demo" --yes`.

El diseño completo está en [PLAN.md](PLAN.md).

---

## English

A local, autonomous orchestrator for AI sub-agents. Give it an instruction: it writes a plan of atomic tasks, runs them with role-specialized agents, and **verifies every task before approving it**. When a verification fails, the agent repairs it on its own (up to 3 attempts).

```bash
pip install "swarm-pilot[all] @ git+https://github.com/deivycom21-cell/swarm-pilot.git"
swarm-pilot run "Build a modern landing page"
```

- **Planner → Workers → Verifier.** An editable `PLAN.md`, Architect, Coder and Tester roles, and parallel execution when it is safe (no overlapping files).
- **Bring your own model.** Your Claude Code session (`claude -p`), the Anthropic API, OpenAI, OpenRouter or a local Ollama. No middle-man servers, no hidden costs.
- **Hybrid mode.** A strong model for planning and fast or free ones for execution.
- **Command center** at `localhost:4000` and a **live preview** of your web app at `localhost:3000`.
- **MCP server.** Use it from Claude Code or Cursor.
- **Clean shutdown.** `Ctrl+C` leaves no orphan processes or busy ports behind (Windows included).

### Install

Python ≥ 3.10 and [git](https://git-scm.com) (installation is from GitHub), plus at least one provider: [Claude Code](https://claude.com/claude-code) on your PATH, an API key, or [Ollama](https://ollama.com).

```bash
pip install git+https://github.com/deivycom21-cell/swarm-pilot.git             # core: claude-cli, OpenAI, OpenRouter, Ollama
pip install "swarm-pilot[all] @ git+https://github.com/deivycom21-cell/swarm-pilot.git"      # + Anthropic SDK + MCP server
swarm-pilot doctor                  # checks providers, tools and ports
```

### How it works

1. **Plan.** The planner reads the directory and writes `PLAN.md`: tasks with a role, dependencies, files and verification commands.
2. **Approve.** It shows the plan and **every command it will run**. Edit `PLAN.md` by hand before approving if you like; it is re-read. `--yes` skips the prompt.
3. **Execute.** Tasks run in dependency order; two tasks run in parallel only when their files don't overlap.
4. **Verify.** Built-in checks (Python syntax, valid JSON, HTML assets exist) plus the task's own commands, with a timeout, inside the project.
5. **Repair.** On failure the same agent gets the error and its diff. After 3 attempts the task is **blocked**: only that branch pauses and asks you (terminal or dashboard) to retry with a hint, mark it fixed by hand, skip it, or abort.

State lives in `.swarm/state.json`; `PLAN.md` is its readable view. `swarm-pilot resume` picks up where a run stopped.

### Commands

`run "goal"` · `plan "goal"` · `resume` · `status` · `doctor [--cleanup]` · `init` · `mcp`. Run options: `--yes`, `--no-ui`, `--no-preview`, `--planner`, `--worker`, `--max-parallel`, `--quiet`, `--exit`, `--dir`. Exit codes: `0` done · `1` error/aborted · `2` paused (blocked tasks) · `130` interrupted.

### Providers and configuration

`claude-cli` (agentic, uses your subscription) · `anthropic[:model]` · `openai[:model]` · `openrouter:model` · `ollama:model`. Configure per role in `swarm.toml` (`swarm-pilot init` creates one) or with `SWARM_PLANNER` / `SWARM_WORKER` / `SWARM_CODER` … Priority: CLI flags > env vars > `./swarm.toml` > user config > auto-detection. See the Spanish section above for the full table.

### Security

Plan approval on by default (every command shown first) · file writes confined to the project root · `claude -p` runs with `acceptEdits` and restricted tools, never `--dangerously-skip-permissions` unless you opt in · keys read only from env vars and redacted from logs · servers bind to `127.0.0.1` only · no telemetry.

### MCP

`claude mcp add swarm-pilot -- swarm-pilot mcp` — tools: `swarm_plan`, `swarm_run` (async), `swarm_status`, `swarm_answer`, `swarm_stop`.

---

MIT License
