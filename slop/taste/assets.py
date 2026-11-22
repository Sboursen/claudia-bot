"""Bounded file access for staged taste assets and explicitly selected source roots."""
from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

JPEG_LIMIT = 25 * 1024 * 1024
FONT_LIMIT = 16 * 1024 * 1024
NO_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _parts(relative: str | Path) -> tuple[str, ...]:
    path = Path(relative)
    if path.anchor or not path.parts or any(
        part in (".", "..") or ":" in part or "\\" in part for part in path.parts
    ):
        raise ValueError("asset path must be relative to its approved directory")
    return path.parts


@contextmanager
def _directory(root: Path, parts: tuple[str, ...], *, create: bool = False):
    """Hold the parent directory open and refuse symlinks on each descendant hop."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _windows_plain_path(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Path:
    path = root
    for part in parts:
        path /= part
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & NO_REPARSE:
            raise OSError("asset directory is not a plain directory")
    return path


def _windows_final_path(fd: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = get_path(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = ctypes.create_unicode_buffer(size + 1)
    length = get_path(handle, buf, len(buf), 0)
    if not length or length >= len(buf):
        raise OSError("could not determine opened asset path")
    return Path(buf.value.removeprefix("\\\\?\\"))


def _read_fd(fd: int, limit: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise OSError("asset must be one bounded regular file")
    with os.fdopen(fd, "rb", closefd=False) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OSError("asset exceeds its size limit")
    return data


def read_file(root: Path, relative: str | Path, limit: int) -> bytes:
    """Read only a bounded, single-link regular file under an approved directory."""
    root = Path(root).resolve(strict=True)
    parts = _parts(relative)
    if os.name == "nt":
        path = _windows_plain_path(root, parts[:-1]) / parts[-1]
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_file_attributes & NO_REPARSE:
            raise OSError("asset is not a plain file")
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            opened = _windows_final_path(fd)
            if opened != path:
                raise OSError("asset handle points outside its requested path")
            return _read_fd(fd, limit)
        finally:
            os.close(fd)
    with _directory(root, parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            return _read_fd(fd, limit)
        finally:
            os.close(fd)


def write_file(root: Path, relative: str | Path, data: bytes) -> None:
    """Stage bytes in the approved directory with an exclusive temporary name and atomic rename."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    parts = _parts(relative)
    name = parts[-1]
    tmp = ".asset-" + secrets.token_hex(16)
    if os.name == "nt":
        parent = _windows_plain_path(root, parts[:-1], create=True)
        try:
            with (parent / tmp).open("xb") as stream:
                if _windows_final_path(stream.fileno()) != parent / tmp:
                    raise OSError("asset staging handle points outside the approved path")
                stream.write(data)
            os.replace(parent / tmp, parent / name)
        finally:
            (parent / tmp).unlink(missing_ok=True)
        return
    with _directory(root, parts[:-1], create=True) as parent:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass


def profile_asset(pdir: Path, relative: str | Path, root: Path | None = None) -> tuple[Path, Path]:
    from .store import profile_root

    selected = Path(root) if root is not None else profile_root(pdir)
    return selected.parent, Path(selected.name, *_parts(relative))
