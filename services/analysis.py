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
    manager_name: str  # ФИО менеджера (из разговора)
    summary: str  # Краткое резюме разговора
    manager_rating: int  # Оценка работы менеджера (1-10)
    what_good: str  # Что хорошо сделал менеджер
    what_improve: str  # Что можно улучшить
    # Дополнительные поля
    client_city: str  # Город клиента
    work_type: str  # Тип работы
    cost: str  # Стоимость
    payment_terms: str  # Условия оплаты
    call_result: str  # Итог звонка
    next_contact_date: str  # Когда связаться


# Системный промпт для анализа
ANALYSIS_SYSTEM_PROMPT = """Ты — ассистент для анализа телефонных разговоров геодезической компании.

Твоя задача — извлечь из транскрибации разговора следующую информацию:

1. **client_name** — Имя клиента (как представился). Если не назвал, напиши "Клиент"
2. **manager_name** — ФИО или имя менеджера (как представился в разговоре)
3. **summary** — Подробное резюме разговора в 3-5 предложениях. Что обсуждали, о чём договорились.
4. **manager_rating** — Оценка работы менеджера от 1 до 10. Учитывай: вежливость, компетентность, работу с возражениями, предложение решений.
5. **what_good** — Что менеджер сделал хорошо (1-2 предложения)
6. **what_improve** — Что менеджер мог сделать лучше, конкретные рекомендации (1-2 предложения)
7. **client_city** — Город или регион клиента. Если не упоминали: "Не указано"
8. **work_type** — Тип работ (топосъёмка, межевание, вынос границ и т.д.). Если не ясно: "Консультация"
9. **cost** — Стоимость работ. Формат: "25 000 ₽" или "Не обсуждали"
10. **payment_terms** — Условия оплаты: "50/50", "100% предоплата" и т.д. Если не обсуждали: "Не обсуждали"
11. **call_result** — Итог: "Договорились", "Клиент думает", "Отказ", "Перезвонить" и т.д.
12. **next_contact_date** — Когда связаться. Если не обсуждали: "Не указано"

ВАЖНО:
- Отвечай ТОЛЬКО в формате JSON
- Оценка менеджера должна быть числом от 1 до 10
- Будь объективен в оценке — не завышай и не занижай
- what_good и what_improve — конкретные наблюдения по этому звонку"""


ANALYSIS_USER_PROMPT = """Проанализируй следующий разговор между менеджером и клиентом.

Тип звонка: {call_type}
Менеджер компании: {manager_name}

ТРАНСКРИБАЦИЯ РАЗГОВОРА:
{transcript}

---

Извлеки информацию и верни в JSON формате:
{{
    "client_name": "имя клиента",
    "manager_name": "ФИО менеджера из разговора",
    "summary": "подробное резюме разговора",
    "manager_rating": 7,
    "what_good": "что хорошо сделал менеджер",
    "what_improve": "что можно улучшить",
    "client_city": "город",
    "work_type": "тип работ",
    "cost": "стоимость",
    "payment_terms": "условия оплаты",
    "call_result": "итог звонка",
    "next_contact_date": "когда связаться"
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
        """
        try:
            logger.info(f"Анализируем разговор ({len(transcript)} символов)...")
            
            call_type_ru = "Входящий" if call_type == "incoming" else "Исходящий"
            
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": ANALYSIS_USER_PROMPT.format(
                        transcript=transcript,
                        call_type=call_type_ru,
                        manager_name=manager_name
                    )}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            result_text = response.choices[0].message.content
            result_json = json.loads(result_text)
            
            # Создаём объект результата
            analysis = CallAnalysis(
                client_name=result_json.get("client_name", "Клиент"),
                manager_name=result_json.get("manager_name", manager_name),
                summary=result_json.get("summary", ""),
                manager_rating=int(result_json.get("manager_rating", 5)),
                what_good=result_json.get("what_good", ""),
                what_improve=result_json.get("what_improve", ""),
                client_city=result_json.get("client_city", "Не указано"),
                work_type=result_json.get("work_type", "Консультация"),
                cost=result_json.get("cost", "Не обсуждали"),
                payment_terms=result_json.get("payment_terms", "Не обсуждали"),
                call_result=result_json.get("call_result", "Не определено"),
                next_contact_date=result_json.get("next_contact_date", "Не указано")
            )
            
            logger.info(f"Анализ завершён: оценка {analysis.manager_rating}/10")
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
        Форматирует результат анализа в примечание для AmoCRM.
        """
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes} мин {seconds} сек" if minutes else f"{seconds} сек"
        call_type_str = "Исходящий" if call_type == "outgoing" else "Входящий"
        
        note = f"""🎙️ АНАЛИЗ ЗВОНКА (AI)

📞 {call_type_str} | {duration_str}

Спикеры:
- {analysis.manager_name} (менеджер)
- {analysis.client_name} (клиент)

Суть:
{analysis.summary}

⭐ Оценка менеджера: {analysis.manager_rating}/10

✅ Что хорошо: {analysis.what_good}

⚠️ Что улучшить: {analysis.what_improve}

📍 Город: {analysis.client_city}
🔧 Работа: {analysis.work_type}
💰 Стоимость: {analysis.cost}
📊 Итог: {analysis.call_result}"""
        
        return note


# Синглтон
analysis_service = AnalysisService()
