"""Контракты действий: схемы, нормализация, таксономия ошибок.

Ошибка действия — всегда ActionError: код, сообщение и машиночитаемые
подсказки (to_tool_result). Валидационные ошибки (unknown_tariff, bad_phone
и т.п.) модель читает и исправляет вызов; отказы бизнес-правил
(BUSINESS_RULE_CODES) повторять бессмысленно — модель пересказывает их
абоненту. Нормализация приводит название «как услышали» к точному значению
каталога.
"""
from __future__ import annotations

import re

BUSINESS_RULE_CODES = frozenset({
    "same_tariff",
    "tariff_not_available",
    "service_already_on",
    "service_already_off",
    "same_contacts",
})


class ActionError(Exception):
    """Отказ действия: код ошибки, сообщение и подсказки для модели."""

    def __init__(self, code: str, message: str, **hints) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints

    def to_tool_result(self) -> dict:
        """Машиночитаемое представление отказа: код, деталь и подсказки."""
        return {"error": self.code, "detail": self.message, **self.hints}


def _heard_key(spoken: str) -> str:
    """Ключ сравнения «как услышали»: нижний регистр, только буквы и цифры."""
    return "".join(symbol for symbol in spoken.lower() if symbol.isalnum())


def _match_by_heard_name(spoken: str, exact_names: list[str]) -> str | None:
    """Найти точное имя из каталога по произнесённому варианту.

    Сначала точное совпадение ключа («семейный» = «Семейный»), затем
    вхождение («семейн» → «Семейный»). Не подобрали — None.
    """
    spoken_key = _heard_key(spoken)
    if not spoken_key:
        return None
    for exact_name in exact_names:
        if _heard_key(exact_name) == spoken_key:
            return exact_name
    for exact_name in exact_names:
        if spoken_key in _heard_key(exact_name):
            return exact_name
    return None


def normalize_tariff(spoken: str, catalog: list[dict]) -> str:
    """Название тарифа «как услышали» → точное имя из каталога.

    Каталог — строки tariffs из БД ({name, available, ...}). Не подобрали —
    ActionError unknown_tariff со списком доступных имён.
    """
    matched = _match_by_heard_name(spoken, [entry["name"] for entry in catalog])
    if matched is None:
        raise ActionError(
            "unknown_tariff",
            f"тариф «{spoken}» не найден в каталоге",
            available=[entry["name"] for entry in catalog if entry.get("available", True)],
        )
    return matched


def normalize_service(spoken: str, catalog_names: list[str]) -> str:
    """Название услуги «как услышали» → точное имя из каталога услуг."""
    matched = _match_by_heard_name(spoken, catalog_names)
    if matched is None:
        raise ActionError(
            "unknown_service",
            f"услуга «{spoken}» не найдена в каталоге",
            available=list(catalog_names),
        )
    return matched


def normalize_phone(spoken: str) -> str:
    """Телефон «как услышали» → канонический вид +7 XXX XXX-XX-XX.

    Принимает 11 цифр с ведущей 7 или 8. Иначе — ActionError bad_phone
    с примером корректного номера.
    """
    digits = re.sub(r"\D", "", spoken or "")
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise ActionError(
            "bad_phone",
            f"номер «{spoken}» не распознан: нужно 11 цифр",
            example="+7 900 111-22-33",
        )
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def normalize_email(spoken: str) -> str:
    """Почта «как услышали» → нижний регистр, проверка формата.

    Иначе — ActionError bad_email с примером корректного адреса.
    """
    candidate = (spoken or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate):
        raise ActionError(
            "bad_email",
            f"почта «{spoken}» не распознана",
            example="anna@example.ru",
        )
    return candidate
