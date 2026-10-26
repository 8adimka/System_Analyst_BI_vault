"""Opt-in real-SDK evaluation with synthetic sources only; no general quality guarantee.

Run from workspace: agent-system/.venv/bin/python agent-system/evals/readability.py --real-sdk
Each invocation keeps a new private run; ordinary imports and --help call no model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "agent-system"))
from orchestrator.access import ArtifactGateway
from orchestrator.inputs import digest, verify_evidence
from orchestrator.jira import validate_jira
from orchestrator.models import Draft, Research, Review, pending_readability, unresolved
from orchestrator.pipeline import State, base_prompt, instructions, save
from orchestrator.sdk_adapter import SDKBackend

SKILLS = ("codebase-investigator", "requirements-clarifier", "jira-task-writer",
          "task-reviser", "consistency-reviewer", "evidence-reviewer")
SOURCE = """Синтетические функциональные требования, не описание корпоративного продукта.
В карточке отчёта есть «Панель параметров» и «Панель результатов».
Настройки Панели результатов сохраняются для текущего пользователя и доступны ему
при повторном открытии карточки. Для Панели параметров сохранение настроек не задано.
После успешной загрузки отчёта карточка показывает текущий отчёт.
При ошибке загрузки остаётся последний успешно загруженный отчёт текущего пользователя.
Панель справки содержит описание полей. Кнопка «Справка» открывает Панель справки,
повторное нажатие закрывает её. Это единственная панель в разделе справки.
Нужно описать только это поведение, без API, технологии хранения и иных доработок.
Материал полностью определяет задачу; исходников приложения и внешних ссылок нет.
"""
SEEDED = """h3. Требуемое поведение.
В карточке отчёта доступны Панель параметров и Панель результатов.
* Настройки панели сохраняются для текущего пользователя при повторном открытии карточки.
* После успешной загрузки отчёта отображается текущий успешно загруженный отчёт.
* При ошибке загрузки сохраняется последний успешно загруженный отчёт текущего пользователя.

----
h3. Справка.
Панель справки содержит описание полей. Панель открывается кнопкой «Справка»
и закрывается повторным нажатием этой кнопки.

