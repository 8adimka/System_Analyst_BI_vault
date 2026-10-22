"""Assemble bounded structured parts without losing draft content or evidence."""
from pydantic import Field, ValidationError

from .models import Draft, Evidence, Message


MAX_DRAFT_PARTS = 80


class DraftPart(Message):
    index: int = Field(ge=1, le=MAX_DRAFT_PARTS)
    jira: str = Field(min_length=20, max_length=30000)
    more: bool
    evidence: list[Evidence] = Field(min_length=1, max_length=200)
    questions: list[str] = Field(max_length=20)
    change_log: list[str] = Field(max_length=100)


def _unique(items):
    result = []
    seen = set()
    for item in items:
        key = item.model_dump_json() if isinstance(item, Evidence) else item
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def combine_parts(parts: list[DraftPart]) -> Draft:
    if not parts or len(parts) > MAX_DRAFT_PARTS:
        raise ValueError(f"Требуется от 1 до {MAX_DRAFT_PARTS} частей постановки")
    for expected, part in enumerate(parts, start=1):
        if part.index != expected:
            raise ValueError("Индексы частей должны идти непрерывно начиная с 1")
        if part.more != (expected < len(parts)):
            raise ValueError("Только последняя часть должна иметь more=false")
    try:
        return Draft.model_validate({
            "jira": "\n\n".join(part.jira for part in parts),
            "evidence": _unique(item for part in parts for item in part.evidence),
            "questions": _unique(item for part in parts for item in part.questions),
            "change_log": _unique(item for part in parts for item in part.change_log),
        })
    except ValidationError:
        raise ValueError("Сборка превысила общие ограничения схемы; части сохранены, "
                         "повторный resume это не исправит") from None


def part_prompt(original_prompt: str, index: int) -> str:
    if type(index) is not int or not 1 <= index <= MAX_DRAFT_PARTS:
        raise ValueError(f"Индекс части должен быть целым числом от 1 до {MAX_DRAFT_PARTS}")
    instruction = (
        f"Верни только JSON по схеме DraftPart, index={index}. "
        + ("Подготовь первую часть постановки по полному запросу выше. " if index == 1 else
           "Продолжи постановку в том же Thread по сохранённому исходному запросу и решениям пользователя. ")
        + "Ориентир на часть: 2800 токенов и 15000 UTF-8 байт. "
        "Разбивай по границам разделов, сохраняй сквозную нумерацию документа и все требования и решения исходного запроса. "
        "Не повторяй ранее готовые части. Раскрывай конкретные настройки и полки полностью; "
        "не заменяй описание именованными профилями и отсылками. Допускаются таблицы и объединение типов "
        "с одинаковым набором настроек внутри одной полки. "
        "Для оставшихся разделов используй короткие Jira-таблицы: настройка, типы, значения/дефолт и условия. "
        "Сохрани каждый пункт меню и его существенное поведение; общие условия раскрывай внутри каждой визуализации. "
        "more=true означает, что запрос ещё не завершён; more=false допустимо только после выполнения всего запроса, "
        "включая критерии приёмки, материалы и границы. Не обрезай JSON и не экономь объём за счёт пропусков. "
        "При настоящей неразрешимой проблеме верни вопросы в questions без выдуманных ответов, "
        "но заверши применимый текст. Для этой части передай подтверждающие evidence и change_log."
    )
    return original_prompt + "\n\n" + instruction if index == 1 else instruction
