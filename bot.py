"""
Telegram-бот (aiogram 3).

Команды:
/start, /help — справка
/news         — последние события из базы (без GPT, бесплатно)
/top          — события с самым высоким insider_score
/digest       — дайджест от Агента №3
/briefing     — главное за 24 часа (доступна всем, даже при ALLOWED_USER_IDS)
/defi, /btc, /eth, /sol — 5 последних новостей по теме (по тегам из category_tags)
/sentiment btc — тональность новостей по теме за 24 часа (без темы — по всем новостям)
/entity Binance — новости про компанию, человека или проект (поиск по колонке entities)
/settings     — выбор тем (таблица user_preferences); /briefing и /digest учитывают их
Любой текст   — вопрос Агенту №3

В /start и /help под справкой есть кнопки (InlineKeyboard) — они делают то же, что команды.

Здесь используется TELEGRAM_BOT_TOKEN (бот), а не личный аккаунт, как в сборщике.
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from agent_3_assistant import answer_question, make_briefing, make_digest
from config import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from database import (
    get_events_by_entity,
    get_events_by_tag,
    get_recent_events,
    get_sentiment_stats,
    get_user_preferences,
    init_db,
    set_user_preferences,
)

logger = logging.getLogger(__name__)

# Telegram не принимает сообщения длиннее 4096 символов
MAX_MESSAGE_LENGTH = 4000
# События с такой оценкой и выше помечаем 🔥
HOT_INSIDER_SCORE = 7
# Команды для выборки по темам: команда -> (тег в базе, заголовок ответа)
TAG_COMMANDS = {
    "defi": ("defi", "🧪 DeFi"),
    "btc": ("bitcoin", "₿ Bitcoin"),
    "eth": ("ethereum", "💎 Ethereum"),
    "sol": ("solana", "☀️ Solana"),
}
# Порядок тем в меню /settings
SETTINGS_TOPICS = ["btc", "eth", "sol", "defi"]
# Тональность: значение в базе -> (значок, подпись)
SENTIMENT_LABELS = {
    "positive": ("🟢", "Позитив"),
    "negative": ("🔴", "Негатив"),
    "neutral": ("⚪", "Нейтрально"),
}

HELP_TEXT = (
    "👋 Привет! Я AI Aggregator — собираю крипто-новости из Telegram-каналов и RSS, "
    "фильтрую их с помощью ИИ и отвечаю на вопросы.\n\n"
    "📰 Новости\n"
    "/news — последние новости\n"
    "/top — самые «инсайдерские» новости\n\n"
    "🤖 Аналитика от ИИ\n"
    "/briefing — главное за 24 часа и тренд\n"
    "/digest — короткий дайджест\n\n"
    "🏷 Новости по темам\n"
    "/btc — Bitcoin\n"
    "/eth — Ethereum\n"
    "/sol — Solana\n"
    "/defi — DeFi\n\n"
    "📊 Настроение рынка\n"
    "/sentiment btc — тональность новостей за 24 часа\n"
    "(темы: btc, eth, sol, defi; без темы — по всем новостям)\n\n"
    "🔍 Поиск\n"
    "/entity Binance — новости про компанию, человека или проект\n"
    "(например: /entity BlackRock, /entity Michael Saylor — имена латиницей)\n\n"
    "⚙️ Персональные настройки\n"
    "/settings — выбрать темы для /briefing и /digest\n\n"
    "❓ Или просто напишите вопрос, например: «Что нового с Bitcoin?»\n\n"
    "/help — показать эту справку\n\n"
    "👇 Или нажмите кнопку:"
)

# Кнопки под справкой. callback_data — что именно нажали (обрабатывается в on_button)
MENU_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📰 Новости", callback_data="news"),
        InlineKeyboardButton(text="🕵️ Топ", callback_data="top"),
    ],
    [
        InlineKeyboardButton(text="🔥 Брифинг 24ч", callback_data="briefing"),
        InlineKeyboardButton(text="🤖 Дайджест", callback_data="digest"),
    ],
    [
        InlineKeyboardButton(text="₿ BTC", callback_data="tag:btc"),
        InlineKeyboardButton(text="💎 ETH", callback_data="tag:eth"),
        InlineKeyboardButton(text="☀️ SOL", callback_data="tag:sol"),
        InlineKeyboardButton(text="🧪 DeFi", callback_data="tag:defi"),
    ],
    [
        InlineKeyboardButton(text="📊 Тональность рынка", callback_data="sentiment:"),
    ],
    [
        InlineKeyboardButton(text="📊 BTC", callback_data="sentiment:btc"),
        InlineKeyboardButton(text="📊 ETH", callback_data="sentiment:eth"),
        InlineKeyboardButton(text="📊 SOL", callback_data="sentiment:sol"),
        InlineKeyboardButton(text="📊 DeFi", callback_data="sentiment:defi"),
    ],
    [
        InlineKeyboardButton(text="⚙️ Мои темы", callback_data="settings"),
    ],
])

NO_NEWS_TEXT = "Новостей пока нет. Подождите, пока агенты их соберут."
# Названия месяцев в родительном падеже для даты «20 октября 2026».
# strftime("%B") зависит от языка Windows, поэтому пишем их сами
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

dp = Dispatcher()


# ============================================================
# Вспомогательные функции
# ============================================================

def is_allowed(user_id):
    """Если задан ALLOWED_USER_IDS — пускаем только этих пользователей."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


