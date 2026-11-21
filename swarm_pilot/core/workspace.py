"""Workspace: aplica cambios de archivos sin salir del proyecto, y mide qué cambió.

- `safe_path()` rechaza rutas absolutas, `..` que escapen de la raíz, y escrituras dentro
  de `.git/` o `.swarm/`.
- `snapshot()` + `diff()` detectan qué archivos tocó un intento (lo use un adapter de
  completado o un agente que edita por su cuenta), y `unified_diff()` produce el diff que
  se le devuelve al agente cuando la verificación falla.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: Directorios que nunca se escanean (dependencias, cachés, metadatos).
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".swarm", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".cache", ".parcel-cache", "dist", "build", "coverage", ".idea", ".vscode", "target",
})  # fmt: skip
#: Archivos propios de swarm-pilot en la raíz: no cuentan como cambios de una tarea.
OWN_ROOT_FILES = frozenset({"PLAN.md"})
#: Directorios donde los agentes no pueden escribir.
PROTECTED_DIRS = frozenset({".git", ".swarm"})

MAX_TEXT_BYTES = 256 * 1024  # contenido guardado en el snapshot para diffs
MAX_SCAN_FILES = 20_000


class WorkspaceError(ValueError):
    pass


def safe_path(root: Path, rel: str) -> Path:
    """Ruta absoluta dentro de `root` para `rel`, o WorkspaceError si escapa o es protegida."""
    cleaned = rel.strip().replace("\\", "/")
    if not cleaned:
        raise WorkspaceError("ruta vacía")
    pure = PurePosixPath(cleaned)
    if pure.is_absolute() or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise WorkspaceError(f"ruta absoluta no permitida: {rel!r}")
    if pure.parts and pure.parts[0] in PROTECTED_DIRS:
        raise WorkspaceError(f"ruta protegida: {rel!r}")
    root_resolved = root.resolve()
    target = (root_resolved / Path(*pure.parts)).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise WorkspaceError(f"la ruta sale del proyecto: {rel!r}")
    if target == root_resolved:
        raise WorkspaceError(f"la ruta apunta a la raíz del proyecto: {rel!r}")
    return target


def rel_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


@dataclass(frozen=True)
class FileState:
    size: int
    digest: str
    text: str | None  # None si es binario o grande


@dataclass
class Snapshot:
    files: dict[str, FileState] = field(default_factory=dict)

    def __contains__(self, rel: str) -> bool:
        return rel in self.files


@dataclass
class Changes:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return sorted({*self.added, *self.modified, *self.deleted})

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def iter_files(root: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        at_root = Path(dirpath) == root
        for name in sorted(filenames):
            if at_root and name in OWN_ROOT_FILES:
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                return
            yield Path(dirpath) / name


def _read_state(path: Path) -> FileState | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    text: str | None = None
    if len(data) <= MAX_TEXT_BYTES and b"\0" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")  # texto en codificación legada (p. ej. cp1252)
        text = text.replace("\r\n", "\n")
    return FileState(len(data), hashlib.sha1(data).hexdigest(), text)


def snapshot(root: Path) -> Snapshot:
    snap = Snapshot()
    for path in iter_files(root):
        state = _read_state(path)
        if state is not None:
            snap.files[rel_posix(root, path)] = state
    return snap


def diff(before: Snapshot, after: Snapshot) -> Changes:
    ch = Changes()
    for rel, st in after.files.items():
        old = before.files.get(rel)
        if old is None:
            ch.added.append(rel)
        elif old.digest != st.digest:
            ch.modified.append(rel)
    ch.deleted = [rel for rel in before.files if rel not in after.files]
    ch.added.sort()
    ch.modified.sort()
    ch.deleted.sort()
    return ch


def unified_diff(
    before: Snapshot, after: Snapshot, max_chars: int = 8000, only: Iterable[str] | None = None
) -> str:
    """Diff legible de lo que cambió entre dos snapshots, recortado a `max_chars`.

    `only` limita el diff a esas rutas (p. ej. las atribuidas a una tarea concreta)."""
    parts: list[str] = []
    wanted = set(only) if only is not None else None
    for rel in diff(before, after).all:
        if wanted is not None and rel not in wanted:
            continue
        old, new = before.files.get(rel), after.files.get(rel)
        if (old and old.text is None) or (new and new.text is None):
            parts.append(f"--- {rel} (binario o grande, sin diff)\n")
            continue
        old_lines = (old.text if old else "").splitlines(keepends=True)
        new_lines = (new.text if new else "").splitlines(keepends=True)
        parts.append(
            "".join(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel}" if old else "/dev/null",
                    tofile=f"b/{rel}" if new else "/dev/null",
                    n=2,
                )
            )
        )
    text = "\n".join(p.rstrip("\n") for p in parts if p)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… (diff recortado, {len(text) - max_chars} caracteres más)"
    return text


@dataclass
class FileEdit:
    path: str
    content: str


def apply_edits(root: Path, files: Iterable[FileEdit], delete: Iterable[str] = ()) -> list[str]:
    """Aplica escrituras y borrados. Valida TODAS las rutas antes de tocar nada."""
    files = list(files)
    delete = list(delete)
    targets = [(safe_path(root, f.path), f) for f in files]
    removals = [safe_path(root, d) for d in delete]
    changed: list[str] = []
    for target, edit in targets:
        if target.is_dir():
            raise WorkspaceError(f"la ruta es un directorio: {edit.path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = edit.content
        if target.exists() and target.read_text(encoding="utf-8", errors="replace") == content:
            continue
        with target.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        changed.append(rel_posix(root, target))
    for target in removals:
        if target.is_file():
            target.unlink()
            changed.append(rel_posix(root, target))
    return changed


def tree(root: Path, max_entries: int = 300, max_depth: int = 4) -> str:
    """Árbol compacto del proyecto (para prompts)."""
    lines: list[str] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        if depth >= max_depth:
            dirnames[:] = []
        indent = "  " * depth
        if depth:
            lines.append(f"{'  ' * (depth - 1)}{rel.name}/")
        for name in sorted(filenames):
            lines.append(f"{indent}{name}")
            if len(lines) >= max_entries:
                lines.append("… (árbol recortado)")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(directorio vacío)"
