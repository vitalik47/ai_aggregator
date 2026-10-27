"""
Telegram-бот (aiogram 3).

Команды:
/start, /help — справка
/news         — последние события из базы (без GPT; доступна и после пробного периода)
/top          — 5 событий с самой высокой важностью insider_score (DeFi-новости получают бонус DEFI_PRIORITY_BONUS)
/briefing     — главное за 24 часа от Агента №3 (доступна всем, даже при ALLOWED_USER_IDS)
/defi, /btc, /eth, /sol — 5 последних новостей по теме (по тегам из category_tags)
/sentiment btc — тональность новостей по теме за 24 часа (без темы — по всем новостям)
/entity AAVE  — новости про актив; только тикеры из ALLOWED_ENTITIES (поиск по колонке entities)
/research вопрос — подробный разбор от Агента №4 (agent_4_researcher.py) по новостям за 7 дней,
                   вопрос не длиннее MAX_RESEARCH_LENGTH символов
/theory       — «Учебная справка по DeFi» от Агента №5: 2-3 предложения о термине из /theory <термин>
                или из последнего ответа (users.last_context)
                + ссылка на сайт с материалами (LANDING_URL)
/settings     — выбор тем (таблица user_preferences); /briefing учитывает их
/feedback     — опрос из 3 шагов (FSM), ответы сохраняются в таблицу feedback.
                Кнопка «Отменить» на каждом шаге; любая команда или кнопка меню тоже отменяет опрос
/stats        — статистика (пользователи, запросы к ИИ) — только для ADMIN_ID из .env
Любой текст   — вопрос Агенту №3 (текст с «/» — неизвестная команда, в ИИ не отправляется)

Пробный период: новый пользователь получает TRIAL_DAYS дней полного доступа (users.trial_end_date).
После него /top, /briefing, /research, /theory и вопросы к ИИ закрыты (check_access),
а бот просит оставить отзыв (/feedback). Администратору доступно всё.

Запросы к ИИ (вопросы, /briefing, /research, /theory и кнопки брифинга и справки) ограничены: DAILY_AI_LIMIT в день
на пользователя (таблица user_limits).

В /start и /help под справкой есть кнопки (InlineKeyboard) — они делают то же, что команды.

Здесь используется TELEGRAM_BOT_TOKEN (бот), а не личный аккаунт, как в сборщике.
"""

import asyncio
import html
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from agent_3_assistant import ERROR_TEXT, answer_question, make_briefing
from agent_4_researcher import research_topic
from agent_5_teacher import explain_term
from config import (
    ADMIN_ID,
    ALLOWED_USER_IDS,
    DAILY_AI_LIMIT,
    LANDING_URL,
    TELEGRAM_BOT_TOKEN,
    TRIAL_DAYS,
)
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
    get_trial_end_date,
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
# События с такой важностью и выше помечаем 🔥 (8-10 — крупные взломы, одобрения ETF, регуляторы)
HOT_INSIDER_SCORE = 8
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)
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

# Список команд по блокам — общий для /start и /help
COMMANDS_TEXT = (
    "📰 Новости\n"
    "/news — Последние новости (все подряд)\n"
    "/top — Только самые важные новости (с высоким рейтингом)\n\n"
    "🧠 ИИ-Аналитика\n"
    "/briefing — Главное за 24 часа: ключевые события и тренд\n"
    "/research — Подробный ответ на вопрос по крипте (напр. /research Что с Ethereum?)\n"
    "Или просто напишите вопрос, например: «Что нового с Bitcoin?»\n"
    f"(запросов к ИИ — до {DAILY_AI_LIMIT} в день)\n\n"
    "📖 Обучение\n"
    "/theory — Учебная справка по DeFi: коротко объясню термин (напр. /theory AMM)\n"
    f"Учебник и симулятор DeFi Compass — скоро на сайте: {LANDING_URL}\n\n"
    "⚙️ Настройки\n"
    "/settings — Темы для брифинга (BTC, ETH, SOL, DeFi)\n"
    "/feedback — Оставить отзыв о боте\n"
    "/help — Эта справка\n\n"
    "⏳ Доступ\n"
    f"Всем новым пользователям дается {TRIAL_DAYS} дней полного доступа ко всем функциям."
)

