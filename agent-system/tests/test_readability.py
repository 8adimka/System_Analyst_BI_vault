"""Offline checks for bounded, meaning-preserving editorial revisions.

Scripted model responses exercise the coordinator policy, not model judgement.
All sources, wording and Thread IDs here are synthetic.
"""
import json
import unittest

from pydantic import ValidationError

from orchestrator.models import Finding, Review, pending_readability, tracked_findings, unresolved
from orchestrator.pipeline import Pipeline, load_state, save
from orchestrator.targeted_pipeline import TargetedPipeline
import test_pipeline as fixtures


class ReadabilityTests(unittest.TestCase):
    # Reuse synthetic setup without inheriting and rediscovering its test suite.
    setUp = fixtures.PipelineTests.setUp
    evidence = fixtures.PipelineTests.evidence
    research = fixtures.PipelineTests.research
    draft = fixtures.PipelineTests.draft
    finding = fixtures.PipelineTests.finding
    review = fixtures.PipelineTests.review
    turns = fixtures.PipelineTests.turns
    run_pipeline = fixtures.PipelineTests.run_pipeline

    def editorial(self, *, status="open", finding_id="R1", **changes):
        return {"id": finding_id, "category": "readability", "severity": "minor",
                "blocking": False, "claim": "The opening repeats the same selection condition.",
                "evidence": [self.evidence()], "impact": "The requirement takes longer to read.",
                "requested_change": "Shorten the condition without changing when clearing occurs.",
                "status": status, "draft_anchor": "h3. Требуемое поведение.", **changes}

    def revised(self):
        return self.draft(text=fixtures.JIRA.replace("При снятии выбранного значения", "При снятии выбора"))

    def test_minor_only_review_gets_one_revision_and_independent_recheck(self):
        backend = fixtures.FakeBackend([
            ("reviewer", "Research", self.research()),
            ("analyst", "Draft", self.draft()),
            ("reviewer", "Review", self.review(1, findings=[self.editorial()])),
            ("analyst", "Draft", self.revised()),
            ("reviewer", "Review", self.review(2, findings=[self.editorial(status="closed")])),
        ])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 2, 2))
        self.assertTrue(result.readability_revision_used)
        self.assertEqual(result.questions, [])
        self.assertFalse(result.bonus_used or result.bonus_active)
        self.assertEqual(result.events.count("readability-revision-1-1"), 1)
        self.assertEqual([c[3] for c in backend.calls if c[0] == "run"],
                         ["Research", "Draft", "Review", "Draft", "Review"])
        self.assertEqual(len([c for c in backend.calls if c[0] == "start"]), 2)
        self.assertEqual((self.gateway.root / result.result).read_text().strip(), self.revised()["jira"].strip())
        report = self.gateway.path("reviews/v2.md").read_text()
        self.assertIn("R1: readability, minor, closed", report)
        self.assertFalse(backend.turns)

    def test_remaining_minors_stay_open_in_report_without_loop_or_user_question(self):
        backend = fixtures.FakeBackend(self.turns(
            self.review(1, findings=[self.editorial()]),
            self.review(2, findings=[self.editorial(), self.editorial(finding_id="R2")]),
        ))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 2, 2))
        self.assertEqual(result.questions, [])
        self.assertTrue(result.readability_revision_used)
        self.assertEqual([f.status for f in result.reviews[-1].findings], ["open", "open"])
        self.assertIn("R1: readability, minor, open", self.gateway.path("reviews/v2.md").read_text())
        self.assertFalse(list(self.gateway.root.glob("questions/*")))
        self.assertEqual(self.run_pipeline(backend, load_state(self.gateway)).cycles, 2)
        self.assertFalse(backend.turns)

    def test_old_states_and_findings_keep_compatible_defaults(self):
        payload = self.state.model_dump()
        payload.pop("readability_revision_used")
        self.gateway.write_json("state.json", payload, replace=True)
        self.assertFalse(load_state(self.gateway).readability_revision_used)
        legacy = Finding.model_validate(self.finding())
        self.assertEqual(legacy.category, "requirements")
        review = Review.model_validate(self.review(1, findings=[self.finding()]))
        self.assertEqual(unresolved(review), review.findings)
        with self.assertRaises(ValidationError):
            Finding.model_validate({**self.editorial(), "category": "unknown"})

    def test_uncategorized_legacy_minor_is_not_reclassified_or_revised(self):
        legacy = {**self.finding(), "severity": "minor", "blocking": False}
        backend = fixtures.FakeBackend(self.turns(self.review(1, findings=[legacy])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 1))
        self.assertFalse(result.readability_revision_used)
        self.assertEqual(result.reviews[-1].findings[0].category, "requirements")
        self.assertFalse(backend.turns)

    def test_major_or_blocking_readability_still_uses_substantive_gate(self):
        for changes in ({"severity": "major"}, {"blocking": True}):
            with self.subTest(changes=changes):
                finding = self.editorial(**changes)
                review = Review.model_validate(self.review(1, findings=[finding]))
                self.assertEqual(unresolved(review), review.findings)
                self.assertEqual(pending_readability(review), [])
                self.assertEqual(tracked_findings(review), review.findings)

        semantic = self.finding()
        semantic["claim"] = "Two different fields satisfy the unqualified target name."
        backend = fixtures.FakeBackend(self.turns(
            self.review(1, findings=[semantic]),
            self.review(2, findings=[{**semantic, "status": "disputed"}]),
        ))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 2))
        self.assertIn("F1:", "\n".join(result.questions))
        self.assertFalse(result.readability_revision_used)

    def test_substantive_and_editorial_findings_share_the_normal_revision(self):
        backend = fixtures.FakeBackend(self.turns(
            self.review(1, findings=[self.finding(), self.editorial()]),
            self.review(2, findings=[self.finding(status="closed"), self.editorial(status="closed")]),
        ))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 2))
        self.assertFalse(result.readability_revision_used)
        prompt = [c[2] for c in backend.calls if c[0] == "run" and c[1] == "analyst"][-1]
        payload, _ = json.JSONDecoder().raw_decode(prompt, prompt.index('{"draft":'))
        self.assertEqual({f["id"] for f in payload["review"]["findings"]}, {"F1", "R1"})
        self.assertFalse(backend.turns)

    def test_question_takes_precedence_over_editorial_only_revision(self):
        backend = fixtures.FakeBackend(self.turns(self.review(
            1, findings=[self.editorial()], questions=["Which of the two fields should be cleared?"])))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("NEEDS_USER", 1))
        self.assertEqual(result.questions, ["Which of the two fields should be cleared?"])
        self.assertFalse(result.readability_revision_used)
        self.assertFalse(backend.turns)

    def test_jira_format_repair_is_not_an_editorial_only_allowance(self):
        backend = fixtures.FakeBackend([
            ("reviewer", "Research", self.research()),
            ("analyst", "Draft", self.draft(text=fixtures.JIRA + "\n## Invalid synthetic heading\n")),
            ("reviewer", "Review", self.review(1, findings=[self.editorial()])),
            ("analyst", "Draft", self.revised()),
            ("reviewer", "Review", self.review(2, findings=[self.editorial(status="closed")])),
        ])
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 2))
        self.assertFalse(result.readability_revision_used)
        self.assertEqual(result.style_errors, [])
        self.assertFalse(backend.turns)

    def test_last_ordinary_review_can_approve_with_reported_minor_without_extension(self):
        reviews = [self.review(i, findings=[self.finding()]) for i in range(1, 5)]
        reviews.append(self.review(5, findings=[self.finding(status="closed"), self.editorial()]))
        backend = fixtures.FakeBackend(self.turns(*reviews))
        result = self.run_pipeline(backend)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 5, 5))
        self.assertFalse(result.readability_revision_used or result.bonus_used)
        self.assertEqual(result.questions, [])
        self.assertEqual(result.reviews[-1].findings[-1].status, "open")
        self.assertIn("R1: readability, minor, open", self.gateway.path("reviews/v5.md").read_text())
        self.assertFalse(backend.turns)

    def test_bonus_review_retains_minor_without_seventh_cycle_or_new_question(self):
        reviews = [self.review(i, findings=[self.finding()]) for i in range(1, 6)]
        backend = fixtures.FakeBackend(self.turns(*reviews))
        pipeline = Pipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        self.assertEqual((waiting.stage, waiting.cycles), ("NEEDS_USER", 5))
        backend.turns.extend([
            ("analyst", "Draft", self.revised()),
            ("reviewer", "Review", self.review(6, findings=[self.finding(status="closed"), self.editorial()])),
        ])
        result = pipeline.answer(load_state(self.gateway), "Synthetic decision resolves the field choice.")
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 6, 6))
        self.assertTrue(result.bonus_used and result.bonus_active)
        self.assertFalse(result.readability_revision_used)
        self.assertEqual(result.questions, [])
        self.assertEqual(result.reviews[-1].findings[-1].status, "open")
        self.assertFalse(backend.turns)

    def test_editorial_attempt_is_checkpointed_before_failure_and_reused_on_resume(self):
        def fail_revision(role, prompt, schema):
            checkpoint = load_state(self.gateway)
            self.assertEqual((checkpoint.stage, checkpoint.retry_stage), ("REVISION", "REVISION"))
            self.assertEqual((checkpoint.version, checkpoint.cycles), (1, 1))
            self.assertTrue(checkpoint.readability_revision_used)
            self.assertEqual(checkpoint.events.count("readability-revision-1-1"), 1)
            raise RuntimeError("Synthetic transport failure")

        backend = fixtures.FakeBackend([
            ("reviewer", "Research", self.research()),
            ("analyst", "Draft", self.draft()),
            ("reviewer", "Review", self.review(1, findings=[self.editorial()])),
            ("analyst", "Draft", fail_revision),
        ])
        with self.assertRaises(RuntimeError):
            self.run_pipeline(backend)
        failed = load_state(self.gateway)
        self.assertEqual((failed.stage, failed.retry_stage), ("FAILED", "REVISION"))
        self.assertTrue(failed.readability_revision_used)
        ids = dict(failed.threads)
        resumed = fixtures.FakeBackend([
            ("analyst", "Draft", self.revised()),
            ("reviewer", "Review", self.review(2, findings=[self.editorial()])),
        ])
        result = self.run_pipeline(resumed, failed)
        self.assertEqual((result.stage, result.cycles), ("APPROVED", 2))
        self.assertEqual(result.threads, ids)
        self.assertEqual(result.events.count("readability-revision-1-1"), 1)
        self.assertFalse(any(c[0] == "start" for c in resumed.calls))
        self.assertFalse(result.bonus_used or result.questions)
        self.assertFalse(resumed.turns)

    def test_followup_review_cannot_silently_omit_attempted_editorial_finding(self):
        backend = fixtures.FakeBackend(self.turns(
            self.review(1, findings=[self.editorial()]), self.review(2)))
        with self.assertRaisesRegex(ValueError, "незакрытые замечания"):
            self.run_pipeline(backend)
        failed = load_state(self.gateway)
        self.assertEqual((failed.stage, failed.retry_stage, failed.cycles), ("FAILED", "REVIEW", 1))
        self.assertTrue(failed.readability_revision_used)
        self.assertEqual(failed.reviews[-1].findings[0].id, "R1")
        self.assertFalse(self.gateway.path("responses/review-2-1-0.json").exists())

    def test_readability_category_does_not_bypass_evidence_validation(self):
        finding = self.editorial()
        finding["evidence"][0]["revision"] = "invented-source-revision"
        backend = fixtures.FakeBackend(self.turns(self.review(1, findings=[finding])))
        with self.assertRaises(ValueError):
            self.run_pipeline(backend)
        failed = load_state(self.gateway)
        self.assertEqual((failed.stage, failed.retry_stage, failed.cycles), ("FAILED", "REVIEW", 0))
        self.assertFalse(failed.readability_revision_used)
        self.assertFalse(self.gateway.path("responses/review-1-0-0.json").exists())

    def test_targeted_pipeline_inherits_editorial_policy_and_rechecks_full_result(self):
        patch = {"replacements": [{"before": "При снятии выбранного значения",
                                    "after": "При снятии выбора", "expected_count": 1}],
                 "evidence": [self.evidence()], "drop_stale_evidence": False,
                 "questions": [], "change_log": ["Address R1 without changing behavior."]}
        backend = fixtures.FakeBackend([
            ("reviewer", "Research", self.research()),
            ("analyst", "Draft", self.draft()),
            ("reviewer", "Review", self.review(1, findings=[self.editorial()])),
            ("analyst", "DraftPatch", patch),
            ("reviewer", "Review", self.review(2, findings=[self.editorial(status="closed")])),
        ])
        result = TargetedPipeline(self.workspace, self.gateway, backend).run(self.state)
        self.assertEqual((result.stage, result.cycles, result.version), ("APPROVED", 2, 2))
        self.assertTrue(result.readability_revision_used)
        self.assertEqual(result.draft.jira, self.revised()["jira"])
        self.assertTrue(self.gateway.path("responses/revision-patch-1-1-1-0.json").exists())
        self.assertTrue(self.gateway.path("responses/revision-1-1-0.json").exists())
        review_prompt = [c[2] for c in backend.calls if c[0] == "run" and c[3] == "Review"][-1]
        payload = json.loads(review_prompt.rsplit("\n", 1)[1])
        self.assertEqual(payload["draft"]["jira"], result.draft.jira)
        self.assertEqual(len([c for c in backend.calls if c[0] == "start"]), 2)
        self.assertFalse(backend.turns)


if __name__ == "__main__":
    unittest.main()
