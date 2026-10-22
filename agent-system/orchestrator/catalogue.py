"""Select and regroup Jira catalogue sections without rewriting their bodies."""
import re


_H3 = re.compile(r"^h3\.[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
_NUMBERED_H3 = re.compile(r"h3\. (\d+)\. (.+)")
_SUBHEADING = re.compile(r"^h([4-9]|[1-9][0-9]+)\.([ \t]+)([^\r\n]*)", re.MULTILINE)


def _body(text: str, old_number: str, new_number: str, nested: bool) -> str:
    lines = text.splitlines(keepends=True)
    while lines and lines[-1].strip() in {"", "----"}:
        lines.pop()
    text = "".join(lines).rstrip("\r\n")

    def replace(match):
        level = int(match[1]) + int(nested)
        if level > 6:
            raise ValueError("Группировка создаёт заголовок глубже h6")
        title = match[3]
        old_prefix = old_number + "."
        if title.startswith(old_prefix):
            title = new_number + "." + title[len(old_prefix):]
        return f"h{level}." + match[2] + title

    return _SUBHEADING.sub(replace, text)


def select_sections(document: str, groups: list[tuple[str, list[str]]]) -> str:
    """Keep exact requested titles in group order, rejecting ambiguous selection."""
    if not groups:
        raise ValueError("Требуется непустой список групп")
    selected = set()
    for new_title, names in groups:
        if not isinstance(new_title, str) or not new_title.strip() or not names:
            raise ValueError("Названия и группы должны быть непустыми")
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Названия и группы должны быть непустыми")
            if name in selected:
                raise ValueError("Исходный заголовок запрошен повторно")
            selected.add(name)

    boundaries = list(_H3.finditer(document))
    sections = {}
    for index, boundary in enumerate(boundaries):
        heading = _NUMBERED_H3.fullmatch(boundary[0].rstrip("\r\n"))
        if heading is None:
            continue
        old_number, title = heading.groups()
        if title in sections:
            raise ValueError("Исходный нумерованный заголовок встречается повторно")
        end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(document)
        sections[title] = (old_number, document[boundary.end():end])
    if selected - sections.keys():
        raise ValueError("Запрошенный исходный заголовок не найден")

    result = []
    for number, (new_title, names) in enumerate(groups, start=1):
        blocks = [f"h3. {number}. {new_title}"]
        nested = len(names) > 1
        for variant, name in enumerate(names, start=1):
            old_number, text = sections[name]
            new_number = f"{number}.{variant}" if nested else str(number)
            if nested:
                blocks.append(f"h4. {new_number}. {name}")
            body = _body(text, old_number, new_number, nested)
            if body:
                blocks.append(body)
        result.append("\n\n".join(blocks))
    return "\n\n----\n\n".join(result)
