import asyncio
import json
import sys
import time

import pytest

from swarm_pilot.core.process_manager import ProcessManager, pid_alive, resolve_executable

PY = sys.executable

# Un hijo que lanza un nieto, imprime el PID del nieto y se queda dormido.
PARENT = """
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(g.pid, flush=True)
time.sleep(120)
"""

# Un hijo que lanza un nieto y termina en el acto, dejándolo huérfano.
ORPHANER = """
import subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(g.pid, flush=True)
"""


def wait_dead(*pids, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(pid_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


async def first_line(mp):
    async for line in mp.lines():
        return line
    raise AssertionError("sin salida")


async def test_shutdown_kills_child_and_grandchild(tmp_path):
    pid_file = tmp_path / ".swarm" / "pids.json"
    pm = ProcessManager(pid_file=pid_file, grace=1)
    mp = await pm.spawn([PY, "-c", PARENT], name="parent")
    grandchild = int(await asyncio.wait_for(first_line(mp), 15))
    assert pid_alive(mp.pid) and pid_alive(grandchild)
    assert json.loads(pid_file.read_text())["processes"][0]["name"] == "parent"

    await pm.shutdown()

    assert wait_dead(mp.pid, grandchild), "quedaron procesos vivos"
    assert not pid_file.exists()
    await pm.shutdown()  # idempotente
    with pytest.raises(RuntimeError):
        await pm.spawn([PY, "-c", "pass"])


async def test_shutdown_kills_orphaned_grandchild(tmp_path):
    pm = ProcessManager(grace=1)
    mp = await pm.spawn([PY, "-c", ORPHANER], name="orphaner")
    grandchild = int(await asyncio.wait_for(first_line(mp), 15))
    await asyncio.wait_for(mp.wait(), 15)  # el padre ya murió; el nieto sigue vivo
    assert pid_alive(grandchild)

    await pm.shutdown()

    assert wait_dead(grandchild), "el nieto huérfano sobrevivió al apagado"


async def test_run_captures_output_and_exit_code():
    pm = ProcessManager(grace=1)
    seen = []
    res = await pm.run(
        [PY, "-c", "import sys; print('hola'); print('error', file=sys.stderr); sys.exit(3)"],
        on_line=seen.append,
    )
    assert res.returncode == 3 and not res.ok and not res.timed_out
    assert "hola" in res.output and "error" in res.output  # stderr mezclado
    assert seen == res.output.splitlines()
    assert pm.processes == []
    await pm.shutdown()


async def test_run_keeps_only_output_tail():
    pm = ProcessManager(grace=1)
    res = await pm.run([PY, "-c", "for i in range(2000): print(f'linea {i}')"], max_output=200)
    assert res.ok and res.truncated
    assert res.output.endswith("linea 1999") and "linea 0\n" not in res.output
    assert len(res.output) <= 200
    await pm.shutdown()


async def test_run_timeout_kills_whole_tree():
    pm = ProcessManager(grace=1)
    lines = []
    res = await pm.run([PY, "-c", PARENT], timeout=3, on_line=lines.append)
    assert res.timed_out and res.returncode is None and not res.ok
    assert wait_dead(int(lines[0])), "el nieto sobrevivió al timeout"
    await pm.shutdown()


async def test_cancelling_run_kills_process():
    pm = ProcessManager(grace=1)
    lines = []
    task = asyncio.create_task(pm.run([PY, "-c", PARENT], on_line=lines.append))
    for _ in range(150):
        if lines:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_dead(int(lines[0]))
    await pm.shutdown()


async def test_cleanup_stale_after_simulated_crash(tmp_path):
    pid_file = tmp_path / "pids.json"
    pm = ProcessManager(pid_file=pid_file, grace=1)
    mp = await pm.spawn([PY, "-c", PARENT], name="dev-server")
    grandchild = int(await asyncio.wait_for(first_line(mp), 15))

    # Un run posterior encuentra el pids.json del run "caído".
    killed = await asyncio.to_thread(ProcessManager.cleanup_stale, pid_file, 1)

    assert killed == [f"dev-server (pid {mp.pid})"]
    assert wait_dead(mp.pid, grandchild)
    assert not pid_file.exists()
    await pm.shutdown()


async def test_cleanup_stale_ignores_reused_pid(tmp_path):
    pm = ProcessManager(grace=1)
    mp = await pm.spawn([PY, "-c", "import time; time.sleep(30)"])
    pid_file = tmp_path / "pids.json"
    pid_file.write_text(
        json.dumps(
            {
                "processes": [
                    {
                        "pid": mp.pid,
                        "create_time": 1.0,
                        "name": "otro",
                    }  # nació "en 1970": no es nuestro
                ]
            }
        )
    )
    assert ProcessManager.cleanup_stale(pid_file) == []
    assert pid_alive(mp.pid)
    await pm.shutdown()


def test_shutdown_sync_without_loop():
    async def start():
        pm = ProcessManager(grace=1)
        mp = await pm.spawn([PY, "-c", PARENT])
        return pm, mp, int(await asyncio.wait_for(first_line(mp), 15))

    loop = asyncio.new_event_loop()
    try:
        pm, mp, grandchild = loop.run_until_complete(start())
        pm.shutdown_sync()
        assert wait_dead(mp.pid, grandchild)
        # Recoger el proceso muerto para que asyncio cierre sus transports antes del loop.
        loop.run_until_complete(asyncio.wait_for(mp.wait(), 5))
    finally:
        loop.close()


def test_resolve_executable():
    assert resolve_executable(PY) == PY  # con directorio: se respeta tal cual
    with pytest.raises(FileNotFoundError):
        resolve_executable("definitivamente-no-existe-xyz")
