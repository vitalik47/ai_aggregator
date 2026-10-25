"""
Telegram-бот (aiogram 3).

Команды:
/start, /help — справка
/news         — последние события из базы (без GPT, бесплатно)
/top          — 5 событий с самым высоким insider_score (DeFi-новости получают бонус DEFI_PRIORITY_BONUS)
/digest       — дайджест от Агента №3
/briefing     — главное за 24 часа (доступна всем, даже при ALLOWED_USER_IDS)
/defi, /btc, /eth, /sol — 5 последних новостей по теме (по тегам из category_tags)
/sentiment btc — тональность новостей по теме за 24 часа (без темы — по всем новостям)
/entity AAVE  — новости про актив; только тикеры из ALLOWED_ENTITIES (поиск по колонке entities)
/research вопрос — подробный разбор от Агента №4 (agent_4_researcher.py) по новостям за 7 дней,
                   вопрос не длиннее MAX_RESEARCH_LENGTH символов
/theory       — «Теория DeFi» от Агента №5: термины из последнего ответа (users.last_context) или своя тема (FSM)
/settings     — выбор тем (таблица user_preferences); /briefing и /digest учитывают их
/feedback     — опрос из 3 шагов (FSM), ответы сохраняются в таблицу feedback
/stats        — статистика (пользователи, запросы к ИИ) — только для ADMIN_ID из .env
Любой текст   — вопрос Агенту №3

Запросы к ИИ (вопросы, /briefing, /digest, /research и кнопки брифинга/дайджеста) ограничены: DAILY_AI_LIMIT в день
на пользователя (таблица user_limits).

В /start и /help под справкой есть кнопки (InlineKeyboard) — они делают то же, что команды.

Здесь используется TELEGRAM_BOT_TOKEN (бот), а не личный аккаунт, как в сборщике.
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from agent_3_assistant import ERROR_TEXT, answer_question, make_briefing, make_digest
from agent_4_researcher import research_topic
from agent_5_teacher import explain_topic, read_chapters, suggest_topics_from_context
from config import ADMIN_ID, ALLOWED_USER_IDS, DAILY_AI_LIMIT, TELEGRAM_BOT_TOKEN
from database import (
    check_and_update_limit,
    get_ai_requests_today,
    get_events_by_entity,
    get_events_by_tag,
    get_last_context,
    get_new_users_today,
    get_recent_events,
    get_sentiment_stats,
    get_total_users,
    get_user_preferences,
    init_db,
    register_user,
    save_feedback,
    save_last_context,
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
# Теги, которые считаем DeFi. Сейчас Агент №2 ставит только "defi", остальные — на будущее
DEFI_TAGS = {"defi", "dex", "yield", "liquidity"}
# Сколько баллов добавлять к insider_score DeFi-новостей при выборе /top
DEFI_PRIORITY_BONUS = 1
# /entity: тикер -> имя, под которым Агент №2 записывает актив в колонку entities
ENTITY_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB", "XRP": "XRP",
    "ADA": "Cardano", "AVAX": "Avalanche", "DOT": "Polkadot", "LINK": "Chainlink", "MATIC": "Polygon",
    "UNI": "Uniswap", "AAVE": "Aave", "LDO": "Lido", "ARB": "Arbitrum", "OP": "Optimism",
}
ALLOWED_ENTITIES = list(ENTITY_NAMES)
ENTITY_NOT_ALLOWED_TEXT = (
    "Пока я не ищу по этому активу. Доступные тикеры: "
    + ", ".join(ALLOWED_ENTITIES)
    + ". Пример: /entity AAVE"
)
# /research: вопросы длиннее этого не отправляем в ИИ
MAX_RESEARCH_LENGTH = 300
RESEARCH_TOO_LONG_TEXT = (
    f"Ваш запрос слишком длинный. Пожалуйста, сократите его до {MAX_RESEARCH_LENGTH} символов, "
    "чтобы ИИ мог дать точный ответ."
)

# Список команд — общий для /start и /help
COMMANDS_TEXT = (
    "🧠 /briefing — Главные события за 24 часа (общий срез рынка)\n"
    "🎯 /top — Топ-5 новостей, которые сильнее всего влияют на рынок (с акцентом на DeFi)\n"
    f"🔍 /entity — Инфо по конкретной монете. Доступно {len(ALLOWED_ENTITIES)} активов (напр. /entity AAVE)\n"
    "🕵️‍♂️ /research — Задай любой вопрос по крипте (напр. /research Что с Ethereum?)\n"
    "📚 /theory — Теория DeFi: объясню сложные термины из последней новости простыми словами\n"
    "📰 /news — Последние новости без ИИ-обработки\n"
    "📊 /sentiment — Настроение рынка по конкретной монете\n"
    "⚙️ /settings — Настроить темы (DeFi, NFT и т.д.)\n\n"
    "❓ Или просто напишите вопрос, например: «Что нового с Bitcoin?»\n"
    f"(запросов к ИИ — до {DAILY_AI_LIMIT} в день)"
)

# Приветствие на /start: коротко про бота + список команд
START_TEXT = (
    "Привет! Я — Crypto Brief 42, твой ИИ-аналитик.\n"
    "Я не просто собираю новости, я их читаю, анализирую и делаю выводы за тебя.\n\n"
    f"{COMMANDS_TEXT}\n\n"
    "👇 Или выбери ИИ-функцию ниже:"
)

HELP_TEXT = (
    "📖 Справка Crypto Brief 42:\n\n"
    f"{COMMANDS_TEXT}\n\n"
    "👇 Или нажмите кнопку:"
)
# Этот блок видит в справке только администратор (ADMIN_ID)
ADMIN_HELP_TEXT = "🔐 Для администратора\n/stats — статистика бота\n\n"
NO_RIGHTS_TEXT = "У вас нет прав"

# Кнопки под /start и /help. callback_data — что именно нажали (обрабатывается в on_button).
# Тексты кнопок можно менять свободно, а callback_data — нет: по ним работает on_button.
# Ряды идут в том же порядке, что и блоки справки
MENU_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    # 1. Главное от ИИ + обычная лента
    [
        InlineKeyboardButton(text="🧠 ИИ-дайджест", callback_data="briefing"),
        InlineKeyboardButton(text="🎯 Топ-тренды (DeFi)", callback_data="top"),
    ],
    [
        InlineKeyboardButton(text="📰 Просто новости", callback_data="news"),
    ],
    # 2. По темам
    [
        InlineKeyboardButton(text="₿ BTC", callback_data="tag:btc"),
        InlineKeyboardButton(text="💎 ETH", callback_data="tag:eth"),
        InlineKeyboardButton(text="☀️ SOL", callback_data="tag:sol"),
        InlineKeyboardButton(text="🧪 DeFi", callback_data="tag:defi"),
    ],
    # 3. ИИ-аналитика
    [
        InlineKeyboardButton(text="📊 ИИ: Настроение рынка", callback_data="sentiment:"),
        InlineKeyboardButton(text="🔍 Инфо по монете", callback_data="entity"),
    ],
    # 4. Прочее
    [
        InlineKeyboardButton(text="📝 ИИ: Краткая сводка", callback_data="digest"),
        InlineKeyboardButton(text="🔬 ИИ: Исследование", callback_data="research"),
    ],
    [
        InlineKeyboardButton(text="📚 Теория DeFi", callback_data="theory"),
    ],
    [
        InlineKeyboardButton(text="💬 Фидбек", callback_data="feedback"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
    ],
])
# Меню администратора: то же самое + кнопка статистики
ADMIN_MENU_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=MENU_KEYBOARD.inline_keyboard + [
    [InlineKeyboardButton(text="📈 Статистика (админ)", callback_data="stats")],
])

# Кнопки под общей тональностью: та же статистика по отдельной теме
SENTIMENT_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📊 BTC", callback_data="sentiment:btc"),
    InlineKeyboardButton(text="📊 ETH", callback_data="sentiment:eth"),
    InlineKeyboardButton(text="📊 SOL", callback_data="sentiment:sol"),
    InlineKeyboardButton(text="📊 DeFi", callback_data="sentiment:defi"),
]])

ENTITY_HINT_TEXT = (
    "🔍 Новости по конкретной монете.\n\n"
    "Напишите команду и тикер, например: /entity AAVE\n\n"
    "Доступные тикеры: " + ", ".join(ALLOWED_ENTITIES)
)

RESEARCH_HINT_TEXT = (
    "🔬 ИИ-исследование: подробный разбор от ИИ по новостям за последние 7 дней.\n\n"
    "Напишите команду и вопрос, например:\n"
    "/research Как ETF влияет на биткоин\n"
    "/research Что происходит с Solana"
)

NO_NEWS_TEXT = "Новостей пока нет. Подождите, пока агенты их соберут."
LIMIT_TEXT = (
    f"⚠️ Вы достигли дневного лимита бесплатных запросов к ИИ ({DAILY_AI_LIMIT}). "
    "Лимит обновится завтра. В будущем можно будет оформить подписку для снятия ограничений."
)
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

def is_admin(user_id):
    return ADMIN_ID is not None and user_id == ADMIN_ID


def is_allowed(user_id):
    """Если задан ALLOWED_USER_IDS — пускаем только этих пользователей (и администратора)."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS or is_admin(user_id)


