"""Идемпотентность исполнения на стороне оркестратора.

execute(key, sub, action, fn) поверх таблицы idempotency:
  - ключ в таблице с результатом → StoredResult(duplicate=True, сохранённый
    результат) — повтор не исполняет операцию второй раз;
  - иначе исполнить fn(); неудачный исход (error в ответе, отказ бизнес-
    правила, дневной лимит) ключ не занимает — сбой не должен запирать
    повторную попытку абонента;
  - успех → INSERT ... ON CONFLICT (key) DO NOTHING. Для смены тарифа
    запись под тем же ключом мог уже вставить биллинг — это нормально:
    повтор отсекает проверка ключа до исполнения, а биллинг держит свой
    собственный барьер дедупликации.

Ключ в оркестраторе — f"{action}:{pending_id}": одно «да» — одна операция.
Для смены тарифа дедупликация есть и на стороне биллинга — два барьера;
для услуг и контактов этот слой единственный.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core import db

# Исходы, при которых повтор имеет смысл: ключ не занимаем, чтобы отказ
# или сбой не запирали следующую попытку абонента.
FAILURE_OUTCOMES = frozenset({
    "not_found",
    "same_tariff",
    "unknown_tariff",
    "tariff_not_available",
    "daily_limit",
    "unknown_service",
})


@dataclass
class StoredResult:
    """Результат execute(): duplicate — вызов был повтором; result — ответ операции."""
    duplicate: bool
    result: dict


async def execute(key: str, sub: str, action: str,
                  fn: Callable[[], Awaitable[dict]]) -> StoredResult:
    """Исполнить fn() не более одного раза на ключ идемпотентности.

    Ключ уже в таблице (наш слой или биллинг) — операцию не исполняем,
    отвечаем сохранённым результатом с duplicate=True. Иначе исполняем;
    неудачный исход ключ не занимает, успех — занимает (ON CONFLICT
    DO NOTHING: параллельный исполнивший первым оставляет свой результат).
    """
    known = db.fetchone("select result from idempotency where key = %s", (key,))
    if known is not None:
        return StoredResult(duplicate=True, result=known["result"])
    result = await fn()
    if result.get("error") or result.get("outcome") in FAILURE_OUTCOMES:
        return StoredResult(duplicate=False, result=result)
    db.execute(
        "insert into idempotency(key, sub, action, result) values (%s, %s, %s, %s)"
        " on conflict (key) do nothing",
        (key, sub, action, json.dumps(result, ensure_ascii=False)),
    )
    return StoredResult(duplicate=False, result=result)
