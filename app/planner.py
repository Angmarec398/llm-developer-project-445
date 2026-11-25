"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов. Действия
(смена тарифа, услуги, контакты, тикет) идут через карточку подтверждения
и политику прав, а не напрямую.
"""
from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field

from agents import Agent, Runner

from app.llm import build_agent

READ_AGENTS = ("data", "kb")
ACTION_AGENTS = ("tariff", "tickets")

PLANNER_PROMPT = """Ты — планировщик ассистента мобильного оператора.
По сообщению абонента составь план: каких доменных агентов запустить и с
какой задачей. Сам ничего не отвечай абоненту и ничего не выполняй.

Каталог агентов:
- data — данные абонента: профиль, текущий тариф, каталог тарифов, списания.
- kb — база знаний: правила, условия, последствия, ответы на «почему».
- tariff — действия: смена тарифа, услуги, контактные данные.
- tickets — обращение в поддержку: всё вне тем оператора связи.

Правила планирования:
- вопрос о данных абонента (мой тариф, списания, профиль) — один шаг data;
- правила и последствия (как сменить, что будет, почему так) — шаг kb;
- «почему списание» — два шага: data (сами списания) и kb (правила);
- просьба что-то изменить (тариф, услуга, контакты) — шаг tariff с
  action=true;
- просьба вне тем оператора (интернет в офис, телевизор и т.п.) — шаг
  tickets с action=true;
- названия тарифов в задачах пиши строчными, без кавычек: игровой,
  семейный, базовый;
- если данных для плана не хватает — не выдумывай шаги, верни уточняющий
  вопрос в поле question и пустой список steps.

Для каждого шага укажи agent, task (что поручить агенту), why (зачем этот
шаг) и action (true только для действий).
"""


class Step(BaseModel):
    """Шаг плана: какого агента запустить и с какой задачей.

    Алиасы — потому что модели отвечают своим диалектом имён:
    agent/agentName, task/job. Оба варианта принимаются.
    """

    agent: str = Field(validation_alias=AliasChoices("agent", "agentName"))
    task: str = Field(validation_alias=AliasChoices("task", "job"))
    why: str = ""
    action: bool = False


class Plan(BaseModel):
    """План хода: шаги по агентам либо уточняющий вопрос абоненту."""

    steps: list[Step] = []
    question: str | None = None


def build_planner() -> Agent:
    """Собрать агента-планировщика со структурированным выходом Plan."""
    return build_agent(name="planner", instructions=PLANNER_PROMPT, output_type=Plan)


def gate(plan: Plan) -> tuple[list[Step], list[str]]:
    """Пропустить план через гейт до исполнения.

    Возврат: (оставшиеся шаги, отброшенное с причинами — для трейса сессии).
    Шаги неизвестных агентов отбрасываются; у агентов чтения пометка action
    снимается — в контуре чтения действий нет по построению.
    """
    known_agents = set(READ_AGENTS) | set(ACTION_AGENTS)
    kept: list[Step] = []
    dropped: list[str] = []
    for step in plan.steps:
        if step.agent not in known_agents:
            dropped.append(f"{step.agent}: неизвестный агент, шаг отброшен")
            continue
        if step.action and step.agent in READ_AGENTS:
            step.action = False
            dropped.append(f"{step.agent}: пометка action снята — агент чтения")
        kept.append(step)
    return kept, dropped


async def next_plan(planner: Agent, history: list, message: str) -> tuple[Plan, list]:
    """Один вызов планировщика по истории диалога.

    Возврат: (план, обновлённая история планировщика). История хранится в
    сессии, поэтому контекст диалога живёт между ходами.
    """
    result = await Runner.run(planner, history + [{"role": "user", "content": message}])
    plan = result.final_output
    if not isinstance(plan, Plan):
        # Модель ответила текстом вместо схемы — считаем это уточняющим вопросом.
        plan = Plan(question=str(plan))
    return plan, result.to_input_list()