def help_text(user_id):
    """Справка: администратору дополнительно показываем блок с /stats."""
    if not is_admin(user_id):
        return HELP_TEXT
    footer = "👇 Или нажмите кнопку:"
    return HELP_TEXT.replace(footer, ADMIN_HELP_TEXT + footer)


def menu_keyboard(user_id):
    return ADMIN_MENU_KEYBOARD if is_admin(user_id) else MENU_KEYBOARD


def stats_text():
    return (
        "📊 Статистика бота:\n"
        f"- Всего пользователей: {get_total_users()}\n"
        f"- Новых сегодня: {get_new_users_today()}\n"
        f"- Запросов к ИИ сегодня: {get_ai_requests_today()}"
    )


@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def track_user(handler, event, data):
    """Перед любой командой, сообщением или кнопкой отмечаем пользователя в таблице users (для /stats)."""
    if event.from_user:
        try:
            register_user(event.from_user.id)
        except Exception:
            # Ошибка учёта не должна мешать ответу пользователю
            logger.exception("Не удалось записать пользователя %s", event.from_user.id)
    return await handler(event, data)


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


def ai_limit_ok(user_id):
    """Засчитывает запрос к ИИ. False — дневной лимит исчерпан, к OpenAI обращаться нельзя."""
    return check_and_update_limit(user_id, DAILY_AI_LIMIT)


