from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GeodesistMessageData:
    lead_id: int
    geodesist_phone: str
    client_name: str
    client_phone: str
    work_type: str
    address: str
    time_slot: str


@dataclass(frozen=True)
class GeodesistWebhookPayload:
    """
    Нормализованный payload, который приходит от робота AmoCRM.

    Минимально нужен lead_id + (geodesist or geodesist_phone).
    """

    lead_id: int
    geodesist: Optional[str] = None  # "1" | "2" | произвольная строка
    geodesist_phone: Optional[str] = None  # "79..." или "+79..."

    # Опционально: если удобнее передавать из робота AmoCRM напрямую (без поиска field_id)
    work_type: Optional[str] = None
    address: Optional[str] = None
    time_slot: Optional[str] = None  # может быть и датой-временем, если поле "Время выезда"

    # Опционально: если хочется вообще не дергать AmoCRM за контактом
    client_name: Optional[str] = None
    client_phone: Optional[str] = None

