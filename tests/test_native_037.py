"""Local Grok Imagine job metadata and saved media validation."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import test_relay as RT  # noqa: E402  (its fake engine and helpers)
K = RT.K

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _pic(p: Path, data: bytes = PNG) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _job(*args):
    return K.run("relay", "job", "--kind", "grok-imagine", *args)


def test_a_clip_goes_to_grok_s_own_tool_at_720p_with_its_frames_and_references(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    first = _pic(env["wd"] / "plates" / "a.png")
    last = _pic(env["wd"] / "plates" / "b.png")
    ref = _pic(env["wd"] / "plates" / "face.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16)
    rc, out = _job("--what", "clip", "--prompt", "she turns to the window", "--image", str(first),
                   "--end-image", str(last), "--ref", str(ref), "--aspect", "16:9", "--seconds", "8")
    assert rc == 0, out
    assert not out["dispatch_enabled"]
    rc, r = K.run("relay", "read")
    j = r["jobs"][0]
    assert j["res"] == "720p" and j["route"] == "native" and j["seconds"] == 8
    assert j["image"] == str(first) and j["end_image"] == str(last) and j["refs"] == [str(ref)]
    assert not r["dispatch_enabled"]


def test_a_1080p_clip_goes_to_grok_com_in_the_browser_and_a_still_has_no_resolution(tmp_path, monkeypatch):
    RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = _job("--what", "clip", "--prompt", "rain on the glass", "--res", "1080p")
    assert rc == 0 and not out["dispatch_enabled"], out
    rc, out = _job("--what", "still", "--prompt", "her on a rooftop", "--aspect", "4:3")
    assert rc == 0, out
    jobs = {j["what"]: j for j in K.run("relay", "read")[1]["jobs"]}
    assert jobs["clip"]["route"] == "browser" and jobs["clip"]["res"] == "1080p"
    assert "res" not in jobs["still"] and jobs["still"]["route"] == "native" and jobs["still"]["aspect"] == "4:3"


@pytest.mark.parametrize("args, says", [
    (("--what", "still", "--prompt", "x", "--aspect", "3:2"), "stills in 1:1, 4:3, 3:4, 16:9, 9:16"),
    (("--what", "still", "--prompt", "x", "--seconds", "6"), "a still takes no"),
    (("--what", "still", "--prompt", "x", "--res", "720p"), "a still takes no"),
    (("--what", "clip", "--prompt", "x", "--seconds", "20"), "1-15 s"),
    (("--what", "clip", "--prompt", "x", "--seconds", "0"), "1-15 s"),
])
def test_the_relay_refuses_what_grok_s_tool_cannot_make(tmp_path, monkeypatch, args, says):
    RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = _job(*args)
    assert rc == 1 and says in out["error"], out
    assert K.run("relay", "read")[1]["jobs"] == [], "nothing was posted"


def test_the_relay_refuses_pictures_grok_imagine_cannot_take(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    gif = _pic(env["wd"] / "a.png", b"GIF89a" + b"\x00" * 20)           # named .png, a GIF inside
    rc, out = _job("--what", "clip", "--prompt", "x", "--image", str(gif))
    assert rc == 1 and "not a JPEG, PNG or WebP" in out["error"], out
    refs = [str(_pic(env["wd"] / f"r{i}.png")) for i in range(15)]
    rc, out = _job("--what", "still", "--prompt", "x", *[a for r in refs for a in ("--ref", r)])
    assert rc == 1 and "at most 14 reference images" in out["error"], out
    big = env["wd"] / "big.png"
    with open(big, "wb") as f:                                         # sparse: 36 MB on paper, nothing on disk
        f.write(PNG)
        f.truncate(36 * 1024 * 1024)
    rc, out = _job("--what", "clip", "--prompt", "x", "--image", str(big))
    assert rc == 1 and "over 35 MB" in out["error"], out
    assert K.run("relay", "read")[1]["jobs"] == []


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_a_native_clip_below_its_resolution_is_flagged_to_grok_and_the_production(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = _job("--what", "clip", "--prompt", "she looks up")
    assert rc == 0, out
    jid, save = out["job"], Path(out["save"])
    clip = save / f"{jid}-1.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=854x480:rate=24",
                    "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=60)
    rc, out = K.run("relay", "done", jid, "--file", str(clip))
    assert rc == 0, out
    assert "below the job's 720p" in out["warn"] and "ask your video tool for 720p" in out["warn"]
    assert "the page may have rendered" not in out.get("warn", ""), "the page's resolution note is for the browser"
    items = K.run("relay", "inbox")[1]["messages"]
    assert "854x480" in items[0]["text"] and f"below the job's 720p: {clip.name}" in items[0]["text"]


def test_saved_suno_takes_get_the_extension_their_bytes_say(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = K.run("relay", "job", "--kind", "suno", "--title", "Hook", "--style", "glassy synth pop",
                    "--instrumental", "--name", "hook")
    assert rc == 0 and not out["dispatch_enabled"], out
    jid, save = out["job"], Path(out["save"])
    wav = _pic(save / "hook-a", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24)       # no extension
    mp3 = _pic(save / "hook-b.wav", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 24)  # an MP3 named .wav
    rc, out = K.run("relay", "done", jid, "--file", str(wav), "--file", str(mp3), "--model", "newest")
    assert rc == 0, out
    assert out["files"] == [str(save / "hook-a.wav"), str(save / "hook-b.mp3")], out
    assert (save / "hook-a.wav").is_file() and (save / "hook-b.mp3").is_file()


def test_a_suno_take_that_is_no_audio_is_refused_before_it_reaches_the_production(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, out = K.run("relay", "job", "--kind", "suno", "--title", "Hook", "--style", "pop", "--instrumental",
                    "--name", "hook")
    assert rc == 0, out
    jid, save = out["job"], Path(out["save"])
    page = _pic(save / "hook-a.wav", b"<!DOCTYPE html><html><body>Sign in to Suno</body></html>")
    rc, out = K.run("relay", "done", jid, "--file", str(page))
    assert rc == 1 and "hook-a.wav is not a song" in out["error"] and "nothing was sent" in out["error"], out
    assert [j["job"] for j in K.run("relay", "read")[1]["jobs"]] == [jid], "the job stays open"


def test_codex_offers_its_newest_effort_levels_and_child_engines_drop_the_parent_session():
    from slop import relay as R
    from slop.agents import engines as E
    assert R.EFFORT_USUAL["codex"][-2:] == ("max", "ultra")
    for k in ("CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ATTENDED",
              "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        assert E.PARENT_SESSION_RE.match(k), k
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "PATH", "CLAUDE_LOGIN_CODE"):
        assert not E.PARENT_SESSION_RE.match(k), k
    assert E.SPEC["claude"]["node_min"] == 22