async def send_long(message: Message, text, reply_markup=None):
    """Отправляет текст любой длины несколькими сообщениями. Кнопки — под последним из них."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=reply_markup if i == len(parts) - 1 else None)


def remember_context(user_id, text):
    """Запоминает отправленный текст для /theory (users.last_context)."""
    try:
        save_last_context(user_id, text)
    except Exception:
        # Ошибка базы не должна мешать ответу пользователю
        logger.exception("Не удалось сохранить last_context для user_id=%s", user_id)


# ============================================================
# Тексты ответов (общие для команд и кнопок)
# ============================================================

def news_text():
    events = get_recent_events(10)
    if not events:
        return with_time(NO_NEWS_TEXT)
    return with_time("📰 Последние новости:\n\n" + "\n\n".join(format_event(e) for e in events))


def is_defi(event):
    tags = (event.get("category_tags") or "").split(",")
    return any(tag.strip() in DEFI_TAGS for tag in tags)


def top_score(event):
    """Оценка для /top: insider_score, у DeFi-новостей — с бонусом."""
    return event["insider_score"] + (DEFI_PRIORITY_BONUS if is_defi(event) else 0)


def top_text():
    # Берём последние 50 событий и выбираем 5 с самой высокой оценкой (DeFi в приоритете)
    events = sorted(get_recent_events(50), key=top_score, reverse=True)[:5]
    if not events:
        return with_time(NO_NEWS_TEXT)
    return with_time(
        "🎯 Топ-5 новостей, которые сильнее всего влияют на рынок (акцент на DeFi):\n\n"
        + "\n\n".join(format_event(e) for e in events)
    )


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


def entity_text(ticker):
    """Новости про актив. ticker — один из ALLOWED_ENTITIES (BTC, AAVE, ...)."""
    ticker = ticker.strip().upper()
    if ticker not in ALLOWED_ENTITIES:
        return ENTITY_NOT_ALLOWED_TEXT
    name = ENTITY_NAMES[ticker]
    # В entities обычно полное имя ("Bitcoin"), но иногда и тикер ("XRP", "BTC")
    events = get_events_by_entity(name, limit=10)
    if not events and name != ticker:
        events = get_events_by_entity(ticker, limit=10)
    title = ticker if name == ticker else f"{ticker} ({name})"
    if not events:
        return f"По активу {title} новостей пока нет"
    items = [f"📅 {format_date(e['created_at'])}\n{e['title']}\n{e['summary']}" for e in events]
    return f"🔍 Новости про {title} (найдено: {len(events)}):\n\n" + "\n\n".join(items)


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
async def cmd_start(message: Message):
    """Приветствие ИИ-аналитика и меню кнопок."""
    if not await check_access(message):
        return
    await message.answer(START_TEXT, reply_markup=menu_keyboard(message.from_user.id))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Полный список команд и то же меню кнопок."""
    if not await check_access(message):
        return
    user_id = message.from_user.id
    await message.answer(help_text(user_id), reply_markup=menu_keyboard(user_id))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика бота — только для администратора (ADMIN_ID в .env)."""
    if not is_admin(message.from_user.id):
        logger.warning("Попытка открыть /stats: user_id=%s", message.from_user.id)
        await message.answer(NO_RIGHTS_TEXT)
        return
    await message.answer(stats_text())


@dp.message(Command("news"))
async def cmd_news(message: Message):
    if not await check_access(message):
        return
    text = news_text()
    remember_context(message.from_user.id, text)
    await send_long(message, text)


@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not await check_access(message):
        return
    await send_long(message, top_text())


@dp.message(Command("digest"))
async def cmd_digest(message: Message):
    if not await check_access(message):
        return
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await send_long(message, await personal_ai_text("digest", message.from_user.id))


@dp.message(Command("briefing"))
async def cmd_briefing(message: Message):
    # Без check_access: брифинг пока бесплатный и доступен всем
    logger.info("Брифинг для user_id=%s", message.from_user.id)
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    text = await personal_ai_text("briefing", message.from_user.id)
    remember_context(message.from_user.id, text)
    await send_long(message, text)


@dp.message(Command("research"))
async def cmd_research(message: Message, command: CommandObject):
    """/research вопрос — развёрнутый ответ Агента №4 по новостям за 7 дней."""
    if not await check_access(message):
        return
    question = (command.args or "").strip()
    if not question:
        # Без вопроса только подсказка — лимит не тратим
        await message.answer(RESEARCH_HINT_TEXT)
        return
    if len(question) > MAX_RESEARCH_LENGTH:
        # Слишком длинный вопрос тоже не тратит лимит
        await message.answer(RESEARCH_TOO_LONG_TEXT)
        return
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
        return
    logger.info("Исследование для user_id=%s: %s", message.from_user.id, question)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    answer = await research_topic(question)
    remember_context(message.from_user.id, answer)
    await send_long(message, with_time(f"🔬 Исследование: {question}\n\n{answer}"))


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
    """/entity AAVE — новости про актив из ALLOWED_ENTITIES."""
    if not await check_access(message):
        return
    logger.info("Поиск сущности '%s' от user_id=%s", command.args, message.from_user.id)
    await send_long(message, entity_text(command.args or ""))


# ============================================================
# Обратная связь (/feedback): опрос из 3 шагов через FSM
# ============================================================

class FeedbackForm(StatesGroup):
    """Шаги опроса. Состояние хранится в памяти бота отдельно для каждого пользователя."""
    interested = State()   # Шаг 1: интересен ли формат
    will_pay = State()     # Шаг 2: готов ли платить
    suggestions = State()  # Шаг 3: что добавить (текст)


FEEDBACK_INTERESTED_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="👍 Да", callback_data="fb:interested:yes"),
    InlineKeyboardButton(text="👎 Нет", callback_data="fb:interested:no"),
]])
FEEDBACK_PAY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="💰 Да", callback_data="fb:pay:yes"),
    InlineKeyboardButton(text="🆓 Только бесплатно", callback_data="fb:pay:free"),
]])
# Ответ в базе -> подпись для пользователя
FEEDBACK_ANSWER_LABELS = {
    "interested": {"yes": "👍 Да", "no": "👎 Нет"},
    "pay": {"yes": "💰 Да", "free": "🆓 Только бесплатно"},
}


async def start_feedback(message: Message, state: FSMContext):
    """Начинает опрос заново (общая часть для команды /feedback и кнопки в меню)."""
    await state.clear()
    await state.set_state(FeedbackForm.interested)
    await message.answer(
        "💬 Обратная связь — 3 коротких вопроса.\n\nШаг 1/3. Интересен ли вам формат бота?",
        reply_markup=FEEDBACK_INTERESTED_KEYBOARD,
    )


async def mark_answer(callback: CallbackQuery, answer_text):
    """Убирает кнопки у вопроса и дописывает выбранный ответ — чтобы нельзя было нажать дважды."""
    await callback.answer()
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\nВаш ответ: {answer_text}")
    except TelegramBadRequest:
        pass


@dp.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    # Без check_access: отзыв может оставить любой
    await start_feedback(message, state)


@dp.callback_query(FeedbackForm.interested, F.data.in_({"fb:interested:yes", "fb:interested:no"}))
async def feedback_interested(callback: CallbackQuery, state: FSMContext):
    """Шаг 1 -> 2: сохраняем ответ ("yes"/"no") и спрашиваем про оплату."""
    answer = callback.data.split(":")[-1]
    await mark_answer(callback, FEEDBACK_ANSWER_LABELS["interested"][answer])
    await state.update_data(is_interested=answer)
    await state.set_state(FeedbackForm.will_pay)
    await callback.message.answer(
        "Шаг 2/3. Готовы ли вы платить 5$ в месяц за подписку?", reply_markup=FEEDBACK_PAY_KEYBOARD
    )


@dp.callback_query(FeedbackForm.will_pay, F.data.in_({"fb:pay:yes", "fb:pay:free"}))
async def feedback_will_pay(callback: CallbackQuery, state: FSMContext):
    """Шаг 2 -> 3: сохраняем ответ ("yes"/"free") и просим написать пожелания."""
    answer = callback.data.split(":")[-1]
    await mark_answer(callback, FEEDBACK_ANSWER_LABELS["pay"][answer])
    await state.update_data(will_pay=answer)
    await state.set_state(FeedbackForm.suggestions)
    await callback.message.answer("Шаг 3/3. Что бы вы хотели добавить или изменить? Напишите одним сообщением.")


@dp.callback_query(F.data.startswith("fb:"))
async def feedback_old_button(callback: CallbackQuery):
    """Нажали кнопку из старого или уже пройденного опроса."""
    await callback.answer("Этот опрос уже неактивен. Начните заново: /feedback", show_alert=True)


# Команды (/news и др.) во время шага 3 обрабатываются как обычно — их обработчики стоят выше.
# Неизвестные команды тоже не считаем ответом
@dp.message(FeedbackForm.suggestions, F.text, ~F.text.startswith("/"))
async def feedback_suggestions(message: Message, state: FSMContext):
    data = await state.get_data()
    save_feedback(message.from_user.id, data["is_interested"], data["will_pay"], message.text.strip())
    await state.clear()
    await message.answer("Спасибо! Ваше мнение очень важно для нас. 🙏")


# ============================================================
# Теория DeFi (/theory): термины из последнего ответа или своя тема (FSM)
# ============================================================

class TheoryStates(StatesGroup):
    """Шаги «Теории DeFi». Предложенные термины и последний разбор хранятся в данных FSM."""
    waiting_for_suggestion_choice = State()  # Предложили термины — ждём кнопку (или свою тему текстом)
    waiting_for_topic = State()              # Ждём тему, написанную вручную


THEORY_ASK_TOPIC_TEXT = "Хорошо, напишите тему или вопрос по DeFi, и я разберу её."
THEORY_FIRST_TOPIC_TEXT = (
    "📚 Теория DeFi: объясню любую тему простыми словами.\n\n"
    "Напишите тему или вопрос по DeFi, например: «Что такое impermanent loss?»"
)
THEORY_OLD_BUTTON_TEXT = "Этот выбор уже неактивен. Начните заново: /theory"

# Кнопки под объяснением темы
THEORY_ANSWER_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📖 Читаем", callback_data="theory_read"),
    InlineKeyboardButton(text="🔄 Другое", callback_data="other_topic"),
]])
# Кнопка под главами
THEORY_CHAPTERS_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🔄 Другая тема", callback_data="other_topic"),
]])


def suggestions_keyboard(terms):
    """По кнопке на каждый термин, ниже — «Изучаем» (все термины сразу) и «Другое»."""
    rows = [
        [InlineKeyboardButton(text=f"{i}. {term}", callback_data=f"theory_term:{i - 1}")]
        for i, term in enumerate(terms, start=1)
    ]
    rows.append([
        InlineKeyboardButton(text="✅ Изучаем", callback_data="read_suggested"),
        InlineKeyboardButton(text="🔄 Другое", callback_data="other_topic"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_allowed(callback: CallbackQuery):
    """Проверка доступа для кнопок: при отказе показывает всплывающее сообщение."""
    if is_allowed(callback.from_user.id):
        return True
    logger.warning("Отказано в доступе (кнопка): user_id=%s", callback.from_user.id)
    await callback.answer(f"Доступ закрыт. Ваш Telegram id: {callback.from_user.id}", show_alert=True)
    return False


async def remove_buttons(callback: CallbackQuery):
    """Убирает кнопки у сообщения, чтобы их нельзя было нажать ещё раз и потратить лимит дважды."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def ask_theory_topic(message: Message, state: FSMContext, text=THEORY_ASK_TOPIC_TEXT):
    await state.set_state(TheoryStates.waiting_for_topic)
    await message.answer(text)


