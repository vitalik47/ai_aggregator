"""
Агент №1: Сборщик.

Собирает сообщения из двух типов источников и сохраняет их в таблицу raw_messages:
- Telegram-каналы (TARGET_CHANNELS) — слушает новые сообщения в реальном времени через Telethon;
- RSS-ленты сайтов (TARGET_WEBSITES) — опрашивает раз в RSS_CHECK_INTERVAL секунд.

Важно: Telethon работает от имени вашего ЛИЧНОГО аккаунта (не бота),
потому что бот не может читать чужие каналы. При первом запуске
Telethon спросит номер телефона и код из Telegram — это нужно сделать один раз,
дальше вход сохраняется в файле scraper.session.
"""

import asyncio
import calendar
import html
import logging
import os
import re
import time

import aiohttp
import feedparser
from telethon import TelegramClient, events

from config import BASE_DIR, KEEP_DAYS, TARGET_CHANNELS, TARGET_WEBSITES, TELEGRAM_API_HASH, TELEGRAM_API_ID
from database import init_db, message_exists, save_raw_message

logger = logging.getLogger(__name__)

# Как часто опрашивать RSS-ленты (в секундах)
RSS_CHECK_INTERVAL = 300
# Файл сессии Telethon (будет создан scraper.session в папке проекта)
SESSION_PATH = os.path.join(BASE_DIR, "scraper")
# Слишком короткие сообщения (стикеры, "👍", пустые подписи к фото) не сохраняем
MIN_TEXT_LENGTH = 20


def clean_html(text):
    """Убирает HTML-теги и лишние пробелы (в RSS описание часто приходит с разметкой)."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def is_too_old(entry):
    """
    True, если статья RSS опубликована раньше, чем KEEP_DAYS дней назад.
    Такие записи удаляются при очистке базы, поэтому сохранять их нельзя —
    иначе после очистки они придут снова как «новые». Статьи без даты не отбрасываем.
    """
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return False
    # feedparser отдаёт дату в UTC, calendar.timegm превращает её в секунды
    return calendar.timegm(published) < time.time() - KEEP_DAYS * 86400


# ============================================================
# RSS
# ============================================================

async def fetch_feed(session, url):
    """Скачивает и разбирает одну RSS-ленту. Возвращает количество новых сообщений."""
    async with session.get(url) as response:
        response.raise_for_status()
        content = await response.text()

    feed = feedparser.parse(content)
    new_count = 0
    for entry in feed.entries:
        if is_too_old(entry):
            continue
        title = clean_html(entry.get("title", ""))
        summary = clean_html(entry.get("summary", ""))
        link = entry.get("link", "")
        text = f"{title}\n\n{summary}\n\n{link}".strip()

        # Пропускаем пустое и то, что уже сохранили при прошлом опросе
        if len(text) < MIN_TEXT_LENGTH or message_exists(url, text):
            continue

        save_raw_message("rss", url, text)
        new_count += 1
    return new_count


async def rss_loop():
    """Бесконечно опрашивает все RSS-ленты с паузой RSS_CHECK_INTERVAL."""
    # Некоторые сайты блокируют запросы без User-Agent
    headers = {"User-Agent": "Mozilla/5.0 (AI Aggregator RSS reader)"}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        while True:
            for url in TARGET_WEBSITES:
                try:
                    new_count = await fetch_feed(session, url)
                    logger.info("RSS %s: новых сообщений %d", url, new_count)
                except Exception:
                    # Ошибка одной ленты не должна останавливать сборщик
                    logger.exception("Не удалось обработать RSS %s", url)
            await asyncio.sleep(RSS_CHECK_INTERVAL)


# ============================================================
# Telegram
# ============================================================

async def resolve_channels(client):
    """Находит каналы по username. Несуществующие пропускает с предупреждением."""
    entities = []
    for channel in TARGET_CHANNELS:
        try:
            entities.append(await client.get_entity(channel))
            logger.info("Подключён канал %s", channel)
        except Exception as e:
            logger.warning("Канал %s не найден, пропускаю: %s", channel, e)
    return entities


def ask_phone():
    """
    Спрашивает номер телефона в терминале.
    Telethon считает любой ввод с двоеточием токеном бота и пытается войти как бот —
    поэтому такой ввод отклоняем и спрашиваем заново.
    """
    while True:
        phone = input("Введите номер телефона вашего аккаунта (например, +79991234567): ").strip()
        if ":" in phone:
            print("Это похоже на токен бота. Здесь нужен НОМЕР ТЕЛЕФОНА личного аккаунта.")
            continue
        if not phone.lstrip("+").isdigit():
            print("Номер должен состоять из цифр, можно с + в начале.")
            continue
        return phone


async def init_telegram_client():
    """
    Создаёт клиент Telethon и входит по НОМЕРУ ТЕЛЕФОНА (личный аккаунт, не бот).
    При первом запуске Telethon спросит номер, код из Telegram и (если включён) пароль 2FA.
    Дальше вход берётся из файла scraper.session, и ничего не спрашивается.
    """
    client = TelegramClient(SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start(phone=ask_phone)

    me = await client.get_me()
    logger.info("Вход в Telegram выполнен: %s (id=%s)", me.username or me.first_name, me.id)
    return client


async def telegram_listener():
    """Подключается к Telegram и сохраняет каждое новое сообщение из TARGET_CHANNELS."""
    client = await init_telegram_client()

    channels = await resolve_channels(client)
    if not channels:
        logger.error("Ни один Telegram-канал не найден — слушать нечего")
        await client.disconnect()
        return

    @client.on(events.NewMessage(chats=channels))
    async def on_new_message(event):
        text = (event.raw_text or "").strip()
        if len(text) < MIN_TEXT_LENGTH:
            return

        chat = await event.get_chat()
        username = getattr(chat, "username", None)
        source = f"@{username}" if username else str(event.chat_id)

        save_raw_message("telegram", source, text)
        logger.info("Новое сообщение из %s", source)

    logger.info("Слушаю Telegram-каналов: %d", len(channels))
    await client.run_until_disconnected()


# ============================================================
# Запуск
# ============================================================

async def run_scraper():
    """Главная функция агента: Telegram и RSS работают параллельно."""
    logger.info("Агент №1 (сборщик) запущен")
    await asyncio.gather(telegram_listener(), rss_loop())


if __name__ == "__main__":
    # Запуск агента отдельно от остального проекта (для проверки и первого входа в Telegram)
    init_db()
    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        logger.info("Агент №1 остановлен (Ctrl+C)")
