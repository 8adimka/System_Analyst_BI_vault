"""Synthetic integration checks for the coordinator state machine.

These tests never instantiate the Codex SDK or expose corporate material.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from orchestrator.access import ArtifactGateway
from orchestrator.inputs import digest
from orchestrator.models import Draft, Research, Review
from orchestrator.pipeline import Pipeline, State, load_state, save
from orchestrator.sdk_adapter import EmptyThreadError


JIRA = """h3. Требуемое поведение.
При снятии выбранного значения поле очищается и доступно для ввода.

----
h3. Критерии приёмки (AC).
AC.1 — *Снятие выбора:*
* *Дано:* значение выбрано;
* *Когда:* пользователь снимает выбор;
* *Тогда:* поле пустое и доступно для ввода.
"""


class FakeBackend:
    """Return scripted structured turns while recording role/session behavior."""

    def __init__(self, turns=(), *, on_start=None):
        self.turns = deque(turns)
        self.calls = []
        self.on_start = on_start
        self.attached = {}

    def start(self, role, instructions):
        self.calls.append(("start", role, instructions))
        if self.on_start:
            self.on_start(role)
        value = "synthetic-" + role
        self.attached[role] = value
        return value

    def resume(self, role, thread_id, instructions):
        self.calls.append(("resume", role, thread_id))
        self.attached[role] = thread_id

    def run(self, role, prompt, schema):
        self.calls.append(("run", role, prompt, schema["title"]))
        if role not in self.attached:
            raise AssertionError("A turn requires its own started/resumed session")
        if not self.turns:
            raise AssertionError("Unexpected model call")
        expected_role, expected_type, result = self.turns.popleft()
        if (role, schema["title"]) != (expected_role, expected_type):
            raise AssertionError(f"Unexpected turn: {(role, schema['title'])}")
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(role, prompt, schema)
        return deepcopy(result)


class EmptyResumeBackend(FakeBackend):
    """Model a started Thread ID whose first turn never created a rollout."""

    def __init__(self, turns=(), *, empty_role, missing_id):
        super().__init__(turns)
        self.empty_role = empty_role
        self.missing_id = missing_id

    def resume(self, role, thread_id, instructions):
        super().resume(role, thread_id, instructions)
        if (role, thread_id) == (self.empty_role, self.missing_id):
            raise EmptyThreadError("Synthetic thread has no persisted rollout")

    def start(self, role, instructions):
        super().start(role, instructions)
        replacement = "replacement-" + role
        self.attached[role] = replacement
        return replacement


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="modus-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.sources = {}
        for index in range(1, 7):
            path = self.workspace / f"topic/source-{index}.txt"
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"Synthetic requirement witness {index}.\n")
            self.sources[str(path.relative_to(self.workspace))] = digest(path)
        for role in ("analyst", "reviewer"):
            path = self.workspace / f".codex/agents/{role}.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'developer_instructions = "Synthetic {role} role."\n')
        standards = self.workspace / "agent-system/standards"
        standards.mkdir(parents=True)
        for name in ("jira-style.md", "requirements-quality.md"):
            (standards / name).write_text("Synthetic quality instructions.\n")
        self.gateway = ArtifactGateway(self.workspace, self.workspace / ".private/runtime/run")
        self.addCleanup(self.gateway.close)
        self.state = State(run_id="run", prompt="Describe synthetic field clearing.",
                           sources=self.sources, repositories={})
        save(self.gateway, self.state)

    def evidence(self, index=1):
        path = f"topic/source-{index}.txt"
        return {"path": path, "symbol": f"requirement-{index}", "revision": self.sources[path],
                "observation": f"Synthetic witness {index} establishes required clearing."}

    def research(self, *, questions=()):
        return {"summary": "INDEPENDENT_RESEARCH_ONLY", "requirements": ["Clear the field."],
                "scenarios": ["Clear a populated field."], "evidence": [self.evidence()],
                "questions": list(questions)}

    def draft(self, *, questions=(), text=JIRA):
        return {"jira": text, "evidence": [self.evidence()], "questions": list(questions),
                "change_log": ["Synthetic changes address the current input."]}

    def finding(self, *, index=1, status="open", finding_id="F1"):
        return {"id": finding_id, "severity": "major", "blocking": True,
                "claim": "Required clearing needs an explicit precondition.",
                "evidence": [self.evidence(index)], "impact": "Acceptance is ambiguous.",
                "requested_change": "Specify the populated field before clearing.",
                "status": status, "draft_anchor": "AC.1"}

    def review(self, version, *, findings=(), questions=()):
        return {"reviewed_version": version, "findings": list(findings), "questions": list(questions)}

    def turns(self, *reviews):
        result = [("reviewer", "Research", self.research())]
        for review in reviews:
            result += [("analyst", "Draft", self.draft()), ("reviewer", "Review", review)]
        return result

    def run_pipeline(self, backend, state=None):
        return Pipeline(self.workspace, self.gateway, backend).run(state or self.state)

    def assert_failed_checkpoint(self, retry_stage):
        state = load_state(self.gateway)
        self.assertEqual(state.stage, "FAILED")
        self.assertEqual(state.retry_stage, retry_stage)
        return state

    def test_blind_research_is_durable_before_analyst_creation_and_hidden_from_analyst(self):
        def observe_start(role):
            if role == "analyst":
                checkpoint = load_state(self.gateway)
                self.assertEqual(checkpoint.stage, "DRAFT")
                self.assertEqual(checkpoint.research.summary, "INDEPENDENT_RESEARCH_ONLY")
                self.assertIsNone(checkpoint.draft)
                self.assertNotIn("analyst", checkpoint.threads)
                responses = list(self.gateway.root.glob("responses/*.json"))
                self.assertEqual([p.name for p in responses], ["blind-0-0-0.json"])
        backend = FakeBackend(self.turns(self.review(1)), on_start=observe_start)
        state = self.run_pipeline(backend)
        self.assertEqual(state.stage, "APPROVED")
        self.assertEqual(state.cycles, 1)
        self.assertEqual(set(state.threads), {"analyst", "reviewer"})
        self.assertEqual(len(set(state.threads.values())), 2)
        self.assertEqual((backend.calls[0][0], backend.calls[0][1]), ("start", "reviewer"))
        for call in backend.calls:
            if call[0] == "run" and call[1] == "analyst":
                self.assertNotIn("INDEPENDENT_RESEARCH_ONLY", call[2])
        self.assertEqual((self.gateway.root / state.result).read_text().strip(), JIRA.strip())
        self.assertFalse(backend.turns)

    def test_failed_turn_resumes_both_saved_thread_ids_without_recreating_sessions(self):
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", RuntimeError("synthetic secret must not be persisted"))])
        with self.assertRaises(RuntimeError):
            self.run_pipeline(backend)
        failed = self.assert_failed_checkpoint("DRAFT")
        saved_ids = dict(failed.threads)
        self.assertNotIn("synthetic secret", (self.gateway.root / "state.json").read_text())
        backend = FakeBackend([("analyst", "Draft", self.draft()),
                               ("reviewer", "Review", self.review(1))])
        result = self.run_pipeline(backend, failed)
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(result.threads, saved_ids)
        self.assertFalse(any(c[0] == "start" for c in backend.calls))
        self.assertEqual({(c[1], c[2]) for c in backend.calls if c[0] == "resume"}, set(saved_ids.items()))

    def test_crash_after_durable_blind_response_reuses_response_without_another_research_turn(self):
        self.state.threads["reviewer"] = "synthetic-reviewer"
        save(self.gateway, self.state)
        self.gateway.write_json("responses/blind-0-0-0.json", self.research())
        backend = FakeBackend([("analyst", "Draft", self.draft()),
                               ("reviewer", "Review", self.review(1))])
        state = self.run_pipeline(backend, load_state(self.gateway))
        self.assertEqual(state.stage, "APPROVED")
        self.assertFalse(any(c[0] == "run" and c[3] == "Research" for c in backend.calls))

    def test_empty_reviewer_thread_can_be_replaced_before_any_research_was_completed(self):
        self.state.stage = "FAILED"
        self.state.retry_stage = "BLIND"
        self.state.threads = {"reviewer": "empty-reviewer"}
        self.state.events = ["first-start-checkpoint"]
        save(self.gateway, self.state)
        backend = EmptyResumeBackend(self.turns(self.review(1)), empty_role="reviewer",
                                     missing_id="empty-reviewer")
        result = self.run_pipeline(backend, load_state(self.gateway))
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(result.threads["reviewer"], "replacement-reviewer")
        self.assertEqual(len(set(result.threads.values())), 2)
        self.assertIn("first-start-checkpoint", result.events)
        self.assertEqual(result.events.count("replaced-empty-thread-reviewer"), 1)
        self.assertIsNotNone(result.research)
        self.assertEqual([c[3] for c in backend.calls if c[0] == "run"], ["Research", "Draft", "Review"])
        self.assertEqual(load_state(self.gateway).threads, result.threads)

    def test_empty_analyst_thread_can_be_replaced_without_repeating_or_losing_research(self):
        research = Research.model_validate(self.research())
        self.state.stage = "FAILED"
        self.state.retry_stage = "DRAFT"
        self.state.research = research
        self.state.threads = {"reviewer": "persisted-reviewer", "analyst": "empty-analyst"}
        self.state.events = ["blind-0-0-0"]
        save(self.gateway, self.state)
        self.gateway.write_json("responses/blind-0-0-0.json", research.model_dump())
        original_research_bytes = (self.gateway.root / "responses/blind-0-0-0.json").read_bytes()
        backend = EmptyResumeBackend([("analyst", "Draft", self.draft()),
                                      ("reviewer", "Review", self.review(1))],
                                     empty_role="analyst", missing_id="empty-analyst")
        result = self.run_pipeline(backend, load_state(self.gateway))
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(result.threads, {"reviewer": "persisted-reviewer", "analyst": "replacement-analyst"})
        self.assertEqual(result.research, research)
        self.assertEqual((self.gateway.root / "responses/blind-0-0-0.json").read_bytes(), original_research_bytes)
        self.assertEqual(result.events.count("blind-0-0-0"), 1)
        self.assertEqual(result.events.count("replaced-empty-thread-analyst"), 1)
        self.assertEqual([c[3] for c in backend.calls if c[0] == "run"], ["Draft", "Review"])

    def test_missing_thread_with_completed_role_history_is_never_silently_replaced(self):
        for role in ("reviewer", "analyst"):
            with self.subTest(role=role):
                research = Research.model_validate(self.research())
                draft = Draft.model_validate(self.draft())
                state = State(run_id="run", prompt=self.state.prompt, sources=self.sources, repositories={},
                              stage="FAILED", retry_stage="REVIEW" if role == "reviewer" else "REVISION",
                              research=research, draft=draft, version=1,
                              reviews=[] if role == "reviewer" else [Review.model_validate(self.review(1, findings=[self.finding()]))],
                              cycles=0 if role == "reviewer" else 1,
                              threads={"reviewer": "persisted-reviewer", "analyst": "persisted-analyst"},
                              events=["blind-completed", "draft-completed"])
                original_ids = dict(state.threads)
                save(self.gateway, state)
                backend = EmptyResumeBackend(empty_role=role, missing_id=state.threads[role])
                with self.assertRaises(EmptyThreadError):
                    self.run_pipeline(backend, load_state(self.gateway))
                failed = load_state(self.gateway)
                self.assertEqual(failed.stage, "FAILED")
                self.assertEqual(failed.error, "EmptyThreadError")
                self.assertEqual(failed.threads, original_ids)
                self.assertEqual(failed.research, research)
                self.assertEqual(failed.draft, draft)
                self.assertEqual(failed.events, ["blind-completed", "draft-completed"])
                self.assertFalse(any(c[0] in {"start", "run"} for c in backend.calls))

    def test_five_reviews_then_one_user_authorized_review_and_all_versions_remain(self):
        reviews = [self.review(i, findings=[self.finding(index=i)]) for i in range(1, 6)]
        backend = FakeBackend(self.turns(*reviews))
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        self.assertEqual((waiting.stage, waiting.cycles, waiting.version), ("NEEDS_USER", 5, 5))
        self.assertFalse(waiting.bonus_used)
        self.assertFalse(backend.turns)
        old_result = self.gateway.root / waiting.result
        old_bytes = old_result.read_bytes()
        backend.turns.extend([("analyst", "Draft", self.draft()),
                              ("reviewer", "Review", self.review(6, findings=[self.finding(index=6, status="closed")]))])
        result = pipeline.answer(load_state(self.gateway), "User resolves the remaining ambiguity.")
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 6, 6))
        self.assertTrue(result.bonus_used)
        self.assertEqual(old_result.read_bytes(), old_bytes)
        self.assertEqual(len(list((self.gateway.root / "responses").glob("review-*.json"))), 6)
        with self.assertRaises(ValueError):
            pipeline.answer(result, "A seventh cycle is not authorized.")

    def test_unresolved_sixth_review_finishes_incomplete_with_explicit_open_questions(self):
        reviews = [self.review(i, findings=[self.finding(index=i)]) for i in range(1, 6)]
        backend = FakeBackend(self.turns(*reviews))
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        backend.turns.extend([("analyst", "Draft", self.draft()),
                              ("reviewer", "Review", self.review(6, findings=[self.finding(index=6)]))])
        result = pipeline.answer(waiting, "Synthetic user clarification.")
        self.assertEqual((result.stage, result.cycles), ("INCOMPLETE", 6))
        text = (self.gateway.root / result.result).read_text()
        self.assertIn("Открытые вопросы", text)
        self.assertIn("F1:", text)
        self.assertIn("Specify the populated field", text)
        self.assertEqual(pipeline.run(result).cycles, 6)

    def test_repeated_dispute_without_new_evidence_escalates_after_second_review(self):
        backend = FakeBackend(self.turns(self.review(1, findings=[self.finding()]),
                                         self.review(2, findings=[self.finding(status="disputed")])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 2))
        self.assertIn("F1:", "\n".join(result.questions))
        self.assertFalse(backend.turns)

    def test_rephrasing_an_observation_does_not_create_new_evidence(self):
        repeated = self.finding(status="disputed")
        repeated["evidence"][0]["observation"] = "The same source still establishes this unchanged requirement."
        backend = FakeBackend(self.turns(self.review(1, findings=[self.finding()]),
                                         self.review(2, findings=[repeated]),
                                         self.review(3, findings=[self.finding(status="closed")])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 2))
        self.assertEqual(len(backend.turns), 2, "A third draft/review must await the user's answer")

    def test_analyst_question_stops_before_review_and_answer_uses_existing_threads(self):
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", self.draft(questions=["Should an empty field remain enabled?"]))])
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        result = pipeline.run(self.state)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 0))
        self.assertEqual(result.wait_reason, "clarification")
        self.assertFalse(result.bonus_used)
        ids = dict(result.threads)
        backend.turns.extend([("analyst", "Draft", self.draft()),
                              ("reviewer", "Review", self.review(2))])
        result = pipeline.answer(result, "Yes, keep the empty field enabled.")
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(result.threads, ids)
        self.assertEqual(result.cycles, 1)
        analyst_turns = [c for c in backend.calls if c[0] == "run" and c[1] == "analyst"]
        self.assertIn("Yes, keep the empty field enabled.", analyst_turns[-1][2])

    def test_independent_research_question_cannot_be_silently_approved(self):
        backend = FakeBackend([("reviewer", "Research", self.research(questions=["Which precondition is required?"])),
                               ("analyst", "Draft", self.draft()),
                               ("reviewer", "Review", self.review(1))])
        result = self.run_pipeline(backend)
        self.assertEqual(result.stage, "NEEDS_USER")
        self.assertEqual(result.cycles, 0)
        self.assertNotIn("analyst", result.threads)
        self.assertIn("Which precondition is required?", result.questions)

    def test_wrong_reviewed_version_is_rejected_without_advancing_review_count(self):
        backend = FakeBackend(self.turns(self.review(2)))
        with self.assertRaises(ValueError):
            self.run_pipeline(backend)
        state = self.assert_failed_checkpoint("REVIEW")
        self.assertEqual(state.cycles, 0)
        self.assertFalse(state.reviews)
        self.assertFalse((self.gateway.root / "responses/review-1-0-0.json").exists())

    def test_cached_review_is_subject_to_the_same_version_validation(self):
        self.state.stage = "REVIEW"
        self.state.research = Research.model_validate(self.research())
        self.state.draft = Draft.model_validate(self.draft())
        self.state.version = 1
        self.state.threads = {"analyst": "synthetic-analyst", "reviewer": "synthetic-reviewer"}
        save(self.gateway, self.state)
        self.gateway.write_json("responses/review-1-0-0.json", self.review(2))
        with self.assertRaises(ValueError):
            self.run_pipeline(FakeBackend(), load_state(self.gateway))
        self.assert_failed_checkpoint("REVIEW")

    def test_omitting_prior_unresolved_finding_cannot_become_approval(self):
        backend = FakeBackend(self.turns(self.review(1, findings=[self.finding()]), self.review(2)))
        with self.assertRaises(ValueError):
            self.run_pipeline(backend)
        state = self.assert_failed_checkpoint("REVIEW")
        self.assertEqual(state.cycles, 1)
        self.assertEqual(state.reviews[-1].findings[0].id, "F1")
        self.assertFalse(state.result)

    def test_jira_syntax_failure_requires_revision_even_when_review_has_no_findings(self):
        invalid = JIRA.replace("h3. Требуемое поведение.", "### Требуемое поведение")
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", self.draft(text=invalid)),
                               ("reviewer", "Review", self.review(1)),
                               ("analyst", "Draft", self.draft()),
                               ("reviewer", "Review", self.review(2))])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 2))
        self.assertEqual(result.style_errors, [])
        revision = [c for c in backend.calls if c[0] == "run" and c[1] == "analyst"][-1][2]
        self.assertIn("Markdown", revision)

    def test_malformed_structured_output_fails_safely_and_can_resume_saved_reviewer(self):
        backend = FakeBackend([("reviewer", "Research", {"summary": "synthetic private text"})])
        with self.assertRaises(ValidationError):
            self.run_pipeline(backend)
        state = self.assert_failed_checkpoint("BLIND")
        self.assertEqual(state.error, "ValidationError")
        self.assertNotIn("synthetic private text", (self.gateway.root / "state.json").read_text())
        self.assertFalse((self.gateway.root / "responses/blind-0-0-0.json").exists())
        backend = FakeBackend(self.turns(self.review(1)))
        self.assertEqual(self.run_pipeline(backend, state).stage, "APPROVED")
        self.assertTrue(any(c[:2] == ("resume", "reviewer") for c in backend.calls))

    def test_interruption_keeps_a_content_free_checkpoint_and_distinct_thread_ids(self):
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", KeyboardInterrupt())])
        result = self.run_pipeline(backend)
        self.assertEqual(result.stage, "CANCELLED")
        checkpoint = load_state(self.gateway)
        self.assertEqual(checkpoint.error, "interrupted")
        self.assertEqual(len(set(checkpoint.threads.values())), 2)
        self.assertIsNotNone(checkpoint.research)

    def test_changed_source_rejects_resume_before_any_backend_call(self):
        (self.workspace / "topic/source-1.txt").write_text("Changed after checkpoint.\n")
        backend = FakeBackend()
        with self.assertRaises(ValueError):
            self.run_pipeline(backend, load_state(self.gateway))
        self.assertFalse(backend.calls)
        self.assert_failed_checkpoint("")

    def test_source_changed_between_stages_cannot_be_approved_under_old_manifest(self):
        def mutate_after_draft(role, prompt, schema):
            (self.workspace / "topic/source-1.txt").write_text("Changed during this run.\n")
            return self.draft()
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", mutate_after_draft),
                               ("reviewer", "Review", self.review(1))])
        with self.assertRaises(ValueError):
            self.run_pipeline(backend)
        self.assertEqual(load_state(self.gateway).stage, "FAILED")

    def test_source_changed_during_final_review_is_detected_before_approval(self):
        def mutate_before_review_response(role, prompt, schema):
            (self.workspace / "topic/source-1.txt").write_text("Changed while reviewer was running.\n")
            return self.review(1)
        backend = FakeBackend(self.turns(mutate_before_review_response))
        with self.assertRaises(ValueError):
            self.run_pipeline(backend)
        self.assertEqual(load_state(self.gateway).stage, "FAILED")
        self.assertFalse(list((self.gateway.root / "results").glob("*approved*")))

    def test_invalid_counters_or_shared_session_cannot_resume(self):
        for changes in ({"cycles": 6, "bonus_used": False}, {"cycles": 7, "bonus_used": True},
                        {"threads": {"analyst": "same", "reviewer": "same"}}):
            with self.subTest(changes=changes):
                payload = self.state.model_dump()
                payload.update(changes)
                self.gateway.write_json("state.json", payload, replace=True)
                with self.assertRaises(ValueError):
                    load_state(self.gateway)


if __name__ == "__main__":
    unittest.main()
