"""Сборка агентов на модели из .env + учёт LLM-вызовов.

build_agent собирает Agent SDK на модели из конфигурации; run_agent прогоняет
агента одним отдельным Runner.run и возвращает доклад вместе с tool-вызовами,
чтобы оркестратор видел, какие инструменты и с каким результатом работали.

TODO(аудит): в run_agent — INSERT в llm_calls: trace_id, имя агента,
  модель, токены (result.context_wrapper.usage), латентность.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""
from __future__ import annotations

import time

from agents import Agent, AgentOutputSchema, Runner
from agents.items import ToolCallItem, ToolCallOutputItem

from core import config


def _raw_field(raw_item: object, field_name: str) -> str:
    """Достать поле из raw_item: dict (chat_completions) или pydantic-модель."""
    if isinstance(raw_item, dict):
        return raw_item.get(field_name) or ""
    return getattr(raw_item, field_name, "") or ""


def build_agent(name: str, instructions: str, tools: list | None = None,
                output_type: type | None = None) -> Agent:
    """Собрать агента на модели из конфигурации.

    output_type оборачивается в нестрогую схему
    (strict_json_schema=False): строгий JSON-режим слишком требователен
    к малым локальным моделям, план падал бы на валидации.
    """
    output_schema = (
        AgentOutputSchema(output_type, strict_json_schema=False) if output_type else None
    )
    return Agent(
        name=name,
        instructions=instructions,
        tools=tools or [],
        output_type=output_schema,
        model=config.MODEL_NAME or config.setup_model(),
    )


async def run_agent(agent: Agent, task: str, *, trace_id: str = "") -> dict:
    """Прогнать агента одним Runner.run и собрать доклад с tool-вызовами.

    Возврат: {"report": финальный текст агента, "tools": [{name, arguments,
    output}] в порядке вызовов, "seconds": латентность}. trace_id пока только
    принимается — запись в llm_calls появится на этапе аудита.
    """
    started_at = time.monotonic()
    result = await Runner.run(agent, task)
    seconds = round(time.monotonic() - started_at, 2)

    calls_by_id: dict[str, dict] = {}
    for run_item in result.new_items:
        if isinstance(run_item, ToolCallItem):
            raw_call = run_item.raw_item
            call_id = _raw_field(raw_call, "call_id") or _raw_field(raw_call, "id")
            calls_by_id[call_id] = {
                "name": _raw_field(raw_call, "name"),
                "arguments": _raw_field(raw_call, "arguments"),
                "output": None,
            }
        elif isinstance(run_item, ToolCallOutputItem):
            call_id = _raw_field(run_item.raw_item, "call_id")
            if call_id in calls_by_id:
                calls_by_id[call_id]["output"] = run_item.output

    return {
        "report": str(result.final_output),
        "tools": list(calls_by_id.values()),
        "seconds": seconds,
    }
