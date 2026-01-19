from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from config import (
    AMO_FIELD_ADDRESS,
    AMO_FIELD_TIME_SLOT,
    AMO_FIELD_WORK_TYPE,
    GEODESIST_1_PHONE,
    GEODESIST_2_PHONE,
    WAPPI_API_TOKEN,
    WAPPI_MAX_PROFILE_ID,
)
from services.amocrm import amocrm_service

from .formatter import format_geodesist_message
from .types import GeodesistMessageData, GeodesistWebhookPayload
from .wappi_max import WappiMaxClient, WappiMaxConfig, normalize_phone_ru

logger = logging.getLogger(__name__)

# Дедуп в памяти, чтобы не спамить при ретраях webhook
_PROCESSED: set[str] = set()
_LOCK = asyncio.Lock()


async def _dedup(key: str) -> bool:
    """
    Возвращает True если ключ уже был обработан.
    """
    async with _LOCK:
        if key in _PROCESSED:
            return True
        if len(_PROCESSED) > 5000:
            _PROCESSED.clear()
        _PROCESSED.add(key)
        return False


def _get_cf_value(lead: Dict[str, Any], field_id: str) -> str:
    """
    Достаёт значение кастомного поля сделки по field_id.
    Поддерживает типовые форматы AmoCRM custom_fields_values.
    """
    if not field_id:
        return ""

    try:
        fid = int(field_id)
    except Exception:
        return ""

    for cf in lead.get("custom_fields_values") or []:
        if cf.get("field_id") != fid:
            continue
        values = cf.get("values") or []
        if not values:
            return ""
        v0 = values[0] or {}
        # текст/число
        if "value" in v0 and v0.get("value") is not None:
            return str(v0["value"]).strip()
        # справочник/enum
        if "enum" in v0 and v0.get("enum") is not None:
            return str(v0["enum"]).strip()
        if "enum_id" in v0 and v0.get("enum_id") is not None:
            return str(v0["enum_id"]).strip()
        return ""

    return ""


def _contact_phone(contact: Dict[str, Any]) -> str:
    """
    Достаёт телефон из контакта (field_code PHONE).
    """
    for cf in contact.get("custom_fields_values") or []:
        if cf.get("field_code") != "PHONE":
            continue
        values = cf.get("values") or []
        for v in values:
            if not isinstance(v, dict):
                continue
            raw = v.get("value")
            if raw:
                return str(raw).strip()
    return ""


def _primary_contact_id(lead: Dict[str, Any]) -> Optional[int]:
    embedded = lead.get("_embedded") or {}
    contacts = embedded.get("contacts") or []
    if not contacts:
        return None
    first = contacts[0] or {}
    cid = first.get("id")
    return int(cid) if cid else None


def _resolve_geodesist_phone(payload: GeodesistWebhookPayload) -> str:
    """
    Определяет номер геодезиста:
    - если пришёл geodesist_phone → используем его
    - если пришёл geodesist "1"/"2" → берём из env
    - иначе:
      - пытаемся извлечь номер из строки (например "Дмитрий, тел +7961...")
      - затем пытаемся трактовать geodesist как номер
    """
    if payload.geodesist_phone:
        return normalize_phone_ru(payload.geodesist_phone)

    g = (payload.geodesist or "").strip()
    if g == "1":
        return normalize_phone_ru(GEODESIST_1_PHONE)
    if g == "2":
        return normalize_phone_ru(GEODESIST_2_PHONE)

    # попытка вытащить телефон из строки
    # поддерживаем формы: +7XXXXXXXXXX, 7XXXXXXXXXX, 8XXXXXXXXXX (будет нормализовано)
    m = re.search(r"(\+?\d[\d\-\s()]{9,}\d)", g)
    if m:
        phone_guess = normalize_phone_ru(m.group(1))
        if phone_guess:
            return phone_guess

    # fallback: если в geodesist пришёл телефон целиком
    return normalize_phone_ru(g)


def _wappi_client() -> WappiMaxClient:
    if not WAPPI_API_TOKEN or not WAPPI_MAX_PROFILE_ID:
        raise RuntimeError("Wappi MAX не настроен: нужны WAPPI_API_TOKEN и WAPPI_MAX_PROFILE_ID")
    return WappiMaxClient(WappiMaxConfig(api_token=WAPPI_API_TOKEN, profile_id=WAPPI_MAX_PROFILE_ID))


async def notify_geodesist(payload: GeodesistWebhookPayload) -> None:
    """
    Основной сценарий:
    lead_id -> get lead + contact -> format -> send MAX -> add note to lead
    """
    # dedup: ключ по сделке + геодезисту (на уровне этапа достаточно)
    dedup_key = f"lead:{payload.lead_id}:g:{payload.geodesist or payload.geodesist_phone or ''}"
    if await _dedup(dedup_key):
        logger.info("⏭️ Геодезист: дубль webhook, скипаем (%s)", dedup_key)
        return

    geodesist_phone = _resolve_geodesist_phone(payload)
    if not geodesist_phone:
        raise ValueError("Не удалось определить телефон геодезиста (geodesist/geodesist_phone пустые)")

    # Данные можно взять напрямую из webhook (если робот их передал),
    # иначе фолбэк: запрашиваем сделку/контакт в AmoCRM.
    lead: Dict[str, Any] = {}
    contact: Dict[str, Any] = {}

    client_name = (payload.client_name or "").strip()
    client_phone = (payload.client_phone or "").strip()
    work_type = (payload.work_type or "").strip()
    address = (payload.address or "").strip()
    time_slot = (payload.time_slot or "").strip()

    need_amo = not (client_name and client_phone and work_type and address and time_slot)
    if need_amo:
        lead = await amocrm_service.get_lead(payload.lead_id) or {}
        contact_id = _primary_contact_id(lead) if lead else None
        if contact_id:
            contact = await amocrm_service.get_contact(contact_id) or {}

        if not client_name:
            client_name = (contact.get("name") or "").strip()
        if not client_phone:
            client_phone = _contact_phone(contact)

        if not work_type:
            work_type = _get_cf_value(lead or {}, AMO_FIELD_WORK_TYPE)
        if not address:
            address = _get_cf_value(lead or {}, AMO_FIELD_ADDRESS)
        if not time_slot:
            time_slot = _get_cf_value(lead or {}, AMO_FIELD_TIME_SLOT)

    client_name = client_name or "Не указано"
    client_phone = client_phone or "Не указано"
    work_type = work_type or "Не указано"
    address = address or "Не указано"
    time_slot = time_slot or "Не указано"

    msg_data = GeodesistMessageData(
        lead_id=payload.lead_id,
        geodesist_phone=geodesist_phone,
        client_name=client_name,
        client_phone=client_phone,
        work_type=work_type,
        address=address,
        time_slot=time_slot,
    )

    message_text = format_geodesist_message(msg_data)

    # 1) отправка в MAX
    client = _wappi_client()
    wappi_result = await client.send_text(recipient_phone=geodesist_phone, body=message_text)

    # 2) примечание в сделку (история)
    note_text = (
        "✅ Геодезисту отправлено в MAX\n\n"
        f"Геодезист: {geodesist_phone}\n"
        f"Клиент: {client_name}\n"
        f"Телефон: {client_phone}\n"
        f"Тип работ: {work_type}\n"
        f"Адрес: {address}\n"
        f"Когда: {time_slot}\n\n"
        f"Wappi: {wappi_result}"
    )
    await amocrm_service.add_note_to_entity(payload.lead_id, note_text, "leads")