# Приветствие на /start. Между приветствием и командами start_text() вставляет срок доступа —
# так его видно сразу, а не в конце длинного списка
START_GREETING = (
    "Привет! Я — Crypto Brief 42, твой ИИ-аналитик.\n"
    "Я не просто собираю новости, я их читаю, анализирую и делаю выводы за тебя."
)
START_COMMANDS = f"{COMMANDS_TEXT}\n\n👇 Или выбери ИИ-функцию ниже:"

# Справка: срок доступа help_text() вставляет сразу после заголовка
HELP_TITLE = "📖 Справка Crypto Brief 42:"
HELP_TEXT = f"{COMMANDS_TEXT}\n\n👇 Или нажмите кнопку:"
# Этот блок видит в справке только администратор (ADMIN_ID)
ADMIN_HELP_TEXT = (
    "🔐 Для администратора\n"
    "/stats — статистика бота\n\n"
)

# Пробный период закончился: идёт тестирование, поэтому просим отзыв
TRIAL_ENDED_TEXT = (
    "🔒 Пробный период закончился. Спасибо, что тестировали бота! "
    "Если вам был полезен этот инструмент, пожалуйста, оставьте отзыв через команду /feedback. "
    "Это поможет нам сделать его лучше."
)
NO_RIGHTS_TEXT = "У вас нет прав"

