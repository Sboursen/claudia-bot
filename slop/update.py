"""`slop update`: is a newer Claudia release out, and the one-step update.

  slop update check [--force]                  this checkout's version and the newest release (the v* tags on its
                                               origin); one network call a day at most (--force: now)
  slop update apply --commit SHA [--to VERSION] [--force]
                                               fetch tags and check out a previously reviewed release commit.
                                               Refused while the production works (--force overrides that check).
  slop update seen                             the reminder for the newest release was shown

The reminder rides on `slop relay read`: an `update` field with one line, once per new release, from the daily check
(SLOP_UPDATE_CHECK=0 turns it off). Applying a release requires its independently reviewed full Git ID.
State: $SLOP_HOME/update.json {checked, latest, notified}.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import util
from .config import SlopError, slop_home, toolkit_root

CHECK_EVERY_S = 24 * 3600
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)


def root() -> Path:
    return Path(os.environ.get("SLOP_UPDATE_ROOT") or toolkit_root())


def state_path() -> Path:
    return slop_home() / "update.json"


def _state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(**kw) -> dict:
    st = {**_state(), **kw}
    state_path().parent.mkdir(parents=True, exist_ok=True)
    util.atomic_write_text(state_path(), json.dumps(st, indent=1) + "\n")
    return st


def parse(v: str | None) -> tuple[int, int, int] | None:
    m = VERSION_RE.match((v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def show(v: tuple[int, int, int] | None) -> str | None:
    return ".".join(map(str, v)) if v else None


def _git(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    command = ["git", "-C", str(root()), "-c", f"core.hooksPath={os.devnull}", "-c", "submodule.recurse=false", *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)


def current() -> tuple[int, int, int] | None:
    """the checked-out code's version (read from the files, so it is right just after a checkout)"""
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', (root() / "slop" / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return parse(m.group(1)) if m else None


def remote_latest(timeout: float = 8.0) -> tuple[int, int, int] | None:
    r = _git("ls-remote", "--tags", "--refs", "origin", timeout=timeout)
    if r.returncode != 0:
        raise SlopError("cannot read the releases from the toolkit's origin: " + (r.stderr or r.stdout).strip()[-200:])
    vs = [parse(line.rsplit("refs/tags/", 1)[-1]) for line in r.stdout.splitlines() if "refs/tags/" in line]
    vs = [v for v in vs if v]
    return max(vs) if vs else None


def check(force: bool = False, timeout: float = 8.0) -> dict:
    """{current, latest, newer, checked, cached[, offline]}: the network at most once a CHECK_EVERY_S."""
    st = _state()
    cur = current()
    fresh = st.get("checked_ts") and time.time() - float(st["checked_ts"]) < CHECK_EVERY_S
    if fresh and not force:
        latest = parse(st.get("latest"))
        cached, offline = True, False
    else:
        cached = False
        try:
            latest = remote_latest(timeout)
            offline = False
            st = _save(checked=util.iso_now(), checked_ts=time.time(), latest=show(latest))
        except (SlopError, OSError, subprocess.SubprocessError):
            latest, offline = parse(st.get("latest")), True
    newer = bool(cur and latest and latest > cur)
    return {"current": show(cur), "latest": show(latest), "newer": newer, "checked": st.get("checked"),
            "cached": cached, **({"offline": True} if offline else {})}


def reminder() -> dict | None:
    """One quiet line, once per new release, for `slop relay read`; None otherwise (and on any error)."""
    if os.environ.get("SLOP_UPDATE_CHECK", "1").strip() == "0":
        return None
    try:
        r = check(timeout=5.0)
        if not r["newer"] or _state().get("notified") == r["latest"]:
            return None
        _save(notified=r["latest"])
        return {"latest": r["latest"], "current": r["current"],
                "line": f"(Claudia v{r['latest']} is available. Review its release commit before applying it.)"}
    except Exception:  # noqa: BLE001 - a reminder never breaks the relay
        return None


def notes_since(text: str, since: tuple[int, int, int] | None, limit: int = 6) -> list[str]:
    """the CHANGELOG.md bullets of the versions newer than `since`, newest first, at most `limit` lines"""
    out, take = [], False
    for line in text.splitlines():
        m = re.match(r"^##\s+v?(\d+\.\d+\.\d+)", line)
        if m:
            v = parse(m.group(1))
            take = bool(v and (since is None or v > since))
            continue
        if take and line.lstrip().startswith("- "):
            out.append(line.strip())
    return out[:limit]


def _production_state() -> str | None:
    """the relay production's state (working / idle / ...), or None when the relay is not set up"""
    try:
        from . import relay
        _, rd = relay._dirs()
        if not (rd / "config.json").is_file():
            return None
        return relay.production(rd).get("state")
    except Exception:  # noqa: BLE001
        return None


