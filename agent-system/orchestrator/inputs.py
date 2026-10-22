"""Explicit primary-source inventory and version checks; never scan credentials."""
import hashlib
import re
import subprocess
from pathlib import Path


MAX_PROMPT_BYTES = 1_000_000


EXCLUDED = {".git", ".agent", ".venv", ".aws", ".ssh", ".gnupg", "node_modules", "__pycache__"}
SENSITIVE = {"Доступы.txt", "auth.json", "config.toml", "credentials", ".env", ".netrc", "id_rsa", "id_ed25519"}
FILE_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".csv",
                 ".json", ".toml", ".yaml", ".yml", ".key", ".pem", ".py", ".js", ".ts",
                 ".tsx", ".jsx", ".vue", ".html", ".css", ".sql", ".xml", ".ini", ".cfg"}
INPUT_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:Источник|Материал|Source):\s*(.*?)\s*$", re.I)
OUTPUT_LINE = re.compile(
    r"(?:\b(?:запиши(?:те)?|записать|сохрани(?:те)?|сохранить|write|save)\b(?=\s|:)"
    r"|^\s*(?:выходной|итоговый|результирующий)\s+(?:файл|документ)\s*:"
    r"|^\s*(?:Output|Результат)\s*:)", re.I,
)
# Bare references cannot contain spaces; use backticks or an input label for those.
# Include the leading /, ~/ or ../ in the match so a disallowed path can never be
# mistaken for an unrelated existing workspace-relative suffix.
BARE_REFERENCE = re.compile(
    r"(?<![\w~:/.-])((?:~/|/|\./|(?:\.\./)+)?(?:[\w().-]+/)*"
    r"[\w().-]+\.(?:txt|md|pdf|docx|xlsx|png|jpe?g|csv|json|toml|ya?ml|key|pem))(?=$|[^\w])", re.I,
)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_path(workspace: Path, value: str) -> Path:
    """Expand the current user's ~/ literally, then enforce the workspace boundary."""
    workspace = workspace.resolve()
    value = value.strip()
    if not value or "\x00" in value or (value.startswith("~") and not value.startswith("~/")):
        raise ValueError(f"Неоднозначный путь к источнику: {value}")
    raw = Path(value).expanduser()
    path = (workspace / raw).resolve()
    if not path.is_relative_to(workspace):
        raise ValueError(f"Источник должен быть файлом внутри workspace: {value}")
    relative = path.relative_to(workspace)
    if (EXCLUDED.intersection(relative.parts) or {".private", ".codex", ".agents"}.intersection(relative.parts)
            or path.name in SENSITIVE or path.name.startswith(".env") or path.suffix in {".key", ".pem"}
            or re.search(r"(?:^|[_.-])(draft|review|final|черновик|ревью)(?:[_.-]|$)", path.stem, re.I)):
        raise ValueError(f"Не первоисточник либо чувствительный путь: {value}")
    return path


def source_path(workspace: Path, value: str) -> Path:
    path = _normalized_path(workspace, value)
    if not path.is_file():
        raise ValueError(f"Источник должен быть файлом внутри workspace: {value}")
    if path.stat().st_size > 20_000_000:
        raise ValueError("Источник превышает 20 MB")
    return path


def _file_reference(workspace: Path, value: str) -> bool:
    """Optional backticks may name product routes or symbols such as WHERE/HAVING."""
    value = value.strip()
    path = Path(value)
    return (path.suffix.lower() in FILE_SUFFIXES or value.startswith(("~/", "./", "../"))
            or (path.is_absolute() and path.is_relative_to(workspace.resolve())))


def discover(workspace: Path, prompt: str) -> list[Path]:
    """Recognize explicit input paths, never scan a directory or guess a version.

    Input labels are mandatory references and fail if unresolved. Other quoted
    or bare mentions are added only if they exist; quoted code symbols and URLs
    are not inputs. Explicit output instructions do not inventory an old result.
    """
    candidates = []
    for line in prompt.splitlines():
        labelled = INPUT_LINE.match(line)
        if labelled:
            value = labelled.group(1).strip()
            if value.startswith("`") and value.endswith("`") and value.count("`") == 2:
                value = value[1:-1]
            candidates.append((value, True))
            continue
        # Words in a quoted filename are data, not output commands.
        command_text = re.sub(r"`[^`\n]+`", lambda match: " " * len(match.group()), line)
        output = OUTPUT_LINE.search(command_text)
        if output:
            # A sentence may name inputs before its output instruction. Keep
            # those references, while withholding the output clause (and any
            # existing previous result it names) from the blind-stage inputs.
            line = line[:output.start()]
        quoted = re.findall(r"`([^`\n]+)`", line)
        candidates += [(value, False) for value in quoted]
        # Do not reparse substrings of quoted paths (especially paths with spaces).
        unquoted = re.sub(r"`[^`\n]+`", " ", line)
        candidates += [(value, False) for value in BARE_REFERENCE.findall(unquoted)]
    found = []
    for value, required in candidates:
        if "://" in value:
            continue
        # A slash alone does not make /fvd, /api/datasets or WHERE/HAVING a file.
        # Explicit file prefixes and extensions still reach the strict boundary
        # check, including prohibited absolute or ~/ paths that do not exist.
        if not required and not _file_reference(workspace, value):
            continue
        path = _normalized_path(workspace, value)
        if required or path.exists():
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
