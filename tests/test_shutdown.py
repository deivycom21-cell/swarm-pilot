"""T20: la CLI real, interrumpida como con Ctrl+C, no deja procesos ni puertos abiertos."""

import os
import signal
import subprocess
import sys
import time

import httpx
import psutil
import pytest

from swarm_pilot.core.preview import find_free_port, port_is_free

IS_WINDOWS = sys.platform == "win32"


def wait_until(predicate, timeout=60.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def descendants(pid):
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


@pytest.mark.timeout(180)
def test_ctrl_c_closes_dashboard_preview_and_children(tmp_path):
    dash = find_free_port(4650)
    prev = find_free_port(3650)
    env = {
        **os.environ,
        "SWARM_CONFIG": str(tmp_path / "none.toml"),
        "SWARM_PLANNER": "fake",
        "SWARM_WORKER": "fake",
        "SWARM_FAKE_DELAY": "0.2",
        "SWARM_DASHBOARD_PORT": str(dash),
        "SWARM_PREVIEW_PORT": str(prev),
        "PYTHONUTF8": "1",
    }
    project = tmp_path / "web"
    log = (tmp_path / "cli.log").open("w", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_pilot", "run", "Crea una landing", "--yes",
         "--dir", str(project)],
        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=flags, start_new_session=not IS_WINDOWS,
    )  # fmt: skip
    try:

        def finished():
            try:
                state = httpx.get(f"http://127.0.0.1:{dash}/api/state", timeout=2).json()
            except (httpx.HTTPError, ValueError):
                return False
            return state.get("summary") is not None and state["preview"]["state"] == "ready"

        assert wait_until(finished, 120), (tmp_path / "cli.log").read_text(encoding="utf-8")
        page = httpx.get(f"http://127.0.0.1:{prev}/", timeout=5)
        assert "Despliega en segundos" in page.text  # la landing generada se sirve en vivo
        children = descendants(proc.pid)
        assert children, "debería haber al menos el servidor de la vista previa"

        os.kill(proc.pid, signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
        code = proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()

    output = (tmp_path / "cli.log").read_text(encoding="utf-8")
    assert code == 130, output
    assert "Todo cerrado" in output
    assert wait_until(
        lambda: not any(c.is_running() and c.status() != psutil.STATUS_ZOMBIE for c in children), 15
    ), "quedaron procesos huérfanos"
    assert wait_until(lambda: port_is_free(dash) and port_is_free(prev), 15), "puertos ocupados"
    assert not (project / ".swarm" / "pids.json").exists()
    assert "### [x] T3" in (project / "PLAN.md").read_text(encoding="utf-8")
