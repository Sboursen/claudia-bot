"""Local relay queue for production messages, replies and job records.

Run `slop relay setup` to create local queue state, then
`slop relay send -- MESSAGE` to record a message. Review it with
`slop relay inbox` and inspect replies or jobs through `relay read`
and `relay status`. Attachments must be staged under the projects directory.

The former production supervisor ran authenticated engines unattended with
host privileges. The relay queues work without starting a production process
until it has an isolated execution and approval broker. Existing queue state
and logs are readable, and `relay stop` can stop a supervisor from an earlier
installation.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path

from . import util
from .config import SlopError, projects_root, resolve_project, slop_home

ENGINES = ("claude", "codex")
DEFAULT_MODEL = {"claude": "opus", "codex": None}
PRODUCTION_BLOCKED = "autonomous production is disabled until an isolated execution broker is available"
WAIT_CAP_S = 240.0
READ_SETTLE = 5                # a production that stops working must stay stopped this many polls (2 s) to end a wait
CLAIM_TTL_S = 10 * 60         # a Grok run ends ~5 min in: a claim this old belongs to a run that is gone
JOB_STALE_S = 6 * 3600         # an open job this old is never run without asking the production first
PROD_STALL_S = 20 * 60         # a working production with no progress line for this long may be stuck
START_GRACE_S = 30.0           # a "starting" claim without a supervisor is given up after this
LOCK_WAIT_S = 20.0
LOCK_STALE_S = 60.0            # every change holds the lock for milliseconds; an older lock is a crashed caller's
EFFORT_DEFAULT = "xhigh"       # both engines; a level the engine or its model does not take falls back to its default
EFFORT_ENV = {"claude": "SLOP_CLAUDE_EFFORT", "codex": "SLOP_CODEX_EFFORT"}
EFFORT_USUAL = {"claude": ("low", "medium", "high", "xhigh", "max"),
                "codex": ("low", "medium", "high", "xhigh", "max", "ultra")}


# ================================================================================================== files and lock
def relay_dir() -> Path:
    return slop_home() / "relay"


def work_dir() -> Path:
    """The production's working folder: the projects folder (it picks or creates the project itself)."""
    return projects_root()


def _dirs() -> tuple[Path, Path]:
    rd, wd = relay_dir(), work_dir()
    rd.mkdir(parents=True, exist_ok=True, mode=0o700)
    wd.mkdir(parents=True, exist_ok=True)
    return wd.resolve(), rd


def _break_stale(lk: Path) -> bool:
    """Remove a lock whose owner process is gone (or, with no owner recorded, one older than LOCK_STALE_S; the wall
    clock can step, so a live owner is never judged by age)."""
    try:
        pid = int((lk / "owner").read_text().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    try:
        if pid:
            if _alive(pid):
                return False
        elif time.time() - lk.stat().st_mtime < LOCK_STALE_S:
            return False
        for f in lk.iterdir():
            f.unlink()
        os.rmdir(lk)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def _lock(rd: Path):
    """A lock folder (mkdir is atomic on every OS) with its owner's pid. Held for milliseconds."""
    rd.mkdir(parents=True, exist_ok=True)
    lk = rd / "lock"
    t0 = time.monotonic()
    while True:
        try:
            os.mkdir(lk)
            break
        except (FileExistsError, PermissionError):
            pass
        if _break_stale(lk):
            continue
        if time.monotonic() - t0 > LOCK_WAIT_S:
            raise SlopError(f"the relay is busy ({lk} is held); try again in a few seconds")
        time.sleep(0.02)
    try:
        (lk / "owner").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    try:
        yield
    finally:
        try:
            (lk / "owner").unlink()
        except OSError:
            pass
        try:
            os.rmdir(lk)
        except OSError:
            pass


def _read_json(p: Path) -> dict:
    for _ in range(5):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):        # Windows: a rename in progress; elsewhere: never half-written
            time.sleep(0.02)
    return {}


def _write_json(p: Path, d: dict) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for _ in range(50):
        try:
            tmp.replace(p)
            return
        except PermissionError:              # Windows: a reader has the file open for a moment
            time.sleep(0.02)
    tmp.replace(p)


def _rows(p: Path) -> list[dict]:
    """Complete lines of a .jsonl file (a last line without its newline is still being written: skipped)."""
    try:
        data = Path(p).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in data.split("\n")[:-1]:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _append(p: Path, rec: dict) -> None:
    util.append_text(p, json.dumps(rec, ensure_ascii=False) + "\n")


def _next_id(p: Path) -> int:
    return max((int(r.get("id") or 0) for r in _rows(p)), default=0) + 1


def _seq(rd: Path) -> int:
    """One order across the inbox and the outbox (never the wall clock: it can step, e.g. in WSL or a resumed VM)."""
    return max((int(r.get("seq") or 0) for n in ("inbox.jsonl", "outbox.jsonl") for r in _rows(rd / n)), default=0) + 1


def _log(rd: Path, msg: str) -> None:
    try:
        util.append_text(rd / "relay.log", f"{util.stamp()} {' '.join(str(msg).split())[:500]}\n")
    except OSError:
        pass


