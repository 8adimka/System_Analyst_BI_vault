"""Synthetic integration checks for the coordinator state machine.

These tests never instantiate the Codex SDK or expose corporate material.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from orchestrator.access import ArtifactGateway
from orchestrator.inputs import digest
from orchestrator.models import Draft, Research, Review
from orchestrator.pipeline import Pipeline, State, base_prompt, load_state, save
from orchestrator.sdk_adapter import EmptyThreadError, OutputLimitExceeded


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

    def test_analyst_context_is_versioned_but_hidden_from_reviewer_manifest(self):
        context = "topic/source-6.txt"
        self.state.analyst_sources = [context]
        analyst = json.loads(base_prompt(self.state, "analyst"))
        reviewer = json.loads(base_prompt(self.state, "reviewer"))
        self.assertEqual(analyst["source_versions"], self.state.sources)
        self.assertEqual(analyst["analyst_context_sources"], [context])
        self.assertIn("не являются первичным контрактом", analyst["context_policy"])
        self.assertNotIn(context, reviewer["source_versions"])
        self.assertEqual(reviewer["analyst_context_sources"], [])
        self.assertIn("Не блокируй независимое исследование", reviewer["context_policy"])

        draft = self.draft()
        draft["evidence"].append(self.evidence(6))
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", draft), ("reviewer", "Review", self.review(1))])
        result = self.run_pipeline(backend)
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(result.sources[context], self.sources[context])
        for call in backend.calls:
            if call[0] == "run":
                payload, _ = json.JSONDecoder().raw_decode(call[2])
                self.assertEqual(context in payload["source_versions"], call[1] == "analyst")

    def test_draft_budget_is_analyst_only_and_preserves_source_manifest(self):
        analyst = json.loads(base_prompt(self.state, "analyst"))
        reviewer = json.loads(base_prompt(self.state, "reviewer"))
        self.assertEqual(analyst["source_versions"], self.state.sources)
        self.assertEqual(reviewer["source_versions"], self.state.sources)
        self.assertIn("5000", analyst["draft_output_budget"])
        self.assertIn("25000", analyst["draft_output_budget"])
        self.assertNotIn("draft_output_budget", reviewer)

    def part(self, index, text, more=False):
        return {"index": index, "jira": text, "more": more,
                "evidence": [self.evidence()], "questions": [], "change_log": []}

    def test_output_limit_resume_assembles_parts_in_same_thread_before_whole_review(self):
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", OutputLimitExceeded("Synthetic limit"))])
        with self.assertRaises(OutputLimitExceeded):
            self.run_pipeline(backend)
        failed = self.assert_failed_checkpoint("DRAFT")
        ids = dict(failed.threads)
        research = (self.gateway.root / "responses/blind-0-0-0.json").read_bytes()
        first, second = JIRA.split("\n\n----\n", 1)

        def observe_review(role, prompt, schema):
            received = json.loads(prompt.rsplit("\n", 1)[1])["draft"]["jira"]
            self.assertIn(first, received)
            self.assertIn("AC.1", received)
            self.assertTrue((self.gateway.root / "responses/draft-0-0-0.json").exists())
            return self.review(1)

        resumed = FakeBackend([("analyst", "DraftPart", self.part(1, first + "\n\n----", True)),
                               ("analyst", "DraftPart", self.part(2, second)),
                               ("reviewer", "Review", observe_review)])
        result = self.run_pipeline(resumed, failed)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 1, 1))
        self.assertTrue(result.segmented_draft)
        self.assertEqual(result.threads, ids)
        self.assertEqual(result.answers, [])
        self.assertFalse(result.bonus_used)
        self.assertFalse(any(c[0] == "start" for c in resumed.calls))
        self.assertEqual((self.gateway.root / "responses/blind-0-0-0.json").read_bytes(), research)
        self.assertIn("AC.1", (self.gateway.root / result.result).read_text())
        self.assertEqual(len(list((self.gateway.root / "responses").glob("draft-part-*.json"))), 2)
        for call in resumed.calls:
            if call[0] == "run" and call[1] == "analyst":
                self.assertNotIn("INDEPENDENT_RESEARCH_ONLY", call[2])

    def test_segmented_resume_reuses_completed_parts_after_transport_failure(self):
        self.state.segmented_draft = True
        first, second = JIRA.split("\n\n----\n", 1)
        first += "\n\n----"
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "DraftPart", self.part(1, first, True)),
                               ("analyst", "DraftPart", RuntimeError("Synthetic failure"))])
        with self.assertRaises(RuntimeError):
            self.run_pipeline(backend)
        failed = self.assert_failed_checkpoint("DRAFT")
        path = self.gateway.root / "responses/draft-part-1-0-0-0.json"
        cached = path.read_bytes()
        self.assertFalse(list((self.gateway.root / "results").glob("*")))
        resumed = FakeBackend([("analyst", "DraftPart", self.part(2, second)),
                               ("reviewer", "Review", self.review(1))])
        result = self.run_pipeline(resumed, failed)
        self.assertEqual(result.stage, "APPROVED")
        self.assertEqual(path.read_bytes(), cached)
        calls = [c for c in resumed.calls if c[0] == "run" and c[1] == "analyst"]
        self.assertEqual(len(calls), 1)
        self.assertNotIn(self.state.prompt, calls[0][2])

    def test_wrong_part_index_fails_without_caching_or_reviewing(self):
        self.state.segmented_draft = True
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "DraftPart", self.part(2, JIRA))])
        with self.assertRaisesRegex(ValueError, "номер части"):
            self.run_pipeline(backend)
        failed = self.assert_failed_checkpoint("DRAFT")
        self.assertEqual((failed.version, failed.cycles), (0, 0))
        self.assertFalse((self.gateway.root / "responses/draft-part-1-0-0-0.json").exists())

    def test_segmented_revision_keeps_previous_findings_and_review_cycles(self):
        self.state.segmented_draft = True
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "DraftPart", self.part(1, JIRA)),
                               ("reviewer", "Review", self.review(1, findings=[self.finding()])),
                               ("analyst", "DraftPart", self.part(1, JIRA)),
                               ("reviewer", "Review", self.review(2, findings=[self.finding(status="closed")]))])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 2, 2))
        self.assertFalse(result.bonus_used)
        revision = [c for c in backend.calls if c[0] == "run" and c[1] == "analyst"][-1][2]
        self.assertIn("F1", revision)

    def test_segmented_draft_stops_at_bounded_part_count(self):
        from unittest.mock import patch
        self.state.segmented_draft = True
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "DraftPart", self.part(1, JIRA, True)),
                               ("analyst", "DraftPart", self.part(2, JIRA, True))])
        with patch("orchestrator.pipeline.MAX_DRAFT_PARTS", 2), self.assertRaisesRegex(ValueError, "предел частей"):
            self.run_pipeline(backend)
        failed = self.assert_failed_checkpoint("DRAFT")
        self.assertEqual((failed.version, failed.cycles), (0, 0))

    def test_saved_part_prevents_replacing_lost_analyst_history(self):
        self.state.stage, self.state.retry_stage = "FAILED", "DRAFT"
        self.state.segmented_draft = True
        self.state.research = Research.model_validate(self.research())
        self.state.threads = {"reviewer": "saved-reviewer", "analyst": "saved-analyst"}
        self.gateway.write_json("responses/draft-part-1-0-0-0.json", self.part(1, JIRA, True))
        save(self.gateway, self.state)
        backend = EmptyResumeBackend(empty_role="analyst", missing_id="saved-analyst")
        with self.assertRaises(EmptyThreadError):
            self.run_pipeline(backend, load_state(self.gateway))
        self.assertFalse(any(c[0] == "start" for c in backend.calls))
        self.assertEqual(load_state(self.gateway).threads["analyst"], "saved-analyst")

    def test_analyst_context_changes_are_checked_before_any_model_call(self):
        self.state.analyst_sources = ["topic/source-6.txt"]
        (self.workspace / self.state.analyst_sources[0]).write_text("Changed historical context.")
        backend = FakeBackend()
        with self.assertRaisesRegex(ValueError, "Первоисточники изменились"):
            self.run_pipeline(backend)
        self.assertFalse(backend.calls)

    def test_old_state_without_analyst_context_is_compatible(self):
        payload = self.state.model_dump()
        payload.pop("analyst_sources")
        self.gateway.write_json("state.json", payload, replace=True)
        loaded = load_state(self.gateway)
        self.assertEqual(loaded.analyst_sources, [])
        self.assertEqual(json.loads(base_prompt(loaded))["source_versions"], loaded.sources)

    def test_context_outside_version_manifest_is_refused(self):
        self.state.analyst_sources = ["topic/missing.txt"]
        save(self.gateway, self.state)
        with self.assertRaisesRegex(ValueError, "Контекст аналитика отсутствует"):
            load_state(self.gateway)

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

    def test_repeated_open_finding_is_revised_until_reviewer_confirms_correction(self):
        backend = FakeBackend(self.turns(self.review(1, findings=[self.finding()]),
                                         self.review(2, findings=[self.finding()]),
                                         self.review(3, findings=[self.finding(status="closed")])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 3, 3))
        self.assertFalse(result.bonus_used)
        self.assertEqual([review.findings[0].status for review in result.reviews],
                         ["open", "open", "closed"])
        self.assertFalse(backend.turns)

    def test_repeated_open_finding_still_stops_at_five_reviews(self):
        reviews = [self.review(version, findings=[self.finding()]) for version in range(1, 6)]
        backend = FakeBackend(self.turns(*reviews))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("NEEDS_USER", 5, 5))
        self.assertEqual(result.wait_reason, "review")
        self.assertIn("F1:", "\n".join(result.questions))
        self.assertTrue(all(review.findings[0].status == "open" for review in result.reviews))
        self.assertFalse(result.bonus_used)
        self.assertFalse(backend.turns)

    def test_previous_dispute_does_not_escalate_a_now_open_finding(self):
        backend = FakeBackend(self.turns(self.review(1, findings=[self.finding(status="disputed")]),
                                         self.review(2, findings=[self.finding()]),
                                         self.review(3, findings=[self.finding(status="closed")])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 3))
        self.assertFalse(result.bonus_used)
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

    def test_continue_research_keeps_thread_inputs_answers_and_immutable_history(self):
        backend = FakeBackend([("reviewer", "Research", self.research(questions=["Inspect the available source?"]))])
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        ids, sources = dict(waiting.threads), dict(waiting.sources)
        old_response = self.gateway.root / "responses/blind-0-0-0.json"
        old_questions = self.gateway.root / "questions/v0-0-0.md"
        response_bytes, question_bytes = old_response.read_bytes(), old_questions.read_bytes()
        backend.calls.clear()
        backend.turns.append(("reviewer", "Research", self.research(questions=["A remaining product choice?"])))
        result = pipeline.continue_research(load_state(self.gateway))
        self.assertEqual((result.stage, result.research_round, result.version, result.cycles),
                         ("NEEDS_USER", 1, 0, 0))
        self.assertEqual(result.threads, ids)
        self.assertEqual(result.sources, sources)
        self.assertEqual(result.answers, [])
        self.assertIsNone(result.draft)
        self.assertFalse(result.reviews)
        self.assertFalse(result.bonus_used)
        self.assertEqual(result.questions, ["A remaining product choice?"])
        self.assertFalse(any(call[0] == "start" or call[1] == "analyst" for call in backend.calls))
        prompt = next(call[2] for call in backend.calls if call[0] == "run")
        self.assertIn('"previous_research_questions": ["Inspect the available source?"]', prompt)
        self.assertIn("не проси пользователя выполнить этот поиск", prompt)
        self.assertNotIn('"draft":', prompt)
        self.assertEqual(old_response.read_bytes(), response_bytes)
        self.assertEqual(old_questions.read_bytes(), question_bytes)
        self.assertTrue((self.gateway.root / "responses/blind-extra-1-0-0-0.json").is_file())
        self.assertIn("A remaining product choice?", (self.gateway.root / "questions/v0-0-0-research-1.md").read_text())

    def test_continue_research_allows_only_two_additional_rounds(self):
        backend = FakeBackend([("reviewer", "Research", self.research(questions=["Initial question?"]))])
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        for research_round in (1, 2):
            backend.turns.append(("reviewer", "Research", self.research(questions=[f"Question {research_round}?"])))
            waiting = pipeline.continue_research(load_state(self.gateway))
            self.assertEqual(waiting.research_round, research_round)
        calls = list(backend.calls)
        checkpoint = (self.gateway.root / "state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "двух дополнительных"):
            pipeline.continue_research(waiting)
        self.assertEqual(backend.calls, calls)
        self.assertEqual((self.gateway.root / "state.json").read_bytes(), checkpoint)
        self.assertEqual((waiting.cycles, waiting.version, waiting.bonus_used, waiting.bonus_active),
                         (0, 0, False, False))
        self.assertEqual(len(list((self.gateway.root / "responses").glob("blind*.json"))), 3)

    def test_continue_research_refuses_other_stages_and_any_analyst_work(self):
        backend = FakeBackend([("reviewer", "Research", self.research(questions=["Question?"]))])
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        variants = [dict(stage=stage) for stage in ("APPROVED", "FAILED", "BLIND", "DRAFT", "INCOMPLETE")]
        variants += [{"wait_reason": "review"}, {"threads": {**waiting.threads, "analyst": "created-analyst"}},
                     {"draft": Draft.model_validate(self.draft())},
                     {"reviews": [Review.model_validate(self.review(1))]}, {"version": 1}, {"cycles": 1}]
        calls = list(backend.calls)
        checkpoint = (self.gateway.root / "state.json").read_bytes()
        for update in variants:
            with self.subTest(update=update), self.assertRaises(ValueError):
                pipeline.continue_research(waiting.model_copy(update=update, deep=True))
        self.assertEqual(backend.calls, calls)
        self.assertEqual((self.gateway.root / "state.json").read_bytes(), checkpoint)

    def test_cached_extra_research_is_used_without_replaying_old_or_new_research(self):
        backend = FakeBackend([("reviewer", "Research", self.research(questions=["Investigate source?"])),
                               ("reviewer", "Research", RuntimeError("Synthetic network failure"))])
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        reviewer = waiting.threads["reviewer"]
        with self.assertRaises(RuntimeError):
            pipeline.continue_research(waiting)
        failed = load_state(self.gateway)
        self.assertEqual((failed.stage, failed.retry_stage, failed.research_round), ("FAILED", "BLIND", 1))
        self.assertEqual(failed.research.questions, ["Investigate source?"])
        # Model response persisted, then the coordinator crashed before advancing.
        self.gateway.write_json("responses/blind-extra-1-0-0-0.json", self.research())
        resumed = FakeBackend([("analyst", "Draft", self.draft()), ("reviewer", "Review", self.review(1))])
        result = self.run_pipeline(resumed, failed)
        self.assertEqual((result.stage, result.research_round), ("APPROVED", 1))
        self.assertEqual(result.threads["reviewer"], reviewer)
        self.assertFalse(any(call[0] == "run" and call[3] == "Research" for call in resumed.calls))
        self.assertEqual(len(list((self.gateway.root / "responses").glob("blind*.json"))), 2)

    def test_failed_resume_checkpoints_active_stage_and_cleared_error_before_model_call(self):
        self.state.stage, self.state.retry_stage, self.state.error = "FAILED", "BLIND", "SyntheticFailure"
        self.state.threads = {"reviewer": "saved-reviewer"}
        save(self.gateway, self.state)

        def observe_call(role, prompt, schema):
            checkpoint = load_state(self.gateway)
            self.assertEqual((checkpoint.stage, checkpoint.retry_stage, checkpoint.error), ("BLIND", "BLIND", ""))
            self.assertEqual(checkpoint.threads, {"reviewer": "saved-reviewer"})
            return self.research(questions=["A genuine product choice?"])

        backend = FakeBackend([("reviewer", "Research", observe_call)])
        result = self.run_pipeline(backend, load_state(self.gateway))
        self.assertEqual(result.stage, "NEEDS_USER")
        self.assertFalse(any(call[0] == "start" for call in backend.calls))

    def test_old_state_defaults_to_first_research_round_and_rejects_out_of_range_rounds(self):
        payload = self.state.model_dump()
        payload.pop("research_round")
        self.gateway.write_json("state.json", payload, replace=True)
        self.assertEqual(load_state(self.gateway).research_round, 0)
        for value in (-1, 3):
            payload["research_round"] = value
            self.gateway.write_json("state.json", payload, replace=True)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                load_state(self.gateway)

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

    def test_jira_only_dependencies_require_another_draft_and_review_before_approval(self):
        dependency = "Delivery depends on the separately planned backend task."
        initial = JIRA + "\n----\nh3. Открытые вопросы.\n* " + dependency + "\n"
        corrected = JIRA + "\n----\nh3. Зависимости.\n* " + dependency + "\n"

        def correct_format(role, prompt, schema):
            checkpoint = load_state(self.gateway)
            self.assertEqual((checkpoint.stage, checkpoint.version, checkpoint.cycles), ("REVISION", 1, 1))
            self.assertEqual(checkpoint.draft.jira, initial)
            self.assertFalse(checkpoint.questions)
            self.assertTrue(checkpoint.style_errors)
            self.assertIn(dependency, prompt)
            self.assertIn("неразрешённые вопросы", prompt)
            self.assertIn("Не выдумывай ответы", prompt)
            self.assertFalse(checkpoint.result)
            return self.draft(text=corrected)

        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", self.draft(text=initial)),
                               ("reviewer", "Review", self.review(1)),
                               ("analyst", "Draft", correct_format),
                               ("reviewer", "Review", self.review(2))])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.version, result.cycles), ("APPROVED", 2, 2))
        self.assertEqual(result.answers, [])
        self.assertFalse(result.bonus_used)
        self.assertEqual(result.style_errors, [])
        self.assertIn(dependency, (self.gateway.root / result.result).read_text())

    def test_true_hidden_question_kept_without_structured_questions_waits_at_five_reviews(self):
        question = "Which behavior should the product require?"
        text = JIRA + "\n----\nh3. Открытые вопросы.\n* " + question + "\n"
        turns = [("reviewer", "Research", self.research())]
        for version in range(1, 6):
            turns += [("analyst", "Draft", self.draft(text=text)), ("reviewer", "Review", self.review(version))]
        backend = FakeBackend(turns)
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 5))
        self.assertIn(question, "\n".join(result.questions))
        self.assertIn(question, (self.gateway.root / result.result).read_text())
        self.assertFalse(result.bonus_used)
        self.assertFalse(backend.turns)
        # Resume cannot grant a sixth automatic format-repair review.
        calls = list(backend.calls)
        self.assertIs(self.run_pipeline(backend, result), result)
        self.assertEqual(backend.calls, calls)

    def test_structured_review_questions_still_wait_immediately_despite_jira_entries(self):
        text = JIRA + "\n----\nh3. Открытые вопросы.\n* Also resolve this scenario?\n"
        backend = FakeBackend([("reviewer", "Research", self.research()),
                               ("analyst", "Draft", self.draft(text=text)),
                               ("reviewer", "Review", self.review(1, questions=["A genuine product choice?"]))])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 1))
        self.assertEqual(result.questions, ["A genuine product choice?", "Also resolve this scenario?"])
        self.assertFalse(result.style_errors)
        self.assertFalse(backend.turns)

    def old_jira_question_wait(self, question="Known external dependency."):
        state = self.state
        state.research = Research.model_validate(self.research())
        state.draft = Draft.model_validate(self.draft(text=JIRA + "\n----\nh3. Открытые вопросы.\n* " + question + "\n"))
        state.reviews = [Review.model_validate(self.review(1))]
        state.version = state.cycles = 1
        state.threads = {"analyst": "saved-analyst", "reviewer": "saved-reviewer"}
        Pipeline(self.workspace, self.gateway, None)._wait(state, [question], "review")
        return state

    def test_old_jira_only_wait_resumes_format_repair_without_answers_or_bonus(self):
        question = "Known external dependency."
        old = self.old_jira_question_wait(question)
        old.answers = ["The user already defined the required behavior."]
        save(self.gateway, old)
        old_result = self.gateway.root / old.result
        old_questions = self.gateway.root / "questions/v1-0-1.md"
        old_bytes, questions_bytes = old_result.read_bytes(), old_questions.read_bytes()
        ids, answers = dict(old.threads), list(old.answers)
        corrected = JIRA + "\n----\nh3. Зависимости.\n* " + question + "\n"
        backend = FakeBackend([("analyst", "Draft", self.draft(text=corrected)),
                               ("reviewer", "Review", self.review(2))])
        result = self.run_pipeline(backend, load_state(self.gateway))
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 2, 2))
        self.assertEqual(result.threads, ids)
        self.assertEqual(result.answers, answers)
        self.assertFalse(result.bonus_used or result.bonus_active)
        self.assertEqual(old_result.read_bytes(), old_bytes)
        self.assertEqual(old_questions.read_bytes(), questions_bytes)
        self.assertFalse(any(call[0] == "start" for call in backend.calls))
        self.assertIn("resume-question-format-1-1", result.events)

    def test_old_question_wait_repair_guard_cannot_drop_findings_or_other_questions(self):
        waiting = self.old_jira_question_wait()
        variants = [{"cycles": 5}, {"wait_reason": "clarification"}, {"bonus_used": True},
                    {"questions": ["A different unresolved user question?"]},
                    {"draft": waiting.draft.model_copy(update={"questions": ["A real question?"]})},
                    {"reviews": [Review.model_validate(self.review(1, questions=["A real question?"]))]},
                    {"reviews": [Review.model_validate(self.review(1, findings=[self.finding()]))]},
                    {"reviews": [Review.model_validate(self.review(2))]}]
        for update in variants:
            state = waiting.model_copy(update=update, deep=True)
            save(self.gateway, state)
            checkpoint = (self.gateway.root / "state.json").read_bytes()
            backend = FakeBackend()
            with self.subTest(update=update):
                self.assertIs(self.run_pipeline(backend, state), state)
                self.assertFalse(backend.calls)
                self.assertEqual((self.gateway.root / "state.json").read_bytes(), checkpoint)

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