----
h3. Критерии приёмки.
AC.1 — *Настройки:*
* *Дано:* в карточке есть Панель параметров и Панель результатов;
* *Когда:* текущий пользователь меняет настройки панели и повторно открывает карточку;
* *Тогда:* его настройки сохранены и доступны ему.
AC.2 — *Успешная загрузка:*
* *Дано:* карточка отчёта открыта;
* *Когда:* загрузка отчёта успешна;
* *Тогда:* отображается текущий отчёт.
AC.3 — *Ошибка загрузки:*
* *Дано:* отчёт текущего пользователя был успешно загружен;
* *Когда:* следующая загрузка завершается ошибкой;
* *Тогда:* остаётся последний успешно загруженный отчёт этого пользователя.
AC.4 — *Справка:*
* *Дано:* Панель справки закрыта;
* *Когда:* пользователь нажимает «Справка», затем нажимает её повторно;
* *Тогда:* описание полей сначала открывается, затем скрывается.
"""


class EvaluationFailure(ValueError):
    """Only fixed evaluation check names may appear in public summaries."""


def require(condition, name):
    if not condition:
        raise EvaluationFailure(name)


def evaluate(args, root):
    files = [".codex/agents/analyst.toml", ".codex/agents/reviewer.toml",
             "agent-system/runtime/AGENTS.md"]
    files += [f"agent-system/standards/{name}.md" for name in
              ("jira-style", "requirements-quality", "review-protocol")]
    files += [f".agents/skills/{name}/SKILL.md" for name in SKILLS]
    with ArtifactGateway(PROJECT, root) as setup:
        for name in files:
            source = PROJECT / name
            require(source == source.resolve() and source.is_file(), "unsafe-instruction-source")
            setup.write_text("workspace/" + name, source.read_text())
        setup.write_text("workspace/example/requirements.txt", SOURCE)
    workspace = root / "workspace"
    source = workspace / "example/requirements.txt"
    run = workspace / ".private/runtime/evaluation"
    state = State(run_id="evaluation", sources={"example/requirements.txt": digest(source)},
                  repositories={}, prompt="По example/requirements.txt подготовь компактную функциональную постановку Jira wiki. Это синтетическая задача; существенных открытых вопросов нет.")
    with ArtifactGateway(workspace, run) as gateway, SDKBackend(
            workspace, run, [source], include_corporate=False,
            model=args.model, timeout=args.timeout) as backend:
        def turn(role, name, prompt, cls):
            print(json.dumps({"operation": name, "run_root": str(root)}), flush=True)
            if role not in state.threads:
                state.threads[role] = backend.start(role, instructions(workspace, role))
                save(gateway, state)
            result = cls.model_validate(backend.run(role, prompt, cls.model_json_schema()))
            evidence = ([e for f in result.findings for e in f.evidence]
                        if isinstance(result, Review) else result.evidence)
            verify_evidence(workspace, evidence, state.model_dump())
            require(digest(source) == state.sources["example/requirements.txt"], "source-changed")
            gateway.write_json(f"responses/{name}.json", result.model_dump())
            require(not result.questions, name + "-unexpected-questions")
            if isinstance(result, Review):
                require(len({f.id for f in result.findings}) == len(result.findings), "duplicate-finding-id")
            return result

        # No draft has been created in the mounted workspace or sent to reviewer.
        state.research = turn("reviewer", "blind", base_prompt(state) +
                              "\nСамостоятельно исследуй исходный материал. Верни Research, не постановку.", Research)
        state.stage = "DRAFT"
        state.events.append("independent-research-fixed")
        save(gateway, state)
        evidence = {"path": "example/requirements.txt", "symbol": "synthetic requirements",
                    "revision": digest(source), "observation": "Источник определяет обе панели, настройки результата, успешную загрузку, сохранение предыдущего отчёта и справку."}
        state.draft = Draft(jira=SEEDED, evidence=[evidence], questions=[], change_log=[])
        state.version, state.stage = 1, "REVIEW"
        gateway.write_json("inputs/seeded-draft.json", state.draft.model_dump())
        save(gateway, state)

        def review_prompt():
            return base_prompt(state) + "\nПроверь Draft по своему независимому исследованию и разделу «Ясность и редактура». Сохраняй ID прежних замечаний и проверяй исправления. Верни Review.\n" + json.dumps(
                {"version": state.version, "draft": state.draft.model_dump(),
                 "previous_review": state.reviews[-1].model_dump() if state.reviews else None}, ensure_ascii=False)

        first = turn("reviewer", "seeded-review", review_prompt(), Review)
        require(first.reviewed_version == 1, "seeded-version-mismatch")
        require(any(f.category == "requirements" for f in unresolved(first)), "missing-substantive-ambiguity")
        require(bool(pending_readability(first)), "missing-editorial-finding")
        state.reviews, state.cycles, state.stage = [first], 1, "REVISION"
        save(gateway, state)
        prompt = base_prompt(state, "analyst") + "\nИсправь замечания локально по «Ясность и редактура», сохраняя смысл остальных требований и AC, допустимые короткие названия и значимые условия состояния. Верни полный Draft.\n" + json.dumps(
            {"draft": state.draft.model_dump(), "review": first.model_dump()}, ensure_ascii=False)
        state.draft = turn("analyst", "corrected-draft", prompt, Draft)
        require(not validate_jira(state.draft.jira), "corrected-jira-invalid")
        require(len(set(state.threads.values())) == 2, "threads-not-independent")
        state.version, state.stage = 2, "REVIEW"
        save(gateway, state)
        final = turn("reviewer", "final-review", review_prompt(), Review)
        require(final.reviewed_version == 2, "final-version-mismatch")
        require({f.id for f in first.findings} <= {f.id for f in final.findings}, "finding-id-lost")
        require(all(f.status == "closed" for f in final.findings), "uncorrected-findings")
        state.reviews.append(final)
        state.cycles, state.stage = 2, "APPROVED"
        save(gateway, state)
        gateway.write_text("results/corrected.jira.txt", state.draft.jira)
    return {"status": "PASS", "run_root": str(root), "turns": 4,
            "initial_findings": len(first.findings), "scope": "synthetic sample; not a general quality guarantee"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-sdk", action="store_true", required=True, help="Opt in to four real model turns")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    root = PROJECT / ".private/runtime" / ("readability-eval-" + uuid.uuid4().hex)
    require(not root.exists(), "run-already-exists")
    try:
        result = evaluate(args, root)
    except Exception as exc:
        result = {"status": "FAIL", "run_root": str(root), "error_type": type(exc).__name__}
        if isinstance(exc, EvaluationFailure):
            result["check"] = str(exc)
    with ArtifactGateway(PROJECT, root) as gateway:
        gateway.write_json("summary.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
