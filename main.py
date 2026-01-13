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
    responsible_user_id: Optional[int] = None,
    phone: str = "",
    entity_type: str = "leads"
):
    """
    Основная функция обработки звонка.
    Выполняется в фоновом режиме.
    """
    from datetime import datetime
    from config import AMOCRM_DOMAIN
    
    try:
        logger.info(f"📞 Обработка звонка для сделки #{lead_id}, тип: {call_type}")
        
        # 1. Получаем имя менеджера
        manager_name = "Менеджер"
        if responsible_user_id:
            manager_name = amocrm_service.get_manager_name(responsible_user_id)
            if manager_name.startswith("Менеджер #"):
                user = await amocrm_service.get_user(responsible_user_id)
                if user:
                    manager_name = user.get("name", manager_name)
        
        # 2. Скачиваем запись
        logger.info("📥 Скачиваем запись...")
        audio_data = await amocrm_service.download_call_recording(record_url)
        
        if len(audio_data) < 10000:
            logger.warning(f"⚠️ Файл слишком маленький ({len(audio_data)} байт)")
            return
        
        # 3. Транскрибируем
        logger.info("🎙️ Транскрибация...")
        transcription = await transcription_service.transcribe_audio(audio_data)
        
        if not transcription.full_text or len(transcription.full_text) < 50:
            logger.warning("⚠️ Транскрибация слишком короткая")
            return
        
        # 4. Определяем роли
        roles = transcription_service.identify_roles(transcription.speakers)
        formatted_transcript = transcription_service.format_with_roles(
            transcription.speakers, 
            roles
        )
        logger.info(f"📝 Транскрибация: {len(formatted_transcript)} символов")
        
        # 5. Анализируем через GPT
        logger.info("🤖 Анализ через GPT...")
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
        logger.info(f"💾 Сохраняем в сделку #{lead_id}...")
        await amocrm_service.add_note_to_lead(lead_id, note_text)
        
        # 8. Отправляем красивый анализ в Telegram
        call_datetime = datetime.now().strftime("%d.%m.%Y %H:%M")
        amocrm_url = f"https://{AMOCRM_DOMAIN}/{entity_type}/detail/{lead_id}"
        
        await telegram_service.send_call_analysis(
            call_datetime=call_datetime,
            call_type=call_type_simple,
            phone=phone or "Не определён",
            manager_name=analysis.manager_name,
            client_name=analysis.client_name,
            summary=analysis.summary,
            manager_rating=analysis.manager_rating,
            what_good=analysis.what_good,
            what_improve=analysis.what_improve,
            amocrm_url=amocrm_url,
            record_url=record_url
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
    
    ЛОГИКА ИЗ MAKE.COM:
    1. Webhook триггерит проверку
    2. Запрашиваем ВСЕ звонки за последний час через API
    3. Для каждого звонка получаем примечание с ссылкой на запись
    4. Обрабатываем звонки
    """
    try:
        # Получаем данные от AmoCRM
        form_data = await request.form()
        body = dict(form_data)
        logger.info(f"📨 Webhook от AmoCRM, ключей: {len(body)}")
        
        # Получаем звонки за последние 10 минут
        events = await amocrm_service.get_recent_calls(minutes=10)
        
        if not events:
            logger.info(f"📭 Нет звонков")
            return JSONResponse(content={"status": "no_calls"}, status_code=200)
        
        logger.info(f"📞 Найдено {len(events)} звонков")
        
        # Обрабатываем каждый звонок
        processed = 0
        for event in events:
            try:
                call_data = await amocrm_service.process_call_event(event)
                
                if call_data and call_data.get("record_url"):
                    logger.info(f"✅ Звонок {call_data['event_id']} → обработка")
                    
                    # Запускаем транскрибацию в фоне
                    background_tasks.add_task(
                        process_call,
                        lead_id=call_data["entity_id"],
                        call_type=call_data["event_type"],
                        record_url=call_data["record_url"],
                        responsible_user_id=call_data.get("created_by"),
                        phone=call_data.get("phone", ""),
                        entity_type=call_data.get("entity_type", "leads")
                    )
                    processed += 1
                    
            except Exception as e:
                logger.error(f"Ошибка события: {e}")
        
        logger.info(f"✅ Запущено {processed} из {len(events)}")
        return JSONResponse(content={"status": "accepted"}, status_code=200)
        
    except Exception as e:
        logger.error(f"❌ Webhook ошибка: {e}")
        # Отправляем ТОЛЬКО критические ошибки в Telegram
        await telegram_service.send_error(
            error_type="Webhook Error",
            error_message=str(e)
        )
        return JSONResponse(content={"status": "error"}, status_code=200)


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
