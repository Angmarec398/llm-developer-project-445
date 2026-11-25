"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента; оркестратор управляет запуском агентов и
собирает события хода для фронтенда.

Карточка действия:
  - в начале хода: cards.active(session) → cards.classify_answer(message):
    «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
    «оператор» → escalate_to_operator (тикет + отмена карточки),
    смена темы → обычный ход, карточка живёт;
  - после агентов: свежая карточка хода → событие {"type": "card"} и ход
    завершается (ждём подтверждение); редактор не нужен — доклад агента
    действия уже пересказывает карточку и спрашивает «да/нет»;
  - execute_pending(session, card): settle("confirmed"), ключ
    f"{action}:{pending_id}", вызов MCP execute_action (идемпотентность
    держит ключ actions/service поверх биллинга) → человекочитаемый
    ответ: executed → «Готово…», duplicate → «Эта операция уже исполнена
    — повтор не выполнялся.», daily_limit → отказ.

TODO(аудит) — аудит: audit.append на все исходы пути действия
  (executed с before/after, duplicate_suppressed, rejected_by_user,
  server_limit, escalated).

TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
  человекочитаемый отказ по reason (session_limit, confirmation_expired,
  card_not_prepared, pdp_unavailable).

TODO(guardrail) — guardrail: input-проверка реплики до всего хода (unsafe →
  отказ, запись в аудит); output-проверка ответа и карточки до показа;
  недоступный сервис → session.guard_state["ok"] = False (fail-closed
  для действий), чтение продолжается.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from mcp.shared.exceptions import MCPError

from app import cards, planner
from app.editor import build_editor, editor_task
from app.llm import run_agent
from app.workers import build_workers
from core import config

Emit = Callable[[dict], Awaitable[None]]


def _kb_sources(run_result: dict) -> list[str]:
    """Источники базы знаний из tool-результатов search_kb одного прогона."""
    found: list[str] = []
    for tool_call in run_result.get("tools", []):
        if tool_call.get("name") != "search_kb" or not tool_call.get("output"):
            continue
        try:
            payload = json.loads(tool_call["output"])
        except (TypeError, ValueError):
            continue
        for hit in payload.get("results", []):
            source = hit.get("source")
            if source and source not in found:
                found.append(source)
    return found


def _executed_reply(action: str, result: dict) -> str:
    """Человекочитаемое «Готово» по исходу исполнения действия."""
    if action == "tariff_change":
        price_before = result.get("price_before", 0)
        price_after = result.get("price_after", 0)
        delta = price_after - price_before
        sign = "+" if delta >= 0 else ""
        return (
            f"Готово: тариф изменён с «{result.get('before')}» на «{result.get('after')}» "
            f"({price_before} → {price_after} руб/мес, {sign}{delta})."
        )
    if action == "service_toggle":
        state_word = "подключена" if result.get("enabled") else "отключена"
        return f"Готово: услуга «{result.get('service')}» {state_word}."
    if action == "contacts_update":
        parts = []
        if result.get("phone"):
            parts.append(f"телефон {result['phone']}")
        if result.get("email"):
            parts.append(f"почта {result['email']}")
        return "Готово: обновлены " + " и ".join(parts) + "."
    return "Готово."


async def execute_pending(session, card) -> str:
    """Исполнить карточку после «да»: подтвердить и вызвать execute_action.

    Ключ f"{action}:{pending_id}" — одно «да» — одна операция; повтор
    вернёт duplicate. Карточка закрывается confirmed до вызова: сбой
    контура не должен позволить второе исполнение той же карточки.
    """
    cards.settle(session, card, "confirmed")
    try:
        result = await session.pool.call(
            "actions", "execute_action",
            {
                "sub": session.user.sub,
                "action": card.action,
                "details_json": json.dumps(card.details, ensure_ascii=False),
                "pending_id": card.pending_id,
                "idempotency_key": f"{card.action}:{card.pending_id}",
            },
        )
    except MCPError as err:
        return f"Изменение не выполнено: {str(getattr(err, 'message', err))[:120]}"
    except Exception as err:  # noqa: BLE001 — сбой контура: честный отказ, не падение хода
        return f"Сервис изменений недоступен — попробуйте ещё раз: {str(err)[:120]}"
    outcome = result.get("outcome")
    if outcome == "executed":
        return _executed_reply(card.action, result)
    if outcome == "duplicate":
        return "Эта операция уже исполнена — повтор не выполнялся."
    if outcome == "daily_limit":
        return (
            f"Сегодня исчерпан дневной лимит смен тарифа ({result.get('limit', 3)})."
            " Попробуйте завтра или обратитесь к оператору."
        )
    return f"Изменение не выполнено: {result.get('detail') or outcome or 'отказ системы'}."


