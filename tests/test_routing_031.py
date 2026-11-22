"""Local relay routing metadata and portable scorer behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


import test_relay as RT  # noqa: E402  (its fake engine and helpers)
K = RT.K
ROOT = Path(__file__).resolve().parents[1]


def test_setup_records_the_selected_engine_without_probing(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch, logged=())
    rc, out = K.run("relay", "setup", "--engine", "codex")
    assert rc == 0 and out["engine"] == "codex", out
    rc, out = K.run("relay", "setup")
    assert rc == 0 and out["engine"] == "codex", out
    assert not K.calls(env["state"])


def test_midjourney_jobs_are_one_prompt_each_with_its_plate(tmp_path, monkeypatch):
    RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = K.run("relay", "job", "--kind", "midjourney", "--text", "{a\\, x, b\\, y} --ar 16:9 --raw")
    assert rc == 1 and "permutation" in out["error"]
    rc, out = K.run("relay", "job", "--kind", "midjourney", "--text", "a red door, at night --ar 16:9", "--text",
                    "a city --ar 16:9", "--plate", "hall-ms")
    assert rc == 1 and "one per --text" in out["error"]
    rc, out = K.run("relay", "job", "--kind", "midjourney", "--text", "a red door, at night --ar 16:9", "--plate",
                    "hall-ms", "--text", "a city {at night} --ar 16:9", "--plate", "city-ws")
    assert rc == 0, out
    rc, r = K.run("relay", "read")
    j = next(j for j in r["jobs"] if j["job_kind"] == "midjourney")
    assert j["plates"] == ["hall-ms", "city-ws"] and j["expect_count"] == 8
    assert "plates: hall-ms, city-ws" in r["text"]


def test_job_fields_are_available_for_local_review(tmp_path, monkeypatch):
    RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    K.run("relay", "job", "--kind", "browser", "--url", "https://example.com/page", "--note", "read the title")
    K.run("relay", "job", "--kind", "suno", "--title", "Hook", "--style", "glassy synth pop", "--exclude", "metal",
          "--lyrics", "[Verse]\nla la")
    rc, r = K.run("relay", "read")
    assert rc == 0 and not r["dispatch_enabled"]
    for want in ("url: https://example.com/page", "title: Hook", "style: glassy synth pop", "exclude: metal",
                 "lyrics:\n[Verse]\nla la"):
        assert want in r["text"], want


@pytest.mark.parametrize("route", ["browser", "native"])
def test_done_reports_each_files_real_size(tmp_path, monkeypatch, route):
    """grok.com's button can show the highest resolution and still render lower: the production learns each file's
    real size, and on the browser route Grok is warned about a file that came out smaller than the rest (by area, so a
    portrait still is no warning). Grok's own tool has no such button: the sizes go to the production, no warning."""
    from PIL import Image
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    job = (("--what", "clip", "--res", "1080p") if route == "browser" else ("--what", "still"))
    rc, out = K.run("relay", "job", "--kind", "grok-imagine", *job, "--prompt", "her on a rooftop")
    assert rc == 0, out
    save = Path(out["save"])
    for name, size in (("a.png", (1280, 704)), ("b.png", (704, 1280)), ("c.png", (736, 400))):
        Image.new("RGB", size, (20, 20, 20)).save(save / name)
    rc, done = K.run("relay", "done", out["job"], "--file", "a.png", "--file", "b.png", "--file", "c.png")
    assert rc == 0, done
    assert done["sizes"] == {str(save / "a.png"): "1280x704", str(save / "b.png"): "704x1280",
                             str(save / "c.png"): "736x400"}
    if route == "browser":
        assert "c.png" in done["warn"] and "a.png" not in done["warn"] and "b.png" not in done["warn"]
    else:
        assert "warn" not in done, done
    msg = [json.loads(x) for x in (env["rd"] / "inbox.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()][-1]
    assert "\nsizes: a.png 1280x704, b.png 704x1280, c.png 736x400" in msg["text"]


def test_a_save_path_from_the_projects_folder_is_not_doubled(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("new", "announce")[0] == 0
    assert K.run("relay", "setup")[0] == 0
    proj = env["wd"] / "announce"
    for save, want in (("announce/inbox/hd", proj / "inbox" / "hd"), ("inbox/hd2", proj / "inbox" / "hd2"),
                       (str(proj / "abs"), proj / "abs")):
        rc, out = K.run("-p", "announce", "relay", "job", "--kind", "grok-imagine", "--what", "still", "--prompt",
                        "x", "--save", save)
        assert rc == 0 and Path(out["save"]) == want.absolute(), (save, out["save"])
    rc, out = K.run("-p", "announce", "relay", "job", "--kind", "person", "--text", "x", "--expect",
                    "announce/song/take.wav")
    rc, r = K.run("relay", "read")
    assert r["jobs"][-1]["expect"] == [str((proj / "song" / "take.wav").absolute())]


def test_the_scorer_runs_without_the_cmudict_package(tmp_path):
    code = ("import sys, runpy; sys.modules['cmudict'] = None; "
            f"sys.argv = ['scansion.py', 'line', 'hello there my friend', '--mode', 'sung']; "
            f"runpy.run_path({str(ROOT / 'tools' / 'scansion.py')!r}, run_name='__main__')")
    env = {**os.environ, "SLOP_HOME": str(tmp_path), "SLOP_OFFLINE": "1"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    assert r.returncode == 0 or "no CMU dictionary" in r.stderr


def test_relay_setup_leaves_login_shells_alone(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    bashrc = env["home"] / ".bashrc"
    bashrc.write_text("# existing local configuration\n", encoding="utf-8")
    for _ in range(2):
        assert K.run("relay", "setup")[0] == 0
    assert bashrc.read_text(encoding="utf-8") == "# existing local configuration\n"
    assert not (env["home"] / ".profile").exists()
