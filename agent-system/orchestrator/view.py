"""A fail-closed Linux filesystem view for SDK app-server processes.

The root filesystem starts empty. Only system executables, explicit input
files, repositories, current Wiki pages, and project instructions are mounted.
In particular, the ordinary workspace, other runs, historical .agent files and
the user's global Codex sessions are never mounted. Each role has its own home.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
from typing import Sequence

from .access import AccessDenied, ArtifactGateway, open_directory


class IsolationUnavailable(RuntimeError):
    pass


_HISTORY = {"legacy", "history", "archive", "archives", "migration", "migrations", "old", "drafts"}
_SECRET_NAMES = {"доступы.txt", "auth.json", ".env", ".netrc", "credentials", "id_rsa", "id_ed25519"}
_RESEARCH_SKILLS = (
    "codebase-investigator", "requirements-clarifier", "jira-task-writer",
    "task-reviser", "consistency-reviewer", "evidence-reviewer",
)
_RUNTIME_INSTRUCTIONS = "agent-system/runtime/AGENTS.md"
_PROXY_ENV = tuple(name for upper in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
                   for name in (upper, upper.lower()))
_CERT_ENV = {"SSL_CERT_FILE": False, "SSL_CERT_DIR": True, "CODEX_CA_CERTIFICATE": False}
# Generated PUBLIC trust stores only. Their private-key/trust-source siblings
# must never be mounted. Arch's /etc/ssl/certs contains symlinks into extracted;
# exposing only /etc/ssl/certs silently breaks TLS in an otherwise healthy view.
_SYSTEM_CA_PATHS = (
    "/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/ca-certificates/extracted",
    "/etc/pki/tls/certs", "/etc/pki/tls/cert.pem", "/etc/pki/ca-trust/extracted",
    "/etc/ssl/ca-bundle.pem", "/var/lib/ca-certificates/ca-bundle.pem",
)
_CERT_SUFFIXES = {".crt", ".cer", ".pem", ".der"}


def _system_certificate_path(path: Path, *, directory: bool) -> bool:
    """Permit normal aliases only inside the already mounted public stores."""
    stores = [Path(value) for value in _SYSTEM_CA_PATHS if Path(value).exists()]
    covered = any(path == store or (store.is_dir() and path.is_relative_to(store)) for store in stores)
    if not path.is_absolute() or not covered or not (path.is_dir() if directory else path.is_file()):
        return False
    target = path.resolve()
    return any(target == store.resolve() or (store.is_dir() and target.is_relative_to(store.resolve()))
               for store in stores)


def _certificate_path(value: str, workspace: Path, *, directory: bool) -> Path:
    """Custom CA grants are explicit, canonical, and contain no other user data."""
    path = Path(value)
    if not value or "\x00" in value or "\n" in value or not path.is_absolute():
        raise AccessDenied("Custom certificate paths must be absolute existing paths")
    if path != path.resolve() or (not path.is_dir() if directory else not path.is_file()):
        raise AccessDenied("Custom certificate paths cannot contain symlinks or missing files")
    lowered = {part.lower() for part in path.parts}
    blocked = {".private", ".agent", ".codex", ".agents", ".git", ".aws", ".ssh", ".gnupg"}
    if (lowered.intersection(blocked) or path.name.lower() in _SECRET_NAMES
            or path.name.lower().startswith(".env")
            or (path.is_relative_to(workspace) and lowered.intersection(_HISTORY))):
        raise AccessDenied("Certificate paths cannot expose credentials or workspace history")
    if directory:
        # OpenSSL uses a flat cert directory. Reject symlink aliases, nested
        # directories and unrelated files instead of granting an entire tree.
        for index, child in enumerate(path.iterdir()):
            if index >= 4096:
                raise AccessDenied("Custom certificate directory is too large")
            _certificate_path(str(child), workspace, directory=False)
    elif (path.suffix.lower() not in _CERT_SUFFIXES
          and re.fullmatch(r"[0-9a-fA-F]{8}\.\d+", path.name) is None):
        raise AccessDenied("Custom CA files must have certificate filenames")
    return path


def _network_environment(workspace: Path) -> tuple[dict[str, str], list[Path]]:
    environment = {name: os.environ[name] for name in _PROXY_ENV if name in os.environ}
    mounts = []
    for name, directory in _CERT_ENV.items():
        value = os.environ.get(name)
        if value:
            # OpenSSL documents SSL_CERT_DIR as a colon-separated search path.
            for entry in value.split(os.pathsep) if directory else (value,):
                path = Path(entry)
                if not _system_certificate_path(path, directory=directory):
                    mounts.append(_certificate_path(entry, workspace, directory=directory))
            environment[name] = value
    if any("\x00" in value or "\n" in value for value in environment.values()):
        raise AccessDenied("Network environment contains an invalid value")
    return environment, list(dict.fromkeys(mounts))


def _canonical_file(path: Path, workspace: Path) -> Path:
    path = path.absolute()
    if path != path.resolve() or not path.is_file() or not path.is_relative_to(workspace):
        raise AccessDenied("Input must be an existing regular workspace file without symlinks")
    relative = path.relative_to(workspace)
    if any(p in {".agent", ".agents", ".git", ".codex", ".aws"} for p in relative.parts):
        raise AccessDenied("Private history and credentials cannot be input sources")
    if relative.parts[:2] in {(".private", "runtime"), (".private", "development"),
                             (".private", "mockups")}:
        raise AccessDenied("Other runs, development artifacts and drafts cannot be input sources")
    if relative.as_posix() in {"AGENTS.md", "AGENT.md"}:
        raise AccessDenied("Interactive instructions cannot replace runtime instructions")
    if path.name.lower() in _SECRET_NAMES or path.name.startswith(".env."):
        raise AccessDenied("Credential files cannot be input sources")
    return path


class PreparedView:
    def __init__(self, workspace: str | Path, run_root: str | Path,
                 sources: Sequence[Path] = (), *, include_corporate: bool = True,
                 auth_home: Path | None = None, analyst_sources: Sequence[Path] = ()):
        self.workspace = Path(workspace).absolute()
        self.run_root = Path(run_root).absolute()
        self.gateway = ArtifactGateway(self.workspace, self.run_root)
        try:
            self.sources = tuple(_canonical_file(Path(p), self.workspace) for p in sources)
            self.analyst_sources = tuple(_canonical_file(Path(p), self.workspace) for p in analyst_sources)
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

    def _files(self, role: str = "reviewer") -> list[Path]:
        files = set(self.sources) | set(self.analyst_sources)
        # Interactive developer instructions and skills are a separate context.
        # An allowlist prevents newly installed roles from entering blind review.
        patterns = (_RUNTIME_INSTRUCTIONS, "agent-system/standards/**/*.md",
                    *(f".agents/skills/{skill}/SKILL.md" for skill in _RESEARCH_SKILLS),
                    ".codex/agents/analyst.toml", ".codex/agents/reviewer.toml")
        for pattern in patterns:
            for path in self.workspace.glob(pattern):
                if path.is_file() and path == path.resolve():
                    files.add(path)
        if self.include_corporate:
            knowledge = self.workspace / ".private" / "knowledge"
            for name in ("sources.json", "fullstack-sources.json"):
                manifest = knowledge / name
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
        if role == "reviewer":
            files.difference_update(self.analyst_sources)
        return sorted(files)

    def _empty_git_config(self) -> Path:
        """Git must see an empty regular config, never an unreadable device mask."""
        relative = "view-files/empty-git-config"
        path = self.gateway.path(relative)
        if not path.exists():
            try:
                self.gateway.write_text(relative, "")
            except FileExistsError:
                pass  # Another caller installed the same immutable artifact.
        parent_fd = open_directory(path.parent)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != 0:
                    raise AccessDenied("Git config surrogate must be an immutable empty regular file")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise AccessDenied("Git config surrogate is unsafe or inaccessible") from exc
        finally:
            os.close(parent_fd)
        return path

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
        for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/gai.conf", "/etc/localtime", *_SYSTEM_CA_PATHS):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        directories: set[Path] = {self.workspace, home, Path.home()}
        files = self._files(role)
        # Codex discovers the root AGENTS.md automatically. Supply the analytics
        # entrypoint at that location instead of the interactive developer file.
        # A missing/unsafe runtime entrypoint never falls back to the host root.
        runtime_instructions = self.workspace / _RUNTIME_INSTRUCTIONS
        mounts: list[tuple[Path, Path]] = [
            (p, self.workspace / "AGENTS.md" if p == runtime_instructions else p) for p in files
        ]
        network_environment, certificates = _network_environment(self.workspace)
        mounts.extend((p, p) for p in certificates)
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
            source = (str(self._empty_git_config())
                      if target.name.lower() == "config" and target.parent.name == ".git" else "/dev/null")
            argv += ["--ro-bind", source, str(target)]
        for name, value in network_environment.items():
            argv += ["--setenv", name, value]
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
        # The wrapper itself is an immutable policy snapshot. A resumed run may
        # need a repaired mount/network policy; retain its old snapshot and use
        # a new content-addressed name rather than overwrite or reject history.
        revision = hashlib.sha256(script.encode("utf-8")).hexdigest()
        relative = "view-files/" + role + "-codex-" + revision
        wrapper = self.gateway.path(relative)
        if wrapper.exists():
            # Resume reuses exactly this snapshot, never an altered/link target.
            if (wrapper.is_symlink() or not wrapper.is_file() or wrapper.stat().st_nlink != 1
                    or wrapper.read_text(encoding="utf-8") != script):
                raise AccessDenied("Existing role wrapper differs from the current isolation policy")
        else:
            self.gateway.write_text(relative, script)
        os.chmod(wrapper, 0o700)
        return wrapper
