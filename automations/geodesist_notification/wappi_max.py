from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def normalize_phone_ru(phone: str) -> str:
    """
    Приводит телефон к формату Wappi примеров: '79XXXXXXXXX' (11 цифр, начинается на 7).
    """
    digits = re.sub(r"\D+", "", phone or "")
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return digits
    return digits  # fallback (на случай не-РФ формата)


@dataclass(frozen=True)
class WappiMaxConfig:
    api_token: str
    profile_id: str
    base_url: str = "https://wappi.pro"


class WappiMaxClient:
    def __init__(self, config: WappiMaxConfig):
        self._cfg = config

    async def send_text(self, recipient_phone: str, body: str) -> Dict[str, Any]:
        """
        Отправить текстовое сообщение в MAX через Wappi.
        Используем async endpoint: POST /maxapi/async/message/send
        """
        phone = normalize_phone_ru(recipient_phone)
        if not phone:
            raise ValueError("recipient_phone is empty")
        if not body or not body.strip():
            raise ValueError("message body is empty")

        url = f"{self._cfg.base_url}/maxapi/async/message/send"
        headers = {"Authorization": self._cfg.api_token}
        params = {"profile_id": self._cfg.profile_id}
        payload = {"recipient": phone, "body": body}

        logger.info("📨 Wappi MAX: отправка сообщения на %s", phone)
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.post(url, headers=headers, params=params, json=payload)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"status_code": resp.status_code, "text": resp.text}

    async def check_contact_registered(self, phone: str) -> Optional[Dict[str, Any]]:
        """
        Опционально: проверить, зарегистрирован ли номер в MAX.
        GET /maxapi/sync/contact/check
        """
        normalized = normalize_phone_ru(phone)
        if not normalized:
            return None
        url = f"{self._cfg.base_url}/maxapi/sync/contact/check"
        headers = {"Authorization": self._cfg.api_token}
        params = {"profile_id": self._cfg.profile_id, "phone": int(normalized)}

        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"status_code": resp.status_code, "text": resp.text}

