"""Synthetic checks for segmented draft transport; no SDK or network calls."""
import unittest

from pydantic import ValidationError

from orchestrator.models import Draft, Evidence
from orchestrator.segmented import MAX_DRAFT_PARTS, DraftPart, combine_parts, part_prompt


class SegmentedTests(unittest.TestCase):
    def evidence(self, observation="First observation"):
        return Evidence(path="source.txt", symbol="setting", revision="synthetic-sha",
                        observation=observation)

    def part(self, index=1, more=False, **changes):
        values = {"index": index, "jira": f"h3. Раздел {index}.\n" + "Подробное требование. " * 2,
                  "more": more, "evidence": [self.evidence()],
                  "questions": [], "change_log": []}
        values.update(changes)
        return DraftPart.model_validate(values)

    def test_combines_in_order_and_only_deduplicates_identical_values(self):
        evidence = self.evidence()
        distinct = self.evidence("Second observation at exactly the same source")
        first = self.part(more=True, evidence=[evidence], questions=["Q2", "Q1"],
                          change_log=["Change 2", "Change 1"])
        second = self.part(2, evidence=[distinct, evidence], questions=["Q1", "Q3"],
                           change_log=["Change 1", "Change 3"])
        result = combine_parts([first, second])
        self.assertIsInstance(result, Draft)
        self.assertEqual(result.jira, first.jira + "\n\n" + second.jira)
        self.assertEqual(result.evidence, [evidence, distinct])
        self.assertEqual(result.questions, ["Q2", "Q1", "Q3"])
        self.assertEqual(result.change_log, ["Change 2", "Change 1", "Change 3"])

    def test_rejects_missing_duplicate_reordered_and_unterminated_parts(self):
        invalid = [[], [self.part(2)], [self.part(more=True), self.part(3)],
                   [self.part(more=True), self.part(1)],
                   [self.part(2, more=True), self.part(1)],
                   [self.part(more=True)], [self.part(), self.part(2)],
                   [self.part(more=True), self.part(2, more=True)]]
        for parts in invalid:
            with self.subTest(indices=[part.index for part in parts],
                              more=[part.more for part in parts]):
                with self.assertRaises(ValueError):
                    combine_parts(parts)

    def test_all_fields_represented_in_evidence_deduplication(self):
        base = self.evidence()
        variants = [base.model_copy(update={field: value}) for field, value in
                    [("path", "other.txt"), ("symbol", "other-setting"),
                     ("revision", "other-sha"), ("observation", "Other observation")]]
        result = combine_parts([self.part(evidence=[base, *variants, base])])
        self.assertEqual(result.evidence, [base, *variants])

    def test_combined_result_is_validated_as_draft(self):
        parts = [self.part(index, more=index < 2,
                           questions=[f"Question {index}-{i}" for i in range(20)])
                 for index in (1, 2)]
        with self.assertRaisesRegex(ValueError, "общие ограничения схемы"):
            combine_parts(parts)

    def test_evidence_overflow_preserves_parts_and_has_safe_diagnostic(self):
        parts = [self.part(more=True,
                           evidence=[self.evidence(f"PRIVATE_OBSERVATION_{i}") for i in range(200)]),
                 self.part(2, more=True,
                           evidence=[self.evidence(f"PRIVATE_OBSERVATION_{i}") for i in range(200, 400)]),
                 self.part(3, evidence=[self.evidence("PRIVATE_OBSERVATION_400")])]
        snapshots = [part.model_dump() for part in parts]
        with self.assertRaises(ValueError) as caught:
            combine_parts(parts)
        self.assertEqual(str(caught.exception),
                         "Сборка превысила общие ограничения схемы; части сохранены, "
                         "повторный resume это не исправит")
        self.assertNotIn("PRIVATE_OBSERVATION", str(caught.exception))
        self.assertEqual([part.model_dump() for part in parts], snapshots)

    def test_combines_201_unique_evidence_items_without_loss(self):
        first = [self.evidence(f"Observation {i}") for i in range(101)]
        second = [self.evidence(f"Observation {i}") for i in range(101, 201)]
        result = combine_parts([self.part(more=True, evidence=first),
                                self.part(2, evidence=second)])
        self.assertEqual(len(result.evidence), 201)
        self.assertEqual(result.evidence, first + second)

    def test_index_schema_bounds_and_strict_validation(self):
        schema = DraftPart.model_json_schema()["properties"]["index"]
        self.assertEqual(schema["minimum"], 1)
        self.assertEqual(schema["maximum"], MAX_DRAFT_PARTS)
        for index in (0, MAX_DRAFT_PARTS + 1, True, "1"):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.part(index)
        self.assertEqual(self.part(MAX_DRAFT_PARTS).index, MAX_DRAFT_PARTS)

    def test_part_field_bounds(self):
        for changes in ({"jira": "short"}, {"jira": "x" * 30001}, {"evidence": []},
                        {"evidence": [self.evidence()] * 201},
                        {"questions": ["Q"] * 21}, {"change_log": ["C"] * 101}):
            with self.subTest(fields=list(changes)), self.assertRaises(ValidationError):
                self.part(**changes)

    def test_prompts_preserve_original_only_for_first_part_and_require_completeness(self):
        original = "UNIQUE_ORIGINAL_REQUEST: preserve exact user requirements."
        first = part_prompt(original, 1)
        continuation = part_prompt(original, 2)
        self.assertTrue(first.startswith(original + "\n\n"))
        self.assertNotIn(original, continuation)
        self.assertIn("первую часть", first)
        self.assertIn("том же Thread", continuation)
        for index, prompt in ((1, first), (2, continuation)):
            with self.subTest(index=index):
                for required in ("DraftPart", f"index={index}", "2800 токенов", "15000 UTF-8 байт",
                                 "сквозную нумерацию", "все требования и решения",
                                 "конкретные настройки и полки полностью", "именованными профилями",
                                 "таблицы", "внутри одной полки", "more=false", "критерии приёмки",
                                 "материалы и границы", "Не обрезай JSON", "пропусков",
                                 "questions без выдуманных ответов", "Не повторяй"):
                    self.assertIn(required, prompt)

    def test_prompt_rejects_invalid_index(self):
        for index in (0, MAX_DRAFT_PARTS + 1, True, "1"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                part_prompt("Synthetic request", index)


if __name__ == "__main__":
    unittest.main()
