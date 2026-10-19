"""Small syntax gate for Jira artifacts; semantic review remains the reviewer's job."""

from __future__ import annotations

import re


_CODE = re.compile(r"\{code(?::[^}\r\n]+)?\}")
_AC = re.compile(r"(?m)^\s*\*?AC\.(\d+)\b[^\r\n]*")
_FIELD = re.compile(r"(?m)^\s*(?:\*\s+)?\*?(Дано|Когда|Тогда):\*?\s*(.*)$")
_ENGLISH_HEADINGS = {
    "context", "background", "goal", "scope", "out of scope",
    "current behavior", "target behavior", "functional requirements",
    "business rules", "edge cases", "test notes", "dependencies", "risks",
    "open questions", "assumptions", "acceptance criteria",
}
_SECTION = re.compile(r"(?m)^[ \t]*h3\.[ \t]+([^\r\n]+)[ \t]*$")
_QUESTION_TITLE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?Открытые\s+вопросы[.:/]*$", re.I
)
_NO_QUESTIONS = {"нет", "отсутствуют", "нет открытых вопросов"}


def _prose(text: str) -> tuple[str, bool]:
    """Mask real Jira code blocks while retaining line structure for diagnostics."""
    result: list[str] = []
    inside = False
    position = 0
    malformed = False
    for token in _CODE.finditer(text):
        fragment = text[position:token.start()]
        result.append(re.sub(r"[^\n]", " ", fragment) if inside else fragment)
        if inside and token.group() != "{code}":
            malformed = True
        inside = not inside
        result.append(" " * len(token.group()))
        position = token.end()
    tail = text[position:]
    result.append(re.sub(r"[^\n]", " ", tail) if inside else tail)
    return "".join(result), malformed or inside


def _question_sections(text: str) -> list[tuple[int, int, int]]:
    """Locate only real Jira sections; code examples cannot create headings."""
    prose, _ = _prose(text)
    headings = list(_SECTION.finditer(prose))
    return [
        (heading.start(), heading.end(), headings[index + 1].start() if index + 1 < len(headings) else len(text))
        for index, heading in enumerate(headings)
        if _QUESTION_TITLE.fullmatch(heading.group(1).strip())
    ]


def _clean_question(value: str) -> str:
    value = " ".join(value.split())
    value = re.sub(r"^(?:[*#]+\s+|\d+[.)]\s+)", "", value).strip()
    if not value or value == "----":
        return ""
    # Ignore an explicit absence marker, including ordinary Jira emphasis.
    marker = value.strip("*_ ").rstrip(".").strip().casefold()
    return "" if marker in _NO_QUESTIONS else value


