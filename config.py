"""
Конфигурация приложения.
Все секретные ключи берутся из переменных окружения Railway.
"""
import os
from dotenv import load_dotenv

# Загружаем .env файл для локальной разработки
load_dotenv()

# ============== AmoCRM ==============
AMOCRM_DOMAIN = os.getenv("AMOCRM_DOMAIN")  # например: stavgeo26.amocrm.ru
AMOCRM_ACCESS_TOKEN = os.getenv("AMOCRM_ACCESS_TOKEN")
AMOCRM_REFRESH_TOKEN = os.getenv("AMOCRM_REFRESH_TOKEN")
AMOCRM_CLIENT_ID = os.getenv("AMOCRM_CLIENT_ID")
AMOCRM_CLIENT_SECRET = os.getenv("AMOCRM_CLIENT_SECRET")

# ============== AssemblyAI ==============
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# ============== OpenAI ==============
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ============== Telegram ==============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # ID чата для уведомлений об ошибках

# ============== Приложение ==============
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
PORT = int(os.getenv("PORT", 8000))

# ============== Список менеджеров ==============
# Формат: {"user_id_в_amocrm": "Имя"}
# Заполни ID своих менеджеров из AmoCRM
MANAGERS = {
    # "12345": "Елена",
    # "12346": "Дмитрий",
    # "12347": "Александр",
}

def validate_config():
    """Проверяет, что все необходимые переменные заданы"""
    required = [
        ("AMOCRM_DOMAIN", AMOCRM_DOMAIN),
        ("AMOCRM_ACCESS_TOKEN", AMOCRM_ACCESS_TOKEN),
        ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_API_KEY),
        ("OPENAI_API_KEY", OPENAI_API_KEY),
    ]
    
    missing = [name for name, value in required if not value]
    
    if missing:
        raise ValueError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")
    
    return True
