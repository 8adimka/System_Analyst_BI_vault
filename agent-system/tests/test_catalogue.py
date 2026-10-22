"""Synthetic checks for exact, mechanical Jira catalogue selection."""
import unittest

from orchestrator.catalogue import select_sections


class CatalogueTests(unittest.TestCase):
    def test_exact_selection_and_requested_order(self):
        source = ("Old introduction.\nh3. 1. Alpha\nAlpha body.\n----\n"
                  "h3. 2. Alpha extra\nExcluded body.\n"
                  "h3. 3. Beta\nh4. 3.1. Shelf\nKeep reference 3.1 unchanged.\n"
                  "h5. 3.1.1. Setting\nSetting body.\nh6. 3.1.1.1. Detail\nDetail body.\n----\n----\n")
        result = select_sections(source, [("Second", ["Beta"]), ("First", ["Alpha"])])
        self.assertEqual(result,
                         "h3. 1. Second\n\nh4. 1.1. Shelf\nKeep reference 3.1 unchanged.\n"
                         "h5. 1.1.1. Setting\nSetting body.\nh6. 1.1.1.1. Detail\nDetail body."
                         "\n\n----\n\nh3. 2. First\n\nAlpha body.")
        self.assertNotIn("Old introduction", result)
        self.assertNotIn("Alpha extra", result)
        self.assertNotIn("Excluded body", result)

    def test_two_variants_keep_bodies_and_renumber_nested_headings(self):
        source = ("h3. 8. Variant A\nh4. 8.1. Shelf A\nBody A mentions 8.1.\n"
                  "h5. 8.1.1. Detail A\nDetail body A.\n----\n"
                  "h3. 9. Variant B\nh4. 9.2. Shelf B\nBody B.\n----\n")
        result = select_sections(source, [("Combined", ["Variant B", "Variant A"])])
        self.assertEqual(result,
                         "h3. 1. Combined\n\nh4. 1.1. Variant B\n\nh5. 1.1.2. Shelf B\nBody B."
                         "\n\nh4. 1.2. Variant A\n\nh5. 1.2.1. Shelf A\nBody A mentions 8.1.\n"
                         "h6. 1.2.1.1. Detail A\nDetail body A.")

    def test_unnumbered_tail_is_not_part_of_last_visualization(self):
        source = ("h3. 4. Alpha\nKeep this body.\n----\n"
                  "h3. Критерии приёмки\nAC.1 — Exclude this tail.\n"
                  "h3. Материалы\nExclude source list.")
        self.assertEqual(select_sections(source, [("Alpha", ["Alpha"])]),
                         "h3. 1. Alpha\n\nKeep this body.")

    def test_rejects_missing_and_duplicate_input_titles(self):
        for source, groups in [
            ("h3. 1. Alpha\nBody.", [("Chosen", ["Missing"])]),
            ("h3. 1. Alpha\nOne.\nh3. 2. Alpha\nTwo.", [("Chosen", ["Alpha"])]),
            ("h3. 1. Alpha\nOne.\nh3. 2. Other\nTwo.\nh3. 3. Other\nThree.",
             [("Chosen", ["Alpha"])])]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                select_sections(source, groups)

    def test_rejects_empty_groups_names_and_repeated_requests(self):
        for groups in ([], [("", ["Alpha"])], [("Chosen", [])], [("Chosen", [""])],
                       [("Chosen", ["Alpha", "Alpha"])],
                       [("One", ["Alpha"]), ("Two", ["Alpha"])]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                select_sections("h3. 1. Alpha\nBody.", groups)

    def test_rejects_promoting_h6_beyond_supported_depth(self):
        source = ("h3. 1. Alpha\nh6. 1.1.1.1. Deep detail\nBody.\n"
                  "h3. 2. Beta\nOther body.")
        with self.assertRaisesRegex(ValueError, "глубже h6"):
            select_sections(source, [("Combined", ["Alpha", "Beta"])])


if __name__ == "__main__":
    unittest.main()
