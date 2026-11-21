import sys

import pytest

from swarm_pilot.core.models import Plan, Task, TaskStatus
from swarm_pilot.core.process_manager import ProcessManager
from swarm_pilot.core.verifier import Verifier

PY = f'"{sys.executable}"'


@pytest.fixture
async def verifier(tmp_path):
    pm = ProcessManager(grace=1)
    yield Verifier(tmp_path, pm, timeout=20)
    await pm.shutdown()


def plan_with(*tasks):
    return Plan(goal="g", tasks=list(tasks))


async def test_no_changes_fails(verifier):
    t = Task(id="T1", title="x", files=["a.py"])
    res = await verifier.verify(t, [], plan_with(t))
    assert not res.ok and "no creó ni modificó" in res.report()


async def test_python_and_json_syntax(verifier, tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def f(:\n")
    (tmp_path / "bad.json").write_text('{"a": }')
    (tmp_path / "tsconfig.json").write_text("{ // comentario permitido\n}")
    t = Task(id="T1", title="x")
    res = await verifier.verify(t, ["ok.py", "bad.py", "bad.json", "tsconfig.json"], plan_with(t))
    report = res.report()
    assert not res.ok
    assert "bad.py:1" in report and "bad.json: JSON inválido" in report
    assert "tsconfig" not in report


async def test_html_assets(verifier, tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css"><script src="app.js?v=2"></script>'
        '<img src="https://cdn/x.png"><img src="/logo.svg"><img src="{{ dynamic }}">'
        '<link rel="preconnect" href="nope.css">'
    )
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "logo.svg").write_text("<svg/>")
    t1 = Task(id="T1", title="html", files=["index.html"])
    t2 = Task(id="T2", title="css", files=["styles.css"], depends=["T1"])
    plan = plan_with(t1, t2)

    res = await verifier.verify(t1, ["index.html"], plan)
    assert not res.ok
    assert "app.js?v=2" in res.report() and "styles.css" not in res.report()

    (tmp_path / "app.js").write_text("")
    assert (await verifier.verify(t1, ["index.html"], plan)).ok
    t2.status = TaskStatus.DONE  # si la tarea que lo creaba ya terminó, ahora sí falta
    assert "styles.css" in (await verifier.verify(t1, ["index.html"], plan)).report()


async def test_commands_run_in_order_and_stop_at_failure(verifier, tmp_path):
    logs = []
    verifier.on_log = logs.append
    t = Task(id="T1", title="x", verify=[
        f'{PY} -c "print(\'primero ok\')"',
        f'{PY} -c "import sys; print(\'roto\'); sys.exit(2)"',
        f'{PY} -c "print(\'no debe correr\')"',
    ])  # fmt: skip
    res = await verifier.verify(t, ["a.txt"], plan_with(t))
    assert not res.ok
    assert [c.ok for c in res.checks] == [True, True, False]  # integrados, cmd1, cmd2
    assert "roto" in res.report() and "no debe correr" not in str(logs)
    assert any(line.startswith("✗") for line in logs)


async def test_command_timeout(verifier):
    verifier.timeout = 2
    t = Task(id="T1", title="x", verify=[f'{PY} -c "import time; time.sleep(30)"'])
    res = await verifier.verify(t, ["a.txt"], plan_with(t))
    assert not res.ok and "tiempo límite" in res.report()


async def test_command_env_is_non_interactive(verifier):
    t = Task(id="T1", title="x", verify=[
        f'{PY} -c "import os, sys; sys.exit(0 if os.environ[\'CI\'] == \'1\' else 1)"'
    ])  # fmt: skip
    assert (await verifier.verify(t, ["a.txt"], plan_with(t))).ok


def test_needs_npm_install(verifier, tmp_path):
    t = Task(id="T1", title="x", verify=["npm run build"])
    assert not verifier.needs_npm_install(t, [])  # sin package.json
    (tmp_path / "package.json").write_text("{}")
    assert verifier.needs_npm_install(t, [])
    assert not verifier.needs_npm_install(Task(id="T2", title="y", verify=["pytest"]), [])
    (tmp_path / "node_modules").mkdir()
    assert not verifier.needs_npm_install(t, [])
    assert verifier.needs_npm_install(t, ["package.json"])  # cambió: reinstalar