def _tail(p: Path, n: int) -> str:
    try:
        return "\n".join(Path(p).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _cfg(rd: Path, need: bool = True) -> dict:
    c = _read_json(rd / "config.json")
    if need and not c.get("engine"):
        raise SlopError("the relay is not set up on this computer: run  slop --json relay setup  first")
    return c


# ================================================================================================== production state
def _alive(pid, start=None) -> bool:
    from .job import alive
    try:
        return alive(int(pid), start) if pid else False
    except (TypeError, ValueError):
        return False


def _runner(rd: Path) -> dict:
    r = _read_json(rd / "runner.json")
    st = r.get("state")
    if st == "starting":                  # the monotonic clock is shared by every process and never steps
        age = time.monotonic() - float(r.get("since_mono") or 0)
        r["alive"] = 0 <= age < START_GRACE_S
    elif st == "running":
        r["alive"] = _alive(r.get("pid"), r.get("pid_start"))
    else:
        r["alive"] = False
    return r


def _require_inactive(rd: Path) -> None:
    if _runner(rd)["alive"]:
        raise SlopError("stop the existing production supervisor with `slop relay stop` before changing the local queue")


def _pending(rd: Path, st: dict) -> list[dict]:
    seen = int(st.get("delivered") or 0)
    return [m for m in _rows(rd / "inbox.jsonl") if int(m.get("id") or 0) > seen]


def job_id(oid: int) -> str:
    return f"J-{int(oid):04d}"


def _jobs(rd: Path, open_only: bool = True) -> list[dict]:
    """Jobs the production asked for (outbox items of kind "job"), with their status from jobs.json."""
    done = _read_json(rd / "jobs.json")
    out = []
    for r in _rows(rd / "outbox.jsonl"):
        if r.get("kind") != "job":
            continue
        st = done.get(r.get("job")) or {}
        j = {**r, "status": st.get("status") or "queued"}
        if j["status"] == "claimed":
            if time.time() - float(st.get("claimed_ts") or 0) > CLAIM_TTL_S:
                j["status"], j["claim_expired"] = "queued", True
            else:
                j["claimed_s"] = round(time.time() - float(st.get("claimed_ts") or 0))
        if j["status"] == "queued" and r.get("t") and time.time() - float(r["t"]) > JOB_STALE_S:
            j["stale_h"] = round((time.time() - float(r["t"])) / 3600, 1)
        for k in ("reason", "ended"):
            if st.get(k):
                j[k] = st[k]
        if st.get("files"):
            j["result_files"] = st["files"]
        if not open_only or j["status"] in ("queued", "claimed"):
            out.append(j)
    return out


def production(rd: Path) -> dict:
    """{state: working|waiting|idle|failed|stopped|queued|not set up, since, turn, last, error, summary}."""
    from .job import dur
    if not (rd / "config.json").is_file():
        return {"state": "not set up", "summary": "the relay is not set up (slop --json relay setup)"}
    r = _runner(rd)
    tj = _read_json(rd / "turn.json")
    st = _read_json(rd / "state.json")
    pend = _pending(rd, st)
    out: dict = {"pending": len(pend)}
    if r["alive"]:
        running = tj.get("state") == "running" and r.get("state") == "running"
        since = float((tj.get("started_ts") if running else r.get("since_ts")) or time.time())
        out.update(state="working", since=(tj.get("started") if running else r.get("since")),
                   turn=tj.get("n") if running else None, last=(tj.get("last") if running else "starting a turn"),
                   last_at=tj.get("last_at"), working_s=round(time.time() - since, 1))
        quiet = time.time() - float((tj.get("last_ts") if running else r.get("since_ts")) or time.time())
        out["quiet_s"] = round(quiet, 1)
        out["summary"] = (f"working for {dur(out['working_s'])}" + (f" (turn {out['turn']})" if out.get("turn") else "")
                          + (f": {out['last']}" if out.get("last") else "")
                          + (f" ({dur(quiet)} ago)" if quiet >= 60 else "")
                          + (f"; {len(pend)} message(s) wait for the next turn" if pend else ""))
        if quiet > PROD_STALL_S:
            out["stalled"] = True
            out["summary"] += (f". No progress for {dur(quiet)}: it may be stuck. `slop --json relay log` shows the "
                               "last lines; use `slop --json relay stop` to stop the legacy supervisor.")
        return out
    state = r.get("state")
    if state == "running":                # its process is gone without a record
        state, r["error"] = "failed", ("the production's process ended without a record (killed, out of memory, or "
                                         "the computer restarted)")
    if state == "failed":
        out.update(state="failed", error=r.get("error"), since=r.get("since"), log_tail=_tail(rd / "relay.log", 8),
                   stderr_tail=_tail(rd / "turns" / f"turn-{int(tj.get('n') or 0):04d}.err", 8))
        out["summary"] = f"failed: {str(r.get('error') or '?')[:300]}"
        return out
    if state == "stopped":
        out.update(state="stopped", since=r.get("since"), summary=f"stopped; {PRODUCTION_BLOCKED}")
        return out
    if pend:
        out.update(state="queued", summary=f"{len(pend)} message(s) wait in the local queue; {PRODUCTION_BLOCKED}")
        return out
    talk = [m for m in _rows(rd / "outbox.jsonl") if m.get("kind") in ("say", "ask")]
    last = talk[-1] if talk else None
    said = [m for m in _rows(rd / "inbox.jsonl") if m.get("from") == "director"]
    if last and last.get("kind") == "ask" and (not said or int(said[-1].get("seq") or 0) < int(last.get("seq") or 0)):
        out.update(state="waiting", question=last.get("text"), default=last.get("default"),
                   summary=f"waiting for the director: {str(last.get('text'))[:200]}")
        return out
    out.update(state="idle", since=r.get("since"), summary=f"local queue is idle; {PRODUCTION_BLOCKED}")
    return out


# ================================================================================================== setup
def cmd_setup(a) -> dict:
    """Create local queue metadata without probing or configuring vendor engines."""
    from .agents import engines as E

    wd, rd = _dirs()
    with _lock(rd):
        _require_inactive(rd)
        previous = _read_json(rd / "config.json")
        eng = E.norm_engine(a.engine) if a.engine else previous.get("engine") or "claude"
        if eng not in ENGINES:
            raise SlopError("choose Claude Code or Codex as the production engine")
        if a.model is not None:
            model = a.model
        else:
            model = previous.get("model", DEFAULT_MODEL[eng]) if previous.get("engine") == eng else DEFAULT_MODEL[eng]
        cfg = {"engine": eng, "model": model or None, "set_up": util.iso_now(),
               "work_dir": str(wd), "production_enabled": False}
        _write_json(rd / "config.json", cfg)
    _log(rd, f"setup: local queue for engine {eng}")
    return {**cfg, "relay_dir": str(rd), "next": "slop --json relay inbox",
            "text": f"local relay queue ready; {PRODUCTION_BLOCKED}"}


# ================================================================================================== send / inbox
def cmd_send(a) -> dict:
    text = " ".join(a.text or [])
    if not text.strip():
        raise SlopError("provide the message after --: slop --json relay send -- MESSAGE")

    files = []
    for name in a.attach or []:
        path = _abs_file(name, work_dir().resolve())
        if path is None:
            raise SlopError("stage each attachment as a regular file under the projects directory first")
        files.append(str(path))
    if files:
        text += "\n" + "\n".join(f"[attached file] {path}" for path in files)
    return {**deliver(text, a.sender), "attached": files}


def deliver(text: str, who: str, job_update: tuple | None = None) -> dict:
    """Append a local inbox message and an optional job update under one lock."""
    _, rd = _dirs()
    with _lock(rd):
        cfg = _cfg(rd)
        _require_inactive(rd)
        if job_update:
            jobs = _read_json(rd / "jobs.json")
            jobs[job_update[0]] = job_update[1]
            _write_json(rd / "jobs.json", jobs)
        mid = _next_id(rd / "inbox.jsonl")
        _append(rd / "inbox.jsonl", {"id": mid, "seq": _seq(rd), "ts": util.iso_now(), "t": round(time.time(), 3),
                                     "from": who, "text": text})
        if who == "director":
            dl = rd / "decisions.md"
            head = "" if dl.is_file() else "# The director's words (slop relay; dated, append-only, never edited)\n\n"
            util.append_text(dl, head + f"- {util.stamp()} - [director, relay #{mid}] "
                             + re.sub(r"\s*\n\s*", " / ", text.strip()) + "\n")
    _log(rd, f"in #{mid} from {who}")
    return {"ok": True, "id": mid, "from": who, "engine": cfg["engine"], "started": False, "queued": True,
            "production_enabled": False, "next": "slop --json relay inbox",
            "text": f"message #{mid} saved in the local queue; {PRODUCTION_BLOCKED}"}


def cmd_inbox(a) -> dict:
    _, rd = _dirs()
    with _lock(rd):
        st = _read_json(rd / "state.json")
        new = _pending(rd, st)
        if new:
            st["delivered"] = max(int(m["id"]) for m in new)
            _write_json(rd / "state.json", st)
    msgs = [{k: m.get(k) for k in ("id", "ts", "from", "text")} for m in new]
    if not msgs:
        return {"messages": [], "count": 0, "text": "no new messages"}
    text = "\n\n".join(f"#{m['id']} from the {m['from']} ({m['ts']}):\n{m['text']}" for m in msgs)
    return {"messages": msgs, "count": len(msgs), "text": text}


# ================================================================================================== say / ask / job
@contextlib.contextmanager
def _staged_dir(path: Path, create: bool = False):
    """Open a directory beneath the configured project root without following links."""
    root = work_dir().resolve()
    relative = path.relative_to(root)
    if ".." in relative.parts:
        raise ValueError("not a staged path")
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks):
        raise OSError("protected file traversal is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in relative.parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _staged_parent(path: Path, create: bool = False):
    if path.name in ("", ".."):
        raise ValueError("not a staged file")
    with _staged_dir(path.parent, create) as parent:
        yield parent, path.name


@contextlib.contextmanager
def _open_staged_file(path: Path):
    with _staged_parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("not a staged regular file")
            yield stream


def _write_staged_file(path: Path, data: bytes) -> None:
    with _staged_parent(path) as (parent, name):
        tmp = f".relay-{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=parent)


def _abs_file(f: str, *roots: Path) -> Path | None:
    p = Path(f).expanduser()
    cands = [p] if p.is_absolute() else [Path.cwd() / p] + [r / p for r in roots if r]
    for candidate in cands:
        candidate = candidate.absolute()
        try:
            with _staged_parent(candidate) as (parent, name):
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISREG(entry.st_mode):
                    return candidate
        except (OSError, ValueError):
            continue
    return None


def _turn_no() -> int | None:
    t = os.environ.get("SLOP_RELAY_TURN") or ""
    return int(t) if t.isdigit() else None


def _post(a, kind: str) -> dict:
    wd, rd = _dirs()
    text = " ".join(a.text or [])
    if not text.strip():
        raise SlopError(f"nothing to {kind}: slop relay {kind} \"the words\" [--show FILE ...]")
    files, missing = [], []
    for f in a.show or []:
        hit = _abs_file(f, wd)
        (files.append(str(hit)) if hit else missing.append(f))
    if missing:
        raise SlopError(f"--show: no such file: {', '.join(missing)} (nothing was posted; give files that exist)")
    if not getattr(a, "as_is", False):
        bad = [(f, why) for f in files if (why := _video_issue(Path(f)))]
        if bad:
            f, why = bad[0]
            raise SlopError(f"{Path(f).name} is {why}: phones play it washed out or not at all, so nothing was posted. "
                            f"Show a phone copy: ffmpeg -i '{f}' -vf scale=out_range=tv,format=yuv420p -color_range tv "
                            "-c:v libx264 -crf 18 -preset medium -c:a aac -movflags +faststart '<name>-phone.mp4' "
                            "(or post it as it is with --as-is)")
        huge = [f for f in files if Path(f).suffix.lower() in VIDEO_EXT and _size(f) > DRIVE_MAX_B]
        if huge:
            raise SlopError(f"{Path(huge[0]).name} is {_size(huge[0]) / 1e6:.0f} MB: over the 100 MB Drive upload "
                            "limit, so it cannot reach the person, and nothing was posted. Show a share copy: "
                            "slop -p NAME render encode --share (under 95 MB), or post it as it is with --as-is")
    rec = {"ts": util.iso_now(), "t": round(time.time(), 3), "kind": kind, "text": text, "files": files,
           "turn": _turn_no()}
    if kind == "ask":
        rec["default"] = a.default
    with _lock(rd):
        oid = _next_id(rd / "outbox.jsonl")
        _append(rd / "outbox.jsonl", {"id": oid, "seq": _seq(rd), **rec})
    _log(rd, f"out #{oid} {kind}" + (f" (+{len(files)} files)" if files else ""))
    return {"id": oid, "kind": kind, "files": files,
            "text": f"recorded locally #{oid} ({kind}{f', {len(files)} file(s)' if files else ''})"}


def _video_probe(path: Path, entries: str, output: str) -> str | None:
    """Probe a staged MP4/MOV descriptor without external media references."""
    if path.suffix.lower() not in VIDEO_EXT or _real_ext(path) not in (".mov", ".mp4"):
        return None
    import shutil
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        with _open_staged_file(path) as stream:
            fd = stream.fileno()
            fd_dir = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
            result = subprocess.run(
                [exe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
                 "-select_streams", "v:0", "-show_entries", entries, "-of", output, f"{fd_dir}/{fd}"],
                capture_output=True, text=True, timeout=30, pass_fds=(fd,), env={"LANG": "C"},
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _video_issue(path: Path) -> str | None:
    """Why a video plays badly on phones (full range yuvj420p, 4:4:4, 10-bit), if known."""
    try:
        st = (json.loads(_video_probe(path, "stream=pix_fmt,color_range", "json") or "{}")
              .get("streams") or [{}])[0]
    except (ValueError, AttributeError):
        return None
    pix, rng = str(st.get("pix_fmt") or ""), str(st.get("color_range") or "")
    if not pix:
        return None
    if pix.startswith("yuvj") or rng == "pc":
        return f"full range ({pix}{', pc range' if rng == 'pc' else ''})"
    if pix not in ("yuv420p", "nv12"):
        return f"{pix} (phones play yuv420p)"
    return None


def cmd_say(a) -> dict:
    return _post(a, "say")


def cmd_ask(a) -> dict:
    return _post(a, "ask")


def _save_base(a, wd: Path) -> Path:
    """Where a job's downloads go by default: the production's project (-p / $SLOP_PROJECT / the folder it works in),
    else the projects folder; then inbox/<kind>/."""
    try:
        return resolve_project(getattr(a, "project", None))
    except SlopError:
        return wd


def _job_path(p: str | Path, base: Path, wd: Path) -> Path:
    """A path a job names. Relative: to the project, unless it already starts at the projects folder (the production
    works there, so `NAME/inbox` from it is the project's inbox, never NAME/NAME/inbox)."""
    q = Path(p).expanduser()
    if q.is_absolute():
        return q.absolute()
    via_wd, proj = (wd / q).absolute(), base.absolute()
    if proj != wd.absolute() and (via_wd == proj or proj in via_wd.parents):
        return via_wd
    return (base / q).absolute()


def _read_text(text: str | None, path: str | None, what: str, *roots: Path) -> str | None:
    if path:
        if text:
            raise SlopError(f"give the {what} as text or as a file, not both")
        staged = _abs_file(path, *roots)
        if staged is None:
            raise SlopError(f"stage the {what} file under the projects directory first")
        try:
            with _open_staged_file(staged) as stream:
                return stream.read().decode("utf-8").strip()
        except (OSError, ValueError):
            raise SlopError(f"cannot read the staged {what} file") from None
    return text


def _project_mj(base: Path) -> dict:
    try:
        with _open_staged_file(base / "project.json") as stream:
            mj = json.load(stream).get("mj")
    except (OSError, ValueError, AttributeError):
        return {}
    return mj if isinstance(mj, dict) else {}


def _imagine_image(path: str | None, flag: str, base: Path, wd: Path) -> Path | None:
    """A picture for Grok's own Grok Imagine tool: it must exist and be JPEG, PNG or WebP."""
    if not path:
        return None
    img = _abs_file(path, base, wd)
    if img is None:
        raise SlopError(f"{flag}: no such file: {path} (nothing was posted)")
    if _real_ext(Path(img)) not in IMAGINE_TYPES:
        raise SlopError(f"{flag}: {path} is not a JPEG, PNG or WebP picture (Grok Imagine takes only those; nothing "
                        "was posted)")
    return Path(img)


def _imagine_fields(a, base: Path, wd: Path) -> dict:
    """grok-imagine: check the job against what Grok's own tool makes (docs.x.ai/grok-bot/files-and-results)."""
    clip = a.what == "clip"
    img = _imagine_image(a.image, "--image", base, wd)
    end = _imagine_image(getattr(a, "end_image", None), "--end-image", base, wd)
    refs = [_imagine_image(r, "--ref", base, wd) for r in (getattr(a, "ref", None) or []) if r.strip()]
    res = getattr(a, "res", None)
    if not clip:
        if end or res or a.seconds:
            raise SlopError("a still takes no --end-image, --res or --seconds (those are for --what clip)")
        if a.aspect and a.aspect not in IMAGINE_STILL_ASPECTS:
            raise SlopError(f"--aspect {a.aspect}: Grok's image tool makes stills in "
                            f"{', '.join(IMAGINE_STILL_ASPECTS)} (nearest: 3:2 -> 4:3, 2:3 -> 3:4); nothing was posted")
    elif a.seconds is not None and not IMAGINE_SECONDS[0] <= a.seconds <= IMAGINE_SECONDS[1]:
        raise SlopError(f"--seconds {a.seconds}: Grok Imagine clips run {IMAGINE_SECONDS[0]}-{IMAGINE_SECONDS[1]} s "
                        "(nothing was posted)")
    if len(refs) > IMAGINE_MAX_REFS:
        raise SlopError(f"--ref: at most {IMAGINE_MAX_REFS} reference images ({len(refs)} given; nothing was posted)")
    pics = [p for p in [img, end, *refs] if p]
    if clip and sum(_size(str(p)) for p in pics) > IMAGINE_MAX_IMAGES_B:
        raise SlopError(f"the clip's images add up to over {IMAGINE_MAX_IMAGES_B // 2**20} MB, the most Grok's video "
                        "tool takes: send smaller copies (JPEG) and post the job again (nothing was posted)")
    res = (res or IMAGINE_DEFAULT_RES) if clip else None
    return {"image": str(img) if img else None, "end_image": str(end) if end else None,
            "refs": [str(r) for r in refs] or None, "res": res,
            "route": "browser" if res == "1080p" else "native"}


def cmd_job(a) -> dict:
    """Record a planned job and its input and output metadata for local review."""
    wd, rd = _dirs()
    _require_inactive(rd)
    base = _save_base(a, wd)
    rec: dict = {"kind": "job", "job_kind": a.kind, "what": a.what, "note": a.note}
    adjusted: list[str] = []
    if a.kind == "midjourney":
        if not a.text:
            raise SlopError("a midjourney job needs its prompts: --text '<one full prompt with its parameters>', once "
                            "per prompt (each becomes its own Midjourney job of four images)")
        if any(re.search(r"\{[^{}]*,[^{}]*\}", t.replace("\\,", "")) for t in a.text):
            raise SlopError("one prompt per --text: a {a, b} permutation block runs several Midjourney jobs; send each "
                            "Sheet A line as its own --text (plates/mj/BATCH.lines.txt)")
        plates_ = [p.strip() for p in (a.plate or []) if p.strip()]
        if plates_ and len(plates_) != len(a.text):
            raise SlopError(f"--plate: one per --text, in the same order ({len(a.text)} prompts, {len(plates_)} "
                            "plates)")
        # the person's Midjourney setup, whatever the prompt says: no --v (its default is its newest model), no
        # profile code (Grok presses the P button), --raw / --hd as they chose (project.json mj)
        from .board.plates import mj_line
        mj = _project_mj(base)
        texts = []
        for t in a.text:
            line, did = mj_line(t, mj)
            texts.append(line)
            adjusted += [d for d in did if d not in adjusted]
        pers = a.personalize or ("on" if mj.get("personalize", True) else "off")
        rec.update(text=texts, expect_count=4 * len(texts), personalize=pers == "on", plates=plates_ or None)
        long_ = [(i, len(t.split(" --")[0].strip())) for i, t in enumerate(texts, 1)
                 if len(t.split(" --")[0].strip()) > MJ_PROMPT_MAX]
        if long_ and not getattr(a, "long", False):  # Midjourney rewrites long prompts silently (a "Long Prompt" badge)
            raise SlopError("; ".join(f"prompt {i} is {n} characters before its parameters" for i, n in long_)
                            + f": Midjourney shortens prompts over {MJ_PROMPT_MAX} characters, so the image would not "
                            "match its prompt. Trim it and send the job again (nothing was posted; --long overrides)")
    elif a.kind == "suno":
        lyrics = _read_text(a.lyrics, a.lyrics_file, "lyrics", base, wd)
        if not (a.style or "").strip():
            raise SlopError("a suno job needs --style '<the Style of Music field>'")
        if not (lyrics or "").strip() and not a.instrumental:
            raise SlopError("a suno job needs --lyrics / --lyrics-file, or --instrumental")
        rec.update(title=a.title, style=a.style, lyrics=lyrics, exclude=a.exclude, instrumental=bool(a.instrumental),
                   expect_count=2)
    elif a.kind == "browser":
        if not (a.note or "").strip():
            raise SlopError("a browser job needs --note '<what Grok should do there, in one or two sentences>'")
        rec.update(url=a.url, text=list(a.text or []))
    elif a.kind == "grok-imagine":
        if a.what not in ("still", "clip"):
            raise SlopError("--what still|clip: what Grok Imagine should make")
        prompt = _read_text(a.prompt, a.prompt_file, "prompt", base, wd)
        if not (prompt or "").strip():
            raise SlopError("a grok-imagine job needs its prompt: --prompt TEXT or --prompt-file FILE")
        rec.update(prompt=prompt, aspect=a.aspect, seconds=a.seconds, **_imagine_fields(a, base, wd))
        default_save = base / "inbox" / "grok-imagine"
    else:
        if not a.text and not a.note:
            raise SlopError("a person job needs its copy blocks (--text, once per block) or a --note saying what to do")
        rec.update(text=list(a.text or []), expect=[str(_job_path(e, base, wd)) for e in a.expect or []])
    if a.kind != "grok-imagine":
        default_save = base / "inbox" / a.kind
    save = _job_path(a.save, base, wd) if a.save else default_save.absolute()
    try:
        with _staged_dir(save, create=True):
            pass
        for expected in rec.get("expect") or []:
            with _staged_parent(Path(expected), create=True) as (parent, name):
                with contextlib.suppress(FileNotFoundError):
                    if not stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        raise OSError("expected output is not a regular file")
    except (OSError, ValueError):
        raise SlopError("stage job destinations under the projects directory without symbolic links") from None
    with _lock(rd):
        _require_inactive(rd)
        oid = _next_id(rd / "outbox.jsonl")
        jid = job_id(oid)
        rec.update(id=oid, seq=_seq(rd), job=jid, ts=util.iso_now(), t=round(time.time(), 3), save=str(save),
                   name=a.name or jid, files=[], turn=_turn_no())
        _append(rd / "outbox.jsonl", {k: v for k, v in rec.items() if v is not None})
    _log(rd, f"out #{oid} job {jid} ({a.kind}{' ' + a.what if a.what else ''})")
    return {"id": oid, "job": jid, "save": str(save), "adjusted": adjusted,
            "dispatch_enabled": False,
            "text": f"job {jid} ({a.kind}) recorded for local review; automatic dispatch is disabled"}


def _open_job(rd: Path, jid: str) -> dict:
    jid = jid.strip().upper()
    if re.fullmatch(r"(?:J-?)?\d+", jid):
        jid = job_id(int(re.sub(r"\D", "", jid)))
    for j in _jobs(rd, open_only=False):
        if j.get("job") == jid:
            if j["status"] not in ("queued", "claimed"):
                raise SlopError(f"job {jid} is already {j['status']}")
            return j
    raise SlopError(f"no job {jid} (open jobs: {', '.join(x['job'] for x in _jobs(rd)) or 'none'})")


def _media_size(path: Path) -> str | None:
    """'1280x704' for a picture or a clip (ffprobe, else Pillow for pictures); None when unknown."""
    info = _video_probe(path, "stream=width,height", "csv=s=x:p=0")
    m = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", (info or "").strip())
    if m:
        return m[0]
    try:
        from PIL import Image
        with _open_staged_file(path) as stream, Image.open(stream) as im:
            return f"{im.width}x{im.height}" if im.width and im.height else None
    except Exception:  # noqa: BLE001 - not a picture, or no Pillow
        return None


def _mj_provenance(j: dict, files: list[str], model: str | None) -> None:
    """Which prompt made each saved Midjourney image (and its plate, personalization and model):
    midjourney-prompts.json beside the images, so a picked image can always be traced back."""
    texts, plates = j.get("text") or [], j.get("plates") or []
    name = str(j.get("name") or j.get("job"))
    by_dir: dict[Path, dict] = {}
    for f in files:
        p = Path(f)
        k = None
        m = re.fullmatch(re.escape(name) + r"-(\d+)-\d+", p.stem)
        if m:
            k = int(m.group(1))
        elif plates:
            m2 = re.fullmatch(r"(.+)-\d+", p.stem)
            if m2 and m2.group(1) in plates:
                k = plates.index(m2.group(1)) + 1
        if k is None and len(texts) == 1:
            k = 1
        ok = bool(k) and k <= len(texts)
        if p.parent not in by_dir:
            try:
                with _open_staged_file(p.parent / "midjourney-prompts.json") as stream:
                    existing = json.load(stream)
            except (OSError, ValueError):
                existing = {}
            by_dir[p.parent] = existing if isinstance(existing, dict) else {}
        idx = by_dir[p.parent]
        idx[p.name] = {"job": j["job"], "k": k if ok else None, "prompt": texts[k - 1] if ok else None,
                       "plate": plates[k - 1] if ok and k <= len(plates) else None,
                       "personalize": j.get("personalize"), "model": model, "saved": util.iso_now()}
    for d, idx in by_dir.items():
        _write_staged_file(d / "midjourney-prompts.json",
                           (json.dumps(idx, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    for f in files:                                   # and inside each PNG itself (a copied file keeps its origin)
        e = by_dir.get(Path(f).parent, {}).get(Path(f).name) or {}
        if Path(f).suffix.lower() == ".png" and e.get("prompt"):
            _png_add_text(Path(f), {"Claudia-Prompt": e["prompt"], "Claudia-Job": e["job"],
                                    "Claudia-Plate": e.get("plate") or "", "Claudia-Model": e.get("model") or "",
                                    "Claudia-Personalize": str(e.get("personalize"))})


IMG_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"))


def _real_ext(path: Path) -> str | None:
    try:
        with _open_staged_file(path) as f:
            head = f.read(16)
    except (OSError, ValueError):
        return None
    for magic, ext in IMG_MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":   # a Suno take (WAV on paid plans)
        return ".wav"
    if head[4:8] == b"ftyp":                          # a Grok Imagine clip (or an M4A song)
        return ".m4a" if head[8:12] in (b"M4A ", b"M4B ") else ".mov" if head[8:12] == b"qt  " else ".mp4"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and head[1] & 0x06):
        return ".mp3"                                 # (an MPEG audio frame; ADTS AAC has layer bits 00)
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"OggS":
        return ".ogg"
    return None


def _with_real_ext(path: Path) -> Path:
    """A download saved without its extension (or with the wrong one) gets the one its bytes say."""
    ext = _real_ext(path)
    fine = ({ext, ".jpeg"} if ext == ".jpg" else VIDEO_EXT if ext in (".mp4", ".mov")
            else {".ogg", ".oga", ".opus"} if ext == ".ogg" else {ext})
    if not ext or path.suffix.lower() in fine:
        return path
    dst = path.with_name(path.name + ext) if not path.suffix or len(path.suffix) > 5 else path.with_suffix(ext)
    with _staged_parent(path) as (parent, name):
        n = 2
        while True:
            try:
                os.stat(dst.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                break
            dst = path.with_name(f"{path.stem}-{n}{ext}")
            n += 1
        os.rename(name, dst.name, src_dir_fd=parent, dst_dir_fd=parent)
    return dst


def _png_add_text(path: Path, fields: dict) -> None:
    """tEXt chunks added before IEND, without re-encoding (Midjourney's own metadata stays)."""
    import struct
    import zlib
    try:
        with _open_staged_file(path) as stream:
            data = stream.read()
        i = data.rindex(b"IEND") - 4
        if not data.startswith(b"\x89PNG") or i < 8:
            return
        chunks = b""
        for k, v in fields.items():
            body = k.encode("latin-1") + b"\x00" + str(v).encode("latin-1", "replace")
            chunks += struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body))
        _write_staged_file(path, data[:i] + chunks + data[i:])
    except (OSError, ValueError):
        pass


def _area(size: str) -> int:
    w, h = size.split("x")
    return int(w) * int(h)


def cmd_done(a) -> dict:
    """(Grok) a job's files are saved: mark it done and tell the production (a turn starts when it is idle)."""
    wd, rd = _dirs()
    _require_inactive(rd)
    j = _open_job(rd, a.job)
    files, missing = [], []
    for f in a.file or []:
        hit = _abs_file(f, Path(j.get("save") or wd), wd)
        if hit and j.get("job_kind") in ("midjourney", "grok-imagine", "suno"):
            hit = _with_real_ext(Path(hit))
        (files.append(str(hit)) if hit else missing.append(f))
    if missing:
        raise SlopError(f"--file: no such file: {', '.join(missing)} (save the downloads into {j.get('save')} first)")
    if j.get("job_kind") == "suno":                   # a dead download link saves a sign-in or error page instead
        bad = [f for f in files if _real_ext(Path(f)) not in AUDIO_EXT]
        if bad:
            raise SlopError(f"{', '.join(Path(f).name for f in bad)} is not a song (its bytes are no WAV, MP3, M4A, "
                            "FLAC or Ogg audio; a download link that expired saves a page instead): download the take "
                            "again with Suno's own download, then run done again (nothing was sent)")
    if not files and not (j.get("job_kind") == "browser" and a.note):
        raise SlopError(f"name the saved files: slop --json relay done {j['job']} --file PATH [--file PATH ...]"
                        + (" (a browser job with no files: --note '<what you found or did>')"
                           if j.get("job_kind") == "browser" else ""))
    model = (getattr(a, "model", None) or "").strip() or None
    if j.get("job_kind") == "midjourney" and files:
        _mj_provenance(j, files, model)
    sizes = {f: s for f in files if (s := _media_size(Path(f)))}      # the real pixel size, for the production
    top = max((_area(s) for s in sizes.values()), default=0)
    native = j.get("job_kind") == "grok-imagine" and j.get("route") != "browser"
    small = [] if native else [f for f, s in sizes.items() if _area(s) < 0.8 * top]
    want = RES_SHORT_SIDE.get(j.get("res") or "")
    low = [f for f, s in sizes.items() if want and Path(f).suffix.lower() in VIDEO_EXT
           and min(int(v) for v in s.split("x")) < 0.95 * want]
    text = (f"[grok] {j['job']} done: files " + ", ".join(files) if files else f"[grok] {j['job']} done") \
        + ("\nsizes: " + ", ".join(f"{Path(f).name} {s}" for f, s in sizes.items()) if sizes else "") \
        + (f"\nbelow the job's {j['res']}: " + ", ".join(Path(f).name for f in low) if low else "") \
        + (f"\nmodel: {model}" if model else "") + (f"\n{a.note}" if a.note else "")
    out = deliver(text, "grok", (j["job"], {"status": "done", "files": files, "sizes": sizes or None,
                                             "model": model, "ended": util.iso_now()}))
    out.update(job=j["job"], files=files, sizes=sizes,
               text=f"job {j['job']} done ({len(files)} file(s)"
                    + (f", {', '.join(sorted(set(sizes.values())))}" if sizes else "") + "); " + out["text"])
    if small:
        out["warn"] = (f"{', '.join(Path(f).name for f in small)} came out smaller than the rest: the page may have "
                       "rendered a lower resolution than it showed. Before the next Generate, switch the resolution "
                       "down and back up")
        out["text"] += "\nwarning: " + out["warn"]
    if low:
        w = (f"{', '.join(Path(f).name for f in low)} came out below the job's {j['res']} (the production was told): "
             + (f"every clip, ask your video tool for {j['res']} (it makes 480p unless asked)" if native
                else "the person's X plan may not allow it"))
        out["warn"] = f"{out['warn']}; {w}" if out.get("warn") else w
        out["text"] += "\nwarning: " + w
    return out


def cmd_claim(a) -> dict:
    """Prevent a legacy automated worker from receiving an execution claim."""
    _, rd = _dirs()
    with _lock(rd):
        job = _open_job(rd, a.job)
        if job.get("job_kind") == "person":
            return {"job": job["job"], "status": job["status"], "dispatch_enabled": False,
                    "text": "person job recorded for local review"}
    raise SlopError("external job dispatch is disabled until an isolated execution broker is available")


def cmd_copy(a) -> dict:
    """Copy a locally reviewed person job's text for manual use."""
    import shutil
    _, rd = _dirs()
    j = _open_job(rd, a.job)
    if j.get("job_kind") != "person":
        raise SlopError("external job dispatch is disabled until an isolated execution broker is available")
    texts = j.get("text") or ([j["prompt"]] if j.get("prompt") else []) or ([j["lyrics"]] if j.get("lyrics") else [])
    field = (a.field or "").strip()
    if field:
        if not j.get(field):
            raise SlopError(f"job {j['job']} has no {field}")
        text = str(j[field])
    else:
        if not texts or not 1 <= a.k <= len(texts):
            raise SlopError(f"job {j['job']} has {len(texts)} text(s): --k 1..{len(texts)}")
        text = texts[a.k - 1]
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
        if shutil.which(cmd[0]):
            try:
                env = {k: os.environ[k] for k in ("HOME", "PATH", "XAUTHORITY", "XDG_RUNTIME_DIR",
                                                 "WAYLAND_DISPLAY") if k in os.environ}
                env["DISPLAY"] = os.environ.get("DISPLAY") or ":0"
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=10, check=True, env=env)
                return {"job": j["job"], "copied": True, "chars": len(text),
                        "text": f"on the clipboard ({len(text)} characters): click the prompt box and paste (Ctrl+V)"}
            except (OSError, subprocess.SubprocessError):
                continue
    f = rd / "clipboard.txt"
    util.atomic_write_text(f, text)
    return {"job": j["job"], "copied": False, "file": str(f), "chars": len(text),
            "text": f"no clipboard tool here: open file://{f} in a new browser tab, select all (Ctrl+A), copy "
                    "(Ctrl+C), close the tab, then paste (Ctrl+V) into the prompt box"}


def cmd_cancel(a) -> dict:
    """A job no one needs any more: closed as cancelled, never as failed. From the production: nothing goes back to
    it. From Grok (the person said so): the production hears it."""
    _, rd = _dirs()
    j = _open_job(rd, a.job)
    reason = (a.reason or "").strip()
    rec = {"status": "cancelled", "reason": reason or None, "ended": util.iso_now()}
    if os.environ.get("SLOP_RELAY_TURN"):              # the production's own job: it knows
        with _lock(rd):
            jobs = _read_json(rd / "jobs.json")
            jobs[j["job"]] = rec
            _write_json(rd / "jobs.json", jobs)
        _log(rd, f"job {j['job']} cancelled by the production" + (f": {reason}" if reason else ""))
        return {"job": j["job"], "status": "cancelled", "text": f"job {j['job']} cancelled: Grok will not run it"}
    out = deliver(f"[grok] {j['job']} cancelled" + (f": {reason}" if reason else ""), "grok", (j["job"], rec))
    out.update(job=j["job"], status="cancelled", text=f"job {j['job']} cancelled; " + out["text"])
    return out


def cmd_fail(a) -> dict:
    """(Grok) a job could not be done: mark it failed (or declined) and tell the production."""
    _, rd = _dirs()
    j = _open_job(rd, a.job)
    status = "declined" if a.declined else "failed"
    text = (f"[grok] {j['job']} declined by the service (final for that image on that service): {a.reason}"
            if a.declined else f"[grok] {j['job']} failed: {a.reason}")
    out = deliver(text, "grok", (j["job"], {"status": status, "reason": a.reason, "ended": util.iso_now()}))
    out.update(job=j["job"], status=status, text=f"job {j['job']} {status}; " + out["text"])
    return out


# ================================================================================================== read / status
def _item(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "ts", "kind", "text", "default", "files", "turn", "auto")
            if r.get(k) is not None or k in ("id", "kind", "text", "files")}


def _render_item(r: dict) -> str:
    head = f"#{r['id']} {r['kind']}" + (" (from the toolkit, not the production: tell the person)"
                                        if r.get("source") == "toolkit" else
                                        " (the production's own words; it posted nothing)" if r.get("auto") else "")
    s = f"{head}: {r['text']}"
    if r.get("kind") == "ask" and r.get("default"):
        s += f"\n   (default: {r['default']})"
    for f in r.get("files") or []:
        s += f"\n   file: {f}"
    return s


JOB_FIELDS = ("id", "job", "job_kind", "what", "prompt", "image", "end_image", "refs", "aspect", "seconds", "res",
              "route", "save", "name", "note", "text",
              "expect", "title", "style", "lyrics", "exclude", "instrumental", "url", "personalize", "plates",
              "expect_count",
              "status", "claimed_s", "claim_expired", "stale_h", "ts")


def _job_item(j: dict) -> dict:
    return {k: j.get(k) for k in JOB_FIELDS if j.get(k) not in (None, [], "")}


def _render_job(j: dict) -> str:
    s = f"job {j['job']} ({j.get('job_kind')}{', ' + j['what'] if j.get('what') else ''}): {j.get('status')}"
    for k in ("prompt", "image", "end_image", "refs", "aspect", "seconds", "res", "route", "url", "title", "style",
              "exclude", "instrumental", "plates", "save", "name", "note"):
        if j.get(k) not in (None, "", [], False):
            s += f"\n   {k}: {', '.join(j[k]) if isinstance(j[k], list) else j[k]}"
    if j.get("lyrics"):
        s += "\n   lyrics:\n" + str(j["lyrics"])
    if j.get("job_kind") == "midjourney":
        s += f"\n   personalization (the P button): {'on' if j.get('personalize', True) else 'off'}"
    for i, block in enumerate(j.get("text") or [], 1):
        s += f"\n   copy block {i}:\n{block}"
    for e in j.get("expect") or []:
        s += f"\n   expect: {e}"
    return s


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
MJ_PROMPT_MAX = 800                     # characters before the parameters (docs/50 §3: longer prompts get shortened)
# Grok's own Grok Imagine tool (docs.x.ai/grok-bot/files-and-results, 2026-11): stills in five shapes; clips 1-15 s,
# 480p unless 720p is asked (720p is its most), from a first frame, first + last frames or up to 14 references, all
# the images of one clip about 35 MB. grok.com in Grok's browser still renders 1080p: `--res 1080p` goes there.
IMAGINE_TYPES = {".jpg", ".png", ".webp"}
IMAGINE_STILL_ASPECTS = ("1:1", "4:3", "3:4", "16:9", "9:16")
IMAGINE_CLIP_ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
IMAGINE_SECONDS = (1, 15)
IMAGINE_MAX_REFS = 14
IMAGINE_MAX_IMAGES_B = 35 * 1024 * 1024
IMAGINE_DEFAULT_RES = "720p"
RES_SHORT_SIDE = {"480p": 480, "720p": 720, "1080p": 1080}
DRIVE_MAX_B = 95 * 1024 * 1024          # Drive takes uploads up to 100 MB: a bigger video never reaches the person


def _size(f: str) -> int:
    try:
        with _open_staged_file(Path(f)) as stream:
            return os.fstat(stream.fileno()).st_size
    except (OSError, ValueError):
        return 0


def _snapshot(rd: Path) -> tuple:
    st = _read_json(rd / "state.json")
    outbox = _rows(rd / "outbox.jsonl")
    prod = production(rd)
    q = len(_jobs(rd))
    new = [r for r in outbox if int(r.get("id") or 0) > int(st.get("read") or 0)]
    sig = (prod.get("state"), q, len(outbox))
    return new, prod, q, sig


def cmd_read(a) -> dict:
    _, rd = _dirs()
    wait = max(0.0, min(float(a.wait or 0), WAIT_CAP_S))
    t0 = time.monotonic()
    calm = 0
    while True:
        # the wait holds its full time unless there is something for Grok: a new item, an open job, or a production
        # that stopped working (waiting, idle, failed) and stays stopped - a blip in its state never ends a wait
        new, prod, q, _sig = _snapshot(rd)
        left = wait - (time.monotonic() - t0)
        if not new and not q and left > 0:
            calm = calm + 1 if prod.get("state") != "working" else 0
            if calm < READ_SETTLE:
                time.sleep(0.4)
                continue
        with _lock(rd):                               # take the new items and mark them read in one step
            st = _read_json(rd / "state.json")
            rows = _rows(rd / "outbox.jsonl")
            new = [r for r in rows if int(r.get("id") or 0) > int(st.get("read") or 0)]
            if new:
                st["read"] = max(int(r["id"]) for r in new)
                _write_json(rd / "state.json", st)
        if new or _jobs(rd) or wait - (time.monotonic() - t0) <= 0 or production(rd).get("state") != "working":
            break
        calm = 0                                      # a misread, nothing new after all: keep waiting
    shown = [r for r in (rows if a.all else new) if r.get("kind") in ("say", "ask")]
    items = [_item(r) for r in shown]
    prod = production(rd)
    jobs = [_job_item(j) for j in _jobs(rd)]
    nxt = "Review the relay queue locally. Automatic job dispatch is disabled."
    lines = [_render_item(r) for r in shown] or ["(no new messages from the production)"]
    lines += [_render_job(j) for j in jobs]
    lines.append(f"production: {prod.get('summary')}")
    if prod.get("state") == "failed" and prod.get("log_tail"):
        lines.append("  log tail:\n" + "\n".join("    " + ln for ln in prod["log_tail"].splitlines()))
    lines.append(f"grok_next: {nxt}")
    return {"items": items, "count": len(items), "new": len(new), "jobs": jobs, "production": prod,
            "waited_s": round(time.monotonic() - t0, 2), "grok_next": nxt, "dispatch_enabled": False,
            "text": "\n".join(lines)}


def cmd_status(a) -> dict:
    wd, rd = _dirs()
    cfg = _cfg(rd, need=False)
    st = _read_json(rd / "state.json")
    prod = production(rd)
    inbox, outbox = _rows(rd / "inbox.jsonl"), _rows(rd / "outbox.jsonl")
    unread = sum(1 for r in outbox if int(r.get("id") or 0) > int(st.get("read") or 0))
    jobs = _jobs(rd)
    eng = cfg.get("engine")
    sess = (st.get("sessions") or {}).get(eng) if eng else None
    lines = [f"relay: production {prod.get('summary')}",
             f"  engine: {eng or 'not set up'}" + (f" ({cfg.get('model')})" if cfg.get("model") else "")
             + (f", session {sess}" if sess else "") + f"; working in {wd}",
             f"  inbox: {len(inbox)} message(s), {prod.get('pending', 0)} not yet read by the production",
             f"  outbox: {len(outbox)} item(s), {unread} not yet shown",
             f"  turns: {st.get('turns', 0)}; open jobs: {', '.join(j['job'] for j in jobs) or 'none'}"]
    eff = effort_for(eng, rd) if eng in ENGINES else (None, "")
    if eng in ENGINES:
        lines.insert(2, f"  effort: {eff[0] or 'the engine default'} ({eff[1]}); `slop relay effort` changes it")
    lines.insert(3, "  wake-ups: disabled")
    return {"production": prod, "engine": eng, "model": cfg.get("model"), "effort": eff[0], "session": sess,
            "turns": st.get("turns", 0), "production_enabled": False, "dispatch_enabled": False,
            "inbox": len(inbox), "outbox": len(outbox), "unread": unread, "jobs": [_job_item(j) for j in jobs],
            "work_dir": str(wd), "relay_dir": str(rd), "text": "\n".join(lines)}


def cmd_effort(a) -> dict:
    """Inspect or save a local engine effort preference."""
    from .agents import engines as E
    wd, rd = _dirs()
    cfg = _cfg(rd, need=False)
    eng = E.norm_engine(a.engine) if a.engine else (cfg.get("engine") if cfg.get("engine") in ENGINES else "claude")
    if eng not in ENGINES:
        raise SlopError(f"--engine claude or codex (the production's engines), not {a.engine}")
    levels, src = effort_levels(eng)
    changed = None
    if a.level:
        lv = a.level.strip().lower()
        if lv not in ("default", "auto"):
            if not re.fullmatch(r"[a-z]{2,16}", lv):
                raise SlopError(f"an effort level is one word ({', '.join(levels)}), or default, or auto")
        with _lock(rd):
            data = _read_json(rd / "effort.json")
            if lv == "default":
                data.pop(eng, None)
            else:
                data[eng] = lv
            for k in ("rejected", "rejected_why"):
                (data.get(k) or {}).pop(eng, None)
            _write_json(rd / "effort.json", data)
        changed = lv
        _log(rd, f"effort: {eng} set to {lv}")
    data = _read_json(rd / "effort.json")
    engines = {}
    lines = []
    for e in ENGINES:
        cur, where = effort_for(e, rd)
        engines[e] = {"effort": cur or "auto", "source": where}
        title = E.SPEC[e]["title"]
        lines.append(f"{title} effort: {cur or 'the engine decides (auto)'} ({where})"
                     + (" - preferred engine" if e == cfg.get("engine") else ""))
        why = (data.get("rejected_why") or {}).get(e)
        if why:
            lines.append(f"  {(data.get('rejected') or {}).get(e)} was turned down: {why[:160]}")
        if os.environ.get(EFFORT_ENV[e]):
            lines.append(f"  ${EFFORT_ENV[e]} sets it; `slop relay effort` changes nothing while it is set")
    lines.append(f"levels for {E.SPEC[eng]['title']}: {', '.join(levels)} ({src}); higher = better work, slower, more "
                 "of the plan's usage; default = xhigh; auto = the engine decides")
    if changed:
        lines.insert(0, f"saved local effort preference for {E.SPEC[eng]['title']}: {changed}")
    return {"engine": eng, "effort": engines[eng]["effort"], "levels": levels, "levels_from": src,
            "default": EFFORT_DEFAULT, "engines": engines, "changed": changed, "text": "\n".join(lines)}


def cmd_log(a) -> dict:
    _, rd = _dirs()
    tail = _tail(rd / "relay.log", max(1, int(a.lines)))
    tj = _read_json(rd / "turn.json")
    err = _tail(rd / "turns" / f"turn-{int(tj.get('n') or 0):04d}.err", 15) if tj.get("n") else ""
    text = tail or f"(the log {rd / 'relay.log'} is empty)"
    if err.strip():
        text += f"\n--- stderr of turn {tj.get('n')} ---\n{err}"
    return {"log": str(rd / "relay.log"), "tail": tail, "stderr_tail": err, "text": text}


def cmd_stop(a) -> dict:
    _, rd = _dirs()
    r = _runner(rd)
    if not r["alive"]:
        return {"stopped": False, "text": "the production is not working; nothing to stop"}
    util.atomic_write_text(rd / "stop", util.iso_now() + "\n")
    from . import job
    jid = r.get("job_id")
    if jid:
        try:
            path, rec = job.find_job(jid, None)
            job.stop_job(path, rec, grace=float(a.grace))
        except SlopError:
            jid = None
    if not jid and r.get("pid"):
        import signal
        job._signal_group({"pid": r.get("pid"), "pid_start": r.get("pid_start")},
                          signal.SIGTERM if os.name != "nt" else 0)
    with _lock(rd):
        _write_json(rd / "runner.json", {"state": "stopped", "since": util.iso_now(), "since_ts": time.time(),
                                         "job_id": r.get("job_id")})
        tj = _read_json(rd / "turn.json")
        if tj.get("state") == "running":
            tj.update(state="stopped", ended=util.iso_now())
            _write_json(rd / "turn.json", tj)
    _log(rd, "stopped by `relay stop`")
    return {"stopped": True, "job": r.get("job_id"),
            "text": "the production is stopped. Use `relay inbox` to inspect pending messages."}


def effort_levels(eng: str) -> tuple[list[str], str]:
    """Return known effort levels without executing a vendor CLI."""
    return list(EFFORT_USUAL.get(eng, ())), "the usual levels"


def effort_for(eng: str, rd: Path) -> tuple[str | None, str]:
    """(the level a turn passes, or None for the engine's own default; where it came from):
    $SLOP_CLAUDE_EFFORT / $SLOP_CODEX_EFFORT, else the person's `slop relay effort`, else xhigh. A level the engine
    turned down (recorded by a turn) is not passed again until a new level is set."""
    env = os.environ.get(EFFORT_ENV.get(eng, ""), "").strip().lower()
    if env:
        return (None if env == "auto" else env), EFFORT_ENV[eng]
    data = _read_json(rd / "effort.json")
    lv = str(data.get(eng) or "").strip().lower() or EFFORT_DEFAULT
    if lv == "auto":
        return None, "set to auto"
    if (data.get("rejected") or {}).get(eng) == lv:
        return None, f"{lv} was turned down by the engine"
    return lv, ("slop relay effort" if data.get(eng) else "default")


def cmd_wake(a) -> dict:
    """Inspect or clear an old wake configuration without contacting its destination."""
    arg = (a.url or "").strip().lower()
    if arg not in ("", "off"):
        raise SlopError("relay wake requests require an authenticated broker and are disabled")
    _, rd = _dirs()
    with _lock(rd):
        cfg = _read_json(rd / "wake.json")
        if arg == "off" and cfg.get("url"):
            cfg.pop("url", None)
            _write_json(rd / "wake.json", cfg)
    return {"on": False, "legacy_configured": bool(cfg.get("url")), "text": "external wake requests are disabled"}


def cmd_run(a) -> dict:
    """Reject supervisors launched by an older job record."""
    raise SlopError(PRODUCTION_BLOCKED)


# ================================================================================================== register
def register(sub) -> None:
    g = sub.add_parser("relay", help="local queue for messages, replies and job records",
                       description="Review the queue locally. Autonomous production and external dispatch require an "
                                   "isolated execution broker.")
    rs = g.add_subparsers(dest="relay_cmd", metavar="ACTION", required=True)
    mine = [g]

    def add(name: str, **kw) -> argparse.ArgumentParser:
        sp = rs.add_parser(name, **kw)
        util.add_common_flags(sp)
        mine.append(sp)
        return sp

    p = add("setup", help="initialize the local relay queue")
    p.add_argument("--engine", help="preferred engine metadata (claude or codex)")
    p.add_argument("--model", help="preferred model metadata")
    p.add_argument("--no-skill", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_setup)

    p = add("send", allow_abbrev=False, help="save literal message text locally")
    p.add_argument("--attach", action="append", metavar="PATH",
                   help="a regular file already staged under the projects directory")
    p.add_argument("--from", dest="sender", choices=["director", "grok"], default="director",
                   help="sender attribution for the local record")
    p.add_argument("text", nargs="+", metavar="MESSAGE", help="literal text; put -- before the message")
    p.set_defaults(func=cmd_send)

    p = add("read", help="read local replies, attachments and job metadata")
    p.add_argument("--wait", type=float, default=0, metavar="S",
                   help=f"wait up to S seconds (cap {WAIT_CAP_S:g}) while the production works; returns on anything "
                        "new")
    p.add_argument("--all", action="store_true", help="show every message, not only the new ones")
    p.set_defaults(func=cmd_read)

    p = add("done", help="record a locally reviewed job result and staged files")
    p.add_argument("job", help="the job id, e.g. J-0003")
    p.add_argument("--file", action="append", help="a saved file (repeat per file)")
    p.add_argument("--note", help="one line for the production")
    p.add_argument("--model", help="the model or version the page used (Midjourney's version, Suno's model), recorded "
                                   "with the files")
    p.set_defaults(func=cmd_done)

    p = add("copy", help="copy text from a manual person job")
    p.add_argument("job", help="the job id, e.g. J-0003")
    p.add_argument("--k", type=int, default=1, help="which prompt of the job (Midjourney: 1 = the first --text)")
    p.add_argument("--field", help="another field instead: style, lyrics, title, exclude, prompt")
    p.set_defaults(func=cmd_copy)

    p = add("claim", help="inspect a manual person job; external execution claims are disabled")
    p.add_argument("job", help="the job id, e.g. J-0003")
    p.add_argument("--stale-ok", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--token", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_claim)

    p = add("cancel", help="close a job no one needs any more (cancelled, not failed): the production's own, or "
                           "Grok's when the person says so")
    p.add_argument("job", help="the job id, e.g. J-0003")
    p.add_argument("--reason", help="why, in a few words")
    p.set_defaults(func=cmd_cancel)

    p = add("fail", help="(Grok) a job could not be done: mark it failed (--declined: the service refused it)")
    p.add_argument("job", help="the job id, e.g. J-0003")
    p.add_argument("--reason", required=True, help="what happened, verbatim")
    p.add_argument("--declined", action="store_true", help="the service declined it (final for that image there)")
    p.set_defaults(func=cmd_fail)

    p = add("say", help="(the production) post a message for the director, with files to show")
    p.add_argument("text", nargs="+", help="the message")
    p.add_argument("--show", nargs="+", metavar="FILE",
                   help="files Grok shows with it (contact sheets, stills, proofs)")
    p.add_argument("--as-is", action="store_true", help="post a video even if phones will play it badly")
    p.set_defaults(func=cmd_say)

    p = add("ask", help="(the production) ask the director one question, with its default")
    p.add_argument("text", nargs="+", help="the question")
    p.add_argument("--default", default=None, help="the answer when they just say go")
    p.add_argument("--show", nargs="+", metavar="FILE", help="files Grok shows with it")
    p.add_argument("--as-is", action="store_true", help="post a video even if phones will play it badly")
    p.set_defaults(func=cmd_ask)

    p = add("job", help="record a proposed job for local review")
    p.add_argument("--kind", choices=["grok-imagine", "midjourney", "suno", "browser", "person"], required=True)
    p.add_argument("--what", help="grok-imagine: still | clip (others: a short label, e.g. plates, song)")
    p.add_argument("--prompt", help="grok-imagine: the prompt, verbatim")
    p.add_argument("--prompt-file", help="grok-imagine: read the prompt from this file")
    p.add_argument("--image", help="grok-imagine: a clip's first frame (JPEG, PNG or WebP; must exist)")
    p.add_argument("--end-image", help="grok-imagine clip: its last frame (the same file as --image makes a loop)")
    p.add_argument("--ref", action="append",
                   help="grok-imagine: a reference image for how a person, an outfit or a place looks (repeat, up to "
                        f"{IMAGINE_MAX_REFS})")
    p.add_argument("--aspect", choices=list(IMAGINE_CLIP_ASPECTS),
                   help="grok-imagine: stills 1:1, 4:3, 3:4, 16:9 or 9:16; clips also 3:2 and 2:3 (default: the "
                        "first frame's shape)")
    p.add_argument("--seconds", type=int, help=f"grok-imagine clip length, {IMAGINE_SECONDS[0]}-{IMAGINE_SECONDS[1]} "
                                               "s (default 6)")
    p.add_argument("--res", choices=["480p", "720p", "1080p"],
                   help=f"grok-imagine clip resolution (default {IMAGINE_DEFAULT_RES}, the most Grok's own tool makes; "
                        "1080p: Grok uses grok.com in its browser, on the person's X plan)")
    p.add_argument("--save", help="where the downloads go (default: the project's inbox/<kind>/)")
    p.add_argument("--name", help="file stem for the downloads (default: the job id)")
    p.add_argument("--note", help="one line for Grok or the person")
    p.add_argument("--text", action="append", help="midjourney: one full prompt per --text (each is its own "
                                                   "Midjourney job); browser: text to paste; person: a copy block")
    p.add_argument("--title", help="suno: the song title")
    p.add_argument("--style", help="suno: the Style of Music field, verbatim")
    p.add_argument("--lyrics", help="suno: the lyrics field, verbatim")
    p.add_argument("--lyrics-file", help="suno: read the lyrics field from this file")
    p.add_argument("--exclude", help="suno: the Exclude styles field")
    p.add_argument("--instrumental", action="store_true", help="suno: an instrumental (no lyrics)")
    p.add_argument("--url", help="browser: the page to start on")
    p.add_argument("--plate", action="append", help="midjourney: the plate each --text is for (one per --text, in "
                                                    "order); Grok names the downloads PLATE-N.png for plates ingest")
    p.add_argument("--long", action="store_true", help="midjourney: send a prompt over 800 characters anyway")
    p.add_argument("--personalize", choices=["on", "off"],
                   help="midjourney: Grok switches the person's personalization on with the P button (default: the "
                        "project's mj.personalize, else on); a profile code never goes into a prompt")
    p.add_argument("--expect", action="append", help="person: where their upload is saved (repeatable)")
    p.set_defaults(func=cmd_job)

    p = add("inbox", help="(the production) new messages from the director and Grok, marked read")
    p.set_defaults(func=cmd_inbox)

    p = add("status", help="the production's state, engine, session, message counts and open jobs")
    p.set_defaults(func=cmd_status)

    p = add("wake", help="inspect or clear a previous wake configuration")
    p.add_argument("url", nargs="?", help="use off to clear an old configuration")
    p.set_defaults(func=cmd_wake)

    p = add("effort", help="inspect or save a local engine effort preference")
    p.add_argument("level", nargs="?", help="a level the engine offers (shown without one), default (xhigh) or auto "
                                            "(the engine decides)")
    p.add_argument("--engine", help="claude | codex (default: the engine the relay runs)")
    p.set_defaults(func=cmd_effort)

    p = add("stop", help="stop the running production turn (waiting messages stay)")
    p.add_argument("--grace", type=float, default=10.0, help="seconds between SIGTERM and SIGKILL (default 10)")
    p.set_defaults(func=cmd_stop)

    p = add("log", help="the tail of the relay log (and the last turn's stderr)")
    p.add_argument("--lines", type=int, default=40)
    p.set_defaults(func=cmd_log)

    p = add("_run")                         # internal: the supervisor job (no help = not in the command list)
    p.set_defaults(func=cmd_run)
    for q in mine:
        util.json_usage_errors(q)
