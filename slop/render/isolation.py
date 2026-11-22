"""Render page resources over an intercepted origin with no listening server."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ORIGIN = "https://claudia-render.invalid"
ENGINE_URL = ORIGIN + "/engine/"
PROJECT_URL = ORIGIN + "/project/"
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MEDIA_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".otf": "font/otf",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
SCRIPT_TYPES = {".js": "text/javascript", ".mjs": "text/javascript"}
PROJECT_RESOURCE_DIRS = {"engine", "plates", "fonts"}


def scoped_path(root: Path, parts: tuple[str, ...]) -> Path | None:
    if not parts or any(not p or p.startswith(".") or any(c in p for c in "/\\:\0") for p in parts):
        return None
    candidate = root.joinpath(*parts)
    try:
        return candidate if candidate.resolve() == candidate else None
    except (OSError, RuntimeError, ValueError):
        return None


def _read_regular(root: Path, parts: tuple[str, ...]) -> bytes | None:
    """Read a regular file within the selected absolute root, rejecting symlink components."""
    candidate = scoped_path(root, parts)
    if candidate is None:
        return None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(root, directory_flags)
            try:
                for part in parts[:-1]:
                    next_fd = os.open(part, directory_flags, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                resource = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
            finally:
                os.close(fd)
            stream = os.fdopen(resource, "rb")
        else:
            if not candidate.is_file():
                return None
            stream = candidate.open("rb")
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESOURCE_BYTES:
                return None
            data = stream.read(MAX_RESOURCE_BYTES + 1)
            return data if len(data) <= MAX_RESOURCE_BYTES else None
    except OSError:
        return None


def read_project_file(pdir: Path, parts: tuple[str, ...]) -> bytes | None:
    return _read_regular(pdir.resolve(), parts)


def project_asset_url(path: Path, pdir: Path) -> str | None:
    """Map a local media reference inside a project resource directory to the render origin."""
    root = pdir.resolve()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if not parts or parts[0] not in PROJECT_RESOURCE_DIRS or scoped_path(root, parts) is None:
        return None
    return PROJECT_URL + "/".join(quote(part, safe="") for part in parts)


class RenderAssets:
    """Map a fixed origin to trusted engine files and scoped project assets."""

    def __init__(self, engine: Path, project: Path, trust_project_scripts: bool):
        self.engine = engine.resolve()
        self.project = project.resolve()
        self.trust_project_scripts = trust_project_scripts
        self.nonce = secrets.token_urlsafe(24)

    def _asset(self, parts: tuple[str, ...]) -> tuple[Path, str] | None:
        namespace, tail = parts[0], parts[1:]
        if not tail:
            return None
        suffix = Path(tail[-1]).suffix.lower()
        if namespace == "engine":
            if tail == ("index.html",):
                return self.engine, "text/html; charset=utf-8"
            if tail == ("fonts", "fonts.js") or (tail[0] == "src" and suffix in SCRIPT_TYPES):
                return self.engine, SCRIPT_TYPES[suffix]
            if tail[0] == "fonts" and suffix in {".otf", ".ttf", ".woff", ".woff2"}:
                return self.engine, MEDIA_TYPES[suffix]
        if namespace == "project" and tail[0] in PROJECT_RESOURCE_DIRS:
            if suffix in MEDIA_TYPES:
                return self.project, MEDIA_TYPES[suffix]
            if tail[0] == "engine" and suffix == ".json":
                return self.project, "application/json"
            if self.trust_project_scripts and tail[0] == "engine" and suffix in SCRIPT_TYPES:
                return self.project, SCRIPT_TYPES[suffix]
        return None

    def _csp(self) -> str:
        inline = "'unsafe-inline'" if self.trust_project_scripts else f"'nonce-{self.nonce}'"
        return (
            f"default-src 'none'; script-src 'self' {inline}; "
            "style-src 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
            "connect-src 'self'; media-src 'self'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'none'; object-src 'none'; worker-src 'none'"
        )

    def handle(self, route) -> None:
        request = route.request
        url = urlsplit(request.url)
        if request.method != "GET" or (url.scheme, url.netloc) != ("https", "claudia-render.invalid"):
            route.abort()
            return
        parts = tuple(unquote(p) for p in url.path.removeprefix("/").split("/"))
        asset = self._asset(parts)
        if asset is None:
            route.fulfill(status=404, body="")
            return
        root, mime = asset
        body = _read_regular(root, parts[1:])
        if body is None:
            route.fulfill(status=404, body="")
            return
        headers = {"Content-Type": mime, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
        if mime.startswith("text/html"):
            body = body.replace(b"<script>", f'<script nonce="{self.nonce}">'.encode(), 1)
            headers["Content-Security-Policy"] = self._csp()
        route.fulfill(status=200, headers=headers, body=body)
