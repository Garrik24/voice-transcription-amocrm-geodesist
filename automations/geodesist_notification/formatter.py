from __future__ import annotations

from .types import GeodesistMessageData


def format_geodesist_message(data: GeodesistMessageData) -> str:
    """
    Формат сообщения геодезисту: без ссылок и финансов, максимум практики.
    """
    return (
        "🧭 ВЫЕЗД ГЕОДЕЗИСТА\n\n"
        f"👤 Клиент: {data.client_name}\n"
        f"☎️ Телефон: {data.client_phone}\n"
        f"🧩 Тип работ: {data.work_type}\n"
        f"📍 Адрес: {data.address}\n"
        f"🕒 Когда: {data.time_slot}\n\n"
        f"ID сделки: {data.lead_id}\n"
    )

