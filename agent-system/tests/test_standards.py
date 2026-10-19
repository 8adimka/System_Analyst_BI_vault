from __future__ import annotations

import unittest

from orchestrator.jira import extract_open_questions, validate_jira, with_open_questions


COMPACT_TASK = """h3. Требуемое поведение.
При снятии выбранного значения поле очищается и остаётся доступным для ввода.

----
h3. Критерии приёмки (AC).
AC.1 — *Снятие выбора:*
* *Дано:* значение выбрано;
* *Когда:* пользователь снимает выбор;
* *Тогда:* поле пустое и доступно для ввода.
"""


class JiraArtifactTests(unittest.TestCase):
    def test_compact_complete_task_does_not_require_empty_template_sections(self):
        self.assertEqual(validate_jira(COMPACT_TASK), [])

    def test_markdown_disguised_as_jira_is_rejected(self):
        broken = COMPACT_TASK.replace("h3. Требуемое поведение.", "### Требуемое поведение")
        broken += "\n```json\n{}\n```\n[Ссылка](https://example.test)\n"
        errors = validate_jira(broken)
        self.assertGreaterEqual(len(errors), 3)
        self.assertTrue(any("Markdown-заголовки" in error for error in errors))
        self.assertTrue(any("Markdown-блоки" in error for error in errors))
        self.assertTrue(any("Markdown-ссылки" in error for error in errors))

    def test_actual_code_can_contain_markdown_and_ac_looking_lines(self):
        technical = """{code:python}
# heading in a program
print("`value`")
AC.99
{code}
"""
        self.assertEqual(validate_jira(COMPACT_TASK.replace("----", technical + "\n----")), [])

    def test_ordinary_jira_numbered_list_is_allowed(self):
        self.assertEqual(validate_jira(COMPACT_TASK.replace("----", "# Очистить значение.\n# Оставить ввод доступным.\n\n----")), [])

    def test_code_opening_cannot_hide_document_from_checks(self):
        self.assertTrue(validate_jira("{code:json}\n" + COMPACT_TASK))

    def test_missing_observable_result_cannot_pass_as_acceptance(self):
        broken = COMPACT_TASK.replace("* *Тогда:* поле пустое и доступно для ввода.", "* *Тогда:* ...")
        self.assertTrue(any("Тогда" in error for error in validate_jira(broken)))

    def test_later_section_cannot_supply_missing_acceptance_result(self):
        broken = COMPACT_TASK.replace("* *Тогда:* поле пустое и доступно для ввода.", "")
        broken += "\nh3. Примечание.\n* *Тогда:* посторонний результат.\n"
        self.assertTrue(any("Тогда" in error for error in validate_jira(broken)))

    def test_duplicate_and_skipped_acceptance_numbers_are_rejected(self):
        extra = "\n" + COMPACT_TASK.split("AC.1", 1)[1]
        self.assertTrue(any("Нумерация" in e for e in validate_jira(COMPACT_TASK + "\nAC.3" + extra)))
        self.assertTrue(any("Нумерация" in e for e in validate_jira(COMPACT_TASK + "\nAC.1" + extra)))

    def test_acceptance_is_not_the_only_place_for_requirements(self):
        broken = COMPACT_TASK.replace(
            "При снятии выбранного значения поле очищается и остаётся доступным для ввода.", ""
        )
        self.assertTrue(any("Перед AC" in error for error in validate_jira(broken)))

    def test_empty_document_and_english_template_are_rejected(self):
        self.assertTrue(validate_jira(""))
        broken = COMPACT_TASK.replace("Требуемое поведение", "Target Behavior")
        self.assertTrue(any("русском" in error for error in validate_jira(broken)))

    def test_semantic_review_is_still_required_after_syntax_gate(self):
        # The syntax gate cannot establish whether product behavior is true.
        plausible_but_unproved = COMPACT_TASK.replace("поле", "Мера")
        self.assertEqual(validate_jira(plausible_but_unproved), [])


