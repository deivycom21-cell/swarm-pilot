"""Gestión de subprocesos y apagado limpio, sin procesos huérfanos.

Todo subproceso de swarm-pilot (claude -p, dev servers, comandos de verificación) se
lanza con `ProcessManager.spawn()` / `.run()`. Al apagar se mata el árbol completo:

- POSIX: cada hijo abre su propia sesión (`start_new_session`), así que su grupo de
  procesos agrupa a todos sus descendientes → SIGTERM al grupo, gracia, SIGKILL.
- Windows: cada hijo va en su propio grupo de consola (CTRL_BREAK para un cierre
  amable) y además en un Job Object con KILL_ON_JOB_CLOSE: si swarm-pilot muere de
  golpe, Windows mata a todos los descendientes al cerrarse el handle.
- `pids.json` registra lo vivo para limpiar tras un crash (`doctor --cleanup`).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import locale
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

IS_WINDOWS = sys.platform == "win32"
STREAM_LIMIT = 16 * 1024 * 1024  # líneas largas: `claude --output-format stream-json`


def resolve_executable(cmd: str) -> str:
    """Ruta absoluta del ejecutable. En Windows resuelve también .cmd/.bat vía PATHEXT."""
    if os.path.dirname(cmd):
        return cmd
    found = shutil.which(cmd)
    if not found:
        raise FileNotFoundError(f"no se encontró el ejecutable {cmd!r} en el PATH")
    return found


def decode_line(raw: bytes) -> str:
    """UTF-8 si es válido; si no, la codificación local (p. ej. cp1252 en Windows)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")
    return text.rstrip("\r\n")


def _is_dead(proc: psutil.Process) -> bool:
    try:
        return proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def pid_alive(pid: int) -> bool:
    try:
        return not _is_dead(psutil.Process(pid))
    except psutil.NoSuchProcess:
        return False


def _same_process(proc: psutil.Process, create_time: float | None) -> bool:
    """Protege contra reutilización de PIDs: el proceso debe haber nacido cuando creemos."""
    if create_time is None:
        return True
    try:
        return abs(proc.create_time() - create_time) < 1.0
    except psutil.Error:
        return False