async def start_theory(message: Message, user_id, state: FSMContext):
    """Начало «Теории DeFi» (команда /theory и кнопка в меню): термины из last_context или ручной ввод."""
    context = get_last_context(user_id)
    data = await state.get_data()
    # Заодно прерывает опрос /feedback, если он был начат
    await state.clear()
    if not context:
        await ask_theory_topic(message, state, THEORY_FIRST_TOPIC_TEXT)
        return

    # Для этого же текста термины уже подбирали — не тратим повторный запрос к GPT
    if data.get("theory_context") == context and data.get("theory_terms"):
        terms = data["theory_terms"]
    else:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        terms = await suggest_topics_from_context(context)
    if not terms:
        # GPT не нашёл терминов или не ответил — просим тему вручную
        await ask_theory_topic(message, state, THEORY_FIRST_TOPIC_TEXT)
        return

    await state.update_data(theory_context=context, theory_terms=terms)
    await state.set_state(TheoryStates.waiting_for_suggestion_choice)
    lines = "\n".join(f"{i}. {term}" for i, term in enumerate(terms, start=1))
    await message.answer(
        "🧠 Я проанализировал последнюю новость/ответ. Вот термины, которые могут быть вам интересны:\n"
        f"{lines}\n\nИзучаем эти термины или хотите задать свою тему?",
        reply_markup=suggestions_keyboard(terms),
    )


