"""
Сервис для работы с AmoCRM API.
Получение данных о звонках и сохранение примечаний.
"""
import httpx
import logging
from typing import Optional, Dict, Any
from config import AMOCRM_DOMAIN, AMOCRM_ACCESS_TOKEN, MANAGERS

logger = logging.getLogger(__name__)


class AmoCRMService:
    """Класс для работы с AmoCRM API"""
    
    def __init__(self):
        self.base_url = f"https://{AMOCRM_DOMAIN}/api/v4"
        self.headers = {
            "Authorization": f"Bearer {AMOCRM_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
    
    async def get_recent_calls(self, hours: int = 1) -> list:
        """
        Получает список недавних звонков из AmoCRM.
        
        Args:
            hours: За сколько часов искать звонки
            
        Returns:
            Список событий звонков
        """
        import time
        try:
            # Время "от" в Unix timestamp
            from_timestamp = int(time.time()) - (hours * 3600)
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.base_url}/events",
                    headers=self.headers,
                    params={
                        "filter[type][]": ["incoming_call", "outgoing_call"],
                        "filter[created_at][from]": from_timestamp
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                events = data.get("_embedded", {}).get("events", [])
                logger.info(f"Найдено {len(events)} звонков за последние {hours} час(ов)")
                return events
                
        except Exception as e:
            logger.error(f"Ошибка получения звонков: {e}")
            return []
    
    async def get_call_details(self, event_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает детальную информацию о звонке.
        
        Args:
            event_id: ID события звонка
            
        Returns:
            Данные события
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.base_url}/events/{event_id}",
                    headers=self.headers
                )
                response.raise_for_status()
                return response.json()
                
        except Exception as e:
            logger.error(f"Ошибка получения деталей звонка {event_id}: {e}")
            return None
    
    async def get_call_record_url(self, entity_id: int, entity_type: str = "leads") -> Optional[str]:
        """
        Получает URL записи звонка из события в AmoCRM.
        
        Args:
            entity_id: ID сущности (сделки)
            entity_type: Тип сущности (leads, contacts, etc.)
            
        Returns:
            URL записи звонка или None
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Получаем события (звонки) связанные со сделкой
                response = await client.get(
                    f"{self.base_url}/events",
                    headers=self.headers,
                    params={
                        "filter[entity]": entity_type,
                        "filter[entity_id]": entity_id,
                        "filter[type][]": ["incoming_call", "outgoing_call"]
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                if not data.get("_embedded", {}).get("events"):
                    logger.warning(f"Нет событий звонков для {entity_type}/{entity_id}")
                    return None
                
                # Берём последний звонок
                latest_event = data["_embedded"]["events"][0]
                logger.info(f"Найден звонок: {latest_event.get('id')}, тип: {latest_event.get('type')}")
                
                # Извлекаем URL записи из value_after
                value_after = latest_event.get("value_after", [])
                for item in value_after:
                    if item.get("link"):
                        logger.info(f"Найдена ссылка на запись: {item.get('link')[:50]}...")
                        return item["link"]
                
                logger.warning(f"Нет ссылки на запись в событии: {latest_event}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка получения записи звонка: {e}")
            raise
    
    async def get_call_info_by_note_id(self, note_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о звонке по ID примечания.
        Используется когда webhook срабатывает на добавление примечания.
        
        Args:
            note_id: ID примечания
            
        Returns:
            Словарь с информацией о звонке
        """
        try:
            async with httpx.AsyncClient() as client:
                # Ищем примечание по всем сущностям
                for entity_type in ["leads", "contacts", "companies"]:
                    response = await client.get(
                        f"{self.base_url}/{entity_type}/notes",
                        headers=self.headers,
                        params={"filter[id]": note_id}
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        notes = data.get("_embedded", {}).get("notes", [])
                        if notes:
                            return notes[0]
                
                return None
                
        except Exception as e:
            logger.error(f"Ошибка получения примечания: {e}")
            raise
    
    async def download_call_recording(self, url: str) -> bytes:
        """
        Скачивает аудиофайл записи звонка.
        
        Args:
            url: URL записи звонка
            
        Returns:
            Бинарные данные аудиофайла
        """
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                # Для записей AmoCRM может потребоваться авторизация
                response = await client.get(url, headers=self.headers)
                
                # Если не требует авторизации, пробуем без неё
                if response.status_code == 401:
                    response = await client.get(url)
                
                response.raise_for_status()
                
                logger.info(f"Скачан аудиофайл: {len(response.content)} байт")
                return response.content
                
        except Exception as e:
            logger.error(f"Ошибка скачивания записи: {e}")
            raise
    
    async def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает данные сделки.
        
        Args:
            lead_id: ID сделки
            
        Returns:
            Данные сделки
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/leads/{lead_id}",
                    headers=self.headers,
                    params={"with": "contacts"}
                )
                response.raise_for_status()
                return response.json()
                
        except Exception as e:
            logger.error(f"Ошибка получения сделки {lead_id}: {e}")
            raise
    
    async def get_contact(self, contact_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает данные контакта.
        
        Args:
            contact_id: ID контакта
            
        Returns:
            Данные контакта
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/contacts/{contact_id}",
                    headers=self.headers
                )
                response.raise_for_status()
                return response.json()
                
        except Exception as e:
            logger.error(f"Ошибка получения контакта {contact_id}: {e}")
            raise
    
    async def add_note_to_lead(self, lead_id: int, text: str) -> bool:
        """
        Добавляет примечание к сделке.
        
        Args:
            lead_id: ID сделки
            text: Текст примечания
            
        Returns:
            True если успешно
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/leads/{lead_id}/notes",
                    headers=self.headers,
                    json=[{
                        "note_type": "common",
                        "params": {
                            "text": text
                        }
                    }]
                )
                response.raise_for_status()
                logger.info(f"Примечание добавлено к сделке {lead_id}")
                return True
                
        except Exception as e:
            logger.error(f"Ошибка добавления примечания к сделке {lead_id}: {e}")
            raise
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает данные пользователя (менеджера).
        
        Args:
            user_id: ID пользователя
            
        Returns:
            Данные пользователя
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/users/{user_id}",
                    headers=self.headers
                )
                response.raise_for_status()
                return response.json()
                
        except Exception as e:
            logger.error(f"Ошибка получения пользователя {user_id}: {e}")
            return None
    
    def get_manager_name(self, user_id: int) -> str:
        """
        Получает имя менеджера по ID.
        Сначала ищет в локальном словаре, потом в API.
        
        Args:
            user_id: ID пользователя в AmoCRM
            
        Returns:
            Имя менеджера
        """
        # Сначала ищем в локальном словаре
        if str(user_id) in MANAGERS:
            return MANAGERS[str(user_id)]
        
        return f"Менеджер #{user_id}"


# Синглтон для использования во всём приложении
amocrm_service = AmoCRMService()
