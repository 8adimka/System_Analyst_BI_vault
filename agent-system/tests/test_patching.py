"""Synthetic exact-edit checks; never load an SDK or corporate materials."""
import unittest

from pydantic import ValidationError

from orchestrator.models import Draft, Evidence
from orchestrator.patching import DraftPatch, Replacement, apply_draft_patch


class PatchingTests(unittest.TestCase):
    def evidence(self, path="source.txt", revision="source-v1", observation="Synthetic fact"):
        return Evidence(path=path, revision=revision, symbol="synthetic", observation=observation)

    def draft(self, **changes):
        values = {"jira": "Stable prefix. TARGET Stable suffix.",
                  "evidence": [self.evidence()], "questions": ["Old question"],
                  "change_log": ["Old change"]}
        values.update(changes)
        return Draft.model_validate(values)

    def patch(self, **changes):
        values = {"replacements": [{"before": "TARGET", "after": "REVISED", "expected_count": 1}],
                  "evidence": [self.evidence()], "drop_stale_evidence": False,
                  "questions": [], "change_log": ["Targeted correction"]}
        values.update(changes)
        return DraftPatch.model_validate(values)

    def manifest(self):
        return {"sources": {"source.txt": "source-v1"}, "repositories": {"repo": "repo-head"}}

    def test_local_edit_preserves_all_untouched_text_and_replaces_metadata(self):
        draft = self.draft()
        snapshot = draft.model_dump()
        patch = self.patch(questions=["Current question"])
        result = apply_draft_patch(draft, patch, self.manifest())
        self.assertEqual(result.jira, "Stable prefix. REVISED Stable suffix.")
        self.assertEqual(result.evidence, draft.evidence)
        self.assertEqual(result.questions, ["Current question"])
        self.assertEqual(result.change_log, ["Targeted correction"])
        self.assertEqual(draft.model_dump(), snapshot)

    def test_replacements_are_sequential_and_allow_empty_deletion(self):
        patch = self.patch(replacements=[{"before": "TARGET", "after": "INTERMEDIATE", "expected_count": 1},
                                         {"before": "INTERMEDIATE", "after": "", "expected_count": 1}])
        self.assertEqual(apply_draft_patch(self.draft(), patch, self.manifest()).jira,
                         "Stable prefix.  Stable suffix.")

    def test_missing_ambiguous_and_overlapping_fragments_fail_without_mutation(self):
        for text, before in [("TARGET TARGET and stable text.", "TARGET"),
                             ("TARGET and stable text here.", "MISSING"),
                             ("aaa and sufficiently stable text.", "aa")]:
            draft = self.draft(jira=text)
            snapshot = draft.model_dump()
            patch = self.patch(replacements=[{"before": before, "after": "CHANGED", "expected_count": 1}])
            with self.subTest(before=before), self.assertRaises(ValueError):
                apply_draft_patch(draft, patch, self.manifest())
            self.assertEqual(draft.model_dump(), snapshot)

    def test_late_failure_does_not_mutate_original(self):
        draft = self.draft()
        snapshot = draft.model_dump()
        patch = self.patch(replacements=[{"before": "TARGET", "after": "FIRST EDIT", "expected_count": 1},
                                         {"before": "ABSENT", "after": "SECOND EDIT", "expected_count": 1}])
        with self.assertRaises(ValueError):
            apply_draft_patch(draft, patch, self.manifest())
        self.assertEqual(draft.model_dump(), snapshot)

    def test_noop_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "не изменяет"):
            apply_draft_patch(self.draft(), self.patch(
                replacements=[{"before": "TARGET", "after": "TARGET", "expected_count": 1}]), self.manifest())

    def test_batch_replacement_requires_the_exact_count(self):
        draft = self.draft(jira="Stable prefix. TARGET; TARGET; TARGET. Stable suffix.")
        snapshot = draft.model_dump()
        patch = self.patch(replacements=[{"before": "TARGET", "after": "REVISED", "expected_count": 3}])
        self.assertEqual(apply_draft_patch(draft, patch, self.manifest()).jira,
                         "Stable prefix. REVISED; REVISED; REVISED. Stable suffix.")
        self.assertEqual(draft.model_dump(), snapshot)
        for expected_count in (1, 2, 4):
            with self.subTest(expected_count=expected_count), self.assertRaises(ValueError):
                apply_draft_patch(draft, self.patch(replacements=[
                    {"before": "TARGET", "after": "REVISED", "expected_count": expected_count}]), self.manifest())
        self.assertEqual(draft.model_dump(), snapshot)

    def test_even_exact_overlapping_count_is_rejected(self):
        draft = self.draft(jira="aaaa and stable untouched text.")
        snapshot = draft.model_dump()
        with self.assertRaises(ValueError):
            apply_draft_patch(draft, self.patch(replacements=[
                {"before": "aa", "after": "b", "expected_count": 3}]), self.manifest())
        self.assertEqual(draft.model_dump(), snapshot)

    def test_drop_stale_checks_source_revisions_and_repository_component_boundaries(self):
        current_source = self.evidence()
        current_repo = self.evidence("repo/nested/file.py", "repo-head")
        current_normalized = self.evidence("./repo/other.py", "repo-head")
        stale = [self.evidence(revision="old-source"),
                 self.evidence("repo/old.py", "old-head"),
                 self.evidence("repoother/file.py", "repo-head"),
                 self.evidence("repo/../unknown.py", "repo-head"),
                 self.evidence("unknown.txt", "unknown-version")]
        added = self.evidence("source.txt", "source-v1", "Additional confirmed fact")
        original = self.draft(evidence=[current_source, *stale, current_repo, current_normalized])
        snapshot = original.model_dump()
        result = apply_draft_patch(original, self.patch(drop_stale_evidence=True,
                                   evidence=[added]), self.manifest())
        self.assertEqual(result.evidence, [current_source, current_repo, current_normalized, added])
        self.assertEqual(original.model_dump(), snapshot)
        self.assertEqual(stale[0].revision, "old-source")

    def test_stale_evidence_is_retained_unless_explicitly_dropped(self):
        old = self.evidence(revision="old-source")
        result = apply_draft_patch(self.draft(evidence=[old]), self.patch(), self.manifest())
        self.assertEqual(result.evidence, [old, self.evidence()])

    def test_deduplication_uses_entire_evidence_and_preserves_order(self):
        first = self.evidence()
        second = self.evidence(observation="Different observation at the same source")
        third = self.evidence(path="other.txt")
        result = apply_draft_patch(self.draft(evidence=[first, second]),
                                  self.patch(evidence=[first, third, second]), self.manifest())
        self.assertEqual(result.evidence, [first, second, third])

    def test_patch_and_replacement_bounds(self):
        for changes in ({"before": ""}, {"before": "b" * 50001}, {"after": "a" * 100001},
                        {"expected_count": 0}, {"expected_count": 101}, {"expected_count": True}):
            values = {"before": "before", "after": "after", "expected_count": 1, **changes}
            with self.subTest(changes=list(changes)), self.assertRaises(ValidationError):
                Replacement.model_validate(values)
        for changes in ({"replacements": []},
                        {"replacements": [{"before": "a", "after": "b", "expected_count": 1}] * 51},
                        {"evidence": []}, {"evidence": [self.evidence()] * 101},
                        {"questions": ["Q"] * 21}, {"change_log": ["C"] * 101},
                        {"drop_stale_evidence": "false"}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValidationError):
                self.patch(**changes)
        with self.assertRaises(ValidationError):
            Replacement.model_validate({"before": "a", "after": "b"})
        self.assertIn("expected_count", Replacement.model_json_schema()["required"])

    def test_final_text_and_evidence_bounds_fail_closed_without_truncation(self):
        for draft, patch in [
            (self.draft(jira="x" * 499994 + "TARGET"), self.patch()),
            (self.draft(evidence=[self.evidence(observation=f"Observation {i}") for i in range(400)]),
             self.patch(evidence=[self.evidence(observation="Observation 400")]))]:
            snapshot = draft.model_dump()
            with self.subTest(size=len(draft.jira)), self.assertRaises(ValueError) as caught:
                apply_draft_patch(draft, patch, self.manifest())
            self.assertEqual(str(caught.exception),
                             "Результат правки нарушает ограничения схемы Draft; исходная постановка сохранена")
            self.assertEqual(draft.model_dump(), snapshot)


if __name__ == "__main__":
    unittest.main()
