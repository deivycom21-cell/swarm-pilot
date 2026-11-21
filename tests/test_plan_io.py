import os

import pytest

from swarm_pilot.core import plan_io
from swarm_pilot.core.models import Attempt, Plan, Role, Task, TaskStatus


def sample_plan() -> Plan:
    return Plan(
        goal="Arma una API en FastAPI",
        tasks=[
            Task(
                id="T1",
                title="Esqueleto del proyecto",
                role=Role.ARCHITECT,
                files=["pyproject.toml", "app/__init__.py"],
                verify=["python -m compileall app"],
            ),
            Task(
                id="T2",
                title="Endpoint /health",
                depends=["T1"],
                files=["app/main.py"],
                verify=["python -m pytest -q", "ruff check app"],
                detail='Devuelve {"ok": true}.\nIncluye test con TestClient.',
            ),
            Task(
                id="T3",
                title="Tests",
                role=Role.TESTER,
                depends=["T1", "T2"],
                status=TaskStatus.DONE,
            ),
            Task(id="T4", title="Docs", status=TaskStatus.SKIPPED),
            Task(id="T5", title="Bloqueada", status=TaskStatus.BLOCKED),
        ],
    )


def test_round_trip_preserves_everything_but_attempts():
    plan = sample_plan()
    again = plan_io.parse_plan_md(plan_io.render_plan_md(plan))
    assert again.goal == plan.goal
    for a, b in zip(plan.tasks, again.tasks, strict=True):
        assert a.model_dump(exclude={"attempts"}) == b.model_dump(exclude={"attempts"})


def test_running_task_is_reread_as_pending():
    plan = sample_plan()
    plan.get("T1").status = TaskStatus.RUNNING
    assert "### [~] T1" in plan_io.render_plan_md(plan)
    again = plan_io.parse_plan_md(plan_io.render_plan_md(plan))
    assert again.get("T1").status is TaskStatus.PENDING


def test_parse_hand_written_plan():
    text = """# Mi plan

**Objetivo:** Landing page

### [ ] A1: Hero
- role: architect
- files: index.html, styles.css
- verify: —

### [x] A2 - Formulario
- depende: A1
- archivos: `form.js`
- detalle: valida el email
  y muestra errores.
- verificar: `node --check form.js`
"""
    plan = plan_io.parse_plan_md(text)
    assert [t.id for t in plan.tasks] == ["A1", "A2"]
    assert plan.get("A1").files == ["index.html", "styles.css"]
    assert plan.get("A1").verify == []
    assert plan.get("A2").status is TaskStatus.DONE
    assert plan.get("A2").detail == "valida el email\ny muestra errores."
    assert plan.get("A2").verify == ["node --check form.js"]


@pytest.mark.parametrize(
    "text, match",
    [
        ("### [ ] T1 · x\n", "Objetivo"),
        ("**Objetivo:** g\n", "no contiene tareas"),
        ("**Objetivo:** g\n### [?] T1 · x\n", "marca"),
        ("**Objetivo:** g\n### [ ] T1 · x\n- depende: T9\n", "inexistentes"),
    ],
)
def test_parse_errors(text, match):
    with pytest.raises(plan_io.PlanParseError, match=match):
        plan_io.parse_plan_md(text)


def test_save_and_load_state(tmp_path):
    plan = sample_plan()
    plan.get("T2").attempts.append(Attempt(number=1, ok=False, error="boom"))
    plan_io.save(tmp_path, plan, run_id="r1")
    assert (tmp_path / "PLAN.md").is_file()
    state = plan_io.load(tmp_path)
    assert state.run_id == "r1"
    assert state.plan.get("T2").attempts[0].error == "boom"


def test_manual_edit_of_plan_md_is_picked_up(tmp_path):
    plan = sample_plan()
    plan.get("T2").attempts.append(Attempt(number=1, ok=False, error="boom"))
    plan_io.save(tmp_path, plan, run_id="r1")
    md = tmp_path / "PLAN.md"
    text = md.read_text(encoding="utf-8").replace("Endpoint /health", "Endpoint /status")
    text += "\n### [ ] T6 · Nueva tarea del usuario\n- depende: T2\n"
    md.write_text(text, encoding="utf-8")
    st = os.stat(md)
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # asegura mtime distinto

    state = plan_io.load(tmp_path)
    assert state.plan.get("T2").title == "Endpoint /status"
    assert state.plan.get("T2").attempts[0].error == "boom"  # se conservan los intentos
    assert state.plan.get("T6").depends == ["T2"]
    assert state.run_id == "r1"


def test_load_with_only_plan_md(tmp_path):
    (tmp_path / "PLAN.md").write_text("**Objetivo:** g\n### [ ] T1 · x\n", encoding="utf-8")
    assert plan_io.load(tmp_path).plan.get("T1").title == "x"
    assert plan_io.load(tmp_path / "vacio") is None