async def check_access(message: Message):
    """Проверка доступа для команд: при отказе отвечает пользователю."""
    if is_allowed(message.from_user.id):
        return True
    logger.warning("Отказано в доступе: user_id=%s", message.from_user.id)
    await message.answer(f"Доступ закрыт. Ваш Telegram id: {message.from_user.id}")
    return False


def format_event(event):
    """Одно событие в виде текста для Telegram."""
    fire = "🔥 " if event["insider_score"] >= HOT_INSIDER_SCORE else ""
    # У старых событий тональности нет (колонка добавлена позже)
    mood = SENTIMENT_LABELS[event["sentiment"]][0] + " " if event.get("sentiment") in SENTIMENT_LABELS else ""
    text = (
        f"{fire}{event['title']}\n"
        f"{event['summary']}\n"
        f"{mood}📂 {event['category']} | 🕵️ {event['insider_score']}/10 | {event['source']}"
    )
    # У старых событий тегов нет (колонка добавлена позже)
    if event.get("category_tags"):
        text += f"\n🏷 {event['category_tags'].replace(',', ', ')}"
    return text


def split_text(text):
    """Делит длинный текст на части, чтобы Telegram их принял (режем по переносам строк)."""
    parts = []
    while len(text) > MAX_MESSAGE_LENGTH:
        cut = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if cut <= 0:
            cut = MAX_MESSAGE_LENGTH
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def with_time(text):
    """Добавляет в начало текста дату и время ответа: «📅 20 октября 2026, 17:45»."""
    # datetime.now() без часового пояса — это время компьютера, где запущен бот
    now = datetime.now()
    return f"📅 {now.day} {MONTHS[now.month - 1]} {now.year}, {now:%H:%M}\n\n{text}"


async def send_long(message: Message, text):
    """Отправляет текст любой длины несколькими сообщениями."""
    for part in split_text(text):
        await message.answer(part)


# ============================================================
# Тексты ответов (общие для команд и кнопок)
# ============================================================

def news_text():
    events = get_recent_events(10)
    if not events:
        return with_time(NO_NEWS_TEXT)
    return with_time("📰 Последние новости:\n\n" + "\n\n".join(format_event(e) for e in events))


def top_text():
    # Берём последние 50 событий и выбираем 5 с самым высоким insider_score
    events = sorted(get_recent_events(50), key=lambda e: e["insider_score"], reverse=True)[:5]
    if not events:
        return with_time(NO_NEWS_TEXT)
    return with_time("🕵️ Самые инсайдерские новости:\n\n" + "\n\n".join(format_event(e) for e in events))


def tag_text(command_name):
    """Новости по теме: command_name — btc, eth, sol или defi."""
    tag, header = TAG_COMMANDS[command_name]
    events = get_events_by_tag(tag, limit=5)
    if not events:
        return with_time(f"Новостей с темой {header} пока нет.")
    return with_time(f"{header} — последние новости:\n\n" + "\n\n".join(format_event(e) for e in events))


def sentiment_text(topic):
    """Тональность за 24 часа. topic — btc/eth/sol/defi (или bitcoin/...), пустая строка — все новости."""
    topic = topic.strip().lower()
    if not topic:
        tag, header = None, "всех новостей"
    else:
        # Понимаем и короткое имя команды (btc), и сам тег (bitcoin)
        found = [(t, h) for cmd, (t, h) in TAG_COMMANDS.items() if topic in (cmd, t)]
        if not found:
            return "Неизвестная тема. Используйте: /sentiment btc, eth, sol или defi"
        tag, header = found[0]

    stats = get_sentiment_stats(tag, hours=24)
    total = sum(stats.values())
    if total == 0:
        return "За последние 24 часа новостей не найдено"

    lines = [f"📊 Тональность {header} за 24 часа (новостей: {total}):", ""]
    for key, (emoji, label) in SENTIMENT_LABELS.items():
        lines.append(f"{emoji} {label}: {round(stats[key] * 100 / total)}% ({stats[key]})")

    # Короткий вывод: что преобладает
    if stats["positive"] > stats["negative"]:
        lines += ["", "Настроение: скорее позитивное 📈"]
    elif stats["negative"] > stats["positive"]:
        lines += ["", "Настроение: скорее негативное 📉"]
    else:
        lines += ["", "Настроение: нейтральное ⚖️"]
    return "\n".join(lines)


