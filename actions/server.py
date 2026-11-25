"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Один MCPServer (пакет mcp, пока БЕЗ auth) с инструментами:
  prepare_tariff_change / prepare_toggle_service /
  prepare_contacts_update — для LLM (подготовка карточки подтверждения);
  execute_action — НЕ для LLM, его вызывает код оркестратора после явного
  «да»; create_ticket — эскалация в поддержку.
Ошибка ActionError превращается в MCPError с кодом и полем data, чтобы
подсказки доехали до модели. sub на этом шаге приходит аргументом, но
подставляет его обёртка оркестратора — модель его не видит.

Запуск: Starlette + Mount("/actions", streamable_http_app()) + lifespan,
где session manager живёт в event loop uvicorn (приём стенда, stand.py).

TODO(права): перевод сервера под JWT —
  make_mcp_server("actions", resource=RESOURCES["actions"]) из
  core.server_auth (OAuth 2.1 resource server: Bearer в каждом вызове,
  проверка подписи/iss/aud); убрать sub из всех контрактов — субъект из
  get_access_token().subject (mcp.server.auth.middleware.auth_context).
  До этого шага аргумент sub — осознанная дыра; попытка протащить чужой
  sub станет тестом IDOR.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.routing import Mount

from actions import service
from actions.contracts import ActionError

ACTION_ERROR_MCP_CODE = -32002  # прикладной код стенда: отказ действия с подсказками

actions_server = MCPServer(
    name="actions",
    instructions="Действия абонента: подготовка изменений (prepare_*) с карточкой "
                 "подтверждения и эскалация в поддержку.",
)


def _as_mcp_error(error: ActionError) -> MCPError:
    """ActionError → MCPError: код ошибки в message, подсказки в data."""
    return MCPError(
        code=ACTION_ERROR_MCP_CODE,
        message=error.code,
        data=error.to_tool_result(),
    )


@actions_server.tool()
async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Подготовить смену тарифа: предусловия и карточка, без исполнения."""
    try:
        return await service.prepare_tariff_change(sub, tariff)
    except ActionError as error:
        raise _as_mcp_error(error) from error


@actions_server.tool()
async def prepare_toggle_service(sub: str, service: str, enabled: bool) -> dict:
    """Подготовить подключение или отключение услуги: предусловия и карточка."""
    try:
        return await service.prepare_toggle_service(sub, service, enabled)
    except ActionError as error:
        raise _as_mcp_error(error) from error


@actions_server.tool()
async def prepare_contacts_update(sub: str, phone: str, email: str) -> dict:
    """Подготовить обновление контактов: нормализация и карточка, без исполнения."""
    try:
        return await service.prepare_contacts_update(sub, phone, email)
    except ActionError as error:
        raise _as_mcp_error(error) from error


@actions_server.tool()
async def execute_action(sub: str, action: str, details_json: str,
                         pending_id: str, idempotency_key: str) -> dict:
    """ИСПОЛНИТЬ подготовленное действие — инструмент кода оркестратора, не LLM.

    Вызывается после явного подтверждения «да»; идемпотентность держит
    ключ idempotency_key (повтор с тем же ключом — исход duplicate).
    """
    try:
        return await service.execute_action(sub, action, details_json, pending_id, idempotency_key)
    except ActionError as error:
        raise _as_mcp_error(error) from error


@actions_server.tool()
async def create_ticket(sub: str, subject: str, note: str) -> dict:
    """Эскалация обращения в поддержку: создать тикет по теме и заметке."""
    try:
        return await service.create_ticket(sub, subject, note)
    except ActionError as error:
        raise _as_mcp_error(error) from error


@asynccontextmanager
async def lifespan(starlette_app: Starlette):
    """Session manager MCP живёт в event loop uvicorn (приём стенда)."""
    manager_session = actions_server.session_manager.run()
    await manager_session.__aenter__()
    try:
        yield
    finally:
        await manager_session.__aexit__(None, None, None)


app = Starlette(
    routes=[Mount("/actions", app=actions_server.streamable_http_app())],
    lifespan=lifespan,
)
