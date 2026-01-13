"""
Сервис анализа разговора через OpenAI GPT.
Извлекает структурированную информацию из транскрибации.
"""
import openai
import json
import logging
from typing import Optional
from dataclasses import dataclass, asdict
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# Настраиваем OpenAI
client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)


@dataclass
class CallAnalysis:
    """Результат анализа звонка"""
    client_name: str  # ФИО или имя клиента
    client_city: str  # Город клиента
    work_type: str  # Тип работы
    cost: str  # Стоимость
    payment_terms: str  # Условия оплаты (50/50, 100% и т.д.)
    summary: str  # Краткое резюме разговора
    manager_tasks: list[str]  # Задачи для менеджера
    call_result: str  # Итог звонка (договорились, отказ, перезвонить и т.д.)
    next_contact_date: str  # Когда связаться


# Системный промпт для анализа
ANALYSIS_SYSTEM_PROMPT = """Ты — ассистент для анализа телефонных разговоров геодезической компании.

Твоя задача — извлечь из транскрибации разговора следующую информацию:

1. **client_name** — ФИО или имя клиента (если назвали). Если не назвали, напиши "Не указано"
2. **client_city** — Город или регион клиента. Если не упоминали, напиши "Не указано"
3. **work_type** — Тип геодезических работ (топосъёмка, межевание, вынос границ и т.д.)
4. **cost** — Стоимость работ, если обсуждали. Формат: "25 000 ₽" или "Не обсуждали"
5. **payment_terms** — Условия оплаты: "предоплата 50%", "100% по факту", "50/50" и т.д.
6. **summary** — Краткое резюме разговора в 2-3 предложениях. Суть запроса клиента.
7. **manager_tasks** — Список конкретных задач для менеджера (что нужно сделать после звонка)
8. **call_result** — Итог: "Договорились о работе", "Клиент думает", "Отказ", "Нужен перезвон" и т.д.
9. **next_contact_date** — Когда перезвонить/связаться. Если не обсуждали, напиши "Не обсуждали"

ВАЖНО:
- Отвечай ТОЛЬКО в формате JSON
- Не выдумывай информацию — если чего-то нет в разговоре, пиши "Не указано" или "Не обсуждали"
- Задачи менеджеру формулируй как конкретные действия
- Будь краток, но информативен"""


ANALYSIS_USER_PROMPT = """Проанализируй следующий разговор между менеджером и клиентом.

ТРАНСКРИБАЦИЯ РАЗГОВОРА:
{transcript}

---

Извлеки информацию и верни в JSON формате:
{{
    "client_name": "...",
    "client_city": "...",
    "work_type": "...",
    "cost": "...",
    "payment_terms": "...",
    "summary": "...",
    "manager_tasks": ["задача 1", "задача 2", ...],
    "call_result": "...",
    "next_contact_date": "..."
}}"""


class AnalysisService:
    """Сервис анализа разговоров через GPT"""
    
    async def analyze_call(
        self, 
        transcript: str,
        call_type: str = "outgoing",
        manager_name: str = "Менеджер"
    ) -> CallAnalysis:
        """
        Анализирует транскрибацию звонка и извлекает структурированные данные.
        
        Args:
            transcript: Текст разговора с ролями
            call_type: Тип звонка (incoming/outgoing)
            manager_name: Имя менеджера
            
        Returns:
            Структурированный результат анализа
        """
        try:
            logger.info(f"Анализируем разговор ({len(transcript)} символов)...")
            
            response = await client.chat.completions.create(
                model="gpt-4o-mini",  # Используем быструю модель
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": ANALYSIS_USER_PROMPT.format(transcript=transcript)}
                ],
                temperature=0.3,  # Низкая температура для точности
                response_format={"type": "json_object"}  # Гарантируем JSON ответ
            )
            
            # Парсим JSON ответ
            result_text = response.choices[0].message.content
            result_json = json.loads(result_text)
            
            # Создаём объект результата
            analysis = CallAnalysis(
                client_name=result_json.get("client_name", "Не указано"),
                client_city=result_json.get("client_city", "Не указано"),
                work_type=result_json.get("work_type", "Не указано"),
                cost=result_json.get("cost", "Не обсуждали"),
                payment_terms=result_json.get("payment_terms", "Не обсуждали"),
                summary=result_json.get("summary", ""),
                manager_tasks=result_json.get("manager_tasks", []),
                call_result=result_json.get("call_result", "Не определено"),
                next_contact_date=result_json.get("next_contact_date", "Не обсуждали")
            )
            
            logger.info(f"Анализ завершён: {analysis.call_result}")
            return analysis
            
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON от GPT: {e}")
            raise
        except Exception as e:
            logger.error(f"Ошибка анализа: {e}")
            raise
    
    def format_note(
        self, 
        analysis: CallAnalysis,
        call_type: str = "outgoing",
        duration_seconds: float = 0,
        manager_name: str = "Менеджер"
    ) -> str:
        """
        Форматирует результат анализа в красивое примечание для AmoCRM.
        
        Args:
            analysis: Результат анализа
            call_type: Тип звонка
            duration_seconds: Длительность в секундах
            manager_name: Имя менеджера
            
        Returns:
            Отформатированный текст примечания
        """
        # Форматируем длительность
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes} мин {seconds} сек" if minutes else f"{seconds} сек"
        
        # Тип звонка
        call_type_str = "Исходящий" if call_type == "outgoing" else "Входящий"
        
        # Форматируем задачи
        tasks_str = ""
        if analysis.manager_tasks:
            tasks_list = "\n".join([f"• {task}" for task in analysis.manager_tasks])
            tasks_str = f"\n\n✅ ЗАДАЧИ МЕНЕДЖЕРУ:\n{tasks_list}"
        
        # Собираем примечание
        note = f"""🎙️ РАСШИФРОВКА ЗВОНКА (AI)

📞 Звонок: {call_type_str} | {duration_str}
👤 Клиент: {analysis.client_name}
📍 Город: {analysis.client_city}
👨‍💼 Менеджер: {manager_name}

📝 СУТЬ РАЗГОВОРА:
{analysis.summary}

🔧 Тип работы: {analysis.work_type}
💰 Стоимость: {analysis.cost}
💳 Оплата: {analysis.payment_terms}

📊 Итог: {analysis.call_result}
📅 Следующий контакт: {analysis.next_contact_date}{tasks_str}"""
        
        return note


# Синглтон
analysis_service = AnalysisService()
