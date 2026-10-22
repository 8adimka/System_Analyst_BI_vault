"""Apply exact draft edits while retaining untouched text and current evidence."""
import posixpath
from pathlib import PurePosixPath

from pydantic import Field, ValidationError

from .models import Draft, Evidence, Message


class Replacement(Message):
    before: str = Field(min_length=1, max_length=50000)
    after: str = Field(max_length=100000)
    expected_count: int = Field(ge=1, le=100)


class DraftPatch(Message):
    replacements: list[Replacement] = Field(min_length=1, max_length=50)
    evidence: list[Evidence] = Field(min_length=1, max_length=100)
    drop_stale_evidence: bool
    questions: list[str] = Field(max_length=20)
    change_log: list[str] = Field(max_length=100)


def _current(item: Evidence, manifest: dict) -> bool:
    path = PurePosixPath(posixpath.normpath(item.path))
    if path.is_absolute() or ".." in path.parts:
        return False
    expected = manifest["sources"].get(str(path))
    for root, revision in manifest["repositories"].items():
        if path.is_relative_to(PurePosixPath(posixpath.normpath(root))):
            expected = revision
    return expected is not None and item.revision == expected


def apply_draft_patch(draft: Draft, patch: DraftPatch, manifest: dict) -> Draft:
    """Return a validated edited copy; the caller must verify its evidence."""
    text = draft.jira
    for replacement in patch.replacements:
        if replacement.before == replacement.after:
            raise ValueError("Правка не изменяет текст")
        count = 0
        position = text.find(replacement.before)
        while position >= 0:
            count += 1
            if count > replacement.expected_count:
                break
            position = text.find(replacement.before, position + 1)
        if count != replacement.expected_count or count != text.count(replacement.before):
            raise ValueError("Число вхождений исходного фрагмента не совпало с ожидаемым "
                             "либо вхождения перекрываются")
        text = text.replace(replacement.before, replacement.after)

    evidence = []
    seen = set()
    for item in draft.evidence:
        if not patch.drop_stale_evidence or _current(item, manifest):
            marker = item.model_dump_json()
            if marker not in seen:
                seen.add(marker)
                evidence.append(item)
    for item in patch.evidence:
        marker = item.model_dump_json()
        if marker not in seen:
            seen.add(marker)
            evidence.append(item)
    try:
        return Draft.model_validate({"jira": text, "evidence": evidence,
                                     "questions": patch.questions,
                                     "change_log": patch.change_log})
    except ValidationError:
        raise ValueError("Результат правки нарушает ограничения схемы Draft; "
                         "исходная постановка сохранена") from None
