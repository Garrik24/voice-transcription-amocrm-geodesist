"""
Сервис анализа разговора через OpenAI GPT.
Извлекает структурированную информацию из транскрибации.
"""
import openai
import json
import logging
import re
from typing import List
from dataclasses import dataclass
from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LLM_PROVIDER,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)

logger = logging.getLogger(__name__)

_client: openai.AsyncOpenAI | None = None
_gemini_client = None


def _normalize_list_field(value) -> List[str]:
    """
    Нормализует поле, которое может прийти как:
    - list[str]
    - многострочная строка с буллетами/нумерацией
    - None
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        items: List[str] = []
        for line in value.splitlines():
            s = line.strip()
            if not s:
                continue
            # убираем буллеты и нумерацию в начале строки
            s = re.sub(r"^(\s*[-•]\s+|\s*\d+\s*[).]\s+)", "", s).strip()
            if s:
                items.append(s)
        return items
    # fallback
    s = str(value).strip()
    return [s] if s else []


def _get_client() -> openai.AsyncOpenAI:
    """
    Инициализируем OpenAI клиент лениво.

    Важно для деплоя: если OPENAI_API_KEY не задан, сервис всё равно должен стартовать
    (например, для автоматизаций без транскрибации).
    """
    global _client
    if _client is not None:
        return _client
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан (нужен для анализа звонков)")
    _client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


def _get_gemini_client():
    """
    Инициализируем Google GenAI (Gemini) клиент лениво.

    Важно: ключи не валим на старте приложения — только при попытке анализа.
    """
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан (нужен для анализа звонков через Gemini)")
    # Импортируем внутри, чтобы не падать, если провайдер не используется.
    from google import genai

    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


@dataclass
class CallAnalysis:
    """Результат анализа звонка (без оценок)"""
    client_name: str  # ФИО или имя клиента
    manager_name: str  # ФИО менеджера (из разговора)
    summary: str  # Краткое резюме разговора
    # Дополнительные поля
    client_city: str  # Город клиента
    work_type: str  # Тип работы
    cost: str  # Стоимость
    payment_terms: str  # Условия оплаты
    call_result: str  # Итог звонка
    next_contact_date: str  # Когда связаться
    next_steps: List[str]  # Следующие шаги для менеджера (0-5)


# Системный промпт для анализа
ANALYSIS_SYSTEM_PROMPT = """Ты — ассистент для анализа телефонных разговоров геодезической компании.

Твоя задача — извлечь ТОЛЬКО ФАКТЫ из транскрибации и вернуть структурированный JSON.
НЕЛЬЗЯ выдумывать. Если данных нет в тексте — пиши "Не указано" или "Не обсуждали" (как указано ниже).

Верни JSON со следующими полями:
{
  "client_name": "Имя клиента (как представился) или 'Клиент'",
  "manager_name": "Имя/ФИО менеджера из разговора или то, что передали в поле manager_name",
  "summary": "Развёрнутая суть разговора: 8–12 предложений (если разговор длинный — до 15). Обязательно охвати начало/середину/конец, укажи итог и следующий шаг. Без воды и повторов.",
  "client_city": "Город/регион или 'Не указано'",
  "work_type": "Тип работ или 'Консультация'",
  "cost": "Стоимость (например: '25 000 ₽') или 'Не обсуждали'",
  "payment_terms": "Условия оплаты (например: '50/50') или 'Не обсуждали'",
  "call_result": "Итог: 'Договорились'/'Клиент думает'/'Отказ'/'Перезвонить' и т.п. Если непонятно — 'Не определено'",
  "next_contact_date": "Когда связаться (если было) иначе 'Не указано'",
  "next_steps": ["1-5 конкретных следующих шагов для менеджера по итогам разговора. Если нет — пустой массив []"]
}

Правила качества summary:
- Пиши человеческим языком, без канцелярита и без 'возможно/наверное'
- Не вставляй мусорные слова, не повторяй одну мысль
- Не обрезай концовку: в summary должен быть финал разговора и чем закончили
- Если в транскрибации каша/обрывки — формулируй только то, что точно ясно; остальное не додумывай

