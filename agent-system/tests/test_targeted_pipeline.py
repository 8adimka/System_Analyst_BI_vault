"""Synthetic targeted-revision checks, using the normal parent state machine."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from orchestrator.access import ArtifactGateway
from orchestrator.inputs import digest
from orchestrator.models import Draft, Research, Review
from orchestrator.pipeline import load_state, save
from orchestrator.targeted_pipeline import TargetedPipeline
from test_pipeline import FakeBackend, JIRA


class TargetedPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="modus-targeted-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        source = self.workspace / "source.txt"
        source.write_text("Synthetic requirement witness.\n")
        self.revision = digest(source)
        for role in ("analyst", "reviewer"):
            path = self.workspace / f".codex/agents/{role}.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'developer_instructions = "Synthetic {role} role."\n')
        standards = self.workspace / "agent-system/standards"
        standards.mkdir(parents=True)
        for name in ("jira-style.md", "requirements-quality.md"):
            (standards / name).write_text("Synthetic instructions.\n")
        self.gateway = ArtifactGateway(self.workspace, self.workspace / ".private/runtime/run")
        self.addCleanup(self.gateway.close)
        from orchestrator.pipeline import State
        self.state = State(run_id="run", prompt="Synthetic original request.", stage="REVISION",
                           sources={"source.txt": self.revision}, repositories={},
                           threads={"analyst": "saved-analyst", "reviewer": "saved-reviewer"},
                           research=Research.model_validate({"summary": "PRIVATE_BLIND_RESEARCH",
                               "requirements": ["Clear the field."], "scenarios": ["Clear a field."],
                               "evidence": [self.evidence()], "questions": []}),
                           draft=Draft.model_validate({"jira": JIRA, "evidence": [self.evidence()],
                               "questions": [], "change_log": []}),
                           reviews=[Review.model_validate(self.review(1))], version=1, cycles=1)
        save(self.gateway, self.state)

    def evidence(self):
        return {"path": "source.txt", "symbol": "synthetic", "revision": self.revision,
                "observation": "Synthetic clearing requirement."}

    def patch(self, before="поле очищается", after="поле полностью очищается", **changes):
        return {"replacements": [{"before": before, "after": after, "expected_count": 1}],
                "evidence": [self.evidence()], "drop_stale_evidence": False,
                "questions": [], "change_log": ["Address F1."], **changes}

    def review(self, version, status="open"):
        return {"reviewed_version": version, "findings": [{"id": "F1", "severity": "major",
                "blocking": True, "claim": "Clearing needs an explicit condition.",
                "evidence": [self.evidence()], "impact": "Acceptance is ambiguous.",
                "requested_change": "Clarify the populated field condition.", "status": status,
                "draft_anchor": "AC.1"}], "questions": []}

    def call_revision(self, pipeline):
        return pipeline._call(self.state, "analyst", "revision", "FULL_CURRENT_DRAFT_AND_REVIEW", Draft)

    def test_local_edit_preserves_text_old_state_and_ordinary_cache_protocol(self):
        snapshot = self.state.model_dump()
        progress = []
        backend = FakeBackend([("analyst", "DraftPatch", self.patch())])
        pipeline = TargetedPipeline(self.workspace, self.gateway, backend, progress.append)
        result = self.call_revision(pipeline)
        self.assertEqual(result.jira, JIRA.replace("поле очищается", "поле полностью очищается"))
        expected = deepcopy(snapshot)
        expected["events"] = ["revision-1-1-0"]
        self.assertEqual(self.state.model_dump(), expected)
        self.assertEqual(self.gateway.read_json("responses/revision-1-1-0.json"), result.model_dump())
        self.assertTrue(self.gateway.path("responses/revision-patch-1-1-1-0.json").exists())
        self.assertFalse(any(call[0] == "start" for call in backend.calls))
        self.assertEqual(progress[0]["operation"], "revision-patch-1")
        self.assertEqual(progress[0]["cycles"], 1)
        prompt = next(call[2] for call in backend.calls if call[0] == "run")
        for text in ("FULL_CURRENT_DRAFT_AND_REVIEW", "2200", "expected_count", "Подсчитай", "DraftPatch"):
            self.assertIn(text, prompt)

    def test_complete_cache_resumes_without_any_sdk_or_patch_read(self):
        draft = self.state.draft.model_dump()
        draft["jira"] = draft["jira"].replace("поле очищается", "поле полностью очищается")
        self.gateway.write_json("responses/revision-1-1-0.json", draft)
        self.gateway.write_json("responses/revision-patch-1-1-1-0.json", {"invalid": "unused"})
        backend = FakeBackend()
        result = self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertEqual(result.jira, draft["jira"])
        self.assertFalse(backend.calls)

    def test_cached_patch_is_assembled_without_sdk(self):
        self.gateway.write_json("responses/revision-patch-1-1-1-0.json", self.patch())
        backend = FakeBackend()
        result = self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertIn("поле полностью очищается", result.jira)
        self.assertFalse(backend.calls)

    def test_bad_anchor_then_valid_patch_preserves_old_version_and_adds_static_feedback(self):
        old_draft = self.state.draft.model_dump()
        backend = FakeBackend([("analyst", "DraftPatch", self.patch(before="PRIVATE_MISSING_ANCHOR")),
                               ("analyst", "DraftPatch", self.patch())])
        result = self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertIn("поле полностью очищается", result.jira)
        self.assertEqual(self.state.draft.model_dump(), old_draft)
        self.assertEqual((self.state.version, self.state.cycles), (1, 1))
        prompts = [call[2] for call in backend.calls if call[0] == "run"]
        self.assertEqual(len(prompts), 2)
        self.assertIn("FULL_CURRENT_DRAFT_AND_REVIEW", prompts[1])
        self.assertIn("Предыдущая правка не применилась механически", prompts[1])
        self.assertNotIn("PRIVATE_MISSING_ANCHOR", prompts[1])

    def test_three_anchor_failures_stop_without_changing_draft_or_cycles(self):
        snapshot = self.state.model_dump()
        backend = FakeBackend([("analyst", "DraftPatch", self.patch(before=f"MISSING_{i}"))
                               for i in range(3)])
        with self.assertRaisesRegex(ValueError, "Три попытки"):
            self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertEqual(self.state.model_dump(), snapshot)
        self.assertEqual(len([call for call in backend.calls if call[0] == "run"]), 3)
        self.assertFalse(self.gateway.path("responses/revision-1-1-0.json").exists())

    def test_out_of_manifest_evidence_is_not_retried_or_cached(self):
        unknown = self.workspace / "unknown.txt"
        unknown.write_text("Synthetic uninventoried witness.")
        evidence = self.evidence()
        evidence.update(path="unknown.txt", revision=digest(unknown))
        backend = FakeBackend([("analyst", "DraftPatch", self.patch(evidence=[evidence]))])
        snapshot = self.state.draft.model_dump()
        with self.assertRaises(ValueError):
            self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertEqual(self.state.draft.model_dump(), snapshot)
        self.assertEqual(len([call for call in backend.calls if call[0] == "run"]), 1)
        self.assertFalse(self.gateway.path("responses/revision-patch-1-1-1-0.json").exists())

    def test_transport_and_schema_errors_are_not_retried(self):
        for response, error in [(RuntimeError("Synthetic transport failure"), RuntimeError),
                                ({"invalid": "payload"}, ValidationError)]:
            backend = FakeBackend([("analyst", "DraftPatch", response)])
            with self.subTest(error=error.__name__), self.assertRaises(error):
                self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
            self.assertEqual(len([call for call in backend.calls if call[0] == "run"]), 1)

    def test_final_evidence_failure_is_not_an_anchor_retry(self):
        self.state.draft.evidence[0].revision = "old-version"
        backend = FakeBackend([("analyst", "DraftPatch", self.patch())])
        with self.assertRaises(ValueError):
            self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertEqual(len([call for call in backend.calls if call[0] == "run"]), 1)
        self.assertTrue(self.gateway.path("responses/revision-patch-1-1-1-0.json").exists())
        self.assertFalse(self.gateway.path("responses/revision-1-1-0.json").exists())

    def test_version_change_is_not_cached_or_retried(self):
        def change_source(role, prompt, schema):
            (self.workspace / "source.txt").write_text("Changed synthetic source.")
            return self.patch()
        backend = FakeBackend([("analyst", "DraftPatch", change_source)])
        with self.assertRaisesRegex(ValueError, "Первоисточники изменились"):
            self.call_revision(TargetedPipeline(self.workspace, self.gateway, backend))
        self.assertEqual(len([call for call in backend.calls if call[0] == "run"]), 1)
        self.assertFalse(self.gateway.path("responses/revision-patch-1-1-1-0.json").exists())

    def test_revision_is_reviewed_and_parent_five_plus_one_limit_is_unchanged(self):
        self.state.version = self.state.cycles = 4
        self.state.reviews = [Review.model_validate(self.review(4))]
        save(self.gateway, self.state)

        def inspect_review(role, prompt, schema):
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            self.assertIn("поле полностью очищается", payload["draft"]["jira"])
            self.assertEqual(payload["version"], 5)
            return self.review(5)

        backend = FakeBackend([("analyst", "DraftPatch", self.patch()),
                               ("reviewer", "Review", inspect_review)])
        pipeline = TargetedPipeline(self.workspace, self.gateway, backend)
        waiting = pipeline.run(self.state)
        self.assertEqual((waiting.stage, waiting.version, waiting.cycles), ("NEEDS_USER", 5, 5))
        self.assertFalse(waiting.bonus_used)
        self.assertEqual(pipeline.run(waiting).cycles, 5)
        self.assertFalse(backend.turns)
        backend.turns.extend([("analyst", "DraftPatch", self.patch(
            before="поле полностью очищается", after="поле полностью очищается после снятия выбора")),
                              ("reviewer", "Review", self.review(6, status="closed"))])
        result = pipeline.answer(load_state(self.gateway), "Synthetic explicit user decision.")
        self.assertEqual((result.stage, result.version, result.cycles), ("APPROVED", 6, 6))
        self.assertTrue(result.bonus_used and result.bonus_active)
        self.assertFalse(any(call[0] == "start" for call in backend.calls))
        self.assertEqual(result.threads, {"analyst": "saved-analyst", "reviewer": "saved-reviewer"})
        for call in backend.calls:
            if call[0] == "run" and call[1] == "analyst":
                self.assertEqual(call[3], "DraftPatch")
                self.assertNotIn("PRIVATE_BLIND_RESEARCH", call[2])


if __name__ == "__main__":
    unittest.main()
