"""E2E de la CLI como subproceso, con el proveedor `fake` (determinista, sin tokens).

La prueba real con Claude Code se activa con `SWARM_E2E=1` (consume cuota de la suscripción).
"""

import json
import os
import shutil
import subprocess
import sys

import pytest


def cli(*args, cwd=None, env_extra=None, timeout=120):
    env = {
        **os.environ,
        "SWARM_PLANNER": "fake",
        "SWARM_WORKER": "fake",
        "PYTHONUTF8": "1",
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "swarm_pilot", *args],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )  # fmt: skip


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "none.toml"))
    return tmp_path


def test_run_headless_builds_project(isolated):
    project = isolated / "landing"
    res = cli("run", "Crea una landing", "--yes", "--no-ui", "--no-preview", "--exit",
              "--dir", str(project))  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Proyecto terminado" in res.stdout
    assert "T3 intento 1 falló" in res.stdout  # la auto-reparación se ve en la terminal
    assert json.loads((project / "config.json").read_text(encoding="utf-8"))["features"]
    events = list((project / ".swarm" / "runs").glob("*/events.jsonl"))
    assert events and events[0].read_text(encoding="utf-8").count("\n") > 10


def test_plan_status_resume_cycle(isolated):
    project = isolated / "p"
    res = cli("plan", "Crea una landing", "--dir", str(project))
    assert res.returncode == 0, res.stderr
    flat = " ".join(res.stdout.split())  # Rich ajusta líneas al ancho de la terminal
    assert (project / "PLAN.md").is_file() and "swarm-pilot resume" in flat

    status = cli("status", "--dir", str(project))
    assert "0 hechas" in status.stdout and "3 pendientes" in status.stdout

    # El usuario edita PLAN.md antes de ejecutar: quita la tarea T3. Pero index.html (T1)
    # enlaza app.js, que ya no crea nadie: el verifier lo detecta y, sin nadie al teclado
    # (stdin = NUL/devnull), el run se pausa en vez de quedarse esperando una respuesta.
    md = (project / "PLAN.md").read_text(encoding="utf-8")
    md = md[: md.index("### [ ] T3")]
    (project / "PLAN.md").write_text(md, encoding="utf-8")

    res = cli("resume", "--yes", "--no-ui", "--no-preview", "--exit", "--dir", str(project),
              timeout=60)  # fmt: skip
    assert res.returncode == 2, res.stdout + res.stderr  # EXIT_PAUSED
    flat = " ".join(res.stdout.split())
    assert "Retomando el plan existente" in flat and "En pausa" in flat
    assert "app.js" in flat  # el error se explica
    assert not (project / "config.json").exists()  # T3 ya no existía
    status = " ".join(cli("status", "--dir", str(project)).stdout.split())
    assert "1 bloqueadas" in status and "T1 bloqueada" in status


def test_errors_are_explained(isolated):
    empty = isolated / "vacio"
    res = cli("run", "--no-ui", "--no-preview", "--exit", "--dir", str(empty))
    assert res.returncode == 1 and "No hay plan" in res.stderr

    res = cli("run", "x", "--no-ui", "--no-preview", "--exit", "--dir", str(empty))
    assert res.returncode == 0 and "usa --yes" in res.stderr  # no interactivo sin aprobación

    bad = cli("run", "x", "--yes", "--dir", str(empty), env_extra={"SWARM_PLANNER": "gemini"})
    assert bad.returncode == 1 and "proveedor desconocido" in bad.stderr


def test_init_and_version(isolated):
    res = cli("init", "--dir", str(isolated))
    assert res.returncode == 0 and (isolated / "swarm.toml").is_file()
    assert "ya existe" in cli("init", "--dir", str(isolated)).stdout.lower()
    assert cli("--version").stdout.startswith("swarm-pilot ")


@pytest.mark.skipif(os.environ.get("SWARM_E2E") != "1", reason="prueba real: SWARM_E2E=1")
@pytest.mark.skipif(shutil.which("claude") is None, reason="requiere Claude Code")
def test_real_claude_cli_landing(isolated):
    project = isolated / "real"
    res = cli(
        "run", "Crea una página HTML estática con un título y un botón que cambie el texto.",
        "--yes", "--no-ui", "--no-preview", "--exit", "--dir", str(project),
        env_extra={"SWARM_PLANNER": "claude-cli", "SWARM_WORKER": "claude-cli"},
        timeout=1800,
    )  # fmt: skip
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    assert list(project.glob("**/*.html"))
