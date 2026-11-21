import json
import socket

import httpx
import pytest

from swarm_pilot.core.events import EventBus
from swarm_pilot.core.models import EventType
from swarm_pilot.core.preview import PreviewManager, detect_preview, find_free_port, port_is_free
from swarm_pilot.core.process_manager import ProcessManager


def write_pkg(path, scripts):
    (path / "package.json").write_text(json.dumps({"scripts": scripts}))


@pytest.mark.parametrize(
    "script, kind, tail",
    [
        ("vite", "vite", ["--", "--port", "3100", "--strictPort", "--host", "127.0.0.1"]),
        ("next dev", "next", ["--", "-p", "3100", "-H", "127.0.0.1"]),
        ("astro dev", "astro", ["--", "--port", "3100", "--host", "127.0.0.1"]),
        ("node server.js", "npm dev", []),
    ],
)
def test_detect_npm_scripts(tmp_path, script, kind, tail):
    write_pkg(tmp_path, {"dev": script})
    cmd = detect_preview(tmp_path, 3100)
    assert cmd.kind == kind
    assert cmd.args == ["npm", "run", "dev", *tail]
    assert cmd.env["PORT"] == "3100" and cmd.needs_install


def test_detect_start_script_and_vite_config(tmp_path):
    write_pkg(tmp_path, {"start": "react-scripts start"})
    (tmp_path / "node_modules").mkdir()
    cmd = detect_preview(tmp_path, 3000)
    assert cmd.args[:3] == ["npm", "run", "start"] and not cmd.needs_install
    write_pkg(tmp_path, {})
    (tmp_path / "vite.config.ts").write_text("")
    assert detect_preview(tmp_path, 3000).args[:2] == ["npx", "vite"]


def test_detect_static_and_nothing(tmp_path):
    assert detect_preview(tmp_path, 3000) is None
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<h1>hi</h1>")
    cmd = detect_preview(tmp_path, 3000)
    assert cmd.kind == "static" and cmd.args[-1] == str(tmp_path / "public")


def test_find_free_port_skips_busy():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert not port_is_free(busy)
        assert find_free_port(busy) != busy


async def test_static_preview_lifecycle(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hola preview</h1>", encoding="utf-8")
    pm = ProcessManager(grace=1)
    bus = EventBus()
    preview = PreviewManager(tmp_path, pm, bus, preferred_port=find_free_port(3700))
    try:
        await preview.ensure()
        assert await preview.wait_ready(30), preview.error
        async with httpx.AsyncClient() as c:
            assert "hola preview" in (await c.get(preview.url)).text
        port = int(preview.url.rsplit(":", 1)[1].strip("/"))

        await preview.ensure(["styles.css"])  # cambio normal: solo recarga
        reloads = [
            e
            for e in bus.history()
            if e.type is EventType.PREVIEW_STATUS and e.payload.get("reload")
        ]
        assert reloads
    finally:
        await preview.stop()
        await pm.shutdown()
    assert preview.state == "stopped"
    assert port_is_free(port)


async def test_failed_preview_reports_error(tmp_path):
    write_pkg(tmp_path, {"dev": 'node -e "process.exit(3)"'})
    (tmp_path / "node_modules").mkdir()
    pm = ProcessManager(grace=1)
    preview = PreviewManager(tmp_path, pm, EventBus(), preferred_port=find_free_port(3750))
    try:
        await preview.ensure()
        assert not await preview.wait_ready(60)
        assert preview.state == "failed" and preview.error
    finally:
        await preview.stop()
        await pm.shutdown()
