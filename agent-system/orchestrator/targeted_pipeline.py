"""Reuse the normal review state machine with exact edits for draft revisions."""
import json

from .inputs import verify_evidence
from .models import Draft
from .patching import DraftPatch, apply_draft_patch
from .pipeline import Pipeline


_PATCH_INSTRUCTIONS = (
    "Для этого ответа итоговый формат — только JSON DraftPatch по output_schema. "
    "Исправь замечания точечными replacements, сохрани весь остальной документ; "
    "не пересоздавай полную постановку. Ориентир JSON: 2200 токенов. "
    "Выбирай небольшие абзацы или строки таблиц. before — точный фрагмент текущего draft.jira, "
    "after — исправленный текст; пустой after удаляет фрагмент. "
    "Обязательный expected_count — точное число всех вхождений before в текущем тексте "
    "на момент этой последовательной правки; для уникального фрагмента укажи 1. "
    "Подсчитай вхождения перед ответом. Пакетная замена допустима, только если все вхождения "
    "относятся к замечанию; иначе добавь в before достаточный уникальный контекст. "
    "Перекрывающиеся вхождения запрещены. Не присылай фиктивные правки before==after. "
    "В evidence передай подтверждающие первоисточники с точными текущими версиями. "
    "drop_stale_evidence=true удалит лишь старые свидетельства, чьи версии не совпадают с manifest; "
    "не перемаркировывай старые версии. questions — полный актуальный список оставшихся вопросов; "
    "change_log свяжи с ID исправляемых замечаний."
)
_PATCH_FEEDBACK = (
    "Предыдущая правка не применилась механически; текущий Draft остался прежним. "
    "Проверь точность before, expected_count, отсутствие перекрытий и фиктивных правок, "
    "а также ограничения итоговой схемы. Верни исправленный DraftPatch."
)


class TargetedPipeline(Pipeline):
    def _call(self, state, role, kind, prompt, cls, *, expected_index=None):
        token = f"{kind}-{state.version}-{state.cycles}-{len(state.answers)}"
        relative = f"responses/{token}.json"
        targeted = (cls is Draft and role == "analyst" and state.stage == "REVISION"
                    and state.draft is not None)
        if not targeted or self.gateway.path(relative).exists():
            return super()._call(state, role, kind, prompt, cls, expected_index=expected_index)

        self._check_versions(state)
        feedback = ""
        for attempt in range(1, 4):
            operation = f"{kind}-patch-{attempt}"
            patch_relative = (f"responses/{operation}-{state.version}-{state.cycles}-"
                              f"{len(state.answers)}.json")
            path = self.gateway.path(patch_relative)
            cached = path.exists()
            self.progress({"stage": state.stage, "role": role, "version": state.version,
                           "review_cycles": state.cycles, "cycles": state.cycles,
                           "operation": operation})
            if cached:
                patch = DraftPatch.model_validate(self.gateway.read_json(patch_relative))
            else:
                if "analyst" not in state.threads:
                    raise ValueError("Для точечных правок нужен сохранённый Thread аналитика")
                self._thread(state, role)
                patch_prompt = prompt + "\n\n" + _PATCH_INSTRUCTIONS
                if feedback:
                    patch_prompt += "\n\n" + feedback
                payload = self.backend.run(role, patch_prompt, DraftPatch.model_json_schema())
                if len(json.dumps(payload).encode()) > 2_000_000:
                    raise ValueError("Ответ модели превышает лимит")
                patch = DraftPatch.model_validate(payload)
            if len(patch.model_dump_json().encode()) > 2_000_000:
                raise ValueError("Ответ модели превышает лимит")
            verify_evidence(self.workspace, patch.evidence, state.model_dump())
            self._check_versions(state)
            if not cached:
                self.gateway.write_json(patch_relative, patch.model_dump())

            try:
                result = apply_draft_patch(state.draft, patch, state.model_dump())
            except ValueError:
                feedback = _PATCH_FEEDBACK
                continue
            verify_evidence(self.workspace, result.evidence, state.model_dump())
            self._check_versions(state)
            if len(result.model_dump_json().encode()) > 2_000_000:
                raise ValueError("Ответ модели превышает лимит")
            self.gateway.write_json(relative, result.model_dump())
            return super()._call(state, role, kind, prompt, cls, expected_index=expected_index)
        raise ValueError("Три попытки точечной правки не применились; "
                         "исходная постановка и счётчик ревью сохранены")
