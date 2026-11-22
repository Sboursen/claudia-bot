"""Constrain and stage files named by project image jobs."""

import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ..config import SlopError

PLATE_ID = re.compile(r"[A-Za-z0-9][\w.~-]{0,79}")
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def candidate_directory(project: Path, plate: str) -> Path:
    if not isinstance(plate, str) or not PLATE_ID.fullmatch(plate):
        raise SlopError("image job plate must be a plate ID, without path separators")
    return project_path(project, Path("plates") / "cands" / plate)


def project_path(project: Path, name: str | Path) -> Path:
    """Return a path within the selected root, rejecting links and traversal."""
    root = Path(project).resolve()
    path = Path(name)
    if path.is_absolute():
        # Preserve absolute paths stored by older projects, including a user-selected
        # alias for the project root, but never resolve the supplied path first.
        for base in (Path(project).absolute(), root):
            if path.is_relative_to(base):
                path = path.relative_to(base)
                break
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SlopError("image jobs can only reference files inside the project")
    candidate = root
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise SlopError("image jobs cannot reference symbolic links")
    return candidate


@contextmanager
def project_directory(root: Path, parts: tuple[str, ...], *, create: bool = False):
    if os.open not in os.supports_dir_fd or not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise SlopError("secure image jobs require no-follow directory handles")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(root, dir_flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, dir_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        yield parent
    finally:
        os.close(parent)


def read_project_file(project: Path, name: str | Path, *, limit: int) -> bytes:
    """Open the validated path through directory handles so later link swaps fail closed."""
    root = Path(project).resolve()
    parts = project_path(project, name).relative_to(root).parts
    try:
        with project_directory(root, parts[:-1]) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SlopError("image jobs require an ordinary, unlinked project file")
                if info.st_size > limit:
                    raise SlopError("image job input exceeds the allowed size")
                content = stream.read(limit + 1)
                if len(content) > limit:
                    raise SlopError("image job input exceeds the allowed size")
                return content
    except FileNotFoundError:
        raise
    except OSError as e:
        raise SlopError("could not safely open a project file for the image job") from e


def write_candidate(project: Path, directory: Path, ext: str, data: bytes, sidecar: dict) -> Path:
    root = Path(project).resolve()
    directory = project_path(project, directory.absolute())
    if not re.fullmatch(r"\.[a-z0-9]+", ext) or ext == ".json":
        raise SlopError("unsupported image job output extension")
    try:
        with project_directory(root, directory.relative_to(root).parts, create=True) as parent:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            indexes = [int(stem) for name in os.listdir(parent)
                       if (stem := Path(name).stem).isascii() and stem.isdecimal()]
            first = max(indexes, default=0) + 1
            for k in range(first, first + 10000):
                meta_name, image_name = f"{k}.json", f"{k}{ext}"
                try:
                    meta = os.open(meta_name, flags, 0o600, dir_fd=parent)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(meta, "w", encoding="utf-8") as stream:
                        json.dump(sidecar, stream, indent=1)
                    image = os.open(image_name, flags, 0o600, dir_fd=parent)
                except FileExistsError:
                    os.unlink(meta_name, dir_fd=parent)
                    continue
                except BaseException:
                    os.unlink(meta_name, dir_fd=parent)
                    raise
                try:
                    with os.fdopen(image, "wb") as stream:
                        stream.write(data)
                except BaseException:
                    os.unlink(image_name, dir_fd=parent)
                    os.unlink(meta_name, dir_fd=parent)
                    raise
                return directory / image_name
    except OSError as e:
        raise SlopError("could not safely save an image job candidate") from e
    raise SlopError(f"could not find a free file number in {directory}")


@contextmanager
def stage_references(project: Path, sources: list[str], confined: list[str] | None):
    """Pass private copies of project-supplied references to media libraries and the provider."""
    if not confined:
        yield sources
        return
    guarded = set(confined)
    with TemporaryDirectory(prefix="slop-image-inputs-") as folder:
        staged = []
        for index, source in enumerate(sources):
            if source in guarded:
                path = Path(folder) / f"{index}{Path(source).suffix}"
                path.write_bytes(read_project_file(project, source, limit=MAX_IMAGE_BYTES))
                staged.append(str(path))
            else:
                staged.append(source)
        yield staged
