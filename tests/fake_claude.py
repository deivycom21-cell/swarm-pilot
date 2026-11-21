"""Simulador de `claude -p --output-format stream-json` para los tests del adapter.

Comportamiento según FAKE_CLAUDE_MODE: ok | error | ratelimit | crash | hang.
Guarda argv y stdin en FAKE_CLAUDE_RECORD (JSON) para que el test los inspeccione.
"""

import json
import os
import sys
import time

if "--version" in sys.argv:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

prompt = sys.stdin.read()
record = os.environ.get("FAKE_CLAUDE_RECORD")
if record:
    sysfile = None
    if "--append-system-prompt-file" in sys.argv:
        with open(
            sys.argv[sys.argv.index("--append-system-prompt-file") + 1], encoding="utf-8"
        ) as f:
            sysfile = f.read()
    with open(record, "w", encoding="utf-8") as f:
        json.dump(
            {
                "argv": sys.argv[1:],
                "prompt": prompt,
                "system": sysfile,
                "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
            },
            f,
        )

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")


def emit(ev):
    print(json.dumps(ev), flush=True)


if mode == "crash":
    print("Error: algo explotó", flush=True)
    sys.exit(1)
if mode == "hang":
    emit({"type": "system", "subtype": "init", "model": "fake"})
    time.sleep(60)

emit({"type": "system", "subtype": "init", "model": "claude-fake", "tools": ["Read"]})
emit(
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": os.path.join(os.getcwd(), "a.txt")},
                }
            ]
        },
    }
)
emit(
    {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "is_error": True, "content": "permiso denegado"}]
        },
    }
)
emit(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Listo.\nDos líneas."}]}}
)
if mode == "error":
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "algo salió mal",
        }
    )
elif mode == "ratelimit":
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Claude usage limit reached. Resets at 5pm",
            "api_error_status": 429,
        }
    )
else:
    out = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Listo.",
        "total_cost_usd": 0.01,
        "usage": {"output_tokens": 5},
    }
    if "--json-schema" in sys.argv:
        out["structured_output"] = {"ok": True}
        out["result"] = '{"ok": true}'
    emit(out)
