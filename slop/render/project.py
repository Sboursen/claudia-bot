"""What the engine page needs from a project: its URL, the data files to load, fallbacks, window.PROJECT, duration —
and the input fingerprint (<frames dir>/.inputs.json) that tells fresh frames from stale ones."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import wave
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import url2pathname

from ..config import SlopError, engine_dir
from .isolation import ENGINE_URL, PROJECT_URL, project_asset_url, read_project_file, scoped_path

# data scripts the page loads from <project>/engine/ (or <project>/engine/assets/), in this order
DATA_NAMES = ["project", "beats", "lyrics", "hits", "spectrum", "clips", "plates", "tracks", "fx", "shots", "edl"]
# window global each data script sets (for reading them back in Python)
DATA_GLOBALS = {"project": "PROJECT", "beats": "BEATS", "lyrics": "LYR", "hits": "HITS", "spectrum": "SPECTRUM",
                "clips": "CLIPS", "plates": "PLATES", "tracks": "TRACK", "fx": "FXSEQ", "shots": "SHOTS", "edl": "EDL"}
LAYER_FILES = ["common.js", "scenes.js", "ch/_index.js"]
# the project.json keys the engine reads (window.PROJECT)
PROJECT_KEYS = ["name", "title", "fps", "size", "look", "chrome", "lead", "looks", "depth_model", "preload", "font_files",
                "bpm", "duration", "board", "counter", "corner", "departures", "cache_mib"]


def file_url(p: Path) -> str:
    return p.resolve().as_uri()


def dir_url(p: Path) -> str:
    u = file_url(p)
    return u if u.endswith("/") else u + "/"


def read_js_asset(path: Path):
    """Parse an engine data script `window.X = <json>;` back into Python (None if it is not plain JSON)."""
    try:
        s = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"window\.[A-Za-z_$][\w$]*\s*=\s*", s) or re.search(r"=\s*", s)
    if not m:
        return None
    body = s[m.end():].strip().rstrip(";").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_project_json(pdir: Path, rel: str):
    raw = read_project_file(pdir, tuple(rel.split("/")))
    if raw is not None:
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return None


def _project_json(pdir: Path) -> dict:
    data = _read_project_json(pdir, "project.json")
    if not isinstance(data, dict):
        raise SlopError("the render project requires a regular project.json file containing a JSON object")
    return data


def asset_files(pdir: Path) -> dict[str, Path]:
    """Existing engine data scripts: name -> path (<project>/engine/<n>.js preferred over engine/assets/<n>.js)."""
    pdir = pdir.resolve()
    out: dict[str, Path] = {}
    for n in DATA_NAMES:
        for cand in (pdir / "engine" / f"{n}.js", pdir / "engine" / "assets" / f"{n}.js"):
            if scoped_path(pdir, cand.relative_to(pdir).parts) is not None and cand.is_file():
                out[n] = cand
                break
    return out


def layer_files(pdir: Path) -> list[str]:
    pdir = pdir.resolve()
    return [f for f in LAYER_FILES if scoped_path(pdir, ("engine", *f.split("/"))) is not None
            and (pdir / "engine" / f).is_file()]


def device_files(shots: bool = True) -> list[str]:
    """Device files the page loads before src/shots.js: src/devices/index.js when present (it loads the library in its
    own order); else none when src/shots.js is used (it loads its own library); else every src/devices/**/*.js
    (sorted, _base.js first) so chapter code can still use them."""
    d = engine_dir() / "src" / "devices"
    if not d.is_dir():
        return []
    if (d / "index.js").is_file():
        return ["src/devices/index.js"]
    if shots and (engine_dir() / "src" / "shots.js").is_file():
        return []
    files = sorted(p.relative_to(engine_dir()).as_posix() for p in d.rglob("*.js"))
    return sorted(files, key=lambda f: (not f.endswith("/_base.js"), f))


def _timed(w: dict) -> bool:
    try:
        return w.get("t0") is not None and math.isfinite(float(w["t0"]))
    except (TypeError, ValueError):
        return False


def untimed_words(words: dict) -> list[dict]:
    """words.json entries with no usable t0 (alignment failed): the lyric gate reports each one as missing."""
    out = []
    for k, w in enumerate((words or {}).get("words", []) or []):
        if isinstance(w, dict) and not _timed(w):
            out.append({"line": int(w.get("line") or 0), "i": int(w["i"]) if isinstance(w.get("i"), int) else k,
                        "w": str(w.get("w", ""))})
    return out


def lyr_from_words(words: dict) -> list[dict]:
    """analysis/words.json -> engine LYR, one token per sung word (no display.txt mapping: that is `slop edit export`).
    Words without a t0 cannot be drawn at their time: they are left out here and reported by untimed_words()."""
    ws = [w for w in (words or {}).get("words", []) if isinstance(w, dict) and _timed(w)]
    lines_meta = {ln.get("line"): ln for ln in (words or {}).get("lines", [])}
    by_line: dict[int, list[dict]] = {}
    for w in ws:
        by_line.setdefault(int(w.get("line", 0)), []).append(w)
    out = []
    for li in sorted(by_line):
        toks = sorted(by_line[li], key=lambda w: float(w["t0"]))
        meta = lines_meta.get(li, {})
        tokens = [{"txt": str(w.get("w", "")), "t0": round(float(w["t0"]), 3),
                   "t1": round(float(w.get("t1") if w.get("t1") is not None else float(w["t0"]) + .2), 3), "st": "", "n": 1}
                  for w in toks]
        out.append({"line": li, "kind": "lyric", "text": meta.get("text") or " ".join(t["txt"] for t in tokens),
                    "t0": tokens[0]["t0"], "t1": tokens[-1]["t1"], "tokens": tokens})
    return out


def plates_from_picks(pdir: Path, picks: dict) -> dict:
    """plates/picks.json {plate: {file, …}} -> PLATES {plate: {file: <file URL>, boxes?, subject_depth?, depth?}}."""
    out = {}
    for name, v in (picks or {}).items():
        if not isinstance(v, dict) or not v.get("file"):
            continue
        meta = {k: v[k] for k in ("boxes", "subject_depth", "depth_model", "w", "h") if k in v}
        f = (pdir / v["file"]) if not Path(v["file"]).is_absolute() else Path(v["file"])
        meta["file"] = file_url(f)
        if isinstance(v.get("depth"), str):
            meta["depth"] = file_url(pdir / v["depth"])
        elif v.get("depth") is False:
            meta["depth"] = False
        if isinstance(v.get("matte"), str):
            meta["matte"] = file_url(pdir / v["matte"])
        out[name] = meta
    return out


def fallback_data(pdir: Path, have: set[str]) -> tuple[dict, list[str]]:
    """Contract JSON for every engine data script that is missing (window globals) + warnings."""
    data: dict = {}
    warn: list[str] = []
    pairs = [("beats", "BEATS", "analysis/beats.json"), ("hits", "HITS", "analysis/hits.json"),
             ("shots", "SHOTS", "board/shots.json"), ("edl", "EDL", "edit/edl.json")]
    for name, glob, rel in pairs:
        if name in have:
            continue
        v = _read_project_json(pdir, rel)
        if v is not None:
            data[glob] = v
    if "lyrics" not in have:
        words = _read_project_json(pdir, "analysis/words.json")
        if words:
            data["LYR"] = lyr_from_words(words)
            warn.append("engine/lyrics.js is missing: lyrics come from analysis/words.json one word per token "
                        "(run `slop edit export` to apply lyrics/display.txt)")
            un = untimed_words(words)
            if un:
                warn.append(f"{len(un)} word(s) in analysis/words.json have no t0 (alignment failed): "
                            + ", ".join(f"L{u['line']:02d} '{u['w']}'" for u in un[:6])
                            + " — ear-check and fix analysis/words.json (the lyric gate counts them as missing)")
        else:
            warn.append("no lyrics: neither engine/lyrics.js nor analysis/words.json exists (the lyric gate checks nothing)")
    if "plates" not in have:
        picks = _read_project_json(pdir, "plates/picks.json")
        if picks:
            data["PLATES"] = plates_from_picks(pdir, picks)
    if "beats" not in have and "BEATS" not in data:
        warn.append("no beat map (engine/beats.js or analysis/beats.json): the clock falls back to a fixed tempo — run `slop analyze`")
    return data, warn


def project_subset(pdir: Path) -> dict:
    proj = _project_json(pdir)
    out = {k: proj[k] for k in PROJECT_KEYS if k in proj}
    out.setdefault("name", pdir.name)
    out.setdefault("title", proj.get("title") or pdir.name)
    out.setdefault("look", {})
    if isinstance(out["look"], dict):
        out["look"].setdefault("mode", "native")
    return out


def project_size(pdir: Path) -> tuple[int, int]:
    size = _project_json(pdir).get("size") or [1920, 1080]
    try:
        return int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return 1920, 1080


def project_fps(pdir: Path) -> float:
    try:
        return float(_project_json(pdir).get("fps") or 24)
    except (TypeError, ValueError):
        return 24.0


RENDER_AUDIO_OPTIONS = ("-protocol_whitelist", "file", "-format_whitelist", "aac,aiff,flac,mov,mp3,ogg,wav")


def audio_path(pdir: Path) -> Path:
    root = pdir.resolve()
    song = _project_json(root).get("song") or "audio/song.wav"
    if not isinstance(song, str):
        raise SlopError("project.json song must be a path inside the project")
    path = Path(song)
    try:
        parts = path.relative_to(root).parts if path.is_absolute() else path.parts
    except ValueError:
        parts = ()
    scoped = scoped_path(root, parts)
    if scoped is None or (scoped.exists() and not scoped.is_file()):
        raise SlopError("render audio must be a regular file inside the project. Add it with `slop song ingest`.")
    return scoped


def audio_duration(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, OSError):
        pass
    try:
        r = subprocess.run(["ffprobe", "-v", "error", *RENDER_AUDIO_OPTIONS, "-show_entries", "format=duration",
                            "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001 — probing is best effort
        return None


def project_duration(pdir: Path) -> float | None:
    """Song duration from the beat map, the storyboard, or the audio (None when nothing says)."""
    pdir = pdir.resolve()
    have = asset_files(pdir)
    for name, rel in (("beats", "analysis/beats.json"), ("shots", "board/shots.json")):
        v = _declarative_export(pdir, have[name], DATA_GLOBALS[name]) if name in have else None
        if v is None:
            v = _read_project_json(pdir, rel)
        if isinstance(v, dict) and v.get("duration"):
            try:
                return float(v["duration"])
            except (TypeError, ValueError):
                pass
    return audio_duration(audio_path(pdir))


def timemap_end(pdir: Path) -> float | None:
    """The final song's length from edit/timemap.json (the last segment's new_t1), or None."""
    for rel in ("edit/timemap.json", "timemap.json"):
        v = _read_project_json(pdir, rel)
        if isinstance(v, dict) and v.get("segments"):
            try:
                return max(float(s[1]) for s in v["segments"])
            except (TypeError, ValueError, IndexError):
                return None
    return None


def render_duration(pdir: Path, timemap: bool = False) -> float | None:
    """How long the film is: with --timemap the FINAL song (time-map end, else the audio), else project_duration()."""
    if timemap:
        return timemap_end(pdir) or audio_duration(audio_path(pdir)) or project_duration(pdir)
    return project_duration(pdir)


# ------------------------------------------------------------ input fingerprint (stale-frame detection) ------------
FINGERPRINT_VERSION = 2
_ENGINE_CODE_GLOBS = ["index.html", "src/**/*.js", "fonts/fonts.js"]


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()[:16]


def _file_sha(f: Path) -> str | None:
    try:
        return _sha(f.read_bytes())
    except OSError:
        return None


def _stat_sig(f: Path) -> str | None:
    try:
        st = f.stat()
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}"


def url_path(u, base: Path) -> Path | None:
    """A media path as the page sees it (file URL, absolute, or relative to `base`) -> a local path (None: http/data)."""
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname
    if not isinstance(u, str) or not u or re.match(r"^(https?|data|clip):", u, re.I):
        return None
    if u.lower().startswith("file:"):
        return Path(url2pathname(unquote(urlparse(u).path)))
    p = Path(u)
    return p if p.is_absolute() else (base / p)


def engine_code_hash() -> str:
    """One hash over the engine's code (index.html, src/**/*.js including devices and shots.js, fonts/fonts.js)."""
    root = engine_dir()
    parts = []
    for g in _ENGINE_CODE_GLOBS:
        for f in sorted(root.glob(g)):
            if f.is_file():
                parts.append(f"{f.relative_to(root).as_posix()}={_file_sha(f)}")
    return _sha("\n".join(parts).encode())


