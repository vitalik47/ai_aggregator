"""
Точка входа AI Aggregator.

Запускает всё вместе в одном процессе:
- Агент №1 (сборщик): Telegram-каналы + RSS -> raw_messages
- Агент №2 (фильтр):  raw_messages -> GPT -> events
- Telegram-бот, который через Агента №3 отвечает пользователю

Перед первым запуском войдите в Telegram отдельно:
    python agent_1_scraper.py
(иначе Telethon будет ждать номер телефона и код прямо здесь)
"""

import asyncio
import logging

from agent_1_scraper import run_scraper
from agent_2_filter import run_filter
from bot import run_bot
from config import KEEP_DAYS
from database import clean_old_events, init_db

logger = logging.getLogger(__name__)


async def main():
    init_db()
    clean_old_events(KEEP_DAYS)
    logger.info("AI Aggregator запускается...")
    # Все три части работают параллельно. Если одна упадёт с ошибкой,
    # программа остановится, и ошибка будет видна в логе.
    await asyncio.gather(run_scraper(), run_filter(), run_bot())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("AI Aggregator остановлен (Ctrl+C)")
    except Exception:
        logger.exception("AI Aggregator остановлен из-за ошибки")
        raise
