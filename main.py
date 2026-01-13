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
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, File, Form, UploadFile
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

# Кэш обработанных звонков, чтобы избежать дублей и петель
# В продакшене лучше использовать Redis, но для начала хватит и Set в памяти
PROCESSED_CALLS = set()
PROCESSED_LOCK = asyncio.Lock()


async def is_already_processed(record_url: str) -> bool:
    """Проверяет, обрабатывался ли уже этот звонок по URL записи"""
    async with PROCESSED_LOCK:
        if record_url in PROCESSED_CALLS:
            return True
        # Ограничиваем размер кэша (храним последние 1000 записей)
        if len(PROCESSED_CALLS) > 1000:
            PROCESSED_CALLS.clear()
        PROCESSED_CALLS.add(record_url)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Обработчик жизненного цикла приложения"""
    # Запуск
    logger.info("🚀 Запуск сервера транскрибации...")
    try:
        validate_config()
        logger.info("✅ Конфигурация валидна")
        # Не спамим в Telegram при каждом старте
        # await telegram_service.send_startup()
        logger.info("🟢 Сервер запущен")
    except Exception as e:
        logger.error(f"❌ Ошибка конфигурации: {e}")
        raise
    
    yield
    
    # Остановка
    logger.info("🛑 Остановка сервера...")
    # Не спамим в Telegram
    # await telegram_service.send_shutdown()
    logger.info("🔴 Сервер остановлен")


app = FastAPI(
    title="Voice Transcription Service",
    description="Сервис транскрибации звонков AmoCRM с диаризацией",
    version="1.0.0",
    lifespan=lifespan
)


async def process_call(
    entity_id: int,
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
        # 0. Проверяем дубликаты
        if await is_already_processed(record_url):
            logger.info(f"⏭️ Звонок {record_url[:50]}... уже обрабатывается или обработан, скипаем")
            return

        # ВАЖНО: если звонок привязан к контакту, находим АКТИВНУЮ сделку или создаём новую!
        lead_id = entity_id
        target_entity_type = entity_type
        
        if entity_type == "contact" or entity_type == "contacts":
            logger.info(f"🔍 Звонок привязан к контакту #{entity_id}, ищем активную сделку...")
            
            # Ищем активную сделку или создаём новую
            found_lead = await amocrm_service.get_or_create_lead_for_contact(
                contact_id=entity_id,
                phone=phone,
                responsible_user_id=responsible_user_id
            )
            
            if found_lead and found_lead != entity_id:
                # Убеждаемся, что получили ID сделки, а не контакта
                lead_id = found_lead
                target_entity_type = "leads"
                logger.info(f"✅ Используем сделку #{lead_id} для контакта #{entity_id}")
            else:
                # Крайний случай - не удалось создать сделку или вернулся тот же ID
                logger.error(f"❌ Не удалось найти/создать сделку для контакта #{entity_id}. Получено: {found_lead}")
                # НЕ отправляем в Telegram - только логируем
                return
        
        logger.info(f"📞 Обработка звонка → {target_entity_type}/{lead_id}, тип: {call_type}")
        
        # 1. Получаем имя менеджера
        manager_name = "Менеджер"
        if responsible_user_id:
            manager_name = amocrm_service.get_manager_name(responsible_user_id)
            if manager_name.startswith("Менеджер #"):
                user = await amocrm_service.get_user(responsible_user_id)
                if user:
                    manager_name = user.get("name", manager_name)
        
        # 2. Скачиваем запись (если не загружена вручную)
        if record_url.startswith("uploaded://"):
            logger.error("❌ process_call вызван с uploaded:// URL - используйте process_uploaded_audio")
            return
        
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
        
        # 7. Сохраняем в AmoCRM (в СДЕЛКУ!)
        logger.info(f"💾 Сохраняем примечание в {target_entity_type}/{lead_id}...")
        try:
            await amocrm_service.add_note_to_entity(lead_id, note_text, target_entity_type)
            logger.info(f"✅ Примечание успешно добавлено к {target_entity_type}/{lead_id}")
        except Exception as note_error:
            logger.error(f"❌ Ошибка добавления примечания к {target_entity_type}/{lead_id}: {note_error}")
            # Проверяем, может быть это ID контакта, а не сделки?
            if target_entity_type == "leads":
                logger.error(f"⚠️ ВНИМАНИЕ: Пытались добавить примечание к сделке #{lead_id}, но получили ошибку!")
                logger.error(f"⚠️ Возможно, {lead_id} - это ID контакта, а не сделки!")
            raise
        
        # 8. Отправляем красивый анализ в Telegram
        call_datetime = datetime.now().strftime("%d.%m.%Y %H:%M")
        amocrm_url = f"https://{AMOCRM_DOMAIN}/{target_entity_type}/detail/{lead_id}"
        
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
        # НЕ отправляем ошибки в Telegram - только логируем (избегаем спама)


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
    """
    try:
        # 1. Получаем данные от AmoCRM
        form_data = await request.form()
        body = dict(form_data)
        logger.info(f"📨 Webhook от AmoCRM: {list(body.keys())[:5]}...")
        
        # 2. Пытаемся извлечь ID сущности из вебхука (любого типа)
        target_entity_id = None
        target_entity_type = "leads"
        
        # Перебираем ключи, ищем [id] или [element_id]
        for key, value in body.items():
            if "[id]" in key or "[element_id]" in key:
                try:
                    target_entity_id = int(value)
                    # Определяем тип сущности по ключу
                    if "contacts" in key:
                        target_entity_type = "contacts"
                    elif "leads" in key:
                        target_entity_type = "leads"
                    break
                except:
                    continue
        
        if not target_entity_id:
            logger.warning("⚠️ Не удалось найти ID сущности в вебхуке")
            # На всякий случай проверяем недавние звонки (ограничим до 5 минут)
            events = await amocrm_service.get_recent_calls(minutes=5)
        else:
            logger.info(f"🔍 Webhook для {target_entity_type} #{target_entity_id}. Ищем звонки...")
            # Запрашиваем звонки ТОЛЬКО для этой сущности
            events = await amocrm_service.get_call_events_for_entity(target_entity_id, target_entity_type)
        
        if not events:
            logger.info(f"📭 Звонков не обнаружено")
            return JSONResponse(content={"status": "no_calls"}, status_code=200)
        
        # 3. Обрабатываем каждый звонок
        processed = 0
        for event in events:
            try:
                call_data = await amocrm_service.process_call_event(event)
                
                if call_data and call_data.get("record_url"):
                    # Запускаем транскрибацию в фоне
                    background_tasks.add_task(
                        process_call,
                        entity_id=call_data["entity_id"],
                        call_type=call_data["event_type"],
                        record_url=call_data["record_url"],
                        responsible_user_id=call_data.get("created_by"),
                        phone=call_data.get("phone", ""),
                        entity_type=call_data.get("entity_type", "leads")
                    )
                    processed += 1
                    
            except Exception as e:
                logger.error(f" Ошибка обработки события звонка: {e}")
        
        return JSONResponse(content={"status": "accepted", "processed": processed}, status_code=200)
        
    except Exception as e:
        logger.error(f"❌ Webhook ошибка: {e}")
        return JSONResponse(content={"status": "error"}, status_code=200)




