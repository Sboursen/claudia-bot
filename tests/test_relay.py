"""CLI fixture and local relay queue integration expectations."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_K = Path(__file__).parent / "fixtures" / "agents" / "kit.py"
_spec = importlib.util.spec_from_file_location("agents_kit", _K)
K = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("agents_kit", K)
_spec.loader.exec_module(K)


def setup(tmp_path, monkeypatch, logged=("claude",)):
    env = K.setup(tmp_path, monkeypatch, engines=("claude", "codex"), logged=logged)
    env["rd"] = env["sh"] / "relay"
    env["wd"] = tmp_path / "projects"
    monkeypatch.setenv("SLOP_PROJECTS", str(env["wd"]))
    monkeypatch.delenv("SLOP_RELAY_TURN", raising=False)
    monkeypatch.delenv("FAKE_RELAY", raising=False)
    return env


def test_local_relay_messages_and_replies_stay_in_the_queue(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    assert K.run("relay", "setup")[0] == 0
    rc, sent = K.run("relay", "send", "--", "hello")
    assert rc == 0 and sent["queued"] and not sent["started"]
    assert K.run("relay", "inbox")[1]["messages"][0]["text"] == "hello"
    assert K.run("relay", "inbox")[1]["count"] == 0
    assert K.run("relay", "say", "local reply")[0] == 0
    rc, read = K.run("relay", "read")
    assert rc == 0 and not read["dispatch_enabled"] and read["items"][0]["text"] == "local reply"
    assert not K.calls(env["state"])
    assert not (env["rd"] / "runner.json").exists()
