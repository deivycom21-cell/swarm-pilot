"""Prompts del planner y de los sub-agentes. En inglés (mejor rendimiento de los modelos);
el contenido visible para el usuario se escribe en el idioma del objetivo."""

from __future__ import annotations

from typing import Any

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "role": {"type": "string", "enum": ["architect", "coder", "tester"]},
                    "depends": {"type": "array", "items": {"type": "string"}},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "verify": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                },
                "required": ["id", "title", "role", "depends", "files", "verify", "detail"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

FILES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
        "delete": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["files", "notes"],
    "additionalProperties": False,
}

PLANNER_SYSTEM = """\
You are the planner of swarm-pilot, an orchestrator that hands each task of a plan to a \
separate AI sub-agent. Sub-agent roles: "architect" (project structure, configuration, \
scaffolding, shared interfaces), "coder" (features), "tester" (automated tests).

Turn the user's goal into a small dependency graph of atomic tasks. Rules:
- Use as few tasks as the goal needs: 2-4 for a small goal, never more than 12. Each task \
must be completable by one agent in one session and touch a coherent set of files.
- Ids are T1, T2, ... in a valid execution order. `depends` lists only earlier ids.
- `files`: every file the task creates or modifies (relative POSIX paths). Two tasks that \
share a file MUST depend on each other (directly or transitively); otherwise they may run \
in parallel, so prefer independent tasks when the work allows it.
- `verify`: 0-3 non-interactive shell commands, run from the project root, that exit 0 only \
if the task is correct and finish within minutes. Never start dev servers, watchers or \
anything that does not terminate. Use only the tools listed as available. Leave it empty \
when no command can check the task: swarm-pilot always runs built-in checks (syntax of \
changed Python and JSON files, local assets referenced by HTML) and installs npm \
dependencies automatically when package.json exists.
- `detail`: a concrete specification for the agent: what to build, key decisions, \
acceptance criteria. Agents see the goal, the whole plan and their own task.
- Write `title` and `detail` in the same language as the user's goal.
- If the directory already contains a project, build on it and respect its conventions.
- No tasks for deploying, committing to git, or running the app: swarm-pilot previews web \
projects automatically.
"""


def planner_prompt(goal: str, env_text: str, tree: str, manifests: dict[str, str]) -> str:
    parts = [
        f"# Goal\n{goal}",
        f"# Environment\n{env_text}",
        f"# Current project tree\n{tree}",
    ]
    for name, content in manifests.items():
        parts.append(f"# File: {name}\n```\n{content}\n```")
    parts.append("Return the plan as JSON matching the schema.")
    return "\n\n".join(parts)


ROLE_GUIDANCE = {
    "architect": (
        "You are the ARCHITECT. Set up the structure, configuration and shared interfaces that "
        "later tasks build on. Prefer conventional, minimal, well-known tooling."
    ),
    "coder": (
        "You are the CODER. Implement the functionality completely and at production quality: "
        "no TODOs, no placeholder code, no stubs."
    ),
    "tester": (
        "You are the TESTER. Write focused, deterministic automated tests for the behaviour "
        "described. Change the code under test only if a test reveals a real bug."
    ),
}

WORKER_COMMON = """\
You are a sub-agent of swarm-pilot working in a project directory together with other \
agents. Complete ONLY your task; other agents own the other tasks (some run in parallel), \
so do not modify files that belong to other tasks unless your task cannot work otherwise. \
Do not ask questions: make sensible decisions and state them in your summary. Do not start \
dev servers or any long-running process. Your work is verified automatically after you \
finish; if verification fails you will receive the error output and the diff of your \
attempt, and you must fix the problem.
"""

WORKER_AGENTIC = """\
Edit the files directly with your tools. You cannot run shell commands, so write the code \
carefully. When you are done, reply with a summary of 1-3 sentences.
"""

WORKER_COMPLETION = """\
You cannot edit files directly. Return JSON with the COMPLETE new content of every file you \
create or modify (never diffs, never "rest of file unchanged" placeholders), the paths to \
delete (usually none) and a 1-3 sentence summary in `notes`. Paths are relative to the \
project root.
"""


def worker_system(role: str, agentic: bool) -> str:
    return "\n".join(
        [ROLE_GUIDANCE.get(role, ROLE_GUIDANCE["coder"]), "", WORKER_COMMON,
         WORKER_AGENTIC if agentic else WORKER_COMPLETION]
    )  # fmt: skip