def kill_tree(pid: int, grace: float = 5.0, create_time: float | None = None) -> list[int]:
    """Termina `pid` y todos sus descendientes: primero amable, tras `grace` s a la fuerza."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    if not _same_process(root, create_time):
        return []
    try:
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    procs = [root, *children]

    if IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # al grupo de consola del hijo
    else:
        with contextlib.suppress(OSError):
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
        for p in procs:
            with contextlib.suppress(psutil.Error):
                p.terminate()

    _, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    if alive:
        psutil.wait_procs(alive, timeout=3)
    return [p.pid for p in procs]


# --- Job Object (Windows) ---------------------------------------------------


class _WindowsJob:
    """Job Object con KILL_ON_JOB_CLOSE. Si algo falla, se degrada a no-op."""

    def __init__(self) -> None:
        self.handle = None
        try:
            self._setup()
        except Exception:  # pragma: no cover - entornos raros (jobs anidados antiguos)
            self.handle = None

    def _setup(self) -> None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self._k32 = k32
        self.handle = handle

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        hproc = self._k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not hproc:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, hproc))
        finally:
            self._k32.CloseHandle(hproc)

    def close(self) -> None:
        """Mata todo lo que quede en el job y libera el handle."""
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)
            self._k32.CloseHandle(self.handle)
            self.handle = None


# --- procesos gestionados ---------------------------------------------------


@dataclass
class RunResult:
    returncode: int | None
    output: str
    timed_out: bool
    duration: float
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class ManagedProcess:
    def __init__(self, name: str, proc: asyncio.subprocess.Process, args: Sequence[str]) -> None:
        self.name = name
        self.proc = proc
        self.args = list(args)
        self.pid = proc.pid
        try:
            self.create_time: float | None = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            self.create_time = None

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    async def wait(self) -> int:
        return await self.proc.wait()

    async def lines(self) -> AsyncIterator[str]:
        """Líneas de stdout (stderr va mezclado por defecto), decodificadas en UTF-8."""
        stream = self.proc.stdout
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except ValueError:  # línea mayor que STREAM_LIMIT: se descarta ese trozo
                continue
            if not raw:
                return
            yield decode_line(raw)

    def __repr__(self) -> str:
        return f"<ManagedProcess {self.name} pid={self.pid} rc={self.returncode}>"


class ProcessManager:
    def __init__(self, pid_file: Path | None = None, grace: float = 5.0) -> None:
        self.pid_file = pid_file
        self.grace = grace
        self._procs: dict[int, ManagedProcess] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._groups: set[int] = set()  # POSIX: grupos creados en esta sesión
        self._started = time.time()
        self._closed = False
        self._job = _WindowsJob() if IS_WINDOWS else None

    # --- lanzar -------------------------------------------------------------

    async def spawn(
        self,
        args: Sequence[str] | str,
        *,
        name: str | None = None,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | None = subprocess.DEVNULL,
        stdout: int | None = subprocess.PIPE,
        merge_stderr: bool = True,
    ) -> ManagedProcess:
        """Lanza un proceso. Si `args` es un str, se ejecuta mediante la shell del sistema
        (cmd.exe en Windows, /bin/sh en POSIX) tal cual, sin re-escapar comillas."""
        if self._closed:
            raise RuntimeError("ProcessManager ya está apagado")
        if not args:
            raise ValueError("args vacío")
        kwargs: dict[str, object] = {
            "cwd": str(cwd) if cwd is not None else None,
            "env": dict(env) if env is not None else None,
            "stdin": stdin,
            "stdout": stdout,
            "stderr": subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            "limit": STREAM_LIMIT,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        if isinstance(args, str):
            argv = [args]
            proc = await asyncio.create_subprocess_shell(args, **kwargs)
            default_name = "sh"
        else:
            argv = [resolve_executable(args[0]), *args[1:]]
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
            default_name = Path(argv[0]).stem
        if self._job is not None:
            self._job.assign(proc.pid)
        else:
            self._groups.add(proc.pid)
        mp = ManagedProcess(name or default_name, proc, argv)
        self._procs[mp.pid] = mp
        self._save_pids()
        watcher = asyncio.create_task(self._watch(mp))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return mp

    async def _watch(self, mp: ManagedProcess) -> None:
        await mp.proc.wait()
        self._untrack(mp)

    async def run(
        self,
        args: Sequence[str] | str,
        *,
        name: str | None = None,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_output: int = 64_000,
        on_line: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Ejecuta hasta terminar; guarda la *cola* de la salida (`max_output` caracteres).

        Al superar `timeout` (o si se cancela la corrutina) se mata el árbol completo.
        """
        start = time.monotonic()
        mp = await self.spawn(args, name=name, cwd=cwd, env=env)
        tail: deque[str] = deque()
        size = 0
        truncated = False

        async def pump() -> None:
            nonlocal size, truncated
            async for line in mp.lines():
                if on_line is not None:
                    on_line(line)
                tail.append(line)
                size += len(line) + 1
                while size > max_output and len(tail) > 1:
                    size -= len(tail.popleft()) + 1
                    truncated = True

        pump_task = asyncio.create_task(pump())
        timed_out = False
        try:
            await asyncio.wait_for(mp.proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if mp.returncode is None:  # timeout o cancelación
                await self.terminate(mp, grace=min(self.grace, 2.0))
            try:
                # Si un nieto heredó el pipe y sigue vivo, no esperamos para siempre.
                await asyncio.wait_for(asyncio.shield(pump_task), 5)
            except asyncio.TimeoutError:
                pump_task.cancel()
                self._kill_leftovers(mp.pid)
            self._untrack(mp)
        return RunResult(
            returncode=None if timed_out else mp.returncode,
            output="\n".join(tail),
            timed_out=timed_out,
            duration=time.monotonic() - start,
            truncated=truncated,
        )

    # --- terminar -----------------------------------------------------------

    async def terminate(self, mp: ManagedProcess, grace: float | None = None) -> None:
        grace = self.grace if grace is None else grace
        await asyncio.to_thread(kill_tree, mp.pid, grace, mp.create_time)
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(mp.proc.wait(), 3)
        self._untrack(mp)

    async def shutdown(self) -> None:
        """Mata todos los árboles en paralelo y limpia restos. Idempotente."""
        if self._closed:
            return
        self._closed = True
        procs = list(self._procs.values())
        await asyncio.gather(*(self.terminate(p) for p in procs), return_exceptions=True)
        for w in list(self._watchers):
            w.cancel()
        self._finalize()

    def shutdown_sync(self) -> None:
        """Versión síncrona para `atexit` o cuando ya no hay event loop."""
        if self._closed:
            return
        self._closed = True
        for mp in list(self._procs.values()):
            with contextlib.suppress(Exception):
                kill_tree(mp.pid, min(self.grace, 2.0), mp.create_time)
        self._procs.clear()
        self._finalize()

    def _finalize(self) -> None:
        if self._job is not None:
            self._job.close()  # restos huérfanos dentro del job
        for pgid in list(self._groups):
            self._kill_leftovers(pgid)
        self._groups.clear()
        self._procs.clear()
        if self.pid_file is not None:
            with contextlib.suppress(OSError):
                self.pid_file.unlink()

    def _kill_leftovers(self, pgid: int) -> None:
        """POSIX: mata procesos del grupo `pgid` nacidos durante esta sesión."""
        if IS_WINDOWS:
            return
        for p in psutil.process_iter(["create_time"]):
            try:
                if os.getpgid(p.pid) == pgid and p.info["create_time"] >= self._started - 1:
                    p.kill()
            except (OSError, psutil.Error):
                continue

    def install_signal_handlers(self, on_signal: Callable[[], None]) -> None:
        """Llama a `on_signal` en el loop ante Ctrl+C / SIGTERM y registra el apagado en atexit."""
        loop = asyncio.get_running_loop()
        if IS_WINDOWS:

            def handler(*_: object) -> None:
                loop.call_soon_threadsafe(on_signal)

            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGBREAK, handler)
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, on_signal)
        atexit.register(self.shutdown_sync)

    # --- registro -----------------------------------------------------------

    @property
    def processes(self) -> list[ManagedProcess]:
        return list(self._procs.values())

    def _untrack(self, mp: ManagedProcess) -> None:
        if self._procs.pop(mp.pid, None) is not None:
            self._save_pids()

    def _save_pids(self) -> None:
        if self.pid_file is None:
            return
        if not self._procs:
            with contextlib.suppress(OSError):
                self.pid_file.unlink()
            return
        data = {
            "owner": os.getpid(),
            "processes": [
                {"pid": p.pid, "create_time": p.create_time, "name": p.name, "args": p.args}
                for p in self._procs.values()
            ],
        }
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.pid_file)

    @staticmethod
    def cleanup_stale(pid_file: Path, grace: float = 2.0) -> list[str]:
        """Mata los procesos registrados por un run anterior que se cayó. Devuelve sus nombres."""
        try:
            data = json.loads(pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        killed: list[str] = []
        for entry in data.get("processes", []):
            if kill_tree(int(entry["pid"]), grace, entry.get("create_time")):
                killed.append(f"{entry.get('name', '?')} (pid {entry['pid']})")
        with contextlib.suppress(OSError):
            pid_file.unlink()
        return killed