def inputs_fingerprint(pdir: Path, spec: dict, timemap: bool = False) -> dict:
    """What a frame depends on, hashed per part: the engine code, project.json (the subset the page reads), every
    project data/code file the page can load (<project>/engine/**/*.js|json), each injected fallback JSON, the media
    (size + mtime of every other file under <project>/engine/ and of the plate files PLATES / picks.json point at
    outside it), the size/fps, the renderer (src/shots.js or the stub) and the time map. Frames drawn under one
    fingerprint are stale under any other."""
    parts: dict[str, str | None] = {"engine": engine_code_hash()}
    sub = {k: v for k, v in project_subset(pdir).items() if k != "cache_mib"}      # memory, not pixels
    parts["project.json"] = _sha(json.dumps(sub, sort_keys=True, default=str).encode())
    parts["size"] = f"{spec['w']}x{spec['h']}@{spec['fps']}"
    parts["renderer"] = "shots" if spec.get("shots", True) else "stub"
    parts["project scripts"] = "trusted" if spec.get("trust_project_scripts") else "declarative"
    for glob, val in sorted((spec.get("browser_fallbacks") or {}).items()):
        parts[f"fallback:{glob}"] = _sha(json.dumps(val, sort_keys=True, default=str).encode())
    pdir = pdir.resolve()
    eng = pdir / "engine"
    media: list[str] = []
    if eng.is_dir():
        for f in sorted(eng.rglob("*")):
            if scoped_path(pdir, f.relative_to(pdir).parts) is None or not f.is_file():
                continue
            rel = f.relative_to(pdir).as_posix()
            if f.suffix.lower() in (".js", ".mjs", ".json"):
                parts[f"data:{rel}"] = _file_sha(f)
            else:
                media.append(f"{rel}={_stat_sig(f)}")
    refs: set[Path] = set()
    have = asset_files(pdir)
    plates = read_js_asset(have["plates"]) if "plates" in have else (spec.get("data") or {}).get("PLATES")
    for v in (plates.values() if isinstance(plates, dict) else []):
        if isinstance(v, dict):
            for k in ("file", "src", "depth", "matte"):
                q = url_path(v.get(k), eng)
                if q is not None:
                    refs.add(q)
    picks = _read_json(pdir / "plates" / "picks.json")
    for v in (picks.values() if isinstance(picks, dict) else []):
        if isinstance(v, dict):
            for k in ("file", "depth", "matte"):
                if isinstance(v.get(k), str):
                    refs.add(Path(v[k]) if Path(v[k]).is_absolute() else pdir / v[k])
    eng_r = eng.resolve()
    for q in sorted(refs, key=str):
        qr = q.resolve()
        if pdir in qr.parents and eng_r not in qr.parents:
            media.append(f"{qr.as_posix()}={_stat_sig(qr)}")
    parts["media"] = _sha("\n".join(media).encode())
    if timemap:
        tm = next((pdir / r for r in ("edit/timemap.json", "timemap.json") if (pdir / r).is_file()), None)
        parts["timemap"] = _file_sha(tm) if tm else "missing"
    return {"version": FINGERPRINT_VERSION, "hash": _sha(json.dumps(parts, sort_keys=True).encode()), "parts": parts,
            "size": [spec["w"], spec["h"]], "fps": spec["fps"], "timemap": bool(timemap), "renderer": parts["renderer"],
            "shots": bool(spec.get("shots", True))}