def _unique_questions(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        question = _clean_question(value)
        key = question.rstrip(" .;?!").casefold()
        if question and key not in seen:
            result.append(question)
            seen.add(key)
    return result


def extract_open_questions(text: str) -> list[str]:
    """Read substantive lines from Russian open-question h3 sections.

    This catches a question left in the artifact even when structured questions
    are empty. It does not infer that an arbitrary question has been resolved.
    """
    prose, _ = _prose(text)
    lines = [
        line
        for _, body, end in _question_sections(text)
        for line in prose[body:end].splitlines()
    ]
    return _unique_questions(lines)


def with_open_questions(text: str, questions: list[str]) -> str:
    """Merge unresolved questions into one section, retaining other sections.

    Existing substantive questions remain until the author removes them from
    the draft. An empty structured list must not silently resolve artifact text.
    """
    sections = _question_sections(text)
    merged = _unique_questions(extract_open_questions(text) + questions)
    if not sections:
        if not merged:
            return text
        return text.rstrip() + "\n\n----\nh3. Открытые вопросы.\n" + "\n".join("* " + q for q in merged) + "\n"
    if len(sections) == 1 and not questions:
        # No update needed; preserve authored formatting and any code context.
        existing = extract_open_questions(text)
        prose, _ = _prose(text)
        _, body, end = sections[0]
        original_lines = [_clean_question(line) for line in prose[body:end].splitlines()]
        substantive = [line for line in original_lines if line]
        if len(substantive) == len(existing):
            return text

    first_start, first_body, first_end = sections[0]
    header = text[first_start:first_body]
    body = "\n".join("* " + question for question in merged) if merged else "Нет."
    # Keep technical context inside a question section when normalizing it.
    # _prose masks code for extraction; preserve the actual blocks verbatim.
    code_context: list[str] = []
    for _, start, end in sections:
        section = text[start:end]
        token_start = None
        for token in _CODE.finditer(section):
            if token_start is None:
                token_start = token.start()
            else:
                block = section[token_start:token.end()]
                if block not in code_context:
                    code_context.append(block)
                token_start = None
    if code_context:
        body += "\n\n" + "\n\n".join(code_context)
    replacement = header + "\n" + body + "\n"
    if first_end < len(text):
        replacement += "\n----\n"
    result = text[:first_start] + replacement
    position = first_end
    for start, _, end in sections[1:]:
        result += text[position:start]
        position = end
    return result + text[position:]


def validate_jira(text: str) -> list[str]:
    """Return actionable formatting errors, with no claim of semantic approval.

    A compact task may combine sections; it still needs described behavior and
    numbered acceptance checks. Code content is exempt from prose markup rules.
    """
    if not isinstance(text, str) or not text.strip():
        return ["Постановка пуста."]
    prose, broken_code = _prose(text)
    errors: list[str] = []
    if broken_code:
        errors.append("Нарушена пара открывающего/закрывающего блока {code}.")
    # A single '#' is the ordinary Jira numbered-list marker.
    if re.search(r"(?m)^\s*#{2,6}\s+", prose):
        errors.append("Markdown-заголовки недопустимы: используй h3./h4.")
    if re.search(r"(?m)^\s*(?:```|~~~)", prose):
        errors.append("Markdown-блоки кода недопустимы: используй {code:...}.")
    if "`" in prose:
        errors.append("Для технических имён используй {{...}} вместо обратных кавычек.")
    if re.search(r"\[[^\]\n]+\]\([^\n)]+\)", prose):
        errors.append("Markdown-ссылки недопустимы: используй [описание|ссылка].")
    if re.search(r"(?m)^h[1256]\.\s", prose):
        errors.append("Основные заголовки должны быть h3., подразделы — h4.")

    headings = list(re.finditer(r"(?m)^h3\.\s+(.+)$", prose))
    if len(headings) < 2:
        errors.append("Нужны раздел с описанием требований и раздел критериев приёмки h3.")
    for heading in re.finditer(r"(?m)^h[34]\.\s+(.+)$", prose):
        title = re.sub(r"^[\d.]+\s*", "", heading.group(1)).strip(" .:").casefold()
        if title in _ENGLISH_HEADINGS:
            errors.append("Шаблонные заголовки должны быть на русском языке.")
            break

    checks = list(_AC.finditer(prose))
    if not checks:
        errors.append("Отсутствуют нумерованные критерии приёмки AC.1, AC.2, ...")
        return errors
    numbers = [int(match.group(1)) for match in checks]
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append("Нумерация AC должна начинаться с 1 и идти без пропусков и повторов.")

    prefix = prose[:checks[0].start()]
    body = re.sub(r"(?m)^h[34]\..*$|^\s*-{3,}\s*$", "", prefix)
    if not re.search(r"[А-Яа-яA-Za-z]{2,}", body):
        errors.append("Перед AC опиши требуемое поведение: AC не заменяют требования.")
    if not any(re.search(r"критери[ияевй].*при[её]мк", h.group(1), re.I) for h in headings):
        errors.append("Добавь русский раздел «Критерии приёмки (AC)».")

    for index, check in enumerate(checks):
        end = checks[index + 1].start() if index + 1 < len(checks) else len(prose)
        chunk = prose[check.end():end]
        # Later sections must not accidentally supply missing fields to an AC.
        chunk = re.split(r"(?m)^h3\.\s", chunk, maxsplit=1)[0]
        fields = {name: value.strip(" ;.*") for name, value in _FIELD.findall(chunk)}
        for name in ("Дано", "Когда", "Тогда"):
            if not fields.get(name) or fields[name] in {"...", "…"}:
                errors.append(f"AC.{check.group(1)}: заполни «{name}» конкретным условием/действием/результатом.")
    return errors
