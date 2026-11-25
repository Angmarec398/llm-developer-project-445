"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

prepare_* ничего не исполняют: проверяют предусловия и собирают view для
карточки («с какого на какой, что изменится в цене»). Исполнение —
execute_action: его вызывает только код оркестратора после явного «да»;
повтор с тем же ключом отсекает слой идемпотентности (app.idempotency):
для смены тарифа это второй барьер поверх дедупликации биллинга, для
услуг и контактов — единственный.

Все запросы — через httpx с trust_env=False к REST-мокам стенда. Субъект
sub приходит аргументом до прав (осознанная дыра): права её закроют.
"""
from __future__ import annotations

import json

import httpx

from actions.contracts import (
    ActionError,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)
from app import idempotency
from core import config

SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]

# Исходы моков, которые снаружи выглядят как успешное исполнение.
_SUCCESS_OUTCOME_ALIASES = {"toggled": "executed", "updated": "executed"}


def _client() -> httpx.AsyncClient:
    """HTTP-клиент к REST-мокам: мимо системного прокси, короткий таймаут."""
    return httpx.AsyncClient(base_url=config.STAND_URL, trust_env=False, timeout=10)


async def _fetch_subscriber(client: httpx.AsyncClient, sub: str) -> dict:
    """Профиль абонента из CRM-мока; нет такого — subscriber_not_found."""
    response = await client.get("/mock/crm/subscriber", params={"sub": sub})
    if response.status_code == 404:
        raise ActionError("subscriber_not_found", f"абонент {sub} не найден в CRM")
    response.raise_for_status()
    return response.json()


async def _fetch_tariff_catalog(client: httpx.AsyncClient) -> list[dict]:
    """Полный каталог тарифов из CRM-мока (включая недоступные)."""
    response = await client.get("/mock/crm/tariffs")
    response.raise_for_status()
    return response.json()["tariffs"]


async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Подготовить смену тарифа: предусловия и view карточки, без исполнения.

    Отказы: subscriber_not_found, unknown_tariff (со списком доступных),
    same_tariff, tariff_not_available. Успех — {"ok", "action", "details",
    "view"} для карточки подтверждения.
    """
    async with _client() as client:
        subscriber = await _fetch_subscriber(client, sub)
        catalog = await _fetch_tariff_catalog(client)
    current_name = subscriber["tariff"]
    target_name = normalize_tariff(tariff, catalog)
    if target_name == current_name:
        raise ActionError("same_tariff", f"тариф «{target_name}» уже подключён", current=current_name)
    current_entry = next(entry for entry in catalog if entry["name"] == current_name)
    target_entry = next(entry for entry in catalog if entry["name"] == target_name)
    if not target_entry.get("available", True):
        raise ActionError("tariff_not_available", f"тариф «{target_name}» сейчас недоступен для подключения")
    view = {
        "что": "смена тарифа",
        "с тарифа": current_name,
        "на тариф": target_name,
        "цена сейчас, руб/мес": current_entry["price_rub"],
        "цена после, руб/мес": target_entry["price_rub"],
        "разница, руб/мес": target_entry["price_rub"] - current_entry["price_rub"],
        "входит": target_entry.get("description", ""),
    }
    return {
        "ok": True,
        "action": "tariff_change",
        "details": {"tariff": target_name},
        "view": view,
    }


async def prepare_toggle_service(sub: str, service: str, enabled: bool) -> dict:
    """Подготовить вкл/выкл услуги: предусловия и view карточки.

    Отказы: subscriber_not_found, unknown_service (со списком каталога),
    service_already_on / service_already_off — повторять бессмысленно.
    """
    async with _client() as client:
        subscriber = await _fetch_subscriber(client, sub)
    service_name = normalize_service(service, SERVICE_CATALOG)
    connected = next(
        (entry for entry in subscriber.get("services", []) if entry["service"] == service_name),
        None,
    )
    currently_enabled = bool(connected and connected["enabled"])
    if enabled and currently_enabled:
        raise ActionError("service_already_on", f"услуга «{service_name}» уже подключена")
    if not enabled and not currently_enabled:
        raise ActionError("service_already_off", f"услуга «{service_name}» уже отключена")
    view = {
        "что": "подключение услуги" if enabled else "отключение услуги",
        "услуга": service_name,
        "цена, руб/мес": connected["price_rub"] if connected else "—",
    }
    return {
        "ok": True,
        "action": "service_toggle",
        "details": {"service": service_name, "enabled": bool(enabled)},
        "view": view,
    }


