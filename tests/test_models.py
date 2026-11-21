import pytest
from pydantic import ValidationError

from swarm_pilot.core.models import (
    Event,
    EventType,
    Plan,
    Role,
    Task,
    TaskStatus,
    paths_overlap,
    tasks_conflict,
)


def T(id, depends=(), files=(), **kw):
    return Task(id=id, title=f"tarea {id}", depends=list(depends), files=list(files), **kw)


def test_plan_valid_and_topological_order_is_stable():
    plan = Plan(goal="g", tasks=[T("C", ["A"]), T("A"), T("B"), T("D", ["C", "B"])])
    assert [t.id for t in plan.topological_order()] == ["A", "B", "C", "D"]


@pytest.mark.parametrize(
    "tasks, msg",
    [
        ([T("A"), T("A")], "duplicados"),
        ([T("A", ["Z"])], "inexistentes"),
        ([T("A", ["A"])], "sí misma"),
        ([T("A", ["C"]), T("B", ["A"]), T("C", ["B"])], "ciclo"),
    ],
)
def test_plan_rejects_invalid_graphs(tasks, msg):
    with pytest.raises(ValidationError, match=msg):
        Plan(goal="g", tasks=tasks)


@pytest.mark.parametrize("bad", ["", "1abc", "a b", "T/1"])
def test_task_rejects_bad_ids(bad):
    with pytest.raises(ValidationError):
        T(bad)


def test_task_normalizes_files_and_dedupes_depends():
    t = Task(id="T1", title=" x ", depends=["A", "A"], files=[".\\src\\a.py", "./src/a.py", ""])
    assert t.files == ["src/a.py"]
    assert t.depends == ["A"]
    assert t.title == "x"
    assert t.role is Role.CODER


def test_ready_tasks_follow_dependencies():
    plan = Plan(goal="g", tasks=[T("A"), T("B", ["A"]), T("C", ["A"]), T("D", ["B", "C"])])
    assert [t.id for t in plan.ready_tasks()] == ["A"]
    plan.get("A").status = TaskStatus.DONE
    assert [t.id for t in plan.ready_tasks()] == ["B", "C"]
    plan.get("B").status = TaskStatus.DONE
    plan.get("C").status = TaskStatus.SKIPPED  # saltada también satisface
    assert [t.id for t in plan.ready_tasks()] == ["D"]


def test_blocked_task_stalls_only_its_branch():
    plan = Plan(goal="g", tasks=[T("A"), T("B", ["A"]), T("C", ["B"]), T("X")])
    plan.get("A").status = TaskStatus.BLOCKED
    assert plan.stalled_by_blocked() == {"B", "C"}
    assert [t.id for t in plan.ready_tasks()] == ["X"]


def test_is_finished_and_counts():
    plan = Plan(goal="g", tasks=[T("A"), T("B")])
    assert not plan.is_finished
    plan.get("A").status = TaskStatus.DONE
    plan.get("B").status = TaskStatus.SKIPPED
    assert plan.is_finished
    assert plan.counts()["done"] == 1 and plan.counts()["skipped"] == 1


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/b.py", False),
        ("src", "src/a.py", True),
        ("src/", "src/deep/a.py", True),
        ("src/*.py", "src/a.py", True),
        ("pkg/**/__init__.py", "pkg/core/__init__.py", True),
        ("srcx/a.py", "src", False),
    ],
)
def test_paths_overlap(a, b, expected):
    assert paths_overlap(a, b) is expected
    assert paths_overlap(b, a) is expected


def test_tasks_conflict():
    a = T("A", files=["index.html"])
    b = T("B", files=["styles.css"])
    c = T("C", files=["index.html", "app.js"])
    assert not tasks_conflict(a, [b])
    assert tasks_conflict(c, [b, a])
    assert not tasks_conflict(T("D"), [a])  # sin archivos declarados, no hay conflicto


def test_plan_round_trips_through_json():
    plan = Plan(goal="g", tasks=[T("A", files=["a.py"], verify=["pytest"]), T("B", ["A"])])
    again = Plan.model_validate_json(plan.model_dump_json())
    assert again == plan


def test_event_defaults():
    ev = Event(type=EventType.TASK_STATUS, task_id="A", payload={"status": "running"})
    assert ev.ts.tzinfo is not None
    assert Event.model_validate_json(ev.model_dump_json()) == ev
