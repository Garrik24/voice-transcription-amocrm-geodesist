"""
Главный файл приложения.
FastAPI сервер с webhook endpoint для AmoCRM.

Запуск:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import httpx

from config import PORT, DEBUG, validate_config
from services.amocrm import amocrm_service
from services.transcription import transcription_service
from services.analysis import analysis_service
from services.telegram import telegram_service

# Настраиваем логирование
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Обработчик жизненного цикла приложения"""
    # Запуск
    logger.info("🚀 Запуск сервера транскрибации...")
    try:
        validate_config()
        logger.info("✅ Конфигурация валидна")
        await telegram_service.send_startup()
    except Exception as e:
        logger.error(f"❌ Ошибка конфигурации: {e}")
        raise
    
    yield
    
    # Остановка
    logger.info("🛑 Остановка сервера...")
    await telegram_service.send_shutdown()


app = FastAPI(
    title="Voice Transcription Service",
    description="Сервис транскрибации звонков AmoCRM с диаризацией",
    version="1.0.0",
    lifespan=lifespan
)


async def process_call(
    lead_id: int,
    call_type: str,
    record_url: str,
    responsible_user_id: Optional[int] = None
):
    """
    Основная функция обработки звонка.
    Выполняется в фоновом режиме.
    
    Args:
        lead_id: ID сделки
        call_type: Тип звонка (incoming_call/outgoing_call)
        record_url: URL записи звонка
        responsible_user_id: ID ответственного менеджера
    """
    try:
        logger.info(f"📞 Обработка звонка для сделки #{lead_id}")
        logger.info(f"   Тип: {call_type}")
        logger.info(f"   URL записи: {record_url[:50]}...")
        
        # 1. Получаем имя менеджера
        manager_name = "Менеджер"
        if responsible_user_id:
            manager_name = amocrm_service.get_manager_name(responsible_user_id)
            # Пробуем получить из API если нет в локальном словаре
            if manager_name.startswith("Менеджер #"):
                user = await amocrm_service.get_user(responsible_user_id)
                if user:
                    manager_name = user.get("name", manager_name)
        
        logger.info(f"   Менеджер: {manager_name}")
        
        # 2. Скачиваем запись
        logger.info("📥 Скачиваем запись звонка...")
        audio_data = await amocrm_service.download_call_recording(record_url)
        
        if len(audio_data) < 10000:  # Меньше 10KB - слишком маленький файл
            logger.warning(f"⚠️ Файл слишком маленький ({len(audio_data)} байт), пропускаем")
            return
        
        # 3. Транскрибируем с диаризацией
        logger.info("🎙️ Транскрибация с диаризацией...")
        transcription = await transcription_service.transcribe_audio(audio_data)
        
        if not transcription.full_text or len(transcription.full_text) < 50:
            logger.warning("⚠️ Транскрибация слишком короткая, пропускаем")
            return
        
        # 4. Определяем роли (менеджер/клиент)
        roles = transcription_service.identify_roles(transcription.speakers)
        formatted_transcript = transcription_service.format_with_roles(
            transcription.speakers, 
            roles
        )
        
        logger.info(f"📝 Транскрибация: {len(formatted_transcript)} символов")
        
        # 5. Анализируем через GPT
        logger.info("🤖 Анализ разговора через GPT...")
        call_type_simple = "outgoing" if "outgoing" in call_type else "incoming"
        analysis = await analysis_service.analyze_call(
            formatted_transcript,
            call_type=call_type_simple,
            manager_name=manager_name
        )
        
        # 6. Формируем примечание
        note_text = analysis_service.format_note(
            analysis,
            call_type=call_type_simple,
            duration_seconds=transcription.duration_seconds,
            manager_name=manager_name
        )
        
        # 7. Сохраняем в AmoCRM
        logger.info(f"💾 Сохраняем примечание в сделку #{lead_id}...")
        await amocrm_service.add_note_to_lead(lead_id, note_text)
        
        # 8. Отправляем уведомление об успехе (опционально)
        await telegram_service.send_success(
            lead_id=lead_id,
            client_name=analysis.client_name,
            call_result=analysis.call_result,
            duration_seconds=transcription.duration_seconds
        )
        
        logger.info(f"✅ Звонок для сделки #{lead_id} успешно обработан!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки звонка для сделки #{lead_id}: {e}")
        await telegram_service.send_error(
            error_type="Ошибка обработки",
            error_message=str(e),
            lead_id=lead_id
        )
        raise


