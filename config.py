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

# ============== Wappi (MAX) ==============
# Токен Wappi передаётся в header Authorization
WAPPI_API_TOKEN = os.getenv("WAPPI_API_TOKEN")
# profile_id MAX профиля в Wappi
WAPPI_MAX_PROFILE_ID = os.getenv("WAPPI_MAX_PROFILE_ID")

# ============== Геодезисты ==============
# Телефоны геодезистов (формат: 79XXXXXXXXX или +79...)
GEODESIST_1_PHONE = os.getenv("GEODESIST_1_PHONE", "")
GEODESIST_2_PHONE = os.getenv("GEODESIST_2_PHONE", "")

# ============== AmoCRM поля сделки (custom_fields_values) ==============
# Храним как строки (IDs в Amo обычно числа, но читаются как строки из env)
AMO_FIELD_WORK_TYPE = os.getenv("AMO_FIELD_WORK_TYPE", "")
AMO_FIELD_ADDRESS = os.getenv("AMO_FIELD_ADDRESS", "")
AMO_FIELD_TIME_SLOT = os.getenv("AMO_FIELD_TIME_SLOT", "")

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
    """
    Проверяет конфигурацию.

    ВАЖНО: сервис может работать в разных режимах (транскрибация звонков / уведомления геодезиста),
    поэтому на старте не валим процесс из-за отсутствия ключей, которые не используются.

    Возвращает список отсутствующих переменных (пустой список = всё ок).
    """
    required = [
        # AmoCRM нужно для записи примечаний и чтения сделок/контактов
        ("AMOCRM_DOMAIN", AMOCRM_DOMAIN),
        ("AMOCRM_ACCESS_TOKEN", AMOCRM_ACCESS_TOKEN),
    ]

    optional_groups = [
        # Транскрибация/анализ
        ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_API_KEY),
        ("OPENAI_API_KEY", OPENAI_API_KEY),
        # Уведомление геодезиста через MAX (Wappi)
        ("WAPPI_API_TOKEN", WAPPI_API_TOKEN),
        ("WAPPI_MAX_PROFILE_ID", WAPPI_MAX_PROFILE_ID),
    ]

    missing_required = [name for name, value in required if not value]
    missing_optional = [name for name, value in optional_groups if not value]

    return missing_required + missing_optional
