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
        await telegram_service.send_message(
            f"📥 [2/7] Скачиваем запись...\nСделка: #{lead_id}",
            disable_notification=True
        )
        
        audio_data = await amocrm_service.download_call_recording(record_url)
        
        if len(audio_data) < 10000:  # Меньше 10KB - слишком маленький файл
            logger.warning(f"⚠️ Файл слишком маленький ({len(audio_data)} байт), пропускаем")
            await telegram_service.send_message(
                f"⚠️ Файл слишком маленький ({len(audio_data)} байт)!\nПропускаем...",
                disable_notification=False
            )
            return
        
        await telegram_service.send_message(
            f"✅ [2/7] Скачано: {len(audio_data)} байт",
            disable_notification=True
        )
        
        # 3. Транскрибируем с диаризацией
        logger.info("🎙️ Транскрибация с диаризацией...")
        await telegram_service.send_message(
            f"🎙️ [3/7] Транскрибация через AssemblyAI...\n(может занять 1-3 минуты)",
            disable_notification=True
        )
        
        transcription = await transcription_service.transcribe_audio(audio_data)
        
        if not transcription.full_text or len(transcription.full_text) < 50:
            logger.warning("⚠️ Транскрибация слишком короткая, пропускаем")
            await telegram_service.send_message(
                f"⚠️ Транскрибация слишком короткая: {len(transcription.full_text or '')} символов",
                disable_notification=False
            )
            return
        
        await telegram_service.send_message(
            f"✅ [3/7] Транскрибировано: {len(transcription.full_text)} символов\n"
            f"Длительность: {transcription.duration_seconds:.0f} сек",
            disable_notification=True
        )
        
        # 4. Определяем роли (менеджер/клиент)
        roles = transcription_service.identify_roles(transcription.speakers)
        formatted_transcript = transcription_service.format_with_roles(
            transcription.speakers, 
            roles
        )
        
        logger.info(f"📝 Транскрибация: {len(formatted_transcript)} символов")
        await telegram_service.send_message(
            f"👥 [4/7] Роли определены\n{len(transcription.speakers)} реплик",
            disable_notification=True
        )
        
        # 5. Анализируем через GPT
        logger.info("🤖 Анализ разговора через GPT...")
        await telegram_service.send_message(
            f"🤖 [5/7] Анализ через GPT...",
            disable_notification=True
        )
        
        call_type_simple = "outgoing" if "outgoing" in call_type else "incoming"
        analysis = await analysis_service.analyze_call(
            formatted_transcript,
            call_type=call_type_simple,
            manager_name=manager_name
        )
        
        await telegram_service.send_message(
            f"✅ [5/7] Анализ завершён\nКлиент: {analysis.client_name}\nГород: {analysis.city}",
            disable_notification=True
        )
        
        # 6. Формируем примечание
        note_text = analysis_service.format_note(
            analysis,
            call_type=call_type_simple,
            duration_seconds=transcription.duration_seconds,
            manager_name=manager_name
        )
        
        await telegram_service.send_message(
            f"📝 [6/7] Примечание сформировано: {len(note_text)} символов",
            disable_notification=True
        )
        
        # 7. Сохраняем в AmoCRM
        logger.info(f"💾 Сохраняем примечание в сделку #{lead_id}...")
        await telegram_service.send_message(
            f"💾 [7/7] Сохраняем в AmoCRM...",
            disable_notification=True
        )
        await amocrm_service.add_note_to_lead(lead_id, note_text)
        
        # 8. Отправляем уведомление об успехе
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
    
    ЛОГИКА ИЗ MAKE.COM:
    1. Webhook триггерит проверку
    2. Запрашиваем ВСЕ звонки за последний час через API
    3. Для каждого звонка получаем примечание с ссылкой на запись
    4. Обрабатываем звонки
    """
    try:
        # Получаем данные от AmoCRM (для логирования)
        form_data = await request.form()
        body = dict(form_data)
        
        logger.info(f"📨 Получен webhook от AmoCRM, ключей: {len(body)}")
        
        # Уведомляем в Telegram
        await telegram_service.send_message(
            f"📨 Webhook получен!\n\nЗапускаем проверку звонков...",
            disable_notification=True
        )
        
        # ЛОГИКА:
        # 1. Получаем звонки за последние 10 минут (для немедленной обработки)
        logger.info(f"🔍 Запрашиваем звонки за последние 10 минут...")
        events = await amocrm_service.get_recent_calls(minutes=10)
        
        if not events:
            logger.info(f"📭 Нет звонков за последние 10 минут")
            await telegram_service.send_message(
                f"📭 Нет звонков за последние 10 минут",
                disable_notification=True
            )
            return JSONResponse(content={"status": "no_calls"}, status_code=200)
        
        logger.info(f"📞 Найдено {len(events)} событий звонков")
        await telegram_service.send_message(
            f"📞 Найдено {len(events)} звонков!\n\nОбрабатываем...",
            disable_notification=True
        )
        
        # 2. Для каждого события получаем данные и обрабатываем
        processed = 0
        for event in events:
            try:
                # Получаем данные звонка (entity_type, entity_id, note_id, record_url)
                call_data = await amocrm_service.process_call_event(event)
                
                if call_data and call_data.get("record_url"):
                    logger.info(f"✅ Звонок {call_data['event_id']} готов к обработке")
                    
                    await telegram_service.send_message(
                        f"🎙️ Обрабатываем звонок!\n\n"
                        f"Тип: {call_data['event_type']}\n"
                        f"Сущность: {call_data['entity_type']}/{call_data['entity_id']}\n"
                        f"URL: {call_data['record_url'][:50]}...",
                        disable_notification=True
                    )
                    
                    # Запускаем транскрибацию в фоне
                    background_tasks.add_task(
                        process_call,
                        lead_id=call_data["entity_id"],
                        call_type=call_data["event_type"],
                        record_url=call_data["record_url"],
                        responsible_user_id=call_data.get("created_by")
                    )
                    processed += 1
                else:
                    logger.info(f"⏭️ Событие {event.get('id')} пропущено (нет записи)")
                    
            except Exception as e:
                logger.error(f"Ошибка обработки события: {e}")
        
        logger.info(f"✅ Обработано {processed} из {len(events)} звонков")
        
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
