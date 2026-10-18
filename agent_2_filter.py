"""
Агент №2: Фильтр.

Берёт необработанные сообщения из raw_messages пачками, отправляет их в GPT и
получает обратно только настоящие новости (без рекламы, мусора и дублей).
Каждая новость сохраняется в таблицу events с категорией и оценкой insider_score.
"""

import asyncio
import json
import logging

from openai import AsyncOpenAI, OpenAIError

from config import OPENAI_API_KEY, OPENAI_MODEL
from database import get_unprocessed_messages, init_db, mark_as_processed, save_event

logger = logging.getLogger(__name__)

# Сколько сообщений отправлять в GPT за один запрос
BATCH_SIZE = 20
# Пауза между проверками новых сообщений (в секундах)
FILTER_INTERVAL = 60
# Длинные сообщения обрезаем, чтобы не тратить лишние токены
MAX_TEXT_CHARS = 2000
# Допустимые категории (всё остальное превращается в "other")
CATEGORIES = ["market", "regulation", "technology", "security", "defi", "nft", "exchange", "other"]
# Допустимые теги для выборки по темам (команды /defi, /btc, /eth, /sol в боте)
TAGS = ["defi", "bitcoin", "ethereum", "solana", "other"]
# Допустимые значения тональности (всё остальное превращается в "neutral")
SENTIMENTS = ["positive", "negative", "neutral"]
# Сколько сущностей максимум сохраняем у одной новости
MAX_ENTITIES = 10

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = f"""You are a crypto news analyst. You receive a JSON list of raw messages
collected from Telegram channels and RSS feeds. Each message has "id", "source" and "text".

Your tasks:
1. Drop noise: ads, promos, giveaways, referral links, memes, greetings,
   channel self-promotion, bare price tickers without any news.
2. If several messages describe the same news, merge them into ONE event.
3. For every real news event return:
   - "title": short headline in Russian, max 100 characters
   - "summary": 2-3 sentences in Russian with the key facts
   - "category": one of {CATEGORIES}
   - "insider_score": integer 1-10, how exclusive and early the information is
     (1 = widely known mainstream news, 5 = notable but already public,
      10 = exclusive leak or insider information from a credible source)
   - "tags": list of topic tags, only from {TAGS}.
     Use every tag that fits (e.g. a DeFi protocol on Ethereum -> ["defi", "ethereum"]).
     Use ["other"] only if none of the other tags fit.
   - "sentiment": the tone of the news for the crypto market and its investors, one of {SENTIMENTS}
     (positive = good news: adoption, approvals, growth, inflows, successful launches;
      negative = bad news: hacks, scams, bans, lawsuits, outflows, price crashes;
      neutral = purely informational or mixed news without a clear direction)
   - "entities": list of the main named entities in the news (1-{MAX_ENTITIES} items):
     companies and organizations, people, crypto projects and coins.
     Write each name in its official English (Latin) spelling, even if the news is in Russian,
     e.g. "BlackRock", "Larry Fink", "Bitcoin", "SEC", "Binance".
     Use full names for coins and projects ("Bitcoin", not "BTC").
     Do not include generic words like "crypto", "market", "investors".
   - "message_ids": list of ids of the messages this event is based on

Respond ONLY with JSON in this format:
{{"events": [{{"title": "...", "summary": "...", "category": "...", "insider_score": 5, "tags": ["bitcoin"], "sentiment": "neutral", "entities": ["BlackRock", "Bitcoin", "Larry Fink"], "message_ids": [1, 2]}}]}}
If there is no real news, respond with {{"events": []}}."""


async def analyze_messages(messages):
    """Отправляет пачку сообщений в GPT и возвращает список событий (словари)."""
    payload = [
        {"id": m["id"], "source": m["source"], "text": m["text"][:MAX_TEXT_CHARS]}
        for m in messages
    ]
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    return data.get("events", [])


def save_events(events, messages):
    """Проверяет ответ GPT и сохраняет события в базу. Возвращает количество сохранённых."""
    # По id сообщения находим его источник
    source_by_id = {m["id"]: m["source"] for m in messages}
    saved = 0

    for event in events:
        title = str(event.get("title", "")).strip()
        summary = str(event.get("summary", "")).strip()
        if not title:
            continue

        category = event.get("category")
        if category not in CATEGORIES:
            category = "other"

        # insider_score приводим к числу от 1 до 10
        try:
            insider_score = min(max(int(event.get("insider_score", 1)), 1), 10)
        except (TypeError, ValueError):
            insider_score = 1

        # Оставляем только допустимые теги, без повторов; если ничего не подошло — "other"
        raw_tags = event.get("tags")
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = []
        for tag in raw_tags:
            tag = str(tag).strip().lower()
            if tag in TAGS and tag not in tags:
                tags.append(tag)
        if len(tags) > 1 and "other" in tags:
            tags.remove("other")
        tags = tags or ["other"]

        sentiment = str(event.get("sentiment", "")).strip().lower()
        if sentiment not in SENTIMENTS:
            sentiment = "neutral"

        # Сущности: только непустые строки, без повторов (без учёта регистра), не больше MAX_ENTITIES
        raw_entities = event.get("entities")
        if not isinstance(raw_entities, list):
            raw_entities = []
        entities = []
        for name in raw_entities:
            name = str(name).strip()
            if name and name.lower() not in [e.lower() for e in entities]:
                entities.append(name)
        entities = entities[:MAX_ENTITIES]

        sources = sorted({source_by_id[i] for i in event.get("message_ids", []) if i in source_by_id})
        source = ", ".join(sources) or "unknown"

        save_event(title, summary, category, source, insider_score, tags, sentiment, entities)
        saved += 1
    return saved


async def run_filter_once():
    """Обрабатывает одну пачку сообщений. Возвращает количество новых событий."""
    messages = get_unprocessed_messages(BATCH_SIZE)
    if not messages:
        logger.debug("Новых сообщений для фильтрации нет")
        return 0

    ids = [m["id"] for m in messages]
    logger.info("Фильтрую сообщений: %d", len(messages))

    try:
        events = await analyze_messages(messages)
    except OpenAIError:
        # Ошибка сети или API — сообщения НЕ помечаем, попробуем в следующий раз
        logger.exception("Ошибка OpenAI API, повторю позже")
        return 0
    except json.JSONDecodeError:
        # GPT вернул сломанный JSON — пропускаем пачку, чтобы не зациклиться на ней
        logger.exception("GPT вернул некорректный JSON, пачка пропущена (id: %s)", ids)
        mark_as_processed(ids)
        return 0

    saved = save_events(events, messages)
    mark_as_processed(ids)
    logger.info("Из %d сообщений получено событий: %d", len(messages), saved)
    return saved


async def run_filter():
    """Главная функция агента: бесконечно фильтрует новые сообщения."""
    logger.info("Агент №2 (фильтр) запущен, модель: %s", OPENAI_MODEL)
    while True:
        try:
            await run_filter_once()
        except Exception:
            # Любая неожиданная ошибка не должна останавливать агента
            logger.exception("Неожиданная ошибка в фильтре")
        await asyncio.sleep(FILTER_INTERVAL)


if __name__ == "__main__":
    # Запуск агента отдельно от остального проекта (для проверки)
    init_db()
    try:
        asyncio.run(run_filter())
    except KeyboardInterrupt:
        logger.info("Агент №2 остановлен (Ctrl+C)")
