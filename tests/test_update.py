"""Release discovery and reviewed-commit checkout against a temporary local Git origin."""
from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from slop import update as U
from slop.config import SlopError


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return r.stdout


def release(work: Path, version: str, note: str) -> str:
    (work / "slop").mkdir(parents=True, exist_ok=True)
    (work / "slop" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    log = work / "CHANGELOG.md"
    old = log.read_text() if log.exists() else "# Changelog\n"
    head, _, rest = old.partition("\n")
    log.write_text(f"{head}\n\n## {version}\n- {note}\n{rest}")
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", version)
    git(work, "tag", "v" + version)
    git(work, "push", "-q", "origin", "HEAD:main", "--tags")
    return git(work, "rev-parse", "HEAD").strip()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "remote", "add", "origin", str(origin))
    release(work, "0.2.5", "the tunnel fix")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    git(checkout, "checkout", "-q", "v0.2.5")
    monkeypatch.setenv("SLOP_UPDATE_ROOT", str(checkout))
    monkeypatch.setenv("SLOP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SLOP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(U, "_production_state", lambda: None)
    return {"work": work, "checkout": checkout}


def test_check_finds_a_newer_release_and_caches_it_for_a_day(box):
    r = U.check()
    assert r["current"] == "0.2.5" and r["latest"] == "0.2.5" and not r["newer"] and not r["cached"]
    release(box["work"], "0.2.6", "updates")
    assert U.check()["cached"] and not U.check()["newer"], "the daily answer is reused"
    r = U.check(force=True)
    assert r["latest"] == "0.2.6" and r["newer"] and not r["cached"]


def test_the_reminder_comes_once_per_new_release(box, monkeypatch):
    release(box["work"], "0.2.6", "updates")
    U.check(force=True)
    first = U.reminder()
    assert first and first["latest"] == "0.2.6" and "Review its release commit" in first["line"]
    assert U.reminder() is None, "shown once"
    release(box["work"], "0.2.7", "more")
    U.check(force=True)
    assert U.reminder()["latest"] == "0.2.7", "a newer release reminds again"
    monkeypatch.setenv("SLOP_UPDATE_CHECK", "0")
    release(box["work"], "0.2.8", "even more")
    U.check(force=True)
    assert U.reminder() is None, "SLOP_UPDATE_CHECK=0 turns it off"


def test_apply_checks_out_the_reviewed_release_even_with_a_same_name_branch(box):
    release(box["work"], "0.2.6", "earlier change")
    reviewed = release(box["work"], "0.2.7", "a later fix")
    git(box["checkout"], "branch", "v0.2.7")
    args = Namespace(to=None, force=False, commit=reviewed)
    relay_config = U.slop_home() / "relay" / "config.json"
    relay_config.parent.mkdir(parents=True)
    relay_config.write_text('{"engine": "claude", "model": "opus-custom"}')
    out = U.cmd_apply(args)
    assert out["updated"] and out["from"] == "0.2.5" and out["to"] == "0.2.7"
    assert out["commit"] == reviewed
    assert out["notes"] == ["- a later fix", "- earlier change"]
    assert U.current() == (0, 2, 7)
    assert git(box["checkout"], "rev-parse", "HEAD").strip() == reviewed
    assert not {"next", "installing", "brief", "recipe"} & out.keys()
    assert json.loads(relay_config.read_text()) == {"engine": "claude", "model": "opus-custom"}
    st = json.loads(U.state_path().read_text())
    assert st["latest"] == st["notified"] == "0.2.7", "no reminder for what is installed"
    again = U.cmd_apply(args)
    assert again["updated"] is False and again["commit"] == reviewed


def test_apply_rejects_missing_short_and_mismatched_commits_before_stashing(box):
    reviewed = release(box["work"], "0.2.6", "updates")
    current = git(box["checkout"], "rev-parse", "HEAD").strip()
    file = box["checkout"] / "slop" / "__init__.py"
    edit = '__version__ = "0.2.5"\n# locally edited\n'
    file.write_text(edit)
    for pin, error in (
        (None, "full Git commit ID"),
        (reviewed[:12], "full Git commit ID"),
        (current, "does not resolve to the reviewed commit"),
    ):
        with pytest.raises(SlopError, match=error):
            U.cmd_apply(Namespace(to="0.2.6", force=True, commit=pin))
        assert file.read_text() == edit
        assert git(box["checkout"], "rev-parse", "HEAD").strip() == current
        assert not git(box["checkout"], "stash", "list").strip()


def test_apply_waits_for_the_production_and_keeps_local_changes(box, monkeypatch):
    reviewed = release(box["work"], "0.2.6", "updates")
    release(box["work"], "0.2.7", "unselected release")
    args = Namespace(to="0.2.6", force=False, commit=reviewed)
    monkeypatch.setattr(U, "_production_state", lambda: "working")
    with pytest.raises(SlopError, match="Opus is working"):
        U.cmd_apply(args)
    monkeypatch.setattr(U, "_production_state", lambda: "idle")
    (box["checkout"] / "slop" / "__init__.py").write_text('__version__ = "0.2.5"\n# edited\n')
    out = U.cmd_apply(args)
    assert out["updated"] and out["stashed"] == ["slop/__init__.py"] and "set aside" in out["text"]
    assert U.current() == (0, 2, 6)
    assert "claudia update to v0.2.6" in git(box["checkout"], "stash", "list")
    assert "# edited" in git(box["checkout"], "stash", "show", "-p"), "the local edit is kept"
    (box["checkout"] / "slop" / "__init__.py").write_text('__version__ = "0.2.6"\n# edited again\n')
    again = U.cmd_apply(args)
    assert again["updated"] is False and "edited again" in (box["checkout"] / "slop" / "__init__.py").read_text()


def test_notes_since_takes_only_newer_versions():
    text = "# Changelog\n\n## 0.2.7\n- c\n\n## 0.2.6\n- b\n- b2\n\n## 0.2.5\n- a\n"
    assert U.notes_since(text, (0, 2, 5)) == ["- c", "- b", "- b2"]
    assert U.notes_since(text, (0, 2, 7)) == []
