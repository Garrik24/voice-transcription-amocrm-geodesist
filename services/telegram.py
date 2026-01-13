"""
Сервис уведомлений через Telegram.
Отправляет уведомления об ошибках и статусах обработки.
"""
import httpx
import logging
from typing import Optional
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


class TelegramService:
    """Сервис отправки уведомлений в Telegram"""
    
    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
    
    @property
    def is_configured(self) -> bool:
        """Проверяет, настроен ли Telegram"""
        return bool(self.bot_token and self.chat_id)
    
    async def send_message(
        self, 
        text: str, 
        parse_mode: str = "HTML",
        disable_notification: bool = False
    ) -> bool:
        """
        Отправляет сообщение в Telegram.
        
        Args:
            text: Текст сообщения
            parse_mode: Режим парсинга (HTML, Markdown)
            disable_notification: Отключить звук уведомления
            
        Returns:
            True если успешно
        """
        if not self.is_configured:
            logger.warning("Telegram не настроен, пропускаем отправку")
            return False
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_notification": disable_notification
                    }
                )
                response.raise_for_status()
                logger.info("Сообщение отправлено в Telegram")
                return True
                
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False
    
    async def send_error(
        self, 
        error_type: str,
        error_message: str,
        lead_id: Optional[int] = None,
        details: Optional[str] = None
    ) -> bool:
        """
        Отправляет уведомление об ошибке.
        
        Args:
            error_type: Тип ошибки
            error_message: Текст ошибки
            lead_id: ID сделки (если есть)
            details: Дополнительные детали
            
        Returns:
            True если успешно
        """
        text = f"""🚨 <b>ОШИБКА ТРАНСКРИБАЦИИ</b>

<b>Тип:</b> {error_type}
<b>Ошибка:</b> {error_message}"""
        
        if lead_id:
            text += f"\n<b>Сделка:</b> #{lead_id}"
        
        if details:
            text += f"\n\n<b>Детали:</b>\n<code>{details[:500]}</code>"
        
        return await self.send_message(text)
    
    async def send_success(
        self,
        lead_id: int,
        client_name: str,
        call_result: str,
        duration_seconds: float
    ) -> bool:
        """
        Отправляет уведомление об успешной обработке.
        (Опционально, можно отключить)
        
        Args:
            lead_id: ID сделки
            client_name: Имя клиента
            call_result: Итог звонка
            duration_seconds: Длительность
            
        Returns:
            True если успешно
        """
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes}:{seconds:02d}"
        
        text = f"""✅ <b>Звонок обработан</b>

<b>Сделка:</b> #{lead_id}
<b>Клиент:</b> {client_name}
<b>Длительность:</b> {duration_str}
<b>Итог:</b> {call_result}"""
        
        return await self.send_message(text, disable_notification=True)
    
    async def send_startup(self) -> bool:
        """Отправляет уведомление о запуске сервера"""
        text = """🟢 <b>Сервер транскрибации запущен</b>

Готов принимать webhook от AmoCRM."""
        
        return await self.send_message(text)
    
    async def send_shutdown(self, reason: str = "Плановая остановка") -> bool:
        """Отправляет уведомление об остановке сервера"""
        text = f"""🔴 <b>Сервер транскрибации остановлен</b>

<b>Причина:</b> {reason}"""
        
        return await self.send_message(text)


# Синглтон
telegram_service = TelegramService()