class OpenQuestionTests(unittest.TestCase):
    def test_question_in_jira_remains_visible_when_structured_list_is_empty(self):
        draft = COMPACT_TASK + "\n----\nh3. 6. Открытые вопросы:\n* Что показать при недоступности сервера?\n"
        self.assertEqual(extract_open_questions(draft), ["Что показать при недоступности сервера?"])
        self.assertEqual(extract_open_questions(with_open_questions(draft, [])),
                         ["Что показать при недоступности сервера?"])

    def test_explicit_absence_markers_do_not_block_approval(self):
        for marker in ("Нет.", "Отсутствуют.", "Нет открытых вопросов.", "* Нет.", "*Нет.*"):
            with self.subTest(marker=marker):
                self.assertEqual(extract_open_questions("h3. Открытые вопросы.\n" + marker), [])
        self.assertEqual(extract_open_questions("h3. Открытые вопросы.\n* Нет. Нужно уточнить реакцию на ошибку."),
                         ["Нет. Нужно уточнить реакцию на ошибку."])

    def test_extraction_stops_at_the_next_h3_and_ignores_code_examples(self):
        text = """h3. 7. Открытые вопросы/
# Какое значение сохранять?

----
h3. Материалы.
* Эта ссылка не является вопросом.
{code:text}
h3. Открытые вопросы.
* Пример заголовка внутри кода.
{code}
"""
        self.assertEqual(extract_open_questions(text), ["Какое значение сохранять?"])

    def test_merge_does_not_duplicate_existing_question_or_header(self):
        draft = COMPACT_TASK + "\nh3. 6. Открытые вопросы.\n* Какое значение сохранять?\n"
        merged = with_open_questions(draft, ["Какое значение сохранять?", "Какое значение сохранять?",
                                            "Нужно ли сохранять пробелы?"])
        self.assertEqual(merged.count("Открытые вопросы"), 1)
        self.assertEqual(extract_open_questions(merged),
                         ["Какое значение сохранять?", "Нужно ли сохранять пробелы?"])
        self.assertEqual(merged.count("Какое значение сохранять?"), 1)
        self.assertEqual(with_open_questions(merged, ["Нужно ли сохранять пробелы?"]), merged)

    def test_merge_replaces_absence_marker_and_retains_later_sections(self):
        later = "h3. Материалы.\n* [Ссылка|https://example.test]\n{code:json}\n{}\n{code}\n"
        draft = COMPACT_TASK + "\nh3. Открытые вопросы.\nНет.\n\n----\n" + later
        merged = with_open_questions(draft, ["Как обрабатывать недоступность?"])
        self.assertTrue(merged.startswith(COMPACT_TASK))
        self.assertTrue(merged.endswith(later))
        self.assertNotIn("\nНет.\n", merged)
        self.assertEqual(extract_open_questions(merged), ["Как обрабатывать недоступность?"])

    def test_multiple_existing_sections_are_consolidated_without_losing_other_sections(self):
        middle = "h3. Ограничения.\nНе изменять сортировку.\n\n----\n"
        draft = ("h3. Открытые вопросы.\n* Вопрос один?\n\n----\n" + middle
                 + "h3. 8. Открытые вопросы:\n* Вопрос один?\n* Вопрос два?\n")
        merged = with_open_questions(draft, ["Вопрос два?", "Вопрос три?"])
        self.assertEqual(merged.count("Открытые вопросы"), 1)
        self.assertIn(middle, merged)
        self.assertEqual(extract_open_questions(merged), ["Вопрос один?", "Вопрос два?", "Вопрос три?"])

    def test_new_section_is_created_only_for_substantive_questions(self):
        self.assertEqual(with_open_questions(COMPACT_TASK, []), COMPACT_TASK)
        self.assertEqual(with_open_questions(COMPACT_TASK, ["Нет.", "Отсутствуют."]), COMPACT_TASK)
        merged = with_open_questions(COMPACT_TASK, ["Проверять ли пробелы?", "Проверять ли пробелы?"])
        self.assertEqual(extract_open_questions(merged), ["Проверять ли пробелы?"])
        self.assertTrue(merged.startswith(COMPACT_TASK.rstrip()))

    def test_technical_context_inside_question_section_survives_merge(self):
        block = "{code:json}\n{\"sample\": true}\n{code}"
        draft = "h3. Открытые вопросы.\n* Как трактовать этот пример?\n" + block + "\n"
        merged = with_open_questions(draft, ["Нужно ли сохранять неизвестные поля?"])
        self.assertIn(block, merged)
        self.assertEqual(extract_open_questions(merged),
                         ["Как трактовать этот пример?", "Нужно ли сохранять неизвестные поля?"])


if __name__ == "__main__":
    unittest.main()
