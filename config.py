"""
Конфигурация проекта AI Aggregator.

Загружает переменные из .env, настраивает логирование
и хранит списки источников для сбора новостей.
"""

import logging
import os
import sys

from dotenv import load_dotenv

# Загружаем переменные из файла .env (лежит рядом с этим файлом)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- Ключи и токены ---
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# --- Необязательные настройки (есть значения по умолчанию) ---
# "or" нужен, чтобы пустая строка в .env тоже заменялась на значение по умолчанию
DATABASE_PATH = os.getenv("DATABASE_PATH") or os.path.join(BASE_DIR, "ai_aggregator.db")
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
# Модель OpenAI для агентов №2 и №3
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
# Кому разрешено пользоваться ботом: Telegram id через запятую (пусто = всем)
ALLOWED_USER_IDS = [
    int(user_id) for user_id in (os.getenv("ALLOWED_USER_IDS") or "").split(",") if user_id.strip().isdigit()
]

# --- Источники новостей ---
TARGET_WEBSITES = [
    "https://cointelegraph.com/rss",
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    "https://cryptoslate.com/feed/",
    "https://thedefiant.io/api/feed",
    "https://www.theblock.co/rss.xml",
    "https://cryptobriefing.com/feed/",
    "https://bitcoinist.com/feed/",
    "https://newsbtc.com/feed/",
    "https://cointelegraph.com/rss/tag/bitcoin",
    "https://cointelegraph.com/rss/tag/ethereum",
    "https://cointelegraph.com/rss/tag/defi",
    "https://cointelegraph.com/rss/tag/solana",
]
TARGET_CHANNELS = [
    "@cointelegraph",
    "@coindesk",
    "@solana",
    "@ethereum",
    "@defi_news"
]
# --- Настройка логирования ---
def setup_logging():
    """Логи пишутся в консоль и в файл app.log."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # encoding="utf-8" обязателен на Windows, иначе русский текст сломается
            logging.FileHandler(os.path.join(BASE_DIR, "app.log"), encoding="utf-8"),
        ],
    )


setup_logging()
logger = logging.getLogger(__name__)


# --- Проверка обязательных переменных ---
def validate_config():
    """Проверяет, что все обязательные переменные заданы в .env."""
    required = {
        "TELEGRAM_API_ID": TELEGRAM_API_ID,
        "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "OPENAI_API_KEY": OPENAI_API_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные в .env: "
            + ", ".join(missing)
            + ". Заполните их в файле "
            + os.path.join(BASE_DIR, ".env")
        )

    # Telethon ожидает API_ID в виде числа
    if not TELEGRAM_API_ID.isdigit():
        raise RuntimeError("TELEGRAM_API_ID должен быть числом (смотрите my.telegram.org)")


validate_config()
TELEGRAM_API_ID = int(TELEGRAM_API_ID)

logger.info("Конфигурация загружена (LOG_LEVEL=%s, DATABASE_PATH=%s)", LOG_LEVEL, DATABASE_PATH)