def format_date(created_at):
    """'2026-10-18 16:08:50' (UTC, как в базе) -> '18.10.2026 19:08' (время этого компьютера)."""
    try:
        moment = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(created_at)


def entity_text(name):
    """Новости про сущность (компанию, человека, проект)."""
    name = name.strip()
    if not name:
        return "Напишите, что искать. Например: /entity Binance"
    events = get_events_by_entity(name, limit=10)
    if not events:
        return f"По запросу {name} ничего не найдено"
    items = [f"📅 {format_date(e['created_at'])}\n{e['title']}\n{e['summary']}" for e in events]
    return f"🔍 Новости про {name} (найдено: {len(events)}):\n\n" + "\n\n".join(items)


# ============================================================
# Персональные темы (/settings)
# ============================================================

def user_topics(user_id):
    """Темы пользователя списком тегов, например ["bitcoin", "defi"]. Пустой список = все новости."""
    prefs = get_user_preferences(user_id)
    return [] if prefs == "all" else [tag for tag in prefs.split(",") if tag]


def topics_title(topics):
    """["bitcoin", "defi"] -> "₿ Bitcoin, 🧪 DeFi"."""
    headers = {tag: header for tag, header in TAG_COMMANDS.values()}
    return ", ".join(headers.get(tag, tag) for tag in topics)


def settings_text(topics):
    current = topics_title(topics) if topics else "все новости"
    return (
        "⚙️ Ваши темы для /briefing и /digest\n\n"
        "Нажмите на тему, чтобы включить или выключить её. Можно выбрать несколько.\n\n"
        f"Сейчас: {current}"
    )


def settings_keyboard(topics):
    """Кнопки тем: ✅ у выбранных. Внизу — сброс на «все новости»."""
    buttons = []
    for command_name in SETTINGS_TOPICS:
        tag, header = TAG_COMMANDS[command_name]
        mark = "✅ " if tag in topics else ""
        buttons.append(InlineKeyboardButton(text=mark + header, callback_data=f"topic:{tag}"))
    reset_mark = "✅ " if not topics else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:2],
        buttons[2:],
        [InlineKeyboardButton(text=reset_mark + "🔄 Сбросить (Все новости)", callback_data="topic:all")],
    ])


def toggle_topic(user_id, tag):
    """Включает/выключает тему у пользователя. tag="all" — сброс. Возвращает новый список тем."""
    topics = user_topics(user_id)
    if tag == "all":
        topics = []
    elif tag in topics:
        topics.remove(tag)
    else:
        topics.append(tag)
    # Храним в постоянном порядке, как в меню
    order = [TAG_COMMANDS[c][0] for c in SETTINGS_TOPICS]
    topics = [t for t in order if t in topics]
    set_user_preferences(user_id, topics or "all")
    return topics


async def personal_ai_text(action, user_id):
    """Брифинг или дайджест с учётом тем пользователя."""
    topics = user_topics(user_id)
    if action == "briefing":
        text = await make_briefing(topics=topics)
    else:
        text = await make_digest(topics=topics)
    if topics:
        text = f"🎯 Ваши темы: {topics_title(topics)} (изменить: /settings)\n\n" + text
    # Время ставим после ответа GPT — в момент, когда брифинг или дайджест готов
    return with_time(text)


# ============================================================
# Команды
# ============================================================

@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(message: Message):
    if not await check_access(message):
        return
    await message.answer(HELP_TEXT, reply_markup=MENU_KEYBOARD)


@dp.message(Command("news"))
async def cmd_news(message: Message):
    if not await check_access(message):
        return
    await send_long(message, news_text())


@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not await check_access(message):
        return
    await send_long(message, top_text())


@dp.message(Command("digest"))
async def cmd_digest(message: Message):
    if not await check_access(message):
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await send_long(message, await personal_ai_text("digest", message.from_user.id))