async def escalate_to_operator(session, card) -> str:
    """Карточка + «оператор»: тикет в поддержку, карточка отменяется."""
    cards.settle(session, card, "rejected")
    try:
        result = await session.pool.call(
            "actions", "create_ticket",
            {
                "sub": session.user.sub,
                "subject": "Просьба связаться с оператором",
                "note": f"Абонент попросил оператора по карточке {card.pending_id} ({card.action}).",
            },
        )
    except Exception as err:  # noqa: BLE001 — эскалация не роняет ход
        return f"Передал просьбу оператору (тикет не создан: {str(err)[:80]}). Карточку отменил."
    return (
        f"Передал обращение оператору, тикет {result.get('ticket_id', '—')}."
        " Карточку отменил — изменения не внесены."
    )


async def run_turn(session, message: str, emit: Emit) -> None:
    """Прогнать один ход абонента и разослать события SSE через emit.

    Порядок: живая карточка (ответ на неё — кодом, без LLM) → план
    от планировщика (история диалога живёт в session.thread) → гейт →
    событие plan → шаги по агентам отдельными Runner.run → свежая
    карточка (событие card, ход ждёт подтверждение) или редактор →
    событие reply с источниками kb. Уточняющий вопрос планировщика
    уходит абоненту сразу, без агентов и редактора.
    """
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    # ── 0. Живая карточка: ответ на неё обслуживает код, без LLM ──
    live_card = cards.active(session)
    if live_card is not None:
        answer = cards.classify_answer(message)
        if answer == "yes":
            await emit({"type": "reply", "text": await execute_pending(session, live_card)})
            return
        if answer == "no":
            cards.settle(session, live_card, "rejected")
            await emit({"type": "reply", "text": "Хорошо, отменяю — изменения не внесены."})
            return
        if answer == "operator":
            await emit({"type": "reply", "text": await escalate_to_operator(session, live_card)})
            return
        # topic: обычный ход ниже, карточка остаётся живой
    prior_card_id = live_card.pending_id if live_card is not None else None

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, session.thread = await planner.next_plan(session.planner, session.thread, message)
    steps, dropped = planner.gate(plan)
    for reason in dropped:
        session.note(f"gate: {reason}")
    await emit({"type": "plan", "agents": [step.agent for step in steps], "dropped": dropped})

    if not steps:
        # Уточняющий вопрос (или полностью отброшенный план) — без редактора.
        await emit({"type": "reply", "text": plan.question or "Уточните, пожалуйста, вопрос."})
        return

    agent_ids = tuple(dict.fromkeys(step.agent for step in steps))
    workers = build_workers(session, agent_ids)

    reports: list[tuple[str, str]] = []
    sources: list[str] = []
    for step in steps:
        worker = workers.get(step.agent)
        if worker is None:
            continue
        task = step.task
        if step.action:
            # Императив из текста задачи весомее инструкций для слабых
            # локальных моделей: без вызова prepare_* карточки не будет.
            task += (
                "\n\nДействуй строго так: СНАЧАЛА вызови нужный prepare_* (смена тарифа — "
                "prepare_tariff_change, услуга — prepare_toggle_service, контакты — "
                "prepare_contacts_update), дождись его ответа, потом перескажи карточку и "
                "спроси «да или нет?». Отвечать до вызова prepare_* запрещено; "
                "read-инструменты для этого не нужны."
            )
        result = await run_agent(worker, task, trace_id=session.trace_id)

        reports.append((step.agent, result["report"]))
        for source in _kb_sources(result):
            if source not in sources:
                sources.append(source)

    # ── свежая карточка этого хода: ждём подтверждение, редактор не нужен ──
    fresh_card = cards.active(session)
    if fresh_card is not None and fresh_card.pending_id != prior_card_id:
        await emit({
            "type": "card",
            "pending_id": fresh_card.pending_id,
            "card": fresh_card.public_view(config.CARD_TTL_SEC),
        })
        text = "\n".join(report for _, report in reports if report and report.strip())
        if text.strip():
            await emit({"type": "reply", "text": text, "sources": sources})
        return

    editor = build_editor(session)
    editor_result = await run_agent(
        editor, editor_task(message, reports), trace_id=session.trace_id,
    )
    await emit({"type": "reply", "text": editor_result["report"], "sources": sources})
