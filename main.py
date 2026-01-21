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
from automations.geodesist_notification.handler import notify_geodesist
from automations.geodesist_notification.types import GeodesistWebhookPayload

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
        missing = validate_config()
        if missing:
            # Не валим процесс: Railway должен получить /health, а функциональность
            # будет зависеть от того, какие переменные заданы.
            logger.warning(f"⚠️ Не все переменные окружения заданы: {', '.join(missing)}")
        else:
            logger.info("✅ Конфигурация валидна")
        # Не спамим в Telegram при каждом старте
        # await telegram_service.send_startup()
        logger.info("🟢 Сервер запущен")
    except Exception as e:
        # Не валим старт: пусть поднимется хотя бы healthcheck.
        logger.error(f"❌ Ошибка конфигурации/старта: {e}")
    
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
    call_created_at: Optional[int] = None,
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
        # Логика согласно документации AmoCRM:
        # 1. Если звонок привязан к контакту → запрашиваем его сделки
        # 2. Если есть активная (не закрытая) сделка → используем её
        # 3. Если все сделки закрыты или нет сделок → создаём новую
        # 4. Добавляем примечание в найденную/созданную сделку
        
        lead_id = entity_id
        target_entity_type = entity_type
        
        # Нормализуем entity_type для проверки (AmoCRM может вернуть "contact" или "contacts")
        normalized_check = entity_type.lower()
        if normalized_check in ["contact", "contacts"]:
            logger.info(f"🔍 Звонок привязан к контакту #{entity_id}")
            logger.info(f"📋 Запрашиваем сделки контакта #{entity_id}...")
            
            # Получаем контакт для проверки
            contact = await amocrm_service.get_contact(entity_id)
            if contact:
                contact_name = contact.get("name", "")
                logger.info(f"📇 Контакт: {contact_name}")
            
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
        # Важно: берём время звонка из AmoCRM (created_at) и отображаем в нужной таймзоне.
        from zoneinfo import ZoneInfo
        from config import APP_TIMEZONE

        if call_created_at:
            ts = int(call_created_at)
            # подстрахуемся: иногда timestamps приходят в миллисекундах
            if ts > 10**12:
                ts = ts // 1000
            call_datetime = datetime.fromtimestamp(ts, tz=ZoneInfo(APP_TIMEZONE)).strftime("%d.%m.%Y %H:%M")
        else:
            call_datetime = datetime.now(ZoneInfo(APP_TIMEZONE)).strftime("%d.%m.%Y %H:%M")
        amocrm_url = f"https://{AMOCRM_DOMAIN}/{target_entity_type}/detail/{lead_id}"
        
        await telegram_service.send_call_analysis(
            call_datetime=call_datetime,
            call_type=call_type_simple,
            phone=phone or "Не определён",
            manager_name=analysis.manager_name,
            client_name=analysis.client_name,
            summary=analysis.summary,
            amocrm_url=amocrm_url,
            record_url=record_url,
            client_city=analysis.client_city,
            work_type=analysis.work_type,
            cost=analysis.cost,
            payment_terms=analysis.payment_terms,
            call_result=analysis.call_result,
            next_contact_date=analysis.next_contact_date,
            next_steps=analysis.next_steps,
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
    
    AmoCRM отправляет webhook когда создаётся примечание о звонке.
    Примечание уже содержит ссылку на запись (params.link).
    """
    try:
        # 1. Получаем данные от AmoCRM
        form_data = await request.form()
        body = dict(form_data)
        
        # Логируем ВСЕ ключи связанные с примечаниями для отладки
        note_keys = [k for k in body.keys() if '[note]' in k]
        if note_keys:
            logger.info(f"📨 Webhook примечание, ключей: {len(note_keys)}")
            # Логируем первые 10 ключей для отладки
            for k in note_keys[:10]:
                logger.info(f"  {k} = {body[k]}")
        else:
            # Это не примечание - другой тип webhook
            keys_preview = list(body.keys())[:5]
            logger.info(f"📨 Webhook (не примечание): {keys_preview}")
        
        # 2. Ищем примечание о звонке в webhook
        # AmoCRM отправляет: contacts[note][0][note][id], contacts[note][0][note][element_id], etc.
        note_id = None
        element_id = None  # ID контакта/сделки к которому привязано примечание
        entity_type = None
        note_type = None
        responsible_user_id = None
        
        for key, value in body.items():
            # Ищем note[id] - ID самого примечания
            if "[note][id]" in key and value:
                try:
                    note_id = int(value)
                except:
                    pass
            
            # Ищем element_id - ID сущности (контакта/сделки)
            if "[note][element_id]" in key and value:
                try:
                    element_id = int(value)
                except:
                    pass
            
            # Определяем тип сущности
            if "contacts[note]" in key:
                entity_type = "contacts"
            elif "leads[note]" in key:
                entity_type = "leads"
            
            # Тип примечания (call_in, call_out, common, etc.)
            if "[note][note_type]" in key and value:
                note_type = value
            
            # Ответственный
            if "[note][responsible_user_id]" in key and value:
                try:
                    responsible_user_id = int(value)
                except:
                    pass
        
        # 3. Если это не примечание - игнорируем (не спамим в лог)
        if not element_id or not entity_type:
            # Это webhook о создании контакта/сделки/задачи - не о звонке
            return JSONResponse(content={"status": "ignored", "reason": "not_a_note"}, status_code=200)
        
        # Логируем извлечённые данные для отладки
        logger.info(f"📋 Извлечено: note_id={note_id}, element_id={element_id}, entity={entity_type}, note_type={note_type}")
        
        # 4. Получаем данные примечания
        note_data = None
        
        if note_id:
            # Если note_id найден - запрашиваем конкретное примечание
            logger.info(f"📝 Запрос примечания #{note_id} для {entity_type}/{element_id}")
            note_data = await amocrm_service.get_note_with_recording(
                entity_type=entity_type.rstrip('s'),  # contacts -> contact
                entity_id=element_id,
                note_id=note_id
            )
        else:
            # Если note_id не найден - получаем последние примечания и ищем звонок
            logger.info(f"🔍 note_id не в webhook, ищем последние примечания {entity_type}/{element_id}")
            recent_notes = await amocrm_service.get_recent_notes(
                entity_type=entity_type,
                entity_id=element_id,
                limit=5
            )
            
            # Ищем примечание о звонке среди последних
            for note in recent_notes:
                if note.get("note_type") in ["call_in", "call_out"]:
                    note_data = note
                    logger.info(f"✅ Найдено примечание о звонке: #{note.get('id')}")
                    break
        
        if not note_data:
            logger.warning(f"⚠️ Не удалось найти примечание о звонке")
            return JSONResponse(content={"status": "note_not_found"}, status_code=200)
        
        # 6. Проверяем тип примечания
        actual_note_type = note_data.get("note_type")
        if actual_note_type not in ["call_in", "call_out"]:
            # Это обычное примечание, не звонок
            logger.info(f"⏭️ Примечание #{note_id} не звонок (тип: {actual_note_type})")
            return JSONResponse(content={"status": "not_a_call", "note_type": actual_note_type}, status_code=200)
        
        # 7. Извлекаем ссылку на запись
        params = note_data.get("params", {})
        record_url = params.get("link")
        phone = params.get("phone", "")
        
        if not record_url:
            logger.warning(f"⚠️ Примечание #{note_id} без записи")
            return JSONResponse(content={"status": "no_recording"}, status_code=200)
        
        logger.info(f"✅ Найден звонок! Тип: {actual_note_type}, запись: {record_url[:50]}...")
        
        # 8. Определяем тип звонка
        call_type = "incoming_call" if actual_note_type == "call_in" else "outgoing_call"
        
        # 9. Запускаем обработку в фоне
        background_tasks.add_task(
            process_call,
            entity_id=element_id,
            call_type=call_type,
            record_url=record_url,
            call_created_at=note_data.get("created_at"),
            responsible_user_id=responsible_user_id or note_data.get("responsible_user_id"),
            phone=phone,
            entity_type=entity_type
        )
        
        return JSONResponse(content={"status": "processing", "note_id": note_id}, status_code=200)
        
    except Exception as e:
        logger.error(f"❌ Webhook ошибка: {e}")
        return JSONResponse(content={"status": "error"}, status_code=200)


@app.post("/webhook/amocrm/geodesist-assigned")
async def geodesist_assigned_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Webhook от робота AmoCRM на этапе "Назначен".
    Ожидаем минимум: lead_id + (geodesist или geodesist_phone).

    Поддерживаем JSON и form-urlencoded.
    """
    try:
        content_type = (request.headers.get("content-type") or "").lower()

        lead_id = None
        geodesist = None
        geodesist_phone = None
        work_type = None
        address = None
        time_slot = None
        client_name = None
        client_phone = None

        if "application/json" in content_type:
            body = await request.json()
            lead_id = body.get("lead_id") or body.get("leadId") or body.get("id")
            geodesist = body.get("geodesist")
            geodesist_phone = body.get("geodesist_phone") or body.get("geodesistPhone")
            work_type = body.get("work_type") or body.get("workType")
            address = body.get("address")
            time_slot = body.get("time_slot") or body.get("timeSlot")
            client_name = body.get("client_name") or body.get("clientName")
            client_phone = body.get("client_phone") or body.get("clientPhone")
        else:
            form = await request.form()
            body = dict(form)
            lead_id = body.get("lead_id") or body.get("leadId") or body.get("id")
            geodesist = body.get("geodesist")
            geodesist_phone = body.get("geodesist_phone") or body.get("geodesistPhone")
            work_type = body.get("work_type") or body.get("workType")
            address = body.get("address")
            time_slot = body.get("time_slot") or body.get("timeSlot")
            client_name = body.get("client_name") or body.get("clientName")
            client_phone = body.get("client_phone") or body.get("clientPhone")

        if lead_id is None:
            return JSONResponse(content={"status": "error", "reason": "lead_id_required"}, status_code=200)

        try:
            lead_id_int = int(str(lead_id).strip())
        except Exception:
            return JSONResponse(content={"status": "error", "reason": "lead_id_invalid"}, status_code=200)

        payload = GeodesistWebhookPayload(
            lead_id=lead_id_int,
            geodesist=str(geodesist).strip() if geodesist is not None else None,
            geodesist_phone=str(geodesist_phone).strip() if geodesist_phone is not None else None,
            work_type=str(work_type).strip() if work_type is not None else None,
            address=str(address).strip() if address is not None else None,
            time_slot=str(time_slot).strip() if time_slot is not None else None,
            client_name=str(client_name).strip() if client_name is not None else None,
            client_phone=str(client_phone).strip() if client_phone is not None else None,
        )

        background_tasks.add_task(notify_geodesist, payload)
        return JSONResponse(content={"status": "processing", "lead_id": lead_id_int}, status_code=200)

    except Exception as e:
        logger.error(f"❌ Геодезист webhook ошибка: {e}")
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