def cmd_check(a) -> dict:
    r = check(force=a.force)
    if r["newer"]:
        text = f"Claudia v{r['latest']} is out (this computer has v{r['current']}). Review its release commit before applying it."
    elif r.get("offline") and not r["latest"]:
        text = f"Claudia v{r['current']} here; the releases could not be read right now (offline?)."
    else:
        text = f"Claudia v{r['current']} is the newest."
    return {**r, "text": text}


def cmd_seen(a) -> dict:
    r = check()
    if r["latest"]:
        _save(notified=r["latest"])
    return {**r, "text": f"reminder for v{r['latest']} marked as shown"}


def cmd_apply(a) -> dict:
    reviewed = getattr(a, "commit", None)
    if not isinstance(reviewed, str) or not COMMIT_RE.fullmatch(reviewed):
        raise SlopError("provide the independently reviewed, full Git commit ID with --commit")
    reviewed = reviewed.lower()
    if not a.force and _production_state() == "working":
        raise SlopError("Opus is working right now. Apply the reviewed commit after the job finishes.")
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        raise SlopError(f"{root()} is not a git checkout of the toolkit: " + dirty.stderr.strip()[-200:])
    before = current()
    f = _git("fetch", "-q", "--tags", "origin", timeout=180)
    if f.returncode != 0:
        raise SlopError("could not fetch the releases: " + (f.stderr or f.stdout).strip()[-300:])
    tags = [parse(t) for t in _git("tag", "--list", "v*").stdout.split()]
    tags = sorted(t for t in tags if t)
    target = parse(a.to) if a.to else (tags[-1] if tags else None)
    if not target or target not in tags:
        raise SlopError(f"no release v{a.to or '?'}: the releases are " + ", ".join("v" + show(t) for t in tags[-5:]))
    selected = _git("rev-parse", "--verify", f"refs/tags/v{show(target)}^{{commit}}")
    if selected.returncode != 0 or selected.stdout.strip() != reviewed:
        raise SlopError(f"release v{show(target)} does not resolve to the reviewed commit")
    head = _git("rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode == 0 and head.stdout.strip() == reviewed:
        _save(latest=show(max(tags)), notified=show(max(tags)))
        return {"updated": False, "current": show(before), "commit": reviewed,
                "text": f"already on reviewed commit {reviewed}"}
    stashed = None
    if dirty.stdout.strip():                 # a local fix (e.g. one the bot made) is kept in git's stash, not lost
        files = [ln[3:] for ln in dirty.stdout.splitlines()]
        s = _git("-c", "user.name=Claudia bot", "-c", "user.email=bot@claudia.invalid", "stash", "push", "-m",
                 f"claudia update to v{show(target)}: local changes set aside {util.iso_now()}")
        if s.returncode != 0:
            raise SlopError(f"the toolkit at {root()} has local changes ({', '.join(files[:5])}) and they could not "
                            "be set aside, so it was not updated: " + (s.stderr or s.stdout).strip()[-200:])
        stashed = files
    co = _git("checkout", "-q", "--detach", reviewed)
    if co.returncode != 0:
        if stashed:
            _git("stash", "pop")             # back as they were
        raise SlopError(f"could not check out v{show(target)}: " + co.stderr.strip()[-300:])
    try:
        notes = notes_since((root() / "CHANGELOG.md").read_text(encoding="utf-8"), before)
    except OSError:
        notes = []
    _save(latest=show(max(tags)), notified=show(max(tags)), checked=util.iso_now(), checked_ts=time.time())
    lines = [f"checked out reviewed release v{show(target)} at {reviewed}"]
    if stashed:
        lines.append(f"local edits set aside ({', '.join(stashed[:5])}). They are kept in the Git stash.")
    if notes:
        lines.extend(notes)
    return {"updated": True, "from": show(before), "to": show(target), "commit": reviewed, "notes": notes,
            "stashed": stashed, "text": "\n".join(lines)}


def register(sub) -> None:
    g = sub.add_parser("update", help="check for releases or check out a reviewed release commit")
    us = g.add_subparsers(dest="update_cmd", metavar="ACTION")
    us.required = True
    p = us.add_parser("check", help="this computer's version and the newest release (one network call a day at most)")
    p.add_argument("--force", action="store_true", help="ask the network now, not the cached daily answer")
    p.set_defaults(func=cmd_check)
    p = us.add_parser("apply", help="check out the selected release after verifying its reviewed commit")
    p.add_argument("--commit", required=True, help="independently reviewed full Git commit ID")
    p.add_argument("--to", help="a release version (e.g. 0.2.6). Defaults to the latest tag.")
    p.add_argument("--force", action="store_true", help="update even while the production works")
    p.set_defaults(func=cmd_apply)
    p = us.add_parser("seen", help="the reminder for the newest release was shown (it is not shown again)")
    p.set_defaults(func=cmd_seen)