async def send_theory(message: Message, user_id, state: FSMContext, topic):
    """Объяснение темы (3-4 предложения + план из 3 глав) с кнопками «Читаем» / «Другое»."""
    if not ai_limit_ok(user_id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    text = await explain_topic(topic)
    # Выходим из шагов FSM, но данные оставляем: «Читаем» берёт из них тему и план
    await state.set_state(None)
    if text == ERROR_TEXT:
        await message.answer(text)
        return
    await state.update_data(theory_topic=topic, theory_plan=text)
    await send_long(message, with_time(f"📚 Теория DeFi: {topic}\n\n{text}"), reply_markup=THEORY_ANSWER_KEYBOARD)


@dp.message(Command("theory"))
async def cmd_theory(message: Message, state: FSMContext):
    if not await check_access(message):
        return
    await start_theory(message, message.from_user.id, state)


@dp.callback_query(F.data == "theory")
async def theory_button(callback: CallbackQuery, state: FSMContext):
    if not await callback_allowed(callback):
        return
    await callback.answer()
    await start_theory(callback.message, callback.from_user.id, state)


@dp.callback_query(TheoryStates.waiting_for_suggestion_choice, F.data == "read_suggested")
async def theory_read_suggested(callback: CallbackQuery, state: FSMContext):
    """«✅ Изучаем»: один разбор сразу по всем предложенным терминам."""
    if not await callback_allowed(callback):
        return
    await callback.answer()
    await remove_buttons(callback)
    terms = (await state.get_data()).get("theory_terms") or []
    await send_theory(callback.message, callback.from_user.id, state, ", ".join(terms))


@dp.callback_query(TheoryStates.waiting_for_suggestion_choice, F.data.startswith("theory_term:"))
async def theory_pick_term(callback: CallbackQuery, state: FSMContext):
    """Нажали на один термин: theory_term:0 — первый в списке."""
    if not await callback_allowed(callback):
        return
    terms = (await state.get_data()).get("theory_terms") or []
    index = callback.data.split(":")[-1]
    if not index.isdigit() or int(index) >= len(terms):
        await callback.answer(THEORY_OLD_BUTTON_TEXT, show_alert=True)
        return
    await callback.answer()
    await remove_buttons(callback)
    await send_theory(callback.message, callback.from_user.id, state, terms[int(index)])


@dp.callback_query(F.data.in_({"read_suggested"}) | F.data.startswith("theory_term:"))
async def theory_old_suggestion(callback: CallbackQuery):
    """Нажали термин из старого списка (уже выбрали другой вариант или бот перезапускался)."""
    await callback.answer(THEORY_OLD_BUTTON_TEXT, show_alert=True)


@dp.callback_query(F.data == "theory_read")
async def theory_read(callback: CallbackQuery, state: FSMContext):
    """«📖 Читаем»: три главы по плану из последнего объяснения."""
    if not await callback_allowed(callback):
        return
    data = await state.get_data()
    topic, plan = data.get("theory_topic"), data.get("theory_plan")
    if not topic or not plan:
        await callback.answer(THEORY_OLD_BUTTON_TEXT, show_alert=True)
        return
    await callback.answer()
    user_id = callback.from_user.id
    message = callback.message
    if not ai_limit_ok(user_id):
        await message.answer(LIMIT_TEXT)
        return
    await remove_buttons(callback)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    text = await read_chapters(topic, plan)
    if text == ERROR_TEXT:
        await message.answer(text)
        return
    await send_long(message, text, reply_markup=THEORY_CHAPTERS_KEYBOARD)


@dp.callback_query(F.data == "other_topic")
async def theory_other_topic(callback: CallbackQuery, state: FSMContext):
    """«🔄 Другое»: ждём тему, написанную вручную."""
    if not await callback_allowed(callback):
        return
    await callback.answer()
    await remove_buttons(callback)
    await ask_theory_topic(callback.message, state)


# Любой из двух шагов: свою тему можно написать и вместо выбора кнопкой.
# Команды во время шагов работают как обычно — их обработчики стоят выше
@dp.message(
    StateFilter(TheoryStates.waiting_for_topic, TheoryStates.waiting_for_suggestion_choice),
    F.text,
    ~F.text.startswith("/"),
)
async def theory_manual_topic(message: Message, state: FSMContext):
    if not await check_access(message):
        return
    topic = message.text.strip()
    if len(topic) > MAX_RESEARCH_LENGTH:
        await message.answer(RESEARCH_TOO_LONG_TEXT)
        return
    await send_theory(message, message.from_user.id, state, topic)


# ============================================================
# Кнопки (InlineKeyboard под /start и /help)
# ============================================================

@dp.callback_query(F.data)
async def on_button(callback: CallbackQuery, state: FSMContext):
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

    # Статистика — только администратору
    if action == "stats":
        if not is_admin(user_id):
            await callback.answer(NO_RIGHTS_TEXT, show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(stats_text())
        return

    # Брифинг, настройки тем и отзыв доступны всем, как и команды /briefing, /settings и /feedback
    if action not in ("briefing", "settings", "feedback") and not is_allowed(user_id):
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
        if not arg:
            # Общая тональность + кнопки, чтобы посмотреть её по каждой теме
            await message.answer(sentiment_text(""), reply_markup=SENTIMENT_KEYBOARD)
            return
        text = sentiment_text(arg)
    elif action == "entity":
        # Имя для поиска кнопкой не передать — подсказываем, как написать команду
        text = ENTITY_HINT_TEXT
    elif action == "research":
        # Вопрос кнопкой не передать — подсказываем, как написать команду
        text = RESEARCH_HINT_TEXT
    elif action == "feedback":
        await start_feedback(message, state)
        return
    elif action == "help":
        await message.answer(help_text(user_id), reply_markup=menu_keyboard(user_id))
        return
    elif action in ("briefing", "digest"):
        if not ai_limit_ok(user_id):
            await message.answer(LIMIT_TEXT)
            return
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        text = await personal_ai_text(action, user_id)
    elif action == "settings":
        topics = user_topics(user_id)
        await message.answer(settings_text(topics), reply_markup=settings_keyboard(topics))
        return
    else:
        logger.warning("Неизвестная кнопка: %s", callback.data)
        return

    # Как и у команд /news и /briefing — запоминаем текст для /theory
    if action in ("news", "briefing"):
        remember_context(user_id, text)
    await send_long(message, text)


@dp.message(F.text)
async def handle_question(message: Message):
    """Любой обычный текст считаем вопросом к ассистенту."""
    if not await check_access(message):
        return
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
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
    commands = [
        # Тот же порядок, что в справке /help
        BotCommand(command="news", description="📰 Просто новости"),
        BotCommand(command="top", description="🎯 Топ-тренды (DeFi)"),
        BotCommand(command="briefing", description="🧠 ИИ-дайджест за 24 часа"),
        BotCommand(command="btc", description="₿ Новости Bitcoin"),
        BotCommand(command="eth", description="💎 Новости Ethereum"),
        BotCommand(command="sol", description="☀️ Новости Solana"),
        BotCommand(command="defi", description="🧪 Новости DeFi"),
        BotCommand(command="sentiment", description="📊 ИИ: Настроение рынка — /sentiment btc"),
        BotCommand(command="entity", description="🔍 Инфо по монете — /entity AAVE"),
        BotCommand(command="digest", description="📝 ИИ: Краткая сводка"),
        BotCommand(command="research", description="🔬 ИИ: Исследование — /research вопрос"),
        BotCommand(command="theory", description="📚 Теория DeFi"),
        BotCommand(command="settings", description="⚙️ Настройки тем"),
        BotCommand(command="feedback", description="💬 Фидбек"),
        BotCommand(command="help", description="❓ Справка"),
    ]
    await bot.set_my_commands(commands)

    # Администратору в его чате с ботом дополнительно показываем /stats
    if ADMIN_ID:
        try:
            await bot.set_my_commands(
                commands + [BotCommand(command="stats", description="📈 Статистика бота (админ)")],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
        except TelegramBadRequest:
            # Например, администратор ещё ни разу не писал боту
            logger.warning("Не удалось показать /stats в меню администратора (ADMIN_ID=%s)", ADMIN_ID)

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
