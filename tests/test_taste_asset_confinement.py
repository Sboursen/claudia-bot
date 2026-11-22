from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from slop.config import SlopError
from slop.taste import embed, fonts, pool, server
from slop.taste.assets import profile_asset, read_file, write_file


def _project(tmp_path):
    project = tmp_path / "episode"
    project.mkdir()
    (project / "project.json").write_text('{"name":"episode"}', encoding="utf-8")
    return project


def test_pool_routes_only_use_valid_staged_jpegs(tmp_path):
    project = _project(tmp_path)
    root, index = profile_asset(project, "pool/pool.jsonl")
    items = [{"id": "photo-1"}, {"id": "bad/name"}, {"id": 42}]
    write_file(root, index, "\n".join(map(json.dumps, items)).encode())
    image = b"\xff\xd8\xffsynthetic JPEG fixture"
    root, relative = pool.image_asset(project, items[0])
    write_file(root, relative, image)
    rows = pool.load_pool(project)
    assert list(rows) == ["photo-1"]
    assert pool.image_bytes(project, rows["photo-1"]) == image


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_asset_reader_rejects_linked_fixtures(tmp_path, kind):
    staged = tmp_path / "stage"
    staged.mkdir()
    adjacent = tmp_path / "sample.jpg"
    adjacent.write_bytes(b"\xff\xd8\xffexternal sample")
    try:
        if kind == "symlink":
            (staged / "photo.jpg").symlink_to(adjacent)
        else:
            os.link(adjacent, staged / "photo.jpg")
    except OSError:
        pytest.skip("links are unavailable on this filesystem")
    with pytest.raises(OSError):
        read_file(staged, "photo.jpg", 1024)


def test_asset_reader_rejects_a_linked_directory(tmp_path):
    staged = tmp_path / "stage"
    staged.mkdir()
    adjacent = tmp_path / "outside"
    adjacent.mkdir()
    (adjacent / "photo.jpg").write_bytes(b"sample")
    try:
        (staged / "images").symlink_to(adjacent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(OSError):
        read_file(staged, "images/photo.jpg", 1024)


def test_legacy_font_session_requires_rebuild_and_keeps_votes(tmp_path, monkeypatch):
    project = _project(tmp_path)
    stock = tmp_path / "font-library"
    stock.mkdir()
    face_data = b"\x00\x01\x00\x00" + bytes(8)
    (stock / "Geist-Regular.ttf").write_bytes(face_data)
    (stock / "TestMono-Regular.ttf").write_bytes(face_data + b"\0")
    monkeypatch.setattr(fonts, "font_dirs", lambda extra=None: [stock])
    root, relative = profile_asset(project, "fonts/session.json")
    old = {"id": "old", "fonts": [{"file": "Preview.ttf", "path": str(stock / "Geist-Regular.ttf")}]}
    write_file(root, relative, json.dumps(old).encode())
    root, taps = profile_asset(project, "fonts/taps.jsonl")
    saved_vote = b'{"session":"old","eid":"sample-vote","winner":"a"}\n'
    write_file(root, taps, saved_vote)
    assert fonts.FontSession(project).font_bytes("Preview.ttf") is None

    result = fonts.setup(project, families=["Geist", "Test Mono"], texts=["A short line"], seed=7)
    assert result["created"] and result["session"]["id"] != old["id"]
    assert all("path" not in face for face in result["session"]["fonts"])
    session = fonts.FontSession(project)
    assert session.font_bytes("Geist-Regular.ttf") == (face_data, ".ttf")
    assert session.frame_bytes().startswith(b"\xff\xd8\xff")
    assert read_file(root, taps, 1024) == saved_vote
    assert fonts.setup(project)["created"] is False


def test_embedding_receives_only_a_verified_private_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_HOME", str(tmp_path / "app-home"))
    project = _project(tmp_path)
    root, index = profile_asset(project, "pool/pool.jsonl")
    write_file(root, index, b'{"id":"sample","sha1":"unused metadata"}\n')
    data = b"\xff\xd8\xffsample"
    root, relative = pool.image_asset(project, {"id": "sample"})
    write_file(root, relative, data)
    original = root / relative

    def convert(paths):
        assert len(paths) == 1 and Path(paths[0]) != original
        assert Path(paths[0]).read_bytes() == data
        return [np.array([1.0, 0.0], dtype="float32")]

    adapter = SimpleNamespace(provider="local", model="test", tag="local-test", embed_images=convert)
    result = embed.embed_pool(project, embedder=adapter, log=lambda _: None)
    assert result["computed"] == 1


def test_saved_cloud_backend_cannot_enable_an_automatic_upload(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(embed, "load_store", lambda *_: ([], None, {"provider": "openrouter"}))
    with pytest.raises(SlopError, match="explicit embed run"):
        embed.embed_pool(project, provider="auto")


def test_detached_no_embedding_switch_replaces_previous_mode(tmp_path, monkeypatch):
    from slop.taste import tunnel

    project = _project(tmp_path)
    known = {"pid": 451, "port": 9800, "token": "fixture", "embed": True, "asset_policy": server.ASSET_POLICY}
    monkeypatch.setattr(server, "read_json", lambda *args: dict(known))
    monkeypatch.setattr(server, "write_json", lambda *args: None)
    monkeypatch.setattr(server, "free_port", lambda *args: 9801)
    monkeypatch.setattr(server, "health", lambda *args, **kwargs: True)
    seen = []
    stopped = []
    monkeypatch.setattr(tunnel, "stop_pid", stopped.append)
    monkeypatch.setattr(tunnel, "spawn_detached", lambda argv, *args, **kwargs: seen.append(argv) or SimpleNamespace(pid=452))

    launched = server.serve(project, embed=False)
    assert launched["running"] and not launched["reused"]
    assert stopped == [451]
    assert seen[0][-1] == "--no-embed"
