"""Descripción del entorno local (SO, herramientas) para prompts y para `doctor`."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = ("python", "python3", "py", "node", "npm", "npx", "pnpm", "yarn", "bun", "git", "ruff")


@dataclass
class Environment:
    os_name: str
    shell: str
    tools: dict[str, str] = field(default_factory=dict)

    @property
    def python_cmd(self) -> str:
        for name in ("python", "python3", "py"):
            if name in self.tools:
                return name
        return "python"

    def describe(self) -> str:
        tools = ", ".join(sorted(self.tools)) or "(ninguna detectada)"
        return (
            f"OS: {self.os_name}. Commands run through {self.shell} from the project root.\n"
            f"Available tools on PATH: {tools}. Use `{self.python_cmd}` to invoke Python."
        )


def detect_environment(project_dir: Path | None = None) -> Environment:
    tools = {}
    path = venv_path(project_dir) if project_dir else None
    for name in TOOLS:
        found = shutil.which(name, path=path)
        if found:
            tools[name] = found
    shell = "cmd.exe" if sys.platform == "win32" else "/bin/sh"
    return Environment(
        os_name=f"{platform.system()} {platform.release()}", shell=shell, tools=tools
    )


def project_venv_bin(project_dir: Path) -> Path | None:
    for venv in (".venv", "venv"):
        bindir = project_dir / venv / ("Scripts" if sys.platform == "win32" else "bin")
        if bindir.is_dir():
            return bindir
    return None


def venv_path(project_dir: Path | None) -> str:
    """PATH con el entorno virtual del proyecto (si existe) delante."""
    base = os.environ.get("PATH", "")
    bindir = project_venv_bin(project_dir) if project_dir else None
    return f"{bindir}{os.pathsep}{base}" if bindir else base


def command_env(project_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Entorno para comandos de verificación: no interactivo, sin colores, UTF-8."""
    env = dict(os.environ)
    env.update(
        {
            "CI": "1",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "npm_config_yes": "true",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "PATH": venv_path(project_dir),
        }
    )
    if extra:
        env.update(extra)
    return env
