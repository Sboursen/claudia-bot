"""Legacy relay job records, staged attachments and local media checks."""
from __future__ import annotations

import json
import time

from slop import relay

import test_relay as RT  # noqa: E402  (its fake engine and helpers)
K = RT.K


def _ready(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    return env


def _job(kind="browser", **kw):
    args = ["relay", "job", "--kind", kind]
    if kind == "browser":
        args += ["--url", "https://example.com", "--note", kw.get("note", "x")]
    elif kind == "suno":
        args += ["--style", "synth pop", "--instrumental"]
    rc, out = K.run(*args)
    assert rc == 0, out
    return out["job"]


def test_external_job_claims_are_denied_even_with_a_legacy_token(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    jobs_file = env["rd"] / "jobs.json"
    jobs = {}
    for kind in ("browser", "suno"):
        jid = _job(kind)
        jobs[jid] = {"status": "claimed", "claimed_ts": time.time(), "token": "synthetic-token"}
        jobs_file.write_text(json.dumps(jobs))
        for opts in ((), ("--token", "synthetic-token"), ("--stale-ok",)):
            rc, out = K.run("relay", "claim", jid, *opts)
            assert rc == 1 and "external job dispatch is disabled" in out["error"]
        assert json.loads(jobs_file.read_text()) == jobs


def test_a_person_job_remains_local(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    rc, out = K.run("relay", "job", "--kind", "person", "--text", "send me your demo", "--expect", "demo.wav")
    assert rc == 0, out
    rc, claim = K.run("relay", "claim", out["job"])
    assert rc == 0 and claim["status"] == "queued" and not claim["dispatch_enabled"]
    assert K.run("relay", "claim", _job("browser"))[0] == 1


def test_an_old_job_is_visible_but_cannot_be_claimed(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    jid = _job("browser", note="confirm context")
    outbox = env["rd"] / "outbox.jsonl"
    rows = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    rows[-1]["t"] = time.time() - relay.JOB_STALE_S - 60
    outbox.write_text("".join(json.dumps(row) + "\n" for row in rows))
    read = K.run("relay", "read")[1]
    assert read["jobs"][0]["stale_h"] >= 6 and not read["dispatch_enabled"]
    assert K.run("relay", "claim", jid, "--stale-ok")[0] == 1


def test_the_persons_files_go_over_as_files(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    f = env["wd"] / "their-song.wav"
    f.write_bytes(b"RIFF")
    rc, out = K.run("relay", "send", "--attach", str(f), "--", "here is my song")
    assert rc == 0 and out["attached"] == [str(f.resolve())], out
    msg = [json.loads(x) for x in (env["rd"] / "inbox.jsonl").read_text().splitlines() if x.strip()][-1]
    assert msg["text"] == f"here is my song\n[attached file] {f.resolve()}"
    rc, out = K.run("relay", "send", "--attach", str(env["wd"] / "nope.png"), "--", "and this")
    assert rc == 1 and "under the projects directory" in out["error"]


def test_a_quiet_production_is_flagged(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    rd = env["rd"]
    (rd / "runner.json").write_text(json.dumps({"state": "running", "pid": __import__("os").getpid()}))
    (rd / "turn.json").write_text(json.dumps({"state": "running", "n": 3, "last": "bash: render frames",
                                              "started_ts": time.time() - 7 * 3600,
                                              "last_ts": time.time() - 6 * 3600}))
    monkeypatch.setattr(relay, "_alive", lambda pid, start=None: True)
    p = relay.production(rd)
    assert p["state"] == "working" and p.get("stalled") and "No progress for" in p["summary"], p
    assert "relay log" in p["summary"]


def test_midjourney_downloads_get_their_extension_and_carry_their_prompt(tmp_path, monkeypatch):
    from PIL import Image
    _ready(tmp_path, monkeypatch)
    rc, out = K.run("relay", "job", "--kind", "midjourney", "--text", "a sequinned diamanté gown --ar 16:9",
                    "--plate", "gown-ms")
    from pathlib import Path
    save = Path(out["save"])
    Image.new("RGB", (8, 8)).save(save / "gown-ms-1", format="PNG")      # saved without its extension
    rc, done = K.run("relay", "done", out["job"], "--file", "gown-ms-1", "--model", "V8.1")
    assert rc == 0 and done["files"] == [str(save / "gown-ms-1.png")], done
    meta = Image.open(save / "gown-ms-1.png").text
    assert meta["Claudia-Prompt"] == "a sequinned diamanté gown --ar 16:9" and meta["Claudia-Model"] == "V8.1"
    assert meta["Claudia-Plate"] == "gown-ms"


def test_a_prompt_goes_on_the_clipboard_never_typed(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))               # no xclip here: the file way
    rc, out = K.run("relay", "job", "--kind", "person", "--text", "first", "--text", "a diamanté bag")
    rc, c = K.run("relay", "copy", out["job"], "--k", "2")
    assert rc == 0 and c["copied"] is False and "file://" in c["text"], c
    from pathlib import Path
    assert Path(c["file"]).read_text(encoding="utf-8") == "a diamanté bag"
    assert K.run("relay", "copy", out["job"], "--k", "3")[0] == 1


def test_a_video_too_big_for_drive_is_never_posted(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    big = env["wd"] / "film" / "out" / "film.mp4"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as f:
        f.truncate(relay.DRIVE_MAX_B + 1)                             # sparse: no disk used
    monkeypatch.setattr(relay, "_video_issue", lambda p: None)
    rc, out = K.run("relay", "say", "the film", "--show", str(big))
    assert rc == 1 and "over the 100 MB Drive upload limit" in out["error"] and "encode --share" in out["error"], out
    assert K.run("relay", "say", "the film", "--show", str(big), "--as-is")[0] == 0
    read = K.run("relay", "read")[1]
    assert not read["dispatch_enabled"] and read["items"][0]["files"] == [str(big)]


def test_a_clip_saved_without_its_extension_gets_it(tmp_path, monkeypatch):
    projects = _ready(tmp_path, monkeypatch)["wd"]
    clip, mov, kept = projects / "take-1", projects / "take-2", projects / "take-3.m4v"
    clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
    mov.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64)
    kept.write_bytes(b"\x00\x00\x00\x18ftypM4V " + b"\x00" * 64)
    assert relay._with_real_ext(clip).name == "take-1.mp4" and relay._with_real_ext(mov).name == "take-2.mov"
    assert relay._with_real_ext(kept) == kept                       # any video name is fine for a video
