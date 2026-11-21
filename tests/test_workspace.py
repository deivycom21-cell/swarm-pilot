import pytest

from swarm_pilot.core.workspace import (
    FileEdit,
    WorkspaceError,
    apply_edits,
    diff,
    safe_path,
    snapshot,
    tree,
    unified_diff,
)


@pytest.mark.parametrize(
    "rel",
    [
        "../fuera.txt",
        "a/../../fuera.txt",
        "/etc/passwd",
        "C:/Windows/x",
        "C:\\x",
        ".git/config",
        ".swarm/state.json",
        "",
        ".",
        "a/..",
    ],
)
def test_safe_path_rejects(tmp_path, rel):
    with pytest.raises(WorkspaceError):
        safe_path(tmp_path, rel)


@pytest.mark.parametrize("rel", ["a.txt", "src/app.py", "./src/../src/x.py", "src\\win.py"])
def test_safe_path_accepts(tmp_path, rel):
    p = safe_path(tmp_path, rel)
    assert tmp_path.resolve() in p.parents


def test_apply_edits_validates_everything_first(tmp_path):
    with pytest.raises(WorkspaceError):
        apply_edits(tmp_path, [FileEdit("ok.txt", "x"), FileEdit("../malo.txt", "y")])
    assert not (tmp_path / "ok.txt").exists(), "no debe aplicar nada si una ruta es inválida"


def test_apply_edits_writes_skips_identical_and_deletes(tmp_path):
    (tmp_path / "old.txt").write_text("bye")
    changed = apply_edits(
        tmp_path,
        [FileEdit("src/a.py", "print(1)\n"), FileEdit("src/b.py", "")],
        delete=["old.txt", "no-existe.txt"],
    )
    assert changed == ["src/a.py", "src/b.py", "old.txt"]
    assert (tmp_path / "src/a.py").read_text() == "print(1)\n"
    assert apply_edits(tmp_path, [FileEdit("src/a.py", "print(1)\n")]) == []


def test_snapshot_diff_and_unified_diff(tmp_path):
    (tmp_path / "keep.txt").write_text("igual\n")
    (tmp_path / "mod.txt").write_text("uno\ndos\n")
    (tmp_path / "del.txt").write_text("adiós\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("ignorado")
    before = snapshot(tmp_path)
    assert "node_modules/x.js" not in before

    (tmp_path / "mod.txt").write_text("uno\nDOS\n")
    (tmp_path / "del.txt").unlink()
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "n.txt").write_text("hola\n")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2")
    after = snapshot(tmp_path)

    ch = diff(before, after)
    assert ch.added == ["bin.dat", "new/n.txt"]
    assert ch.modified == ["mod.txt"]
    assert ch.deleted == ["del.txt"]
    text = unified_diff(before, after)
    assert "-dos" in text and "+DOS" in text and "+hola" in text and "-adiós" in text
    assert "bin.dat (binario" in text
    assert len(unified_diff(before, after, max_chars=20)) < 80


def test_tree(tmp_path):
    assert tree(tmp_path) == "(directorio vacío)"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    (tmp_path / "README.md").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("")
    assert tree(tmp_path) == "README.md\nsrc/\n  a.py"
