"""Command line interface; no raw provider errors or private responses on stdout."""
import argparse
import contextlib
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from .access import ArtifactGateway
from .inputs import digest, discover, repositories, source_path
from .pipeline import Pipeline, State, load_state, save

WORKSPACE = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def locked(gateway):
    parent, name = gateway._parent("run.lock", create=True)
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ValueError("Этот прогон уже выполняется другим координатором") from None
    finally:
        os.close(fd)


def run_root(workspace, value):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
        raise ValueError("Недопустимый run-id")
    return workspace / ".private/runtime" / value


def summary(state, root):
    return {"run_id": state.run_id, "stage": state.stage, "review_cycles": state.cycles,
            "version": state.version, "result": str(root / state.result) if state.result else None,
            "review_report": str(root / f"reviews/v{state.version}.md") if state.reviews and state.reviews[-1].reviewed_version == state.version else None,
            "questions_file": str(root / f"questions/v{state.version}-{len(state.answers)}-{state.cycles}.md") if state.questions else None,
            "questions_count": len(state.questions), "error_type": state.error or None}


def execute(workspace, root, args, state, answer=None, corporate=True):
    from .sdk_adapter import SDKBackend
    gateway = ArtifactGateway(workspace, root)
    with locked(gateway):
        if gateway.path("state.json").exists():
            state = load_state(gateway)
        else:
            save(gateway, state)
        if state.stage in {"APPROVED", "INCOMPLETE", "NEEDS_USER", "CANCELLED"} and answer is None:
            print(json.dumps(summary(state, root), ensure_ascii=False, indent=2))
            return 0 if state.stage == "APPROVED" else 2
        sources = [workspace / key for key in state.sources]
        with SDKBackend(workspace, root, sources, model=args.model, timeout=args.timeout,
                        include_corporate=corporate) as backend:
            pipeline = Pipeline(workspace, gateway, backend,
                                progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
            result = pipeline.answer(state, answer) if answer is not None else pipeline.run(state)
        print(json.dumps(summary(result, root), ensure_ascii=False, indent=2))
        return 0 if result.stage == "APPROVED" else 2


def create_state(workspace, run_id, prompt, sources):
    if not prompt.strip() or len(prompt.encode()) > 150000:
        raise ValueError("Промпт пустой либо превышает 150 KB")
    sources = list(dict.fromkeys(sources + discover(workspace, prompt)))
    if not sources:
        raise ValueError("Нужен хотя бы один исходный файл (--source либо --prompt)")
    return State(run_id=run_id, prompt=prompt,
                 sources={str(p.relative_to(workspace)): digest(p) for p in sources},
                 repositories=repositories(workspace))


def require_corporate(args):
    if not args.company_approved:
        raise ValueError("Для боевого запуска нужен --company-approved: подтверждение права передачи корпоративных материалов провайдеру модели. Для проверки без корпоративных данных используйте smoke.")


def doctor(workspace):
    import inspect
    import tomllib
    from openai_codex import Codex, Sandbox, Thread
    checks = {"python": sys.version.split()[0], "openai-codex": importlib.metadata.version("openai-codex"),
              "thread_start": "developer_instructions" in inspect.signature(Codex.thread_start).parameters,
              "thread_resume": "sandbox" in inspect.signature(Codex.thread_resume).parameters,
              "structured_output": "output_schema" in inspect.signature(Thread.run).parameters,
              "read_only": str(Sandbox.read_only), "bwrap": shutil.which("bwrap")}
    for role in ("analyst", "reviewer"):
        data = tomllib.loads((workspace / f".codex/agents/{role}.toml").read_text())
        checks[role] = all(data.get(key) for key in ("name", "description", "developer_instructions"))
    checks["repository_count"] = len(repositories(workspace))
    tracked = subprocess.check_output(["git", "-C", str(workspace), "ls-files", "--", ".private/", "Доступы.txt"], text=True)
    checks["private_index_empty"] = not tracked.strip()
    checks["private_ignored"] = subprocess.run(["git", "-C", str(workspace), "check-ignore", "-q", ".private/runtime/probe.json"]).returncode == 0
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    required = ("thread_start", "thread_resume", "structured_output", "analyst", "reviewer", "bwrap", "private_index_empty", "private_ignored")
    return 0 if all(checks[k] for k in required) else 1


def wiki_check(workspace):
    index = json.loads((workspace / ".private/knowledge/sources.json").read_text())
    stale = []
    for source in index["sources"]:
        repo = index["repositories"][source["repository"]]
        root = Path(repo["root"])
        if not root.is_absolute():
            root = workspace / root
        root = root.resolve()
        if not root.is_relative_to((workspace / ".private/repositories").resolve()):
            raise ValueError("Wiki указывает источник вне репозиториев")
        path = (root / source["relative_path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Неверный путь в Wiki")
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        if not path.is_file() or digest(path).removeprefix("sha256:") != source["file_sha256"] or head != source["commit"]:
            stale.append(source["id"])
    print(json.dumps({"sources": len(index["sources"]), "stale_source_ids": stale}, ensure_ascii=False))
    return 1 if stale else 0


def account_check(workspace, args):
    from .sdk_adapter import SDKBackend
    root = workspace / ".private/runtime" / ("account-check-" + uuid.uuid4().hex[:12])
    with SDKBackend(workspace, root, include_corporate=False, timeout=args.timeout) as backend:
        authenticated = backend.check_account()
    print(json.dumps({"authenticated": authenticated, "model_called": False}))
    return 0 if authenticated else 1


def smoke(workspace, args):
    root = workspace / ".private/runtime" / ("smoke-" + uuid.uuid4().hex[:12])
    synthetic = root / "workspace"
    synthetic.mkdir(parents=True, mode=0o700)
    for name in ("AGENTS.md", ".codex/agents", ".agents/skills", "agent-system/standards"):
        source, target = workspace / name, synthetic / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
    topic = synthetic / "example"
    topic.mkdir()
    (topic / "catalog.py").write_text('def find(values, query):\n    return [value for value in values if query in value]\n')
    prompt = ("Синтетическая задача: в example/catalog.py поиск списка строк сейчас чувствителен к регистру. "
              "Требуется регистронезависимый поиск по подстроке через casefold с сохранением порядка и дублей, "
              "без изменения входного списка. Пустой запрос возвращает все значения. "
              "Типы входов остаются list[str] и str; UI, HTTP, SQL и изменение обработки других типов вне рамок. "
              "Напиши компактную постановку на русском в Jira wiki с текущим/целевым поведением, "
              "требованиями, ссылкой на материал и проверяемыми AC. Вопросов для этой задачи нет. "
              "Это проверка координатора; не изменяй исходник и не выдумывай интеграции.")
    (topic / "prompt.txt").write_text(prompt)
    state = create_state(synthetic, "synthetic", prompt, [topic / "prompt.txt", topic / "catalog.py"])
    return execute(synthetic, run_root(synthetic, "synthetic"), args, state, corporate=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Независимые аналитик и ревьюер через официальный Codex SDK")
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--model", default=None, help="Доступная модель Codex; по умолчанию SDK")
    parser.add_argument("--timeout", type=int, default=900)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Новая постановка")
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="Путь к *.txt от workspace")
    group.add_argument("--stdin", action="store_true", help="Промпт из stdin; нужен --source")
    run.add_argument("--source", action="append", default=[])
    run.add_argument("--company-approved", action="store_true")
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--answer-file", type=Path)
    resume.add_argument("--continue-cancelled", action="store_true")
    resume.add_argument("--company-approved", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--show-questions", action="store_true", help="Показать вопросы в терминале")
    commands.add_parser("finalize").add_argument("run_id")
    export = commands.add_parser("export", help="Экспорт проверенного для публикации итогового текста")
    export.add_argument("run_id")
    export.add_argument("--to", required=True, help="Путь *.txt внутри исходной тематической папки")
    export.add_argument("--reviewed-for-publication", action="store_true")
    commands.add_parser("doctor")
    commands.add_parser("wiki-check")
    commands.add_parser("account-check", help="Проверка маршрутизации аккаунта без модельного запроса")
    commands.add_parser("smoke", help="Реальный SDK, только синтетические вводные")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    try:
        if args.timeout <= 0:
            raise ValueError("timeout должен быть положительным")
        if args.command == "doctor":
            return doctor(workspace)
        if args.command == "wiki-check":
            return wiki_check(workspace)
        if args.command == "account-check":
            return account_check(workspace, args)
        if args.command == "smoke":
            return smoke(workspace, args)
        if args.command == "run":
            require_corporate(args)
            sources = [source_path(workspace, v) for v in args.source]
            if args.prompt:
                path = source_path(workspace, args.prompt)
                if path.suffix != ".txt":
                    raise ValueError("Вводный промпт должен быть *.txt")
                prompt = path.read_text()
                sources.insert(0, path)
            else:
                prompt = sys.stdin.read(150001)
            run_id = "task-" + uuid.uuid4().hex[:16]
            state = create_state(workspace, run_id, prompt, sources)
            return execute(workspace, run_root(workspace, run_id), args, state)
        root = run_root(workspace, args.run_id)
        if not (root / "state.json").is_file():
            raise ValueError("Прогон не найден")
        gateway = ArtifactGateway(workspace, root)
        if args.command == "status":
            state = load_state(gateway)
            payload = summary(state, root)
            if args.show_questions:
                payload["questions"] = state.questions
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export":
            from .exporting import export_result
            with locked(gateway):
                state = load_state(gateway)
                path = export_result(workspace, gateway, state, args.to,
                                     reviewed_for_publication=args.reviewed_for_publication)
            print(json.dumps({"run_id": state.run_id, "stage": state.stage,
                              "exported": str(path)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "finalize":
            with locked(gateway):
                state = load_state(gateway)
                if state.stage != "NEEDS_USER":
                    raise ValueError("finalize доступен только при ожидании пользователя")
                Pipeline(workspace, gateway, None)._finish(state, "INCOMPLETE")
            print(json.dumps(summary(state, root), ensure_ascii=False, indent=2))
            return 2
        require_corporate(args)
        state = load_state(gateway)
        if state.stage == "CANCELLED" and args.continue_cancelled:
            with locked(gateway):
                state = load_state(gateway)
                state.stage = "FAILED"
                save(gateway, state)
        answer = args.answer_file.read_text() if args.answer_file else None
        return execute(workspace, root, args, state, answer)
    except KeyboardInterrupt:
        print("Прервано; используйте status/resume.", file=sys.stderr)
        return 130
    except Exception as exc:
        from .sdk_adapter import BackendError
        safe = str(exc) if type(exc) is ValueError or isinstance(exc, BackendError) else type(exc).__name__
        print("Ошибка: " + safe, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
