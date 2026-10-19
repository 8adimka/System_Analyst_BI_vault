"""The coordinator's only writer: immutable artifacts and atomic state.

Paths are relative to a single private run directory. Directory descriptors and
O_NOFOLLOW are used for every component, including creation; a checked symlink
cannot be exchanged for a writable destination between checking and opening.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePath
import secrets
import stat
from typing import Any


class AccessDenied(ValueError):
    """A requested path is outside the coordinator's explicit write grant."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _parts(relative: str | Path) -> tuple[str, ...]:
    raw = os.fspath(relative)
    parts = PurePath(raw).parts
    if not raw or raw.startswith("/") or "\\" in raw or any(
        part in ("", ".", "..") for part in raw.split("/")
    ):
        raise AccessDenied("An artifact needs a nonempty relative path without traversal")
    return parts


def open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory by walking from / without following links."""
    path = path.absolute()
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            if component in ("", ".", ".."):
                raise AccessDenied("Noncanonical directory")
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except (OSError, ValueError) as exc:
        os.close(descriptor)
        raise AccessDenied("Directory is missing, inaccessible, or contains a symlink") from exc


class ArtifactGateway:
    """Writes only below .private/runtime/<run>; existing artifacts are immutable."""

    def __init__(self, workspace: str | Path, run_root: str | Path):
        self.workspace = Path(workspace).absolute()
        self.root = Path(run_root).absolute()
        if self.workspace != self.workspace.resolve():
            raise AccessDenied("Workspace must be canonical")
        runtime = self.workspace / ".private" / "runtime"
        try:
            suffix = self.root.relative_to(runtime)
        except ValueError as exc:
            raise AccessDenied("Writes are allowed only in a private runtime run") from exc
        if not suffix.parts or any(p in (".", "..") for p in suffix.parts):
            raise AccessDenied("A dedicated run directory is required")
        # resolve() is an additional containment check, not the race protection.
        if not self.root.resolve().is_relative_to(runtime.resolve()):
            raise AccessDenied("Run directory escapes runtime")
        self._root_fd = open_directory(self.root, create=True)
        self._device = os.fstat(self._root_fd).st_dev

    def close(self) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def __enter__(self) -> "ArtifactGateway":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def path(self, relative: str | Path) -> Path:
        parts = _parts(relative)
        candidate = self.root.joinpath(*parts)
        if not candidate.resolve().is_relative_to(self.root.resolve()):
            raise AccessDenied("Artifact path escapes the run directory")
        return candidate

    def _parent(self, relative: str | Path, *, create: bool) -> tuple[int, str]:
        parts = _parts(relative)
        self.path(relative)
        descriptor = os.dup(self._root_fd)
        try:
            for component in parts[:-1]:
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if os.fstat(descriptor).st_dev != self._device:
                    raise AccessDenied("Cross-device artifact directories are forbidden")
            return descriptor, parts[-1]
        except (OSError, ValueError) as exc:
            os.close(descriptor)
            raise AccessDenied("Artifact parent is unsafe or inaccessible") from exc

    def write_text(self, relative: str | Path, text: str, *, replace: bool = False) -> Path:
        if replace and os.fspath(relative) != "state.json":
            raise AccessDenied("Only state.json may be atomically replaced")
        data = text.encode("utf-8")
        descriptor, name = self._parent(relative, create=True)
        temporary = ".pending-" + secrets.token_hex(16)
        try:
            try:
                target = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target and (not stat.S_ISREG(target.st_mode) or target.st_nlink != 1):
                raise AccessDenied("Artifact target must be a regular unlinked file")
            if target and not replace:
                raise FileExistsError("Artifact already exists; history is immutable")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            else:
                # link() atomically fails if a concurrent writer installed name.
                os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
                os.unlink(temporary, dir_fd=descriptor)
            os.fsync(descriptor)
            return self.root.joinpath(*_parts(relative))
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            raise AccessDenied("Artifact write refused") from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            os.close(descriptor)

    def write_json(self, relative: str | Path, payload: Any, *, replace: bool = False) -> Path:
        return self.write_text(relative, json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", replace=replace)

    def read_json(self, relative: str | Path) -> Any:
        descriptor, name = self._parent(relative, create=False)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise AccessDenied("State is not a regular file")
                return json.load(stream)
        except OSError as exc:
            raise AccessDenied("Artifact read refused") from exc
        finally:
            os.close(descriptor)
