"""Explicit, reviewed publication into one of the run's source topic folders."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat

from .access import AccessDenied, ArtifactGateway, _parts, open_directory


_PROJECT_DIRECTORIES = {
    "agent-system", "old", "src", "tests", "node_modules", "__pycache__",
    "repositories", "plugins", "build", "dist", "venv",
}


def export_result(workspace: Path, gateway: ArtifactGateway, state, destination: str,
                  *, reviewed_for_publication: bool) -> Path:
    """Copy only the final text, atomically, without replacing an existing file.

    The caller must hold the run lock. Source topic names form an explicit
    allowlist; reports, responses, code and runtime state cannot be exported.
    """
    if not reviewed_for_publication:
        raise ValueError("Экспорт требует --reviewed-for-publication после проверки допустимости публикации текста.")
    if state.stage not in {"APPROVED", "INCOMPLETE", "NEEDS_USER"} or not state.result:
        raise ValueError("У прогона ещё нет итогового документа для экспорта.")
    parts = _parts(destination)
    if len(parts) < 2 or any(part.startswith(".") for part in parts):
        raise ValueError("Путь экспорта должен вести в тематический каталог внутри workspace.")
    allowed_topics = {
        relative.parts[0]
        for key in state.sources
        if len((relative := Path(key)).parts) > 1
        and not relative.is_absolute()
        and relative.parts[0] not in _PROJECT_DIRECTORIES
        and not relative.parts[0].startswith(".")
        and ".." not in relative.parts
    }
    if parts[0] not in allowed_topics or parts[0] in _PROJECT_DIRECTORIES:
        raise ValueError("Экспорт разрешён только в тематические каталоги исходных материалов этого прогона.")
    if Path(parts[-1]).suffix != ".txt":
        raise ValueError("Итоговую Jira-постановку экспортируй в файл *.txt.")
    source = _parts(state.result)
    expected = f"results/v{state.version}-{state.stage.lower()}.jira.txt"
    if len(source) != 2 or source[0] != "results" or state.result != expected:
        raise AccessDenied("Export source must be a final Jira artifact")

    source_parent, source_name = gateway._parent(state.result, create=False)
    try:
        fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2_000_000:
                raise AccessDenied("Export source must be a regular private final document")
            data = stream.read(2_000_001)
        if len(data) > 2_000_000:
            raise AccessDenied("Export source exceeds the final document limit")
        data.decode("utf-8")
    finally:
        os.close(source_parent)

    # open_directory walks every ancestor with O_NOFOLLOW. Parents must already
    # exist: publication does not create unexpected directories in workspace.
    parent_path = workspace.joinpath(*parts[:-1])
    if not parent_path.resolve().is_relative_to(workspace.resolve()):
        raise AccessDenied("Export destination escapes workspace")
    descriptor = open_directory(parent_path, create=False)
    temporary = ".export-pending-" + secrets.token_hex(16)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication fails for regular files, hard links and symlinks
        # already at the target, including a target installed concurrently.
        os.link(temporary, parts[-1], src_dir_fd=descriptor, dst_dir_fd=descriptor,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)
    return workspace.joinpath(*parts)
