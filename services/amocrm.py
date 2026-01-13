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
        Точно как в Make.com: GET /api/v4/events с фильтрами
        
        Args:
            hours: За сколько часов искать звонки
            
        Returns:
            Список событий звонков
        """
        import time
        try:
            # Время "от" в Unix timestamp (как в Make: formatDate(addHours(now; -6); "X"))
            from_timestamp = int(time.time()) - (hours * 3600)
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Точный URL из Make.com:
                # /api/v4/events?filter[type][0]=outgoing_call&filter[type][1]=incoming_call&filter[created_at][from]=...
                response = await client.get(
                    f"{self.base_url}/events",
                    headers=self.headers,
                    params={
                        "filter[type][0]": "outgoing_call",
                        "filter[type][1]": "incoming_call",
                        "filter[created_at][from]": from_timestamp
                    }
                )
                
                if response.status_code == 204:
                    logger.info("Нет звонков (204 No Content)")
                    return []
                    
                response.raise_for_status()
                data = response.json()
                
                events = data.get("_embedded", {}).get("events", [])
                logger.info(f"Найдено {len(events)} звонков за последние {hours} час(ов)")
                return events
                
        except Exception as e:
            logger.error(f"Ошибка получения звонков: {e}")
            return []
    
    async def get_note_with_recording(self, entity_type: str, entity_id: int, note_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает примечание с записью звонка.
        Как в Make.com: GET /api/v4/{entity_type}/{entity_id}/notes/{note_id}
        
        Args:
            entity_type: Тип сущности (leads, contacts, companies)
            entity_id: ID сущности
            note_id: ID примечания
            
        Returns:
            Данные примечания с params.link
        """
        try:
            # Преобразуем entity_type как в Make: switch(entity_type; "contact"; "contacts"; ...)
            type_map = {
                "lead": "leads",
                "contact": "contacts",
                "company": "companies"
            }
            api_type = type_map.get(entity_type, entity_type)
            
            url = f"{self.base_url}/{api_type}/{entity_id}/notes/{note_id}"
            logger.info(f"Запрос примечания: {url}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)
                
                if response.status_code == 204:
                    logger.warning(f"Примечание не найдено (204)")
                    return None
                    
                response.raise_for_status()
                data = response.json()
                logger.info(f"Получено примечание: {data}")
                return data
                
        except Exception as e:
            logger.error(f"Ошибка получения примечания: {e}")
            return None
    
    async def process_call_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Обрабатывает событие звонка и получает ссылку на запись.
        Логика из Make.com:
        1. Из события берём entity_type, entity_id, value_after[].note.id
        2. Запрашиваем примечание
        3. Из примечания берём params.link
        
        Args:
            event: Событие звонка из API
            
        Returns:
            Словарь с данными для обработки или None
        """
        try:
            event_id = event.get("id")
            event_type = event.get("type")  # incoming_call или outgoing_call
            entity_type = event.get("entity_type")  # lead, contact, company
            entity_id = event.get("entity_id")
            created_by = event.get("created_by")
            
            logger.info(f"Обработка события #{event_id}: {event_type} для {entity_type}/{entity_id}")
            
            # Ищем note.id в value_after
            value_after = event.get("value_after", [])
            note_id = None
            for item in value_after:
                if isinstance(item, dict) and "note" in item:
                    note_id = item["note"].get("id")
                    break
            
            if not note_id:
                logger.warning(f"Нет note_id в событии #{event_id}")
                return None
            
            logger.info(f"Найден note_id: {note_id}")
            
            # Получаем примечание с записью
            note_data = await self.get_note_with_recording(entity_type, entity_id, note_id)
            
            if not note_data:
                logger.warning(f"Не удалось получить примечание {note_id}")
                return None
            
            # Извлекаем ссылку на запись из params.link
            params = note_data.get("params", {})
            record_link = params.get("link")
            
            if not record_link:
                logger.warning(f"Нет ссылки на запись в примечании {note_id}")
                return None
            
            logger.info(f"✅ Найдена ссылка на запись: {record_link[:50]}...")
            
            return {
                "event_id": event_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "note_id": note_id,
                "record_url": record_link,
                "created_by": created_by,
                "params": params
            }
            
        except Exception as e:
            logger.error(f"Ошибка обработки события: {e}")
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