Отвечай ТОЛЬКО JSON, без пояснений и без Markdown.
"""


ANALYSIS_USER_PROMPT = """Проанализируй разговор между менеджером и клиентом.

Тип звонка: {call_type}
Менеджер компании: {manager_name}

ТРАНСКРИБАЦИЯ РАЗГОВОРА:
{transcript}
"""


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

            provider = (LLM_PROVIDER or "openai").strip().lower()

            if provider == "gemini":
                gemini = _get_gemini_client()
                from google.genai import types

                # Схема ответа: строго JSON-объект с ожидаемыми полями.
                response_schema = {
                    "type": "OBJECT",
                    "required": [
                        "client_name",
                        "manager_name",
                        "summary",
                        "client_city",
                        "work_type",
                        "cost",
                        "payment_terms",
                        "call_result",
                        "next_contact_date",
                        "next_steps",
                    ],
                    "properties": {
                        "client_name": {"type": "STRING"},
                        "manager_name": {"type": "STRING"},
                        "summary": {"type": "STRING"},
                        "client_city": {"type": "STRING"},
                        "work_type": {"type": "STRING"},
                        "cost": {"type": "STRING"},
                        "payment_terms": {"type": "STRING"},
                        "call_result": {"type": "STRING"},
                        "next_contact_date": {"type": "STRING"},
                        "next_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                }

                prompt = (
                    f"{ANALYSIS_SYSTEM_PROMPT}\n\n"
                    + ANALYSIS_USER_PROMPT.format(
                        transcript=transcript,
                        call_type=call_type_ru,
                        manager_name=manager_name,
                    )
                )

                response = await gemini.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=1200,
                        response_mime_type="application/json",
                        response_schema=response_schema,
                    ),
                )

                result_text = response.text or ""
                result_json = json.loads(result_text)

            else:
                client = _get_client()
                response = await client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": ANALYSIS_USER_PROMPT.format(
                            transcript=transcript,
                            call_type=call_type_ru,
                            manager_name=manager_name
                        )}
                    ],
                    temperature=0.1,
                    max_tokens=1200,
                    response_format={"type": "json_object"}
                )

                result_text = response.choices[0].message.content
                result_json = json.loads(result_text)

            next_steps = result_json.get("next_steps") or []
            if not isinstance(next_steps, list):
                next_steps = []
            
            # Создаём объект результата
            analysis = CallAnalysis(
                client_name=result_json.get("client_name", "Клиент"),
                manager_name=result_json.get("manager_name", manager_name),
                summary=result_json.get("summary", ""),
                client_city=result_json.get("client_city", "Не указано"),
                work_type=result_json.get("work_type", "Консультация"),
                cost=result_json.get("cost", "Не обсуждали"),
                payment_terms=result_json.get("payment_terms", "Не обсуждали"),
                call_result=result_json.get("call_result", "Не определено"),
                next_contact_date=result_json.get("next_contact_date", "Не указано"),
                next_steps=[str(x).strip() for x in next_steps if str(x).strip()][:5],
            )
            
            logger.info("Анализ завершён")
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

        steps_block = ""
        if analysis.next_steps:
            steps_block = "\n\n✅ Следующие шаги:\n" + "\n".join([f"- {s}" for s in analysis.next_steps])
        
        note = f"""🎙️ АНАЛИЗ ЗВОНКА (AI)

📞 {call_type_str} | {duration_str}

Спикеры:
- {analysis.manager_name} (менеджер)
- {analysis.client_name} (клиент)

Суть:
{analysis.summary}

📍 Город: {analysis.client_city}
🔧 Работа: {analysis.work_type}
💰 Стоимость: {analysis.cost}
💳 Оплата: {analysis.payment_terms}
📊 Итог: {analysis.call_result}
📅 Следующий контакт: {analysis.next_contact_date}{steps_block}"""
        
        return note


# Синглтон
analysis_service = AnalysisService()
