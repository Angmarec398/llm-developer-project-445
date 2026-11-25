"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  подтверждение карточки (с контрактов)

События SSE хода: plan / card / reply / error / done.

POST /api/cards/{pending_id}/decision — кнопка карточки:
  «да» → orchestrator.execute_pending, «нет» → отмена.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import cards, orchestrator, sessions
from core import config


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Подключить LLM до приёма запросов: клиент и модель по умолчанию SDK."""
    config.setup_model()
    yield


app = FastAPI(title="AI-оператор: backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # фронтенд живёт на другом порту стенда
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionRequest(BaseModel):
    """Тело POST /api/session."""

    user: str


class ChatRequest(BaseModel):
    """Тело POST /api/chat."""

    session_id: str
    message: str


@app.get("/api/health")
async def health() -> dict:
    """Живость backend."""
    return {"ok": True}


@app.post("/api/session")
async def open_session(request: SessionRequest) -> JSONResponse:
    """Открыть сессию чата и вернуть видимые инструменты её MCP-пула."""
    try:
        session = await sessions.open_session(request.user)
    except KeyError:
        return JSONResponse(
            status_code=400,
            content={"error": f"неизвестный пользователь: {request.user}"},
        )
    return JSONResponse(content={
        "session_id": session.session_id,
        "name": session.user.name,
        "role": session.user.role,
        "visible_tools": session.pool.tool_names(),
    })


@app.post("/api/chat")
async def chat(request: ChatRequest) -> Response:
    """Прогнать ход оркестратора и отдать его события потоком SSE.

    События хода гонятся через asyncio.Queue: run_turn кладёт, поток
    отдаёт. Сбой хода становится событием error, а не падением потока;
    последним всегда уходит done.
    """
    session = sessions.get(request.session_id)
    if session is None:
        return JSONResponse(status_code=400, content={"error": "неизвестная сессия"})

    events: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(event: dict) -> None:
        """Положить событие хода в очередь потока."""
        await events.put(event)

    async def run() -> None:
        """Выполнить ход; любой сбой превратить в событие error."""
        try:
            await orchestrator.run_turn(session, request.message, emit)
        except Exception as exc:  # noqa: BLE001 — сбой хода не роняет поток
            await emit({"type": "error", "detail": str(exc)[:200]})
        finally:
            await events.put(None)

    async def stream() -> AsyncIterator[str]:
        """Отдавать события очереди в формате SSE до конца хода."""
        turn_task = asyncio.create_task(run())
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        finally:
            await turn_task

    return StreamingResponse(stream(), media_type="text/event-stream")

class CardDecisionRequest(BaseModel):
    """Тело POST /api/cards/{pending_id}/decision — ответ кнопкой карточки."""

    session_id: str
    answer: str  # "yes" | "no"


@app.post("/api/cards/{pending_id}/decision")
async def card_decision(pending_id: str, request: CardDecisionRequest) -> JSONResponse:
    """Кнопка карточки: «да» → исполнение кода оркестратора, «нет» → отмена."""
    session = sessions.get(request.session_id)
    if session is None:
        return JSONResponse(status_code=400, content={"error": "неизвестная сессия"})
    card = cards.active(session)
    if card is None or card.pending_id != pending_id:
        return JSONResponse(status_code=400, content={"error": "карточка не найдена или истекла"})
    if request.answer == "yes":
        text = await orchestrator.execute_pending(session, card)
    elif request.answer == "no":
        cards.settle(session, card, "rejected")
        text = "Хорошо, отменяю — изменения не внесены."
    else:
        return JSONResponse(status_code=400, content={"error": "answer: yes|no"})
    return JSONResponse({"text": text})