@app.get("/")
async def root():
    """Проверка работоспособности"""
    return {
        "status": "ok",
        "service": "Voice Transcription Service",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check для Railway"""
    return {"status": "healthy"}


@app.post("/webhook/amocrm")
async def amocrm_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Webhook endpoint для AmoCRM.
    
    AmoCRM отправляет сюда события о звонках.
    Обработка выполняется в фоновом режиме.
    """
    try:
        # Получаем данные от AmoCRM
        # AmoCRM отправляет form data, не JSON
        form_data = await request.form()
        body = dict(form_data)
        
        # ВАЖНО: Логируем ВСЕ данные для отладки
        logger.info(f"📨 Получен webhook от AmoCRM")
        logger.info(f"📦 Ключи в body: {list(body.keys())}")
        logger.info(f"📦 Полные данные: {body}")
        
        # AmoCRM может отправлять разные типы событий
        # Нас интересуют события о звонках
        
        # Отправим в Telegram для отладки
        await telegram_service.send_message(
            f"📨 Webhook получен!\n\nКлючи: {list(body.keys())}\n\nДанные: {str(body)[:1000]}",
            disable_notification=True
        )
        
        # Вариант 1: Событие о добавлении примечания типа "звонок"
        # AmoCRM отправляет данные в формате notes[add][0][field_name]
        notes_data = {}
        for key, value in body.items():
            if key.startswith("notes["):
                notes_data[key] = value
        
        logger.info(f"📝 Данные примечаний: {notes_data}")
        
        if notes_data or "notes[add]" in str(body):
            notes = body.get("notes[add]", [])
            if isinstance(notes, str):
                import json
                try:
                    notes = json.loads(notes)
                except:
                    notes = []
            
            for note in notes if isinstance(notes, list) else [notes]:
                note_type = note.get("note_type")
                
                # Типы примечаний для звонков: 10 (входящий), 11 (исходящий)
                if note_type in ["10", "11", 10, 11]:
                    lead_id = note.get("element_id")
                    params = note.get("params", {})
                    record_url = params.get("link")
                    
                    if lead_id and record_url:
                        # Определяем тип звонка
                        call_type = "incoming_call" if note_type in ["10", 10] else "outgoing_call"
                        responsible_user_id = note.get("responsible_user_id")
                        
                        # Запускаем обработку в фоне
                        background_tasks.add_task(
                            process_call,
                            lead_id=int(lead_id),
                            call_type=call_type,
                            record_url=record_url,
                            responsible_user_id=int(responsible_user_id) if responsible_user_id else None
                        )
                        
                        logger.info(f"📌 Задача добавлена: сделка #{lead_id}")
        
        # Вариант 2: Событие через кастомный webhook
        # Добавь свою логику если нужно
        
        return JSONResponse(
            content={"status": "accepted"},
            status_code=200
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки webhook: {e}")
        await telegram_service.send_error(
            error_type="Webhook Error",
            error_message=str(e)
        )
        # Возвращаем 200 чтобы AmoCRM не ретраил
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=200
        )


@app.post("/webhook/test")
async def test_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Тестовый endpoint для проверки обработки.
    
    Пример запроса:
    POST /webhook/test
    {
        "lead_id": 12345,
        "record_url": "https://...",
        "call_type": "outgoing_call",
        "responsible_user_id": 123
    }
    """
    try:
        data = await request.json()
        
        lead_id = data.get("lead_id")
        record_url = data.get("record_url")
        call_type = data.get("call_type", "outgoing_call")
        responsible_user_id = data.get("responsible_user_id")
        
        if not lead_id or not record_url:
            raise HTTPException(
                status_code=400,
                detail="Требуются параметры: lead_id, record_url"
            )
        
        # Запускаем обработку в фоне
        background_tasks.add_task(
            process_call,
            lead_id=int(lead_id),
            call_type=call_type,
            record_url=record_url,
            responsible_user_id=int(responsible_user_id) if responsible_user_id else None
        )
        
        return {"status": "processing", "lead_id": lead_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=DEBUG
    )