def fingerprint_changes(old: dict | None, new: dict) -> list[str]:
    """The parts that differ (e.g. 'data:engine/assets/lyrics.js', 'fallback:SHOTS', 'engine', 'media')."""
    if not isinstance(old, dict) or not isinstance(old.get("parts"), dict):
        return ["no record of what they were drawn from (.inputs.json missing)"]
    a, b = old["parts"], new["parts"]
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k)) or (["version"] if old.get("hash") != new.get("hash") else [])


def read_fingerprint(frames: Path) -> dict | None:
    v = _read_json(Path(frames) / ".inputs.json")
    return v if isinstance(v, dict) and v.get("hash") else None


def write_fingerprint(frames: Path, fp: dict) -> None:
    f = Path(frames) / ".inputs.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(".inputs.json.tmp")
    tmp.write_text(json.dumps(fp, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, f)


def current_fingerprint(pdir: Path, rec: dict) -> dict:
    """The fingerprint the project has NOW for the settings a recorded one was drawn with (size, renderer, time map)."""
    w, h = (rec.get("size") or [None, None])[:2]
    spec = page_spec(pdir, w, h, shots=rec.get("shots", rec.get("renderer", "shots") == "shots"))
    return inputs_fingerprint(pdir, spec, timemap=bool(rec.get("timemap")))


def _declarative_export(pdir: Path, f: Path, name: str):
    raw = read_project_file(pdir, f.relative_to(pdir).parts)
    if raw is None:
        return None
    try:
        data = raw.decode("utf-8").strip()
        if name == "SHOTS":
            data = data.removesuffix("window.SHOTLIST = window.SHOTS;").rstrip()
        match = re.match(r"window\." + re.escape(name) + r"\s*=\s*", data)
        return json.loads(data[match.end():-1]) if match and data.endswith(";") else None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None


def _browser_json(value) -> str:
    def no_prototype_keys(obj):
        if isinstance(obj, dict):
            return {k: no_prototype_keys(v) for k, v in obj.items()
                    if k not in {"__proto__", "constructor", "prototype"}}
        if isinstance(obj, list):
            return [no_prototype_keys(v) for v in obj]
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        return obj

    return "JSON.parse(" + json.dumps(json.dumps(no_prototype_keys(value))) + ")"


def _browser_asset_ref(value, pdir: Path):
    if not isinstance(value, str) or not value:
        return value
    if value.lower().startswith("file:"):
        try:
            local = urlsplit(value)
            if local.netloc:
                return PROJECT_URL + "unavailable"
            path = Path(url2pathname(local.path))
        except ValueError:
            return PROJECT_URL + "unavailable"
    elif Path(value).is_absolute():
        path = Path(value)
    else:
        return value
    return (project_asset_url(path, pdir) if path is not None else None) or PROJECT_URL + "unavailable"


def _browser_config(pdir: Path, slop: dict, proj: dict, have: dict, trust_scripts: bool):
    warnings: list[str] = []
    declared = {}
    for name, path in have.items():
        value = _declarative_export(pdir, path, DATA_GLOBALS[name])
        if value is None:
            if not trust_scripts:
                warnings.append(f"skipped non-declarative render export {path.relative_to(pdir)}")
        else:
            declared[name] = value
    active_exports = set(have) if trust_scripts else set(declared)
    defaults, fallback_warnings = fallback_data(pdir, active_exports)
    data = {**defaults, **{DATA_GLOBALS[name]: val for name, val in declared.items() if name != "project"}}
    if isinstance(declared.get("project"), dict):
        proj = declared["project"]
    for glob, keys in (("PLATES", ("file", "src", "depth", "matte")), ("CLIPS", ("dir",)), ("FXSEQ", ("dir",))):
        if isinstance(data.get(glob), dict):
            data[glob] = {
                k: {field: _browser_asset_ref(x, pdir) if field in keys else x for field, x in val.items()}
                if isinstance(val, dict) else val for k, val in data[glob].items()
            }
    if isinstance(proj.get("font_files"), list):
        proj = {**proj, "font_files": [{**f, "file": _browser_asset_ref(f.get("file"), pdir)}
                                     for f in proj["font_files"] if isinstance(f, dict)]}
    if slop["layer"] and not trust_scripts:
        warnings.append("custom project render code was skipped. For locally reviewed projects, explicitly set "
                        "SLOP_RENDER_TRUST_PROJECT_SCRIPTS=1 to load it")
    config = {
        **slop,
        "engine": ENGINE_URL,
        "project": PROJECT_URL,
        "projectEngine": PROJECT_URL + "engine/",
        "assets": [p.relative_to(pdir / "engine").as_posix() for key, p in have.items()
                   if trust_scripts and key not in declared],
        "layer": slop["layer"] if trust_scripts else [],
        "data": {glob: data[glob] for glob in defaults},
    }
    # Match the legacy order: load the project's exported globals before the trusted engine starts.
    init = "window.SLOP = " + _browser_json(config) + ";\nwindow.PROJECT = " + _browser_json(proj) + ";\n"
    for name in declared:
        glob = DATA_GLOBALS[name]
        if glob != "PROJECT":
            init += f"window.{glob} = " + _browser_json(data[glob]) + ";\n"
    return init, fallback_warnings + warnings, config["data"], active_exports


def page_spec(pdir: Path, w: int | None = None, h: int | None = None, shots: bool | None = None) -> dict:
    """Everything needed to open the engine page for a project: url, init script, warnings, size.

    shots=False skips engine/src/shots.js (the engine's minimal stub renderer draws the storyboard instead);
    default: $SLOP_ENGINE_SHOTS=0 also skips it."""
    if shots is None:
        shots = os.environ.get("SLOP_ENGINE_SHOTS", "1").strip().lower() not in ("0", "false", "no", "off")
    pdir = pdir.resolve()
    engine = engine_dir().resolve()
    idx = engine / "index.html"
    if not idx.is_file():
        raise SlopError(f"engine page missing: {idx} (reinstall the toolkit: bin/setup.sh)")
    pw, ph = project_size(pdir)
    w, h = int(w or pw), int(h or ph)
    have = asset_files(pdir)
    data, _ = fallback_data(pdir, set(have))
    eng_dir = pdir / "engine"
    slop = {
        "project": dir_url(pdir), "projectEngine": dir_url(eng_dir) if eng_dir.is_dir() else dir_url(pdir) + "engine/",
        "engine": dir_url(engine_dir()), "w": w, "h": h, "fps": project_fps(pdir),
        "assets": [p.relative_to(eng_dir).as_posix() for p in have.values()],
        "layer": layer_files(pdir), "deviceFiles": device_files(shots), "data": data, "shots": bool(shots),
    }
    cache = cache_mib()
    if cache:
        slop["cacheMiB"] = cache
    proj = project_subset(pdir)
    init = ("window.SLOP = " + json.dumps(slop, ensure_ascii=False) + ";\n"
            "window.PROJECT = Object.assign(window.PROJECT || {}, " + json.dumps(proj, ensure_ascii=False) + ");\n")
    url = file_url(idx) + f"?w={w}&h={h}&project=" + quote(dir_url(pdir), safe="")
    trust_scripts = os.environ.get("SLOP_RENDER_TRUST_PROJECT_SCRIPTS") == "1"
    browser_init, browser_warnings, browser_fallbacks, browser_exports = _browser_config(pdir, slop, proj, have, trust_scripts)
    untimed = untimed_words(_read_project_json(pdir, "analysis/words.json") or {}) if "lyrics" not in browser_exports else []
    return {"url": url, "init": init, "warnings": browser_warnings, "w": w, "h": h, "fps": project_fps(pdir),
            "assets": sorted(browser_exports), "fallbacks": sorted(browser_fallbacks), "untimed": untimed, "data": data,
            "shots": bool(shots),
            "engine_root": engine, "project_root": pdir, "trust_project_scripts": trust_scripts,
            "browser_url": ENGINE_URL + f"index.html?w={w}&h={h}", "browser_init": browser_init,
            "browser_fallbacks": browser_fallbacks}


def cache_mib() -> float | None:
    """$SLOP_CACHE_MIB: the engine page's decoded-image budget in MiB (core.js; default 320, else project.json
    cache_mib). Not part of the frame fingerprint: it changes memory use, never pixels."""
    v = os.environ.get("SLOP_CACHE_MIB", "").strip()
    try:
        f = float(v) if v else 0.0
    except ValueError:
        raise SlopError(f"SLOP_CACHE_MIB must be a number of MiB, e.g. 320 (got {v!r})") from None
    return f if f > 0 else None


def frame_count(duration: float, fps: float) -> int:
    return int(math.ceil(duration * fps - 1e-6))
