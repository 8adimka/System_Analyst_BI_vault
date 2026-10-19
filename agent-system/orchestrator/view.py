"""A fail-closed Linux filesystem view for SDK app-server processes.

The root filesystem starts empty. Only system executables, explicit input
files, repositories, current Wiki pages, and project instructions are mounted.
In particular, the ordinary workspace, other runs, historical .agent files and
the user's global Codex sessions are never mounted. Each role has its own home.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Sequence

from .access import AccessDenied, ArtifactGateway, open_directory


class IsolationUnavailable(RuntimeError):
    pass


_HISTORY = {"legacy", "history", "archive", "archives", "migration", "migrations", "old", "drafts"}
_SECRET_NAMES = {"доступы.txt", "auth.json", ".env", ".netrc", "credentials", "id_rsa", "id_ed25519"}


def _canonical_file(path: Path, workspace: Path) -> Path:
    path = path.absolute()
    if path != path.resolve() or not path.is_file() or not path.is_relative_to(workspace):
        raise AccessDenied("Input must be an existing regular workspace file without symlinks")
    relative = path.relative_to(workspace)
    if any(p in {".agent", ".git", ".codex", ".aws"} for p in relative.parts):
        raise AccessDenied("Private history and credentials cannot be input sources")
    if relative.parts[:2] == (".private", "runtime"):
        raise AccessDenied("Other runs and drafts cannot be input sources")
    if path.name.lower() in _SECRET_NAMES or path.name.startswith(".env."):
        raise AccessDenied("Credential files cannot be input sources")
    return path


class PreparedView:
    def __init__(self, workspace: str | Path, run_root: str | Path,
                 sources: Sequence[Path] = (), *, include_corporate: bool = True,
                 auth_home: Path | None = None):
        self.workspace = Path(workspace).absolute()
        self.run_root = Path(run_root).absolute()
        self.gateway = ArtifactGateway(self.workspace, self.run_root)
        try:
            self.sources = tuple(_canonical_file(Path(p), self.workspace) for p in sources)
        except Exception:
            self.gateway.close()
            raise
        self.include_corporate = include_corporate
        self.auth_home = Path(auth_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).absolute()
        self.bwrap = shutil.which("bwrap")
        self._mount_plan: tuple[list[Path], list[Path]] | None = None
        if not self.bwrap:
            self.gateway.close()
            raise IsolationUnavailable("bubblewrap (bwrap) is required; no unisolated fallback is allowed")

    def close(self) -> None:
        self.gateway.close()

    def _files(self) -> list[Path]:
        files = set(self.sources)
        for pattern in ("AGENTS.md", "agent-system/standards/**/*.md", ".agents/skills/**/SKILL.md", ".codex/agents/*.toml"):
            for path in self.workspace.glob(pattern):
                if path.is_file() and path == path.resolve():
                    files.add(path)
        if self.include_corporate:
            knowledge = self.workspace / ".private" / "knowledge"
            manifest = knowledge / "sources.json"
            if manifest.is_file() and manifest == manifest.resolve():
                files.add(manifest)
            # This one extracted normative checklist is an explicit input; the
            # rest of legacy (migration notes and historical drafts) stays hidden.
            checklist = knowledge / "legacy" / "domain-checks.md"
            if checklist.is_file() and checklist == checklist.resolve():
                files.add(checklist)
            for path in knowledge.rglob("*.md") if knowledge.is_dir() else ():
                parts = path.relative_to(knowledge).parts
                if not any(part.startswith(".") or part.lower() in _HISTORY for part in parts[:-1]):
                    if path.is_file() and path == path.resolve():
                        files.add(path)
        return sorted(files)

    def command(self, role: str, codex_bin: str | Path, args: Sequence[str] = ()) -> list[str]:
        """Return argv, with no shell interpolation, for a selected role."""
        if role not in ("analyst", "reviewer"):
            raise ValueError("Unknown role")
        executable = Path(codex_bin).resolve(strict=True)
        if not executable.is_file():
            raise IsolationUnavailable("Codex executable is not a regular file")
        home = self.run_root / ("codex-" + role)
        fd = open_directory(home, create=True)
        os.close(fd)
        # Keep runtime writes private, while never letting one role see the other.
        os.chmod(home, 0o700)
        # Do not set --die-with-parent: SDK construction runs in a bounded
        # worker, and Linux PDEATHSIG follows that thread's lifetime. Killing it
        # when construction returns would terminate a healthy app-server.
        # SDK close() owns termination; a crashed coordinator closes stdin,
        # causing the official stdio app-server to exit on EOF.
        argv = [self.bwrap, "--new-session", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--clearenv"]
        for path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/etc"]
        for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/gai.conf", "/etc/ssl/certs", "/etc/localtime"):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        directories: set[Path] = {self.workspace, home, Path.home()}
        files = self._files()
        mounts: list[tuple[Path, Path]] = [(p, p) for p in files]
        masked: list[Path] = []
        if self.include_corporate:
            repositories = self.workspace / ".private" / "repositories"
            if repositories.is_dir():
                if repositories != repositories.resolve():
                    raise AccessDenied("Repository root must not be a symlink")
                mounts.append((repositories, repositories))
                # Repository history remains readable for evidence, but local
                # credentials and execution hooks are unnecessary for research.
                if self._mount_plan is None:
                    for path in repositories.rglob("*"):
                        lowered = path.name.lower()
                        if path.is_file() and (lowered in _SECRET_NAMES or lowered.startswith(".env.")
                                               or lowered.endswith((".pem", ".key"))
                                               or (lowered == "config" and path.parent.name == ".git")):
                            masked.append(path)
                    self._mount_plan = (files, masked)
                else:
                    _, masked = self._mount_plan
        # Native Codex distributions may need codex-code-mode-host and bundled rg.
        # Only its binary directory is mounted, never the user's VS Code profile.
        distribution = executable.parent
        if (executable.parent.parent / "codex-package.json").is_file():
            distribution = executable.parent.parent
        if not executable.is_relative_to(Path("/usr")) and not executable.is_relative_to(Path("/bin")):
            mounts.append((distribution, distribution))
        for _, target in mounts:
            directories.add(target.parent)
        auth = self.auth_home / "auth.json"
        if auth.is_file():
            if auth.is_symlink() or self.auth_home != self.auth_home.resolve():
                raise AccessDenied("Codex authentication path must not be a symlink")
            # A mount is not a copy: the builder never reads credential content.
            # Refresh writes are intentionally refused; login stays user-managed.
            mounts.append((auth, home / "auth.json"))
        expanded: set[Path] = set()
        for directory in directories:
            expanded.add(directory)
            expanded.update(directory.parents)
        for directory in sorted(expanded - {Path("/")}, key=lambda p: (len(p.parts), str(p))):
            if not str(directory).startswith(("/usr/", "/bin/", "/lib/", "/lib64/")):
                argv += ["--dir", str(directory)]
        argv += ["--bind", str(home), str(home)]
        for source, target in mounts:
            argv += ["--ro-bind", str(source), str(target)]
        for target in masked:
            argv += ["--ro-bind", "/dev/null", str(target)]
        argv += ["--setenv", "HOME", str(Path.home()), "--setenv", "CODEX_HOME", str(home),
                 "--setenv", "PATH", str(executable.parent) + ":" + str(distribution / "codex-path") + ":/usr/bin:/bin",
                 "--setenv", "LANG", "C.UTF-8", "--setenv", "TMPDIR", "/tmp",
                 "--chdir", str(self.workspace), str(executable), *args]
        return argv

    def prepare(self, role: str, codex_bin: str | Path) -> Path:
        argv = self.command(role, codex_bin)
        # Probe the namespace before launching SDK, including nested sandbox support.
        probe = argv[:argv.index("--chdir")] + ["--chdir", str(self.workspace), "/usr/bin/true"]
        result = subprocess.run(probe, capture_output=True, timeout=15, check=False)
        if result.returncode:
            raise IsolationUnavailable("bubblewrap cannot create the required filesystem view")
        script = "#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n'
        relative = "view-files/" + role + "-codex"
        wrapper = self.gateway.path(relative)
        if wrapper.exists():
            # Resume uses the same deterministic command; never overwrite history.
            if wrapper.is_symlink() or wrapper.read_text(encoding="utf-8") != script:
                raise AccessDenied("Existing role wrapper differs from the current isolation policy")
        else:
            self.gateway.write_text(relative, script)
        os.chmod(wrapper, 0o700)
        return wrapper
