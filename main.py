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
    
    НОВАЯ ЛОГИКА:
    1. Webhook приходит при любом событии
    2. Извлекаем lead_id из данных
    3. Запрашиваем записи звонков через API
    4. Обрабатываем звонки
    """
    try:
        # Получаем данные от AmoCRM
        form_data = await request.form()
        body = dict(form_data)
        
        logger.info(f"📨 Получен webhook от AmoCRM")
        
        # Собираем все lead_id из webhook
        lead_ids = set()
        contact_ids = set()
        
        for key, value in body.items():
            # Ищем linked_leads_id в контактах
            if "linked_leads_id" in key and value:
                lead_ids.add(value)
            # Ищем element_id в примечаниях (это может быть lead_id)
            if "element_id" in key and value:
                lead_ids.add(value)
            # Ищем id в leads
            if "leads[" in key and "[id]" in key and value:
                lead_ids.add(value)
            # Ищем id в contacts
            if "contacts[" in key and "[id]" in key and value:
                contact_ids.add(value)
        
        logger.info(f"📋 Найдены lead_ids: {lead_ids}")
        logger.info(f"📋 Найдены contact_ids: {contact_ids}")
        
        # Уведомляем в Telegram
        await telegram_service.send_message(
            f"📨 Webhook получен!\n\nLeads: {lead_ids}\nContacts: {contact_ids}",
            disable_notification=True
        )
        
        # Для каждого lead_id проверяем есть ли звонки
        for lead_id in lead_ids:
            try:
                lead_id_int = int(lead_id)
                logger.info(f"🔍 Проверяем звонки для сделки #{lead_id_int}")
                
                # Получаем URL записи звонка через API
                record_url = await amocrm_service.get_call_record_url(lead_id_int, "leads")
                
                if record_url:
                    logger.info(f"✅ Найдена запись для сделки #{lead_id_int}")
                    await telegram_service.send_message(
                        f"🎙️ Найдена запись!\n\nСделка: #{lead_id_int}\nURL: {record_url[:50]}...",
                        disable_notification=True
                    )
                    
                    # Получаем данные сделки для ответственного
                    lead_data = await amocrm_service.get_lead(lead_id_int)
                    responsible_user_id = lead_data.get("responsible_user_id") if lead_data else None
                    
                    # Запускаем обработку в фоне
                    background_tasks.add_task(
                        process_call,
                        lead_id=lead_id_int,
                        call_type="outgoing_call",
                        record_url=record_url,
                        responsible_user_id=responsible_user_id
                    )
                else:
                    logger.info(f"❌ Нет записи для сделки #{lead_id_int}")
                    
            except ValueError:
                logger.warning(f"Невалидный lead_id: {lead_id}")
        
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