async def prepare_contacts_update(sub: str, phone: str, email: str) -> dict:
    """Подготовить обновление контактов: нормализация и view карточки.

    Пустой аргумент ("" или None) означает «не менять». Отказы:
    subscriber_not_found, bad_phone, bad_email, nothing_to_update,
    same_contacts.
    """
    async with _client() as client:
        subscriber = await _fetch_subscriber(client, sub)
    new_phone = normalize_phone(phone) if phone else None
    new_email = normalize_email(email) if email else None
    if new_phone is None and new_email is None:
        raise ActionError("nothing_to_update", "укажите новый телефон или новую почту")
    if new_phone is not None and new_phone == subscriber.get("phone"):
        new_phone = None
    if new_email is not None and new_email == subscriber.get("email"):
        new_email = None
    if new_phone is None and new_email is None:
        raise ActionError("same_contacts", "новые контакты совпадают с текущими — менять нечего")
    view = {"что": "обновление контактов"}
    if new_phone is not None:
        view["телефон"] = f"{subscriber.get('phone')} → {new_phone}"
    if new_email is not None:
        view["почта"] = f"{subscriber.get('email')} → {new_email}"
    return {
        "ok": True,
        "action": "contacts_update",
        "details": {"phone": new_phone, "email": new_email},
        "view": view,
    }


async def execute_action(sub: str, action: str, details_json: str,
                         pending_id: str, idempotency_key: str) -> dict:
    """Исполнить подготовленное действие — вызывает только код оркестратора.

    Ключ оркестратора строится как f"{action}:{pending_id}" и приходит как
    idempotency_key: повтор с тем же ключом не исполняет операцию второй
    раз (app.idempotency); биллинг дедуплицирует смену тарифа ещё и сам.
    Неудачный исход ключ не занимает — сбой не должен запирать повторную
    попытку. Исходы: executed / duplicate / daily_limit / отказы моков.
    """
    try:
        details = json.loads(details_json or "{}")
    except (TypeError, ValueError):
        raise ActionError("bad_arguments", "details_json — не JSON; повтори вызов") from None

    async def run() -> dict:
        async with _client() as client:
            if action == "tariff_change":
                response = await client.post("/mock/billing/tariff-change", json={
                    "sub": sub,
                    "tariff": details.get("tariff", ""),
                    "idempotency_key": idempotency_key,
                })
            elif action == "service_toggle":
                response = await client.post("/mock/crm/toggle-service", json={
                    "sub": sub,
                    "service": details.get("service", ""),
                    "enabled": bool(details.get("enabled", True)),
                })
            elif action == "contacts_update":
                response = await client.post("/mock/crm/contacts", json={
                    "sub": sub,
                    "phone": details.get("phone"),
                    "email": details.get("email"),
                })
            else:
                raise ActionError(
                    "unknown_action",
                    f"действие «{action}» неизвестно",
                    available=["tariff_change", "service_toggle", "contacts_update"],
                )
        response.raise_for_status()
        result = response.json()
        result["outcome"] = _SUCCESS_OUTCOME_ALIASES.get(result.get("outcome"), result.get("outcome"))
        return result

    stored = await idempotency.execute(idempotency_key, sub, action, run)
    if stored.duplicate:
        return {"outcome": "duplicate", **{key: value for key, value in stored.result.items() if key != "outcome"}}
    return stored.result


async def create_ticket(sub: str, subject: str, note: str) -> dict:
    """Эскалация обращения: тикет в очередь поддержки, риск низкий.

    Вызов модели — тоже действие: идёт в аудит на этапе аудита.
    """
    async with _client() as client:
        response = await client.post("/mock/tickets/create", json={
            "sub": sub,
            "subject": subject,
            "note": note,
        })
    response.raise_for_status()
    return {"ok": True, **response.json()}
