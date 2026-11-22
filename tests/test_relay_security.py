"""Regression tests for the local-only relay and literal message intake."""

import argparse
import os
import time

import pytest

from slop import job, relay
from slop.agents import status
from slop.config import SlopError


@pytest.fixture
def local_relay(tmp_path, monkeypatch):
    work = tmp_path / "projects"
    state = tmp_path / "relay"
    work.mkdir()
    state.mkdir()
    monkeypatch.setattr(relay, "work_dir", lambda: work)
    monkeypatch.setattr(relay, "relay_dir", lambda: state)

    parser = argparse.ArgumentParser()
    relay.register(parser.add_subparsers(required=True))

    def invoke(*words):
        options = parser.parse_args(["relay", *words])
        return options.func(options)

    return work, state, invoke


def unexpected(*_args, **_kwargs):
    pytest.fail("the local relay unexpectedly reached a vendor process or job launcher")


def test_setup_and_send_remain_local(local_relay, monkeypatch):
    work, state, invoke = local_relay
    monkeypatch.setattr(status, "engine_status", unexpected)
    monkeypatch.setattr(job, "start", unexpected)
    monkeypatch.setattr(relay.subprocess, "Popen", unexpected)

    setup = invoke("setup", "--engine", "codex", "--model", "saved-model")
    assert not setup["production_enabled"]
    assert setup["engine"] == "codex"
    assert sorted(p.name for p in work.iterdir()) == []

    queued = invoke("send", "--", "literal message")
    assert queued["queued"] and not queued["started"]
    assert invoke("inbox")["messages"][0]["text"] == "literal message"
    assert not (state / "runner.json").exists()
    assert "literal message" not in (state / "relay.log").read_text()


def test_chat_has_no_file_option_and_accepts_option_shaped_text(local_relay):
    _, state, invoke = local_relay
    invoke("setup")
    with pytest.raises(SystemExit):
        invoke("send", "--file", "example.txt")
    with pytest.raises(SystemExit):
        invoke("send", "--from=grok")
    assert not (state / "inbox.jsonl").exists()

    invoke("send", "--", "--topic=literal")
    record = invoke("inbox")["messages"][0]
    assert record["text"] == "--topic=literal" and record["from"] == "director"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires protected file traversal")
def test_attachment_stays_in_project_scope(local_relay, tmp_path):
    work, state, invoke = local_relay
    invoke("setup")
    local = work / "reference.txt"
    outside = tmp_path / "outside.txt"
    local.write_text("local")
    outside.write_text("outside")

    with pytest.raises(SlopError, match="under the projects directory"):
        invoke("send", "--attach", str(outside), "--", "outside reference")
    assert not (state / "inbox.jsonl").exists()
    saved = invoke("send", "--attach", str(local), "--", "local reference")
    assert saved["attached"] == [str(local.resolve())]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires protected file traversal")
def test_symlinked_attachment_inputs_and_destinations_are_rejected(local_relay, tmp_path):
    work, state, invoke = local_relay
    invoke("setup")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private.txt"
    secret.write_text("synthetic private text")
    leaf = work / "link.txt"
    leaf.symlink_to(secret)
    parent = work / "linked-directory"
    parent.symlink_to(outside, target_is_directory=True)

    for supplied in (leaf, parent / secret.name):
        with pytest.raises(SlopError, match="under the projects directory"):
            invoke("send", "--attach", str(supplied), "--", "reference")
        with pytest.raises(SlopError, match="under the projects directory"):
            invoke("job", "--kind", "suno", "--style", "synthetic", "--lyrics-file", str(supplied))

    with pytest.raises(SlopError, match="job destinations"):
        invoke("job", "--kind", "person", "--text", "manual", "--save", str(outside / "forbidden"))
    with pytest.raises(SlopError, match="job destinations"):
        invoke("job", "--kind", "person", "--text", "manual", "--expect", str(parent / "new" / "clip.wav"))
    assert not (state / "inbox.jsonl").exists()
    assert not (state / "outbox.jsonl").exists()
    assert sorted(p.name for p in outside.iterdir()) == [secret.name]


def test_legacy_supervisor_cannot_start_or_receive_more_work(local_relay, monkeypatch):
    _, state, invoke = local_relay
    monkeypatch.setattr(job, "start", unexpected)
    invoke("setup")

    with pytest.raises(SlopError, match="autonomous production is disabled"):
        invoke("_run")
    assert not (state / "runner.json").exists()

    relay._write_json(state / "runner.json", {"state": "starting", "since_mono": time.monotonic()})
    with pytest.raises(SlopError, match="stop the existing production supervisor"):
        invoke("send", "--", "new work")
    assert not (state / "inbox.jsonl").exists()


def test_legacy_external_job_and_wake_remain_review_only(local_relay):
    _, state, invoke = local_relay
    invoke("setup")
    relay._write_json(state / "wake.json", {"url": "https://example.invalid/legacy-wake"})
    relay._append(state / "outbox.jsonl", {
        "id": 1, "seq": 1, "kind": "job", "job": "J-0001", "job_kind": "browser",
    })

    assert not invoke("read")["dispatch_enabled"]
    with pytest.raises(SlopError, match="external job dispatch is disabled"):
        invoke("claim", "J-0001")
    with pytest.raises(SlopError, match="external job dispatch is disabled"):
        invoke("copy", "J-0001")
    assert not (state / "jobs.json").exists()
    with pytest.raises(SlopError, match="wake requests require an authenticated broker"):
        invoke("wake", "test")
    assert invoke("wake")["legacy_configured"]
    assert not invoke("wake", "off")["legacy_configured"]
