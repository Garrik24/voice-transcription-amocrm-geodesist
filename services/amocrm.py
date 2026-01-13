"""
Сервис для работы с AmoCRM API.
Получение данных о звонках и сохранение примечаний.
"""
import httpx
import ssl
import logging
from typing import Optional, Dict, Any
from config import AMOCRM_DOMAIN, AMOCRM_ACCESS_TOKEN, MANAGERS

logger = logging.getLogger(__name__)

# SSL контекст с отключенной проверкой (для серверов с проблемными сертификатами)
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


class AmoCRMService:
    """Класс для работы с AmoCRM API"""
    
    def __init__(self):
        self.base_url = f"https://{AMOCRM_DOMAIN}/api/v4"
        self.headers = {
            "Authorization": f"Bearer {AMOCRM_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
    
    async def get_recent_calls(self, minutes: int = 10) -> list:
        """
        Получает список недавних звонков из AmoCRM.
        Точно как в Make.com: GET /api/v4/events с фильтрами
        
        Args:
            minutes: За сколько минут искать звонки
            
        Returns:
            Список событий звонков
        """
        import time
        try:
            # Время "от" в Unix timestamp
            from_timestamp = int(time.time()) - (minutes * 60)
            logger.info(f"🕐 Ищем звонки с timestamp: {from_timestamp} (последние {minutes} мин)")
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
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
                logger.info(f"Найдено {len(events)} звонков за последние {minutes} минут")
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
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
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
            
            # Извлекаем телефон из params
            phone = params.get("phone", "")
            
            return {
                "event_id": event_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "note_id": note_id,
                "record_url": record_link,
                "created_by": created_by,
                "phone": phone,
                "params": params
            }
            
        except Exception as e:
            logger.error(f"Ошибка обработки события: {e}")
            return None
    
    async def get_call_events_for_entity(self, entity_id: int, entity_type: str) -> list:
        """
        Получает события звонков для конкретной сущности (контакта или сделки).
        
        Args:
            entity_id: ID сущности
            entity_type: Тип сущности (contacts, leads)
            
        Returns:
            Список событий звонков
        """
        try:
            # Преобразуем entity_type для API
            api_entity_type = "contact" if entity_type == "contacts" else "lead"
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.get(
                    f"{self.base_url}/events",
                    headers=self.headers,
                    params={
                        "filter[entity]": api_entity_type,
                        "filter[entity_id]": entity_id,
                        "filter[type][0]": "outgoing_call",
                        "filter[type][1]": "incoming_call"
                    }
                )
                
                if response.status_code == 204:
                    logger.info(f"Нет звонков для {entity_type}/{entity_id}")
                    return []
                    
                response.raise_for_status()
                data = response.json()
                
                events = data.get("_embedded", {}).get("events", [])
                logger.info(f"Найдено {len(events)} звонков для {entity_type}/{entity_id}")
                return events
                
        except Exception as e:
            logger.error(f"Ошибка получения звонков для {entity_type}/{entity_id}: {e}")
            return []
    
    async def download_call_recording(self, url: str) -> bytes:
        """
        Скачивает аудиофайл записи звонка.
        Обходит проверку SSL для серверов с невалидными сертификатами.
        
        Args:
            url: URL записи звонка
            
        Returns:
            Бинарные данные аудиофайла
        """
        import ssl
        import httpx
        
        try:
            logger.info(f"📥 Скачиваем запись: {url[:80]}...")
            
            # Создаём SSL контекст, который полностью игнорирует проверку сертификатов
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            
            async with httpx.AsyncClient(
                follow_redirects=True, 
                timeout=120.0, 
                verify=ssl_ctx  # Используем кастомный SSL контекст
            ) as client:
                # Сначала пробуем без авторизации
                response = await client.get(url)
                
                # Если требует авторизации, пробуем с ней
                if response.status_code in [401, 403]:
                    response = await client.get(url, headers=self.headers)
                
                response.raise_for_status()
                
                content_length = len(response.content)
                logger.info(f"✅ Скачано: {content_length} байт")
                
                return response.content
                
        except Exception as e:
            logger.error(f"❌ Ошибка скачивания: {e}")
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
    
    async def add_note_to_entity(self, entity_id: int, text: str, entity_type: str = "leads") -> bool:
        """
        Добавляет примечание к сущности (сделке, контакту, компании).
        
        Args:
            entity_id: ID сущности
            text: Текст примечания
            entity_type: Тип сущности (leads, contacts, companies)
            
        Returns:
            True если успешно
        """
        try:
            # Приводим entity_type к множественному числу если нужно
            if entity_type == "lead":
                entity_type = "leads"
            elif entity_type == "contact":
                entity_type = "contacts"
            elif entity_type == "company":
                entity_type = "companies"
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.post(
                    f"{self.base_url}/{entity_type}/{entity_id}/notes",
                    headers=self.headers,
                    json=[{
                        "note_type": "common",
                        "params": {
                            "text": text
                        }
                    }]
                )
                
                if response.status_code == 400:
                    error_text = response.text
                    try:
                        error_json = response.json()
                        logger.error(f"AmoCRM вернул 400 для {entity_type}/{entity_id}: {error_json}")
                    except:
                        logger.error(f"AmoCRM вернул 400 для {entity_type}/{entity_id}: {error_text}")
                    # Пробуем получить больше информации об ошибке
                    logger.error(f"Запрос был: POST {self.base_url}/{entity_type}/{entity_id}/notes")
                    logger.error(f"Текст примечания (первые 200 символов): {text[:200]}")
                
                response.raise_for_status()
                logger.info(f"Примечание добавлено к {entity_type}/{entity_id}")
                return True
                
        except Exception as e:
            logger.error(f"Ошибка добавления примечания к {entity_type}/{entity_id}: {e}")
            raise
    
    async def add_note_to_lead(self, lead_id: int, text: str) -> bool:
        """Обратная совместимость"""
        return await self.add_note_to_entity(lead_id, text, "leads")
    
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
    
    async def get_active_lead_for_contact(self, contact_id: int) -> Optional[int]:
        """
        Получает ID АКТИВНОЙ сделки, привязанной к контакту.
        
        Закрытые сделки (status_id = 142 - успех, 143 - провал) игнорируются!
        
        Args:
            contact_id: ID контакта
            
        Returns:
            ID активной сделки или None
        """
        # Статусы "закрытых" сделок (не добавляем примечания туда)
        CLOSED_STATUSES = {
            142,  # Успешно реализовано
            143,  # Закрыто и не реализовано
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                # 1. Получаем связи контакта
                response = await client.get(
                    f"{self.base_url}/contacts/{contact_id}/links",
                    headers=self.headers
                )
                
                if response.status_code == 204:
                    logger.info(f"У контакта {contact_id} нет связей")
                    return None
                    
                response.raise_for_status()
                data = response.json()
                
                # 2. Собираем ID всех связанных сделок
                links = data.get("_embedded", {}).get("links", [])
                lead_ids = [
                    link.get("to_entity_id") 
                    for link in links 
                    if link.get("to_entity_type") == "leads"
                ]
                
                if not lead_ids:
                    logger.info(f"У контакта {contact_id} нет сделок")
                    return None
                
                logger.info(f"🔍 Контакт {contact_id} имеет {len(lead_ids)} сделок: {lead_ids}")
                
                # 3. Проверяем статус каждой сделки
                for lead_id in lead_ids:
                    try:
                        lead_response = await client.get(
                            f"{self.base_url}/leads/{lead_id}",
                            headers=self.headers
                        )
                        
                        if lead_response.status_code == 200:
                            lead_data = lead_response.json()
                            status_id = lead_data.get("status_id")
                            lead_name = lead_data.get("name", "")
                            
                            logger.info(f"  Сделка #{lead_id} '{lead_name}': статус {status_id}")
                            
                            # Если сделка НЕ закрыта - используем её
                            if status_id not in CLOSED_STATUSES:
                                logger.info(f"✅ Найдена активная сделка #{lead_id}")
                                return lead_id
                            else:
                                logger.info(f"  ⏭️ Сделка #{lead_id} закрыта, пропускаем")
                                
                    except Exception as e:
                        logger.warning(f"Не удалось проверить сделку {lead_id}: {e}")
                
                logger.info(f"❌ Все сделки контакта {contact_id} закрыты")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка получения активной сделки для контакта {contact_id}: {e}")
            return None
    
    async def create_lead_for_contact(
        self, 
        contact_id: int, 
        contact_name: str = "",
        phone: str = "",
        responsible_user_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Создаёт новую сделку и привязывает к контакту.
        
        Args:
            contact_id: ID контакта
            contact_name: Имя контакта (для названия сделки)
            phone: Телефон (для названия сделки)
            responsible_user_id: Ответственный менеджер
            
        Returns:
            ID созданной сделки или None
        """
        try:
            # Формируем название сделки
            lead_name = f"Входящий звонок: {contact_name or phone or contact_id}"
            
            # Данные для создания сделки
            lead_data = [{
                "name": lead_name,
                "_embedded": {
                    "contacts": [{"id": contact_id}]
                }
            }]
            
            # Добавляем ответственного если есть
            if responsible_user_id:
                lead_data[0]["responsible_user_id"] = responsible_user_id
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.post(
                    f"{self.base_url}/leads",
                    headers=self.headers,
                    json=lead_data
                )
                
                if response.status_code == 400:
                    logger.error(f"Ошибка создания сделки: {response.text}")
                    return None
                
                response.raise_for_status()
                data = response.json()
                
                # Получаем ID созданной сделки
                leads = data.get("_embedded", {}).get("leads", [])
                if leads:
                    lead_id = leads[0].get("id")
                    logger.info(f"✅ Создана сделка #{lead_id} для контакта #{contact_id}")
                    return lead_id
                
                return None
                
        except Exception as e:
            logger.error(f"Ошибка создания сделки для контакта {contact_id}: {e}")
            return None
    
    async def get_or_create_lead_for_contact(
        self, 
        contact_id: int,
        phone: str = "",
        responsible_user_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Получает АКТИВНУЮ сделку контакта или создаёт новую.
        
        Логика:
        - Если есть активная (не закрытая) сделка → возвращаем её
        - Если все сделки закрыты или нет сделок → создаём новую
        
        Args:
            contact_id: ID контакта
            phone: Телефон для названия сделки
            responsible_user_id: Ответственный менеджер
            
        Returns:
            ID сделки (существующей активной или новой)
        """
        # Ищем АКТИВНУЮ сделку (не закрытую)
        lead_id = await self.get_active_lead_for_contact(contact_id)
        
        if lead_id:
            logger.info(f"✅ Используем активную сделку #{lead_id}")
            return lead_id
        
        # Нет активной сделки - создаём новую
        logger.info(f"📝 Создаём новую сделку для контакта #{contact_id}...")
        
        contact = await self.get_contact(contact_id)
        contact_name = contact.get("name", "") if contact else ""
        
        return await self.create_lead_for_contact(
            contact_id=contact_id,
            contact_name=contact_name,
            phone=phone,
            responsible_user_id=responsible_user_id
        )


# Синглтон для использования во всём приложении
amocrm_service = AmoCRMService()
