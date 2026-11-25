"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента; оркестратор управляет запуском агентов и
собирает события хода для фронтенда.

TODO(контракты) — карточка действия:
  - в начале хода: cards.active(session) → cards.classify_answer(message):
    «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
    «оператор» → escalate, смена темы → обычный ход с живой карточкой;
  - после агентов: свежая карточка → событие {"type": "card"} и ход
    завершается (ждём подтверждение);
  - execute_pending(session, card): settle("confirmed"), ключ
    f"{action}:{pending_id}", idempotency.execute → вызов MCP execute_action
    → человекочитаемый ответ (executed / duplicate / daily_limit / отказ).

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

from app import planner
from app.editor import build_editor, editor_task
from app.llm import run_agent
from app.workers import build_workers

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


async def run_turn(session, message: str, emit: Emit) -> None:
    """Прогнать один ход абонента и разослать события SSE через emit.

    Порядок: сквозной trace_id → план от планировщика (история диалога
    живёт в session.thread) → гейт (отброшенное — в трейс сессии) →
    событие plan → шаги по агентам отдельными Runner.run → редактор →
    событие reply с источниками kb. Уточняющий вопрос планировщика уходит
    абоненту сразу, без агентов и редактора.
    """
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

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
        if worker is None:  # агент вне CATALOG (действия — со следующего этапа)
            continue
        result = await run_agent(worker, step.task, trace_id=session.trace_id)
        reports.append((step.agent, result["report"]))
        for source in _kb_sources(result):
            if source not in sources:
                sources.append(source)

    editor = build_editor(session)
    editor_result = await run_agent(
        editor, editor_task(message, reports), trace_id=session.trace_id,
    )
    await emit({"type": "reply", "text": editor_result["report"], "sources": sources})
