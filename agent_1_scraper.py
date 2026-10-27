"""
Агент №1: Сборщик.

Собирает сообщения из двух типов источников и сохраняет их в таблицу raw_messages:
- Telegram-каналы (TARGET_CHANNELS) — раз в TELEGRAM_CHECK_INTERVAL секунд читает последние посты
  через Telethon (client.get_messages); подписка на публичный канал для этого не нужна;
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import feedparser
from telethon import TelegramClient

from config import BASE_DIR, KEEP_DAYS, TARGET_CHANNELS, TARGET_WEBSITES, TELEGRAM_API_HASH, TELEGRAM_API_ID
from database import init_db, message_exists, save_raw_message

logger = logging.getLogger(__name__)

# Как часто опрашивать RSS-ленты (в секундах)
RSS_CHECK_INTERVAL = 300
# Файл сессии Telethon (будет создан scraper.session в папке проекта)
SESSION_PATH = os.path.join(BASE_DIR, "scraper")
# Как часто читать Telegram-каналы (в секундах) и сколько последних постов брать из каждого
TELEGRAM_CHECK_INTERVAL = 300
TELEGRAM_FETCH_LIMIT = 20
# Посты старше этого не сохраняем: иначе при первом запуске старые новости попали бы в ленту как свежие
TELEGRAM_MAX_AGE_HOURS = 24
# Слишком короткие сообщения (стикеры, "👍", пустые подписи к фото) не сохраняем
MIN_TEXT_LENGTH = 20


def clean_html(text):
    """Убирает HTML-теги и лишние пробелы (в RSS описание часто приходит с разметкой)."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_url(url):
    """
    Ссылка на статью без меток рекламы: ...?utm_source=rss_feed&utm_medium=rss -> ...
    Не http(s) — None (такую ссылку в Telegram не откроешь).
    """
    if not url.startswith(("http://", "https://")):
        return None
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if not key.startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


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

        # link — ссылка на саму статью (url здесь — адрес RSS-ленты)
        save_raw_message("rss", url, text, url=clean_url(link))
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


def channel_username(channel):
    """
    Username канала без @. У каналов с купленным (коллекционным) именем, например @crypto,
    поле username пустое, а имя лежит в списке usernames.
    """
    if getattr(channel, "username", None):
        return channel.username
    active = [u.username for u in getattr(channel, "usernames", None) or [] if u.active]
    return active[0] if active else None


async def fetch_channel(client, channel):
    """Читает последние посты канала и сохраняет новые. Возвращает количество новых сообщений."""
    username = channel_username(channel)
    source = f"@{username}" if username else str(channel.id)
    since = time.time() - TELEGRAM_MAX_AGE_HOURS * 3600
    new_count = 0

    # get_messages отдаёт посты от новых к старым — сохраняем по порядку публикации
    for post in reversed(await client.get_messages(channel, limit=TELEGRAM_FETCH_LIMIT)):
        text = (post.raw_text or "").strip()
        # Пропускаем короткое (фото без подписи, стикеры), старое и уже сохранённое
        if len(text) < MIN_TEXT_LENGTH or post.date.timestamp() < since or message_exists(source, text):
            continue
        # Ссылка на пост: https://t.me/<канал>/<номер сообщения> (у закрытых каналов без username её нет)
        post_url = f"https://t.me/{username}/{post.id}" if username else None
        save_raw_message("telegram", source, text, url=post_url)
        new_count += 1
    return new_count


async def telegram_loop():
    """
    Раз в TELEGRAM_CHECK_INTERVAL читает последние посты каждого канала из TARGET_CHANNELS.
    Раньше бот ждал уведомлений о новых постах (events.NewMessage), но Telegram присылает их
    только по каналам, на которые аккаунт подписан, — без подписки не приходило ничего.
    get_messages читает публичный канал и без подписки.
    """
    client = await init_telegram_client()

    channels = await resolve_channels(client)
    if not channels:
        logger.error("Ни один Telegram-канал не найден — читать нечего")
        await client.disconnect()
        return

    logger.info("Читаю Telegram-каналов: %d (раз в %d сек.)", len(channels), TELEGRAM_CHECK_INTERVAL)
    while True:
        for channel in channels:
            try:
                new_count = await fetch_channel(client, channel)
                logger.info("Telegram @%s: новых сообщений %d", channel_username(channel), new_count)
            except Exception:
                # Ошибка одного канала не должна останавливать сборщик
                logger.exception("Не удалось прочитать канал @%s", channel_username(channel))
        await asyncio.sleep(TELEGRAM_CHECK_INTERVAL)


# ============================================================
# Запуск
# ============================================================

async def run_scraper():
    """Главная функция агента: Telegram и RSS работают параллельно."""
    logger.info("Агент №1 (сборщик) запущен")
    await asyncio.gather(telegram_loop(), rss_loop())


if __name__ == "__main__":
    # Запуск агента отдельно от остального проекта (для проверки и первого входа в Telegram)
    from instance_lock import acquire_instance_lock
    acquire_instance_lock("agent_1_scraper.py")
    init_db()
    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        logger.info("Агент №1 остановлен (Ctrl+C)")
