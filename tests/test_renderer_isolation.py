"""Checks for the renderer resource policy without opening a browser."""
from types import SimpleNamespace

import pytest

from slop.config import SlopError
from slop.render import project
from slop.render.isolation import ENGINE_URL, PROJECT_URL, RenderAssets


class Request:
    def __init__(self, url, method="GET"):
        self.request = SimpleNamespace(url=url, method=method)
        self.response = None

    def abort(self):
        self.response = {"status": "blocked"}

    def fulfill(self, **response):
        self.response = response


def response(assets, url, method="GET"):
    route = Request(url, method)
    assets.handle(route)
    return route.response


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_intercepted_origin_limits_readable_resources(tmp_path):
    engine = tmp_path / "engine"
    pdir = tmp_path / "project"
    write(engine, "index.html", b"<script>window.loaded = true;</script>")
    write(pdir, "engine/assets/plate.jpg", b"fixture")
    write(pdir, "engine/scenes.js", b"window.sceneFixture = true;")
    write(pdir, "notes/private.json", b"{}")
    assets = RenderAssets(engine, pdir, False)

    page = response(assets, ENGINE_URL + "index.html")
    assert page["status"] == 200
    assert "'nonce-" in page["headers"]["Content-Security-Policy"]
    assert b'<script nonce="' in page["body"]
    assert response(assets, PROJECT_URL + "engine/assets/plate.jpg")["body"] == b"fixture"
    assert response(assets, PROJECT_URL + "engine/scenes.js")["status"] == 404
    assert response(assets, PROJECT_URL + "notes/private.json")["status"] == 404
    assert response(assets, PROJECT_URL + "engine/assets/plate.jpg", "POST")["status"] == "blocked"
    assert response(assets, "https://example.invalid/")["status"] == "blocked"

    trusted = RenderAssets(engine, pdir, True)
    csp = response(trusted, ENGINE_URL + "index.html")["headers"]["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert response(trusted, PROJECT_URL + "engine/scenes.js")["status"] == 200
    assert response(trusted, "https://example.invalid/")["status"] == "blocked"


def test_page_spec_loads_canonical_exports_as_data(tmp_path, monkeypatch):
    monkeypatch.delenv("SLOP_RENDER_TRUST_PROJECT_SCRIPTS", raising=False)
    write(tmp_path, "project.json", b"{}")
    write(tmp_path, "engine/beats.js", b'window.BEATS = {"duration": 2};')
    write(tmp_path, "engine/shots.js", b'window.SHOTS = {"duration": 2};\nwindow.SHOTLIST = window.SHOTS;\n')
    write(tmp_path, "engine/lyrics.js", b"window.LYR = []; window.fixture = true;")
    write(tmp_path, "engine/scenes.js", b"window.chapter = true;")

    safe = project.page_spec(tmp_path)
    assert safe["browser_url"].startswith(ENGINE_URL)
    assert safe["assets"] == ["beats", "shots"]
    assert "window.BEATS = JSON.parse(" in safe["browser_init"]
    assert "window.SHOTS = JSON.parse(" in safe["browser_init"]
    assert "window.SHOTLIST = window.SHOTS" not in safe["browser_init"]
    assert "window.fixture" not in safe["browser_init"]
    assert any("custom project render code was skipped" in w for w in safe["warnings"])
    assert any("skipped non-declarative" in w for w in safe["warnings"])

    monkeypatch.setenv("SLOP_RENDER_TRUST_PROJECT_SCRIPTS", "1")
    trusted = project.page_spec(tmp_path)
    assert trusted["assets"] == ["beats", "lyrics", "shots"]
    assert trusted["trust_project_scripts"]
    assert trusted["browser_url"] == safe["browser_url"]
    assert trusted["browser_init"] != safe["browser_init"]


def test_project_audio_stays_inside_the_project(tmp_path):
    pdir = tmp_path / "project"
    write(pdir, "project.json", b'{"song": "audio/picked.wav"}')
    expected = write(pdir, "audio/picked.wav", b"fixture")
    assert project.audio_path(pdir) == expected.resolve()

    write(pdir, "project.json", b'{"song": "../outside.wav"}')
    write(tmp_path, "outside.wav", b"fixture")
    with pytest.raises(SlopError, match="inside the project"):
        project.audio_path(pdir)