@app.post("/upload-audio")
async def upload_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    lead_id: int = Form(...),
    call_type: str = Form("incoming_call"),
    phone: str = Form(""),
    manager_name: str = Form("Менеджер")
):
    """
    Загрузка аудиофайла вручную для транскрибации.
    
    Используй когда SSL сертификат не работает:
    1. Скачай запись вручную
    2. Загрузи через этот endpoint
    3. Результат появится в AmoCRM и Telegram
    
    Пример curl:
    curl -X POST https://voice-transcription-production.up.railway.app/upload-audio \
      -F "file=@recording.mp3" \
      -F "lead_id=12345" \
      -F "call_type=incoming_call" \
      -F "phone=+79001234567"
    """
    from datetime import datetime
    from config import AMOCRM_DOMAIN
    
    try:
        # Читаем файл
        audio_data = await file.read()
        logger.info(f"📤 Загружен файл: {file.filename}, размер: {len(audio_data)} байт")
        
        if len(audio_data) < 10000:
            raise HTTPException(status_code=400, detail="Файл слишком маленький")
        
        # Запускаем обработку напрямую (без скачивания)
        background_tasks.add_task(
            process_uploaded_audio,
            audio_data=audio_data,
            lead_id=lead_id,
            call_type=call_type,
            phone=phone,
            manager_name=manager_name
        )
        
        return {
            "status": "processing",
            "lead_id": lead_id,
            "file_size": len(audio_data),
            "message": "Файл принят в обработку. Результат появится в Telegram и AmoCRM."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def process_uploaded_audio(
    audio_data: bytes,
    lead_id: int,
    call_type: str,
    phone: str,
    manager_name: str
):
    """Обработка загруженного аудио (без скачивания)"""
    from datetime import datetime
    from config import AMOCRM_DOMAIN
    
    try:
        logger.info(f"📞 Обработка загруженного аудио для сделки #{lead_id}")
        
        # Используем общую логику обработки (без скачивания)
        # 1. Транскрибируем
        logger.info("🎙️ Транскрибация...")
        transcription = await transcription_service.transcribe_audio(audio_data)
        
        if not transcription.full_text or len(transcription.full_text) < 50:
            logger.warning("⚠️ Транскрибация слишком короткая")
            return
        
        # 2. Определяем роли
        roles = transcription_service.identify_roles(transcription.speakers)
        formatted_transcript = transcription_service.format_with_roles(
            transcription.speakers, 
            roles
        )
        logger.info(f"📝 Транскрибация: {len(formatted_transcript)} символов")
        
        # 3. Анализируем через GPT
        logger.info("🤖 Анализ через GPT...")
        call_type_simple = "outgoing" if "outgoing" in call_type else "incoming"
        analysis = await analysis_service.analyze_call(
            formatted_transcript,
            call_type=call_type_simple,
            manager_name=manager_name
        )
        
        # 4. Формируем примечание
        note_text = analysis_service.format_note(
            analysis,
            call_type=call_type_simple,
            duration_seconds=transcription.duration_seconds,
            manager_name=manager_name
        )
        
        # 5. Сохраняем в AmoCRM (в СДЕЛКУ!)
        logger.info(f"💾 Сохраняем примечание в leads/{lead_id}...")
        await amocrm_service.add_note_to_entity(lead_id, note_text, "leads")
        logger.info(f"✅ Примечание успешно добавлено к leads/{lead_id}")
        
        # 6. Отправляем красивый анализ в Telegram
        call_datetime = datetime.now().strftime("%d.%m.%Y %H:%M")
        amocrm_url = f"https://{AMOCRM_DOMAIN}/leads/detail/{lead_id}"
        
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
            record_url=""
        )
        
        logger.info(f"✅ Загруженный файл для сделки #{lead_id} обработан!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки загруженного файла: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=DEBUG
    )
