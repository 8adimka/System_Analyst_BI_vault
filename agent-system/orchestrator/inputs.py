"""Explicit primary-source inventory and version checks; never scan credentials."""
import hashlib
import re
import subprocess
from pathlib import Path


EXCLUDED = {".git", ".agent", ".venv", ".aws", ".ssh", ".gnupg", "node_modules", "__pycache__"}
SENSITIVE = {"Доступы.txt", "auth.json", "config.toml", "credentials", ".env", ".netrc", "id_rsa", "id_ed25519"}


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def source_path(workspace: Path, value: str) -> Path:
    raw = Path(value)
    path = (workspace / raw).resolve()
    if not path.is_relative_to(workspace) or not path.is_file():
        raise ValueError(f"Источник должен быть файлом внутри workspace: {value}")
    relative = path.relative_to(workspace)
    if (EXCLUDED.intersection(relative.parts) or relative.parts[0] in {".private", ".codex", ".agents"}
            or path.name in SENSITIVE or path.name.startswith(".env") or path.suffix in {".key", ".pem"}
            or re.search(r"(?:^|[_.-])(draft|review|final|черновик|ревью)(?:[_.-]|$)", path.stem, re.I)):
        raise ValueError(f"Не первоисточник либо чувствительный путь: {value}")
    if path.stat().st_size > 20_000_000:
        raise ValueError("Источник превышает 20 MB")
    return path


def discover(workspace: Path, prompt: str) -> list[Path]:
    """Recognize literal workspace-relative file references, without following URLs."""
    candidates = re.findall(r"`([^`\n]+)`", prompt)
    candidates += re.findall(r"(?im)^\s*(?:Источник|Материал|Source):\s*(.+?)\s*$", prompt)
    candidates += re.findall(r"(?<![\w:/])((?:[\w.\-]+/)+[\w.\-]+\.(?:txt|md|pdf|docx|xlsx|png|jpg))", prompt)
    found = []
    for value in candidates:
        if "://" in value:
            continue
        if (workspace / value).is_file():
            path = source_path(workspace, value)
            if path not in found:
                found.append(path)
    return found


def repositories(workspace: Path) -> dict[str, str]:
    roots = workspace / ".private/repositories"
    paths = [roots / name for name in ("bi_react", "bi-forms", "modusbi")]
    plugins = roots / "plugins"
    if plugins.is_dir():
        paths += sorted(p for p in plugins.iterdir() if p.is_dir() and (p / ".git").exists())
    versions = {}
    for path in paths:
        if not (path / ".git").exists():
            continue
        revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True)
        if dirty:
            raise ValueError(f"Незакоммиченные изменения исходников: {path.name}; требуется проверяемая версия")
        versions[str(path.relative_to(workspace))] = revision
    return versions


def verify_evidence(workspace: Path, items, manifest: dict) -> None:
    for item in items:
        path = (workspace / item.path).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            raise ValueError("В свидетельстве отсутствует доступный файл")
        key = str(path.relative_to(workspace))
        expected = manifest["sources"].get(key)
        for root, revision in manifest["repositories"].items():
            if path.is_relative_to(workspace / root):
                relative = path.relative_to(workspace / root)
                if ".git" in relative.parts or path.name in SENSITIVE or path.name.startswith(".env"):
                    raise ValueError("Чувствительное свидетельство")
                expected = revision
        if expected is None or item.revision != expected:
            raise ValueError(f"Свидетельство вне входного набора или неверная версия: {key}")
