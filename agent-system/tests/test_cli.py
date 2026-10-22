"""CLI tests use only synthetic material and never start a provider session."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.access import ArtifactGateway
from orchestrator.inputs import MAX_PROMPT_BYTES, digest
from orchestrator.main import main
from orchestrator.models import Evidence, Research
from orchestrator.pipeline import State, load_state, save


FINAL = """h3. Требования.
Синтетическое поле сохраняет введённую строку при пустом результате поиска.
----
h3. Критерии приёмки (AC).
AC.1 — *Пустой результат:*
* *Дано:* введена строка;
* *Когда:* поиск вернул пустой список;
* *Тогда:* строка сохранена, список пуст.
"""


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.topic = self.workspace / "topic"
        self.topic.mkdir()
        self.prompt = self.topic / "prompt.txt"
        self.prompt.write_text("Синтетическая задача: сохранить строку.")
        self.source = self.topic / "material.md"
        self.source.write_text("Исходные пользовательские требования синтетического примера.")
        self.repo = self.workspace / ".private/repositories/bi_react"
        self.repo.mkdir(parents=True)
        (self.repo / "source.py").write_text("unchanged")
        self.run_id = "task-test"
        self.root = self.workspace / ".private/runtime" / self.run_id
        self.gateway = ArtifactGateway(self.workspace, self.root)
        evidence = Evidence(path="topic/material.md", symbol="", revision=digest(self.source),
                            observation="Требование пользователя")
        self.state = State(
            run_id=self.run_id, stage="APPROVED", prompt="Синтетическая задача",
            sources={"topic/material.md": digest(self.source)}, repositories={},
            research=Research(summary="Синтетическое исследование", requirements=["Сохранить ввод"],
                              scenarios=["Пустой список"], evidence=[evidence], questions=[]),
            version=1, cycles=1, result="results/v1-approved.jira.txt",
        )
        self.gateway.write_text(self.state.result, FINAL)
        self.gateway.write_text("reviews/private-report.md", "SYNTHETIC PRIVATE REVIEW")
        save(self.gateway, self.state)

    def tearDown(self):
        self.gateway.close()
        self.temp.cleanup()

    def invoke(self, *args, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("sys.stdin", io.StringIO(stdin)):
            code = main(["--workspace", str(self.workspace), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def export(self, target="topic/result.txt", reviewed=True):
        args = ["export", self.run_id, "--to", target]
        if reviewed:
            args.append("--reviewed-for-publication")
        return self.invoke(*args)

    def test_export_copies_only_final_text_after_explicit_publication_review(self):
        code, stdout, stderr = self.export()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual((self.topic / "result.txt").read_text(), FINAL)
        self.assertEqual(json.loads(stdout)["exported"], str(self.topic / "result.txt"))
        self.assertNotIn("Синтетическое поле", stdout)
        self.assertEqual(sorted(path.name for path in self.topic.iterdir()),
                         ["material.md", "prompt.txt", "result.txt"])
        self.assertEqual((self.root / "reviews/private-report.md").read_text(), "SYNTHETIC PRIVATE REVIEW")

    def test_export_requires_publication_review_flag(self):
        code, _, stderr = self.export(reviewed=False)
        self.assertEqual(code, 1)
        self.assertIn("--reviewed-for-publication", stderr)
        self.assertFalse((self.topic / "result.txt").exists())

    def test_export_does_not_overwrite_and_cleans_temporary_file(self):
        target = self.topic / "result.txt"
        target.write_text("keep existing task")
        self.assertEqual(self.export()[0], 1)
        self.assertEqual(target.read_text(), "keep existing task")
        self.assertFalse(list(self.topic.glob(".export-pending-*")))

    def test_export_refuses_traversal_and_project_or_unrelated_directories(self):
        (self.workspace / "other").mkdir()
        (self.workspace / "agent-system").mkdir()
        for target in ("../escaped.txt", "topic/../escaped.txt", "topic//result.txt",
                       "topic/./result.txt", "topic\\result.txt", "/tmp/escaped.txt",
                       ".private/repositories/bi_react/new.txt", "agent-system/result.txt",
                       "other/result.txt", "result.txt", "topic/.hidden/result.txt",
                       "topic/result.md"):
            with self.subTest(target=target):
                self.assertEqual(self.export(target)[0], 1)
        self.assertEqual((self.repo / "source.py").read_text(), "unchanged")
        self.assertFalse((self.workspace / "escaped.txt").exists())
        self.assertFalse((self.workspace / "other/result.txt").exists())

    def test_project_code_cannot_be_allowed_by_a_corrupt_source_inventory(self):
        (self.workspace / "agent-system").mkdir()
        self.state.sources["agent-system/main.py"] = "sha256:synthetic"
        save(self.gateway, self.state)
        self.assertEqual(self.export("agent-system/result.txt")[0], 1)
        self.assertFalse((self.workspace / "agent-system/result.txt").exists())

    def test_export_refuses_destination_file_and_parent_symlinks(self):
        victim = self.repo / "source.py"
        (self.topic / "result.txt").symlink_to(victim)
        (self.topic / "linked").symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.export()[0], 1)
        self.assertEqual(self.export("topic/linked/result.txt")[0], 1)
        # A symlink back into the same topic is forbidden as well.
        (self.topic / "inside").symlink_to(self.topic, target_is_directory=True)
        self.assertEqual(self.export("topic/inside/second.txt")[0], 1)
        self.assertEqual(victim.read_text(), "unchanged")
        self.assertFalse((self.repo / "result.txt").exists())

    def test_export_refuses_a_symlink_replacing_the_final_artifact(self):
        final = self.root / self.state.result
        final.unlink()
        final.symlink_to(self.repo / "source.py")
        self.assertEqual(self.export()[0], 1)
        self.assertFalse((self.topic / "result.txt").exists())

    def test_export_refuses_a_hardlink_replacing_the_final_artifact(self):
        final = self.root / self.state.result
        final.unlink()
        os.link(self.repo / "source.py", final)
        self.assertEqual(self.export()[0], 1)
        self.assertFalse((self.topic / "result.txt").exists())

    def test_export_cannot_publish_state_or_review_by_tampering_with_result(self):
        for result in ("state.json", "reviews/private-report.md", "../source.py",
                       "results/v0-approved.jira.txt", "results/private.jira.txt"):
            with self.subTest(result=result):
                self.state.result = result
                save(self.gateway, self.state)
                self.assertEqual(self.export()[0], 1)
                self.assertFalse((self.topic / "result.txt").exists())

    def test_parent_symlink_swap_between_validation_and_open_is_refused(self):
        safe = self.topic / "safe"
        safe.mkdir()
        original_open = os.open
        swapped = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "safe" and not swapped:
                swapped = True
                safe.rename(self.topic / "original-safe")
                safe.symlink_to(self.repo, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("orchestrator.access.os.open", racing_open):
            self.assertEqual(self.export("topic/safe/result.txt")[0], 1)
        self.assertTrue(swapped)
        self.assertFalse((self.repo / "result.txt").exists())

    def test_target_created_during_export_is_never_replaced(self):
        original_link = os.link

        def racing_link(source, target, *args, **kwargs):
            if target == "result.txt":
                (self.topic / "result.txt").write_text("concurrent task")
            return original_link(source, target, *args, **kwargs)

        with patch("orchestrator.exporting.os.link", racing_link):
            self.assertEqual(self.export()[0], 1)
        self.assertEqual((self.topic / "result.txt").read_text(), "concurrent task")
        self.assertFalse(list(self.topic.glob(".export-pending-*")))

    def test_unfinished_run_has_no_exportable_final_document(self):
        self.state.stage = "DRAFT"
        self.state.result = ""
        save(self.gateway, self.state)
        self.assertEqual(self.export()[0], 1)

    def test_incomplete_document_keeps_open_questions_when_explicitly_exported(self):
        self.state.stage = "INCOMPLETE"
        self.state.questions = ["Как обрабатывать недоступность сервера?"]
        self.state.result = "results/v1-incomplete.jira.txt"
        final = FINAL + "\nh3. Открытые вопросы.\n* " + self.state.questions[0] + "\n"
        self.gateway.write_text(self.state.result, final)
        save(self.gateway, self.state)
        code, stdout, _ = self.export()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["stage"], "INCOMPLETE")
        self.assertEqual((self.topic / "result.txt").read_text(), final)

    def test_status_prints_question_text_only_when_explicitly_requested(self):
        self.state.stage = "NEEDS_USER"
        question = "Синтетический вопрос: нужно ли сохранять пробелы?"
        self.state.questions = [question]
        save(self.gateway, self.state)
        code, stdout, _ = self.invoke("status", self.run_id)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["questions_count"], 1)
        self.assertNotIn(question, stdout)
        code, stdout, _ = self.invoke("status", self.run_id, "--show-questions")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["questions"], [question])

    def waiting_research(self):
        self.state.stage = "NEEDS_USER"
        self.state.version = self.state.cycles = 0
        self.state.result = ""
        self.state.threads = {"reviewer": "saved-independent-reviewer"}
        self.state.wait_reason = "clarification"
        self.state.questions = self.state.research.questions = ["Inspect the available source first?"]
        save(self.gateway, self.state)
        role = self.workspace / ".codex/agents/reviewer.toml"
        role.parent.mkdir(parents=True)
        role.write_text('developer_instructions = "Synthetic independent reviewer."\n')
        standards = self.workspace / "agent-system/standards"
        standards.mkdir(parents=True)
        for name in ("jira-style.md", "requirements-quality.md"):
            (standards / name).write_text("Synthetic requirements standard.")

    def test_resume_continue_research_preserves_thread_without_fabricating_an_answer(self):
        self.waiting_research()
        updated = self.state.research.model_copy(deep=True)
        updated.questions = ["A remaining product choice?"]
        backend = Mock()
        backend.run.return_value = updated.model_dump()
        with patch("orchestrator.sdk_adapter.SDKBackend") as factory:
            factory.return_value.__enter__.return_value = backend
            code, stdout, stderr = self.invoke("resume", self.run_id, "--continue-research", "--company-approved")
        self.assertEqual(code, 2, stderr)
        summary = json.loads(stdout)
        self.assertEqual((summary["stage"], summary["research_round"], summary["review_cycles"]),
                         ("NEEDS_USER", 1, 0))
        self.assertTrue(Path(summary["questions_file"]).is_file())
        self.assertIn("A remaining product choice?", Path(summary["questions_file"]).read_text())
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["answers"], [])
        self.assertEqual(state["threads"], {"reviewer": "saved-independent-reviewer"})
        self.assertIsNone(state["draft"])
        backend.start.assert_not_called()
        self.assertEqual(backend.resume.call_args.args[:2], ("reviewer", "saved-independent-reviewer"))
        self.assertEqual(backend.run.call_args.args[0], "reviewer")

    def test_continue_research_refuses_ineligible_state_before_starting_sdk(self):
        with patch("orchestrator.sdk_adapter.SDKBackend") as factory:
            code, _, stderr = self.invoke("resume", self.run_id, "--continue-research", "--company-approved")
            self.assertEqual(code, 1)
            self.assertIn("до запуска аналитика", stderr)
            factory.assert_not_called()
        self.waiting_research()
        for update in ({"stage": "FAILED", "retry_stage": "BLIND"},
                       {"threads": {"reviewer": "saved-independent-reviewer", "analyst": "already-created"}},
                       {"research_round": 2}):
            state = self.state.model_copy(update=update, deep=True)
            save(self.gateway, state)
            with self.subTest(update=update), patch("orchestrator.sdk_adapter.SDKBackend") as factory:
                self.assertEqual(self.invoke("resume", self.run_id, "--continue-research", "--company-approved")[0], 1)
                factory.assert_not_called()

    def test_continue_research_and_user_answer_are_mutually_exclusive(self):
        with patch("orchestrator.sdk_adapter.SDKBackend") as factory, self.assertRaises(SystemExit) as caught:
            self.invoke("resume", self.run_id, "--continue-research", "--answer-file", "unused.txt",
                        "--company-approved")
        self.assertEqual(caught.exception.code, 2)
        factory.assert_not_called()

    def test_continue_research_requires_corporate_approval_and_refuses_cancelled_flag(self):
        self.waiting_research()
        checkpoint = (self.root / "state.json").read_bytes()
        with patch("orchestrator.sdk_adapter.SDKBackend") as factory:
            self.assertEqual(self.invoke("resume", self.run_id, "--continue-research")[0], 1)
            self.assertEqual(self.invoke("resume", self.run_id, "--continue-research", "--continue-cancelled",
                                         "--company-approved")[0], 1)
            factory.assert_not_called()
        self.assertEqual((self.root / "state.json").read_bytes(), checkpoint)

    def test_run_accepts_prompt_file_without_starting_real_sdk(self):
        with patch("orchestrator.main.execute", return_value=0) as execute:
            self.assertEqual(self.invoke("run", "--prompt", "topic/prompt.txt", "--company-approved")[0], 0)
        state = execute.call_args.args[3]
        self.assertEqual(state.prompt, self.prompt.read_text())
        self.assertIn("topic/prompt.txt", state.sources)

    def test_stdin_preserves_prompt_and_uses_explicit_material(self):
        prompt = "Синтетическое требование.\nСохранить исходный порядок.\n"
        with patch("orchestrator.main.execute", return_value=0) as execute:
            code, _, _ = self.invoke("run", "--stdin", "--source", "topic/material.md",
                                     "--company-approved", stdin=prompt)
        self.assertEqual(code, 0)
        state = execute.call_args.args[3]
        self.assertEqual(state.prompt, prompt)
        self.assertEqual(list(state.sources), ["topic/material.md"])

    def test_stdin_without_sources_empty_or_oversized_prompt_fails_before_sdk(self):
        with patch("orchestrator.main.execute") as execute:
            self.assertEqual(self.invoke("run", "--stdin", "--company-approved", stdin="Task")[0], 1)
            for prompt in ("  ", "x" * (MAX_PROMPT_BYTES + 1), "Я" * (MAX_PROMPT_BYTES // 2 + 1)):
                self.assertEqual(self.invoke("run", "--stdin", "--source", "topic/material.md",
                                             "--company-approved", stdin=prompt)[0], 1)
            execute.assert_not_called()

    def test_full_size_russian_prompt_is_saved_unchanged_from_file_or_stdin(self):
        prompt = "Я" * (MAX_PROMPT_BYTES // 2)
        self.assertEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)
        self.assertGreater(len(prompt.encode("utf-8")), 150000)
        self.prompt.write_bytes(prompt.encode("utf-8"))

        def persist(workspace, root, args, state):
            with ArtifactGateway(workspace, root) as gateway:
                save(gateway, state)
                self.assertEqual(load_state(gateway).prompt, prompt)
            return 0

        for args in (("--prompt", "topic/prompt.txt"), ("--stdin", "--source", "topic/material.md")):
            with self.subTest(args=args), patch("orchestrator.main.execute", side_effect=persist) as execute:
                code, _, stderr = self.invoke("run", *args, "--company-approved", stdin=prompt)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(execute.call_args.args[3].prompt, prompt)

    def test_binary_stdin_preserves_exact_utf8_text_and_rejects_oversize_before_sdk(self):
        prompt = "Синтетическое требование.\r\n" + "Я" * 100000
        for value, expected in ((prompt.encode("utf-8"), 0),
                                (("Я" * (MAX_PROMPT_BYTES // 2 + 1)).encode("utf-8"), 1)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(expected=expected), io.TextIOWrapper(io.BytesIO(value), encoding="utf-8") as stream, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                    patch("sys.stdin", stream), patch("orchestrator.main.execute", return_value=0) as execute:
                code = main(["--workspace", str(self.workspace), "run", "--stdin", "--source",
                             "topic/material.md", "--company-approved"])
                self.assertEqual(code, expected, stderr.getvalue())
                if expected == 0:
                    self.assertEqual(execute.call_args.args[3].prompt, prompt)
                else:
                    execute.assert_not_called()

    def test_oversized_prompt_file_fails_before_sdk(self):
        self.prompt.write_text("Я" * (MAX_PROMPT_BYTES // 2 + 1))
        with patch("orchestrator.main.execute") as execute:
            self.assertEqual(self.invoke("run", "--prompt", "topic/prompt.txt", "--company-approved")[0], 1)
            execute.assert_not_called()

    def test_repeatable_analyst_sources_override_autodiscovered_and_explicit_sources(self):
        context = self.topic / "previous_context.txt"
        second = self.topic / "additional_context.txt"
        context.write_text("Synthetic previous context.")
        second.write_text("Synthetic additional context.")
        self.prompt.write_text("Use `topic/material.md` and `topic/previous_context.txt`.")
        with patch("orchestrator.main.execute", return_value=0) as execute:
            code, _, stderr = self.invoke("run", "--prompt", "topic/prompt.txt", "--source",
                "topic/previous_context.txt", "--analyst-source", "topic/previous_context.txt",
                "--analyst-source", "topic/additional_context.txt", "--company-approved")
        self.assertEqual(code, 0, stderr)
        state = execute.call_args.args[3]
        self.assertEqual(state.analyst_sources, ["topic/previous_context.txt", "topic/additional_context.txt"])
        for path in (self.prompt, self.source, context, second):
            self.assertEqual(state.sources[str(path.relative_to(self.workspace))], digest(path))

    def test_analyst_source_cannot_hide_prompt_or_expand_sensitive_input_access(self):
        for relative in ("topic/prompt.txt", "topic/Доступы.txt", "topic/draft.txt", ".private/context.txt"):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("Synthetic forbidden context.")
            with self.subTest(relative=relative), patch("orchestrator.main.execute") as execute:
                self.assertEqual(self.invoke("run", "--prompt", "topic/prompt.txt", "--analyst-source",
                                             relative, "--company-approved")[0], 1)
                execute.assert_not_called()

    def test_corporate_run_requires_authorization_and_rejects_sensitive_sources(self):
        with patch("orchestrator.main.execute") as execute:
            self.assertEqual(self.invoke("run", "--prompt", "topic/prompt.txt")[0], 1)
            (self.topic / "Доступы.txt").write_text("SYNTHETIC SECRET")
            code, stdout, stderr = self.invoke("run", "--stdin", "--source", "topic/Доступы.txt",
                                               "--company-approved", stdin="Синтетический ввод")
            self.assertEqual(code, 1)
            self.assertNotIn("SYNTHETIC SECRET", stdout + stderr)
            execute.assert_not_called()

    def test_non_text_prompt_and_invalid_run_id_fail(self):
        with patch("orchestrator.main.execute") as execute:
            self.assertEqual(self.invoke("run", "--prompt", "topic/material.md", "--company-approved")[0], 1)
            self.assertEqual(self.invoke("status", "../other")[0], 1)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