@dp.message(Command("briefing"))
async def cmd_briefing(message: Message):
    # Без check_access: брифинг пока бесплатный и доступен всем
    logger.info("Брифинг для user_id=%s", message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await send_long(message, await personal_ai_text("briefing", message.from_user.id))


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    # Без check_access: темы влияют на /briefing, а он доступен всем
    topics = user_topics(message.from_user.id)
    await message.answer(settings_text(topics), reply_markup=settings_keyboard(topics))


@dp.message(Command(*TAG_COMMANDS))
async def cmd_by_tag(message: Message, command: CommandObject):
    """Один обработчик для /defi, /btc, /eth, /sol — отличается только тег."""
    if not await check_access(message):
        return
    await send_long(message, tag_text(command.command.lower()))


@dp.message(Command("sentiment"))
async def cmd_sentiment(message: Message, command: CommandObject):
    """/sentiment btc — доля позитивных, негативных и нейтральных новостей по теме за 24 часа."""
    if not await check_access(message):
        return
    await message.answer(sentiment_text(command.args or ""))


@dp.message(Command("entity"))
async def cmd_entity(message: Message, command: CommandObject):
    """/entity Binance — новости, где упоминается эта компания, человек или проект."""
    if not await check_access(message):
        return
    logger.info("Поиск сущности '%s' от user_id=%s", command.args, message.from_user.id)
    await send_long(message, entity_text(command.args or ""))


# ============================================================
# Кнопки (InlineKeyboard под /start и /help)
# ============================================================

@dp.callback_query(F.data)
async def on_button(callback: CallbackQuery):
    """Нажатие кнопки: callback_data вида "news", "tag:btc", "sentiment:eth", "topic:bitcoin"."""
    action, _, arg = callback.data.partition(":")
    user_id = callback.from_user.id

    # Выбор темы в /settings: меняем галочки в том же сообщении, новое не отправляем
    if action == "topic":
        known_tags = [TAG_COMMANDS[c][0] for c in SETTINGS_TOPICS]
        if arg != "all" and arg not in known_tags:
            await callback.answer()
            return
        topics = toggle_topic(user_id, arg)
        await callback.answer("Темы: " + (topics_title(topics) if topics else "все новости"))
        try:
            await callback.message.edit_text(settings_text(topics), reply_markup=settings_keyboard(topics))
        except TelegramBadRequest:
            # Например, «Сбросить» нажали, когда и так «все новости» — сообщение не изменилось
            pass
        return

    # Брифинг и настройки тем доступны всем, как и команды /briefing и /settings
    if action not in ("briefing", "settings") and not is_allowed(user_id):
        logger.warning("Отказано в доступе (кнопка): user_id=%s", callback.from_user.id)
        await callback.answer(f"Доступ закрыт. Ваш Telegram id: {callback.from_user.id}", show_alert=True)
        return

    # Сразу подтверждаем нажатие, иначе на кнопке будут крутиться «часики»
    await callback.answer()
    logger.info("Кнопка %s от user_id=%s", callback.data, callback.from_user.id)
    message = callback.message

    if action == "news":
        text = news_text()
    elif action == "top":
        text = top_text()
    elif action == "tag" and arg in TAG_COMMANDS:
        text = tag_text(arg)
    elif action == "sentiment":
        text = sentiment_text(arg)
    elif action in ("briefing", "digest"):
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        text = await personal_ai_text(action, user_id)
    elif action == "settings":
        topics = user_topics(user_id)
        await message.answer(settings_text(topics), reply_markup=settings_keyboard(topics))
        return
    else:
        logger.warning("Неизвестная кнопка: %s", callback.data)
        return

    await send_long(message, text)


@dp.message(F.text)
async def handle_question(message: Message):
    """Любой обычный текст считаем вопросом к ассистенту."""
    if not await check_access(message):
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await send_long(message, await answer_question(message.text))


# ============================================================
# Запуск
# ============================================================

async def run_bot():
    """Главная функция бота: получает сообщения от Telegram (long polling)."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Подсказки команд в меню Telegram
    await bot.set_my_commands([
        BotCommand(command="news", description="Последние новости"),
        BotCommand(command="top", description="Самые инсайдерские новости"),
        BotCommand(command="digest", description="Дайджест от ИИ"),
        BotCommand(command="briefing", description="Главное за 24 часа"),
        BotCommand(command="btc", description="Новости Bitcoin"),
        BotCommand(command="eth", description="Новости Ethereum"),
        BotCommand(command="sol", description="Новости Solana"),
        BotCommand(command="defi", description="Новости DeFi"),
        BotCommand(command="sentiment", description="Тональность за 24 часа: /sentiment btc"),
        BotCommand(command="entity", description="Поиск по имени: /entity Binance"),
        BotCommand(command="settings", description="Мои темы для брифинга и дайджеста"),
        BotCommand(command="help", description="Справка"),
    ])

    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS не задан — ботом может пользоваться кто угодно")

    me = await bot.get_me()
    logger.info("Бот @%s запущен", me.username)
    # handle_signals=False: Ctrl+C обрабатывает main.py, а не aiogram
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    # Запуск только бота (без сборщика и фильтра) — для проверки
    init_db()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен (Ctrl+C)")