# Кнопки под /start и /help. callback_data — что именно нажали (обрабатывается в on_button).
# Тексты кнопок можно менять свободно, а callback_data — нет: по ним работает on_button.
# Ряды идут в том же порядке, что и блоки справки
MENU_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    # 1. Главное от ИИ + обычная лента
    [
        InlineKeyboardButton(text="🧠 Главное за 24 часа", callback_data="briefing"),
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
        InlineKeyboardButton(text="🔬 ИИ: Исследование", callback_data="research"),
    ],
    [
        InlineKeyboardButton(text="📖 Учебная справка по DeFi", callback_data="theory"),
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
LIMIT_TEXT = f"⚠️ Вы достигли дневного лимита запросов к ИИ ({DAILY_AI_LIMIT}). Лимит обновится завтра."
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


def parse_utc(value):
    """'2026-11-02 16:30:00' (UTC, как в базе) -> datetime с часовым поясом UTC."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def check_access(user_id):
    """
    Есть ли полный доступ.
    True — администратор или пробный период ещё идёт; False — триал закончился.
    """
    if is_admin(user_id):
        return True
    return datetime.now(timezone.utc) < parse_utc(get_trial_end_date(user_id))


def access_status_text(user_id):
    """Строка о доступе пользователя для /start и /help."""
    if is_admin(user_id):
        return "🔐 Вы администратор: доступ ко всем функциям без ограничений."
    trial_end_date = get_trial_end_date(user_id)
    left = parse_utc(trial_end_date) - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "🔒 Пробный период закончился. Будем рады вашему отзыву: /feedback"
    # Округляем вверх: осталось 6 дней и 2 часа -> «7 дн.»
    days_left = left.days + (1 if left.seconds else 0)
    return f"⏳ Пробный период до {format_date(trial_end_date)} (осталось дней: {days_left})"


async def trial_ok(message: Message, user_id):
    """Проверка пробного периода перед платной функцией: если он закончился — отвечает и возвращает False."""
    if check_access(user_id):
        return True
    logger.info("Пробный период закончился: user_id=%s", user_id)
    await message.answer(TRIAL_ENDED_TEXT)
    return False


def start_text(user_id):
    """Приветствие: сразу после него — срок доступа пользователя, потом команды."""
    return f"{START_GREETING}\n\n{access_status_text(user_id)}\n\n{START_COMMANDS}"


def help_text(user_id):
    """Справка: срок доступа — сразу под заголовком; администратору — ещё блок его команд перед кнопками."""
    text = f"{HELP_TITLE}\n\n{access_status_text(user_id)}\n\n{HELP_TEXT}"
    if is_admin(user_id):
        footer = "👇 Или нажмите кнопку:"
        text = text.replace(footer, ADMIN_HELP_TEXT + footer)
    return text


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


async def check_allowed(message: Message):
    """Проверка доступа для команд: при отказе отвечает пользователю."""
    if is_allowed(message.from_user.id):
        return True
    logger.warning("Отказано в доступе: user_id=%s", message.from_user.id)
    await message.answer(f"Доступ закрыт. Ваш Telegram id: {message.from_user.id}")
    return False


def source_name(source):
    """
    Источник для подписи: адрес RSS-ленты -> название сайта.
    "https://www.coindesk.com/arc/outboundfeeds/rss/, @coindesk" -> "coindesk.com, @coindesk".
    """
    names = []
    for part in (source or "").split(", "):
        if part.startswith(("http://", "https://")):
            part = urlparse(part).netloc.removeprefix("www.")
        if part and part not in names:
            names.append(part)
    return ", ".join(names)


def format_event(event):
    """Одно событие в виде текста для Telegram: заголовок, суть, важность, источник и ссылка на оригинал."""
    fire = "🔥 " if event["insider_score"] >= HOT_INSIDER_SCORE else ""
    # У старых событий тональности нет (колонка добавлена позже)
    mood = SENTIMENT_LABELS[event["sentiment"]][0] + " " if event.get("sentiment") in SENTIMENT_LABELS else ""
    text = (
        f"{fire}{event['title']}\n"
        f"{event['summary']}\n"
        f"{mood}📂 {event['category']} | ⭐ важность {event['insider_score']}/10 | {source_name(event['source'])}"
    )
    # У старых событий тегов нет (колонка добавлена позже)
    if event.get("category_tags"):
        text += f"\n🏷 {event['category_tags'].replace(',', ', ')}"
    # Ссылка на статью есть только у событий, собранных после появления колонки url
    if event.get("url"):
        text += f"\n🔗 {event['url']}"
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
        await message.answer(
            part,
            reply_markup=reply_markup if i == len(parts) - 1 else None,
            # В ленте до 10 ссылок на статьи — большое превью первой из них только мешает
            link_preview_options=NO_LINK_PREVIEW,
        )


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
        "⚙️ Ваши темы для /briefing\n\n"
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


async def briefing_text(user_id):
    """Брифинг с учётом тем пользователя."""
    topics = user_topics(user_id)
    text = await make_briefing(topics=topics)
    if topics:
        text = f"🎯 Ваши темы: {topics_title(topics)} (изменить: /settings)\n\n" + text
    # Время ставим после ответа GPT — в момент, когда брифинг готов
    return with_time(text)


# ============================================================
# Команды
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Приветствие ИИ-аналитика и меню кнопок."""
    if not await check_allowed(message):
        return
    user_id = message.from_user.id
    await message.answer(start_text(user_id), reply_markup=menu_keyboard(user_id))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Полный список команд и то же меню кнопок."""
    if not await check_allowed(message):
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
    if not await check_allowed(message):
        return
    text = news_text()
    remember_context(message.from_user.id, text)
    await send_long(message, text)


@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not await check_allowed(message):
        return
    if not await trial_ok(message, message.from_user.id):
        return
    text = top_text()
    remember_context(message.from_user.id, text)
    await send_long(message, text)


@dp.message(Command("briefing"))
async def cmd_briefing(message: Message):
    # Без check_allowed: брифинг доступен всем, даже при ALLOWED_USER_IDS (но только в пробный период)
    logger.info("Брифинг для user_id=%s", message.from_user.id)
    if not await trial_ok(message, message.from_user.id):
        return
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    text = await briefing_text(message.from_user.id)
    remember_context(message.from_user.id, text)
    await send_long(message, text)


@dp.message(Command("research"))
async def cmd_research(message: Message, command: CommandObject):
    """/research вопрос — развёрнутый ответ Агента №4 по новостям за 7 дней."""
    if not await check_allowed(message):
        return
    if not await trial_ok(message, message.from_user.id):
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
    # Без check_allowed: темы влияют на /briefing, а он доступен всем
    topics = user_topics(message.from_user.id)
    await message.answer(settings_text(topics), reply_markup=settings_keyboard(topics))


@dp.message(Command(*TAG_COMMANDS))
async def cmd_by_tag(message: Message, command: CommandObject):
    """Один обработчик для /defi, /btc, /eth, /sol — отличается только тег."""
    if not await check_allowed(message):
        return
    await send_long(message, tag_text(command.command.lower()))


@dp.message(Command("sentiment"))
async def cmd_sentiment(message: Message, command: CommandObject):
    """/sentiment btc — доля позитивных, негативных и нейтральных новостей по теме за 24 часа."""
    if not await check_allowed(message):
        return
    await message.answer(sentiment_text(command.args or ""))


@dp.message(Command("entity"))
async def cmd_entity(message: Message, command: CommandObject):
    """/entity AAVE — новости про актив из ALLOWED_ENTITIES."""
    if not await check_allowed(message):
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


# Кнопка «Отменить» — на каждом шаге опроса
FEEDBACK_CANCEL_ROW = [InlineKeyboardButton(text="❌ Отменить", callback_data="fb:cancel")]
FEEDBACK_INTERESTED_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="👍 Да", callback_data="fb:interested:yes"),
        InlineKeyboardButton(text="👎 Нет", callback_data="fb:interested:no"),
    ],
    FEEDBACK_CANCEL_ROW,
])
FEEDBACK_PAY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="💰 Да", callback_data="fb:pay:yes"),
        InlineKeyboardButton(text="🆓 Только бесплатно", callback_data="fb:pay:free"),
    ],
    FEEDBACK_CANCEL_ROW,
])
FEEDBACK_TEXT_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[FEEDBACK_CANCEL_ROW])
FEEDBACK_CANCELLED_TEXT = "Опрос отменён. Пройти заново: /feedback"
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


def in_feedback(current_state):
    """True, если пользователь сейчас на одном из шагов опроса ("FeedbackForm:suggestions" и т.п.)."""
    return bool(current_state) and current_state.startswith(f"{FeedbackForm.__name__}:")


@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def leave_feedback(handler, event, data):
    """
    Любая команда (/news, /help...) или кнопка меню во время опроса отменяет опрос.
    Иначе брошенный на шаге 3 опрос «съел» бы следующий вопрос пользователя как отзыв.
    """
    state = data.get("state")
    if isinstance(event, Message):
        leaves = bool(event.text) and event.text.startswith("/")
    else:
        # Кнопки самого опроса (fb:...) опрос не отменяют
        leaves = bool(event.data) and not event.data.startswith("fb:")
    if state and leaves and in_feedback(await state.get_state()):
        await state.clear()
        logger.info("Опрос /feedback прерван: user_id=%s", event.from_user.id)
    return await handler(event, data)


@dp.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    # Без check_allowed: отзыв может оставить любой
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
    question = await callback.message.answer(
        "Шаг 3/3. Что бы вы хотели добавить или изменить? Напишите одним сообщением.",
        reply_markup=FEEDBACK_TEXT_KEYBOARD,
    )
    # Запоминаем вопрос, чтобы убрать у него кнопку «Отменить», когда придёт ответ
    await state.update_data(question_message_id=question.message_id)


@dp.callback_query(F.data == "fb:cancel")
async def feedback_cancel(callback: CallbackQuery, state: FSMContext):
    """«❌ Отменить» на любом шаге: выходим из опроса, ничего не сохраняем."""
    if not in_feedback(await state.get_state()):
        await callback.answer("Этот опрос уже неактивен. Начните заново: /feedback", show_alert=True)
        return
    await state.clear()
    await mark_answer(callback, "❌ Отменено")
    await callback.message.answer(FEEDBACK_CANCELLED_TEXT)


@dp.callback_query(F.data.startswith("fb:"))
async def feedback_old_button(callback: CallbackQuery):
    """Нажали кнопку из старого или уже пройденного опроса."""
    await callback.answer("Этот опрос уже неактивен. Начните заново: /feedback", show_alert=True)


# Команды во время шага 3 сюда не попадают: leave_feedback уже отменил опрос, и команда выполняется как обычно
@dp.message(FeedbackForm.suggestions, F.text, ~F.text.startswith("/"))
async def feedback_suggestions(message: Message, state: FSMContext):
    data = await state.get_data()
    save_feedback(message.from_user.id, data["is_interested"], data["will_pay"], message.text.strip())
    await state.clear()
    if data.get("question_message_id"):
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=data["question_message_id"], reply_markup=None
            )
        except TelegramBadRequest:
            pass
    await message.answer("Спасибо за отзыв! Это очень помогает нам делать бота лучше.")


# ============================================================
# Учебная справка по DeFi (/theory): 2-3 предложения о термине + ссылка на сайт
# ============================================================

THEORY_TITLE = "📖 Учебная справка по DeFi"
THEORY_NO_CONTEXT_TEXT = (
    f"{THEORY_TITLE}: коротко объясню сложный термин из последней новости или ответа ИИ.\n\n"
    "Сначала откройте новости (/news) или брифинг (/briefing), а потом снова нажмите кнопку справки.\n"
    "Или спросите про термин сразу: /theory impermanent loss"
)


def theory_footer_html():
    """Ссылка на сайт с полными материалами — в конце каждого ответа справки."""
    return (
        "\n\n📚 <b>Хочешь узнать больше, чтобы не терять деньги?</b>\n"
        "Полный учебник и симулятор DeFi Compass скоро появятся на моем сайте. "
        f"Следи за обновлениями: {html.escape(LANDING_URL)}"
    )


async def callback_allowed(callback: CallbackQuery):
    """Проверка доступа для кнопок: при отказе показывает всплывающее сообщение."""
    if is_allowed(callback.from_user.id):
        return True
    logger.warning("Отказано в доступе (кнопка): user_id=%s", callback.from_user.id)
    await callback.answer(f"Доступ закрыт. Ваш Telegram id: {callback.from_user.id}", show_alert=True)
    return False


async def send_theory(message: Message, user_id, term=""):
    """
    Справка по термину: term — из команды (/theory AMM), без него — самый сложный термин из last_context.
    Ровно один запрос к GPT, ответ ограничен MAX_ANSWER_TOKENS (agent_5_teacher.py).
    """
    if not await trial_ok(message, user_id):
        return
    context = "" if term else get_last_context(user_id)
    logger.info("Справка для user_id=%s: термин %r, last_context %d символов", user_id, term, len(context))
    if not term and not context:
        await message.answer(html.escape(THEORY_NO_CONTEXT_TEXT) + theory_footer_html(), parse_mode="HTML")
        return
    if not ai_limit_ok(user_id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    answer = await explain_term(term=term, context=context)
    if answer == ERROR_TEXT:
        await message.answer(answer)
        return
    # Ответ GPT экранируем: символы < > & в нём не должны ломать HTML-разметку
    text = html.escape(f"{THEORY_TITLE}\n\n{answer}") + theory_footer_html()
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("theory"))
async def cmd_theory(message: Message, command: CommandObject):
    if not await check_allowed(message):
        return
    term = (command.args or "").strip()
    if len(term) > MAX_RESEARCH_LENGTH:
        await message.answer(RESEARCH_TOO_LONG_TEXT)
        return
    await send_theory(message, message.from_user.id, term)


@dp.callback_query(F.data == "theory")
async def theory_button(callback: CallbackQuery):
    if not await callback_allowed(callback):
        return
    await callback.answer()
    await send_theory(callback.message, callback.from_user.id)


# Кнопки старой версии справки (выбор терминов, главы) в уже отправленных сообщениях
@dp.callback_query(
    F.data.in_({"read_suggested", "other_topic", "theory_read"})
    | F.data.startswith("theory_term:")
    | F.data.startswith("theory_read:")
    | F.data.startswith("theory_chapter:")
)
async def theory_old_button(callback: CallbackQuery):
    await callback.answer(f"Эта кнопка устарела. Нажмите «{THEORY_TITLE}» в меню: /start", show_alert=True)


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

    # Те же функции, что закрыты после пробного периода у команд /top и /briefing
    if action in ("top", "briefing", "digest") and not await trial_ok(message, user_id):
        return

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
    # "digest" — кнопка «Краткая сводка» из старых сообщений: сводки больше нет, отвечаем брифингом
    elif action in ("briefing", "digest"):
        if not ai_limit_ok(user_id):
            await message.answer(LIMIT_TEXT)
            return
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        text = await briefing_text(user_id)
    elif action == "settings":
        topics = user_topics(user_id)
        await message.answer(settings_text(topics), reply_markup=settings_keyboard(topics))
        return
    else:
        logger.warning("Неизвестная кнопка: %s", callback.data)
        return

    # Как и у команд — запоминаем текст для /theory
    if action in ("news", "top", "briefing", "digest"):
        remember_context(user_id, text)
    await send_long(message, text)


UNKNOWN_COMMAND_TEXT = "Такой команды нет. Список команд: /help"


@dp.message(F.text)
async def handle_question(message: Message):
    """Любой обычный текст считаем вопросом к ассистенту."""
    if not await check_allowed(message):
        return
    # Неизвестная или удалённая команда (например, /digest) — не тратим на неё запрос к ИИ
    if message.text.startswith("/"):
        await message.answer(UNKNOWN_COMMAND_TEXT)
        return
    if not await trial_ok(message, message.from_user.id):
        return
    if not ai_limit_ok(message.from_user.id):
        await message.answer(LIMIT_TEXT)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    answer = await answer_question(message.text)
    remember_context(message.from_user.id, answer)
    await send_long(message, answer)


# ============================================================
# Запуск
# ============================================================

async def run_bot():
    """Главная функция бота: получает сообщения от Telegram (long polling)."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Подсказки команд в меню Telegram
    commands = [
        # Тот же порядок, что в справке /help
        BotCommand(command="news", description="📰 Последние новости (все подряд)"),
        BotCommand(command="top", description="🎯 Только самые важные новости"),
        BotCommand(command="btc", description="₿ Новости Bitcoin"),
        BotCommand(command="eth", description="💎 Новости Ethereum"),
        BotCommand(command="sol", description="☀️ Новости Solana"),
        BotCommand(command="defi", description="🧪 Новости DeFi"),
        BotCommand(command="briefing", description="🧠 Главное за 24 часа"),
        BotCommand(command="sentiment", description="📊 ИИ: Настроение рынка — /sentiment btc"),
        BotCommand(command="entity", description="🔍 Инфо по монете — /entity AAVE"),
        BotCommand(command="research", description="🔬 ИИ: Исследование — /research вопрос"),
        BotCommand(command="theory", description="📖 Учебная справка по DeFi"),
        BotCommand(command="settings", description="⚙️ Настройки тем"),
        BotCommand(command="feedback", description="💬 Фидбек"),
        BotCommand(command="help", description="❓ Справка"),
    ]
    await bot.set_my_commands(commands)

    # Администратору в его чате с ботом дополнительно показываем команды администратора
    if ADMIN_ID:
        try:
            await bot.set_my_commands(
                commands + [
                    BotCommand(command="stats", description="📈 Статистика бота (админ)"),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
        except TelegramBadRequest:
            # Например, администратор ещё ни разу не писал боту
            logger.warning("Не удалось показать команды администратора в его меню (ADMIN_ID=%s)", ADMIN_ID)

    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS не задан — ботом может пользоваться кто угодно")

    me = await bot.get_me()
    logger.info("Бот @%s запущен", me.username)
    # handle_signals=False: Ctrl+C обрабатывает main.py, а не aiogram
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    # Запуск только бота (без сборщика и фильтра) — для проверки
    from instance_lock import acquire_instance_lock
    acquire_instance_lock("bot.py")
    init_db()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен (Ctrl+C)")
