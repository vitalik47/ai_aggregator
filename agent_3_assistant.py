"""
Агент №3: Ассистент.

Отвечает на вопросы пользователя, делает дайджест и брифинг за 24 часа.
Работает только с событиями из таблицы events (их готовит Агент №2).
Эти функции вызывает Telegram-бот (bot.py).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI, OpenAIError

from config import OPENAI_API_KEY, OPENAI_MODEL
from database import get_events_by_topics, get_recent_events

logger = logging.getLogger(__name__)

# Сколько последних событий давать GPT как контекст для ответа на вопрос
EVENTS_FOR_CONTEXT = 30

NO_EVENTS_TEXT = "В базе пока нет новостей. Подождите, пока сборщик и фильтр их наберут."
ERROR_TEXT = "Не получилось получить ответ от ИИ. Попробуйте чуть позже."
NO_TOPIC_EVENTS_TEXT = "По вашим темам за 24 часа новостей не найдено"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

QUESTION_PROMPT = """You are a crypto news assistant inside a Telegram bot.
Answer the user's question using ONLY the news events listed below.
Rules:
- Answer in the same language as the question (usually Russian).
- Be concise: up to 150 words, plain text, no Markdown tables.
- Mention the source when it is useful.
- If the events do not contain the answer, say honestly that there is no such news in the database yet.
- Do not give financial advice.

News events (newest first):
{events}"""

DIGEST_PROMPT = """You are a crypto news editor. Write a short digest in Russian
based ONLY on the news events listed below.
Rules:
- Start with one sentence about the overall picture.
- Then list the most important events, one line each, starting with "•".
- Put events with a high insider_score first and mark them with 🔥.
- Plain text, up to 200 words, no Markdown tables.

News events (newest first):
{events}"""

BRIEFING_PROMPT = """You are a professional crypto market analyst.
Below are the news events from the last 24 hours.
Pick the 3-5 most important ones. For each, write a headline and one line on why it matters.
Then describe the main trend in one or two sentences.
Write in Russian. Plain text, no Markdown. Use EXACTLY this format:

🔥 Главное за 24 часа:
1. [Headline] — [Why it matters]
2. [Headline] — [Why it matters]
...

⚡ Тренд: [Short description of the main trend]

News events (newest first):
{events}"""


def format_events(events):
    """Превращает список событий в текст, понятный для GPT."""
    lines = []
    for e in events:
        lines.append(
            f"- [{e['created_at']}] [{e['category']}] [insider_score {e['insider_score']}/10] "
            f"{e['title']}: {e['summary']} (source: {e['source']})"
        )
    return "\n".join(lines)


async def ask_gpt(system_prompt, user_text):
    """Один запрос к GPT. Возвращает текст ответа."""
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )
    return response.choices[0].message.content.strip()


async def answer_question(question):
    """Отвечает на вопрос пользователя по последним новостям."""
    events = get_recent_events(EVENTS_FOR_CONTEXT)
    if not events:
        return NO_EVENTS_TEXT

    logger.info("Вопрос пользователя: %s", question)
    try:
        answer = await ask_gpt(QUESTION_PROMPT.format(events=format_events(events)), question)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при ответе на вопрос")
        return ERROR_TEXT

    logger.info("Ответ готов (%d символов)", len(answer))
    return answer


async def make_digest(limit=10, topics=None):
    """
    Делает короткий дайджест.
    topics — список тегов пользователя (["bitcoin", "defi"]): тогда берём только его темы за 24 часа.
    Без topics — последние limit событий, как раньше.
    """
    if topics:
        events = get_events_by_topics(topics, hours=24, limit=limit)
        if not events:
            return NO_TOPIC_EVENTS_TEXT
    else:
        events = get_recent_events(limit)
        if not events:
            return NO_EVENTS_TEXT

    logger.info("Готовлю дайджест по %d событиям (темы: %s)", len(events), topics or "все")
    try:
        return await ask_gpt(DIGEST_PROMPT.format(events=format_events(events)), "Make the digest.")
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при создании дайджеста")
        return ERROR_TEXT


async def make_briefing(topics=None):
    """
    Брифинг: 3–5 главных событий за последние 24 часа + общий тренд.
    topics — список тегов пользователя: тогда в брифинг идут только новости по его темам.
    """
    if topics:
        events = get_events_by_topics(topics, hours=24, limit=50)
        if not events:
            return NO_TOPIC_EVENTS_TEXT
    else:
        # created_at в базе хранится в UTC в виде "ГГГГ-ММ-ДД ЧЧ:ММ:СС" — такие строки можно сравнивать напрямую
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        events = [e for e in get_recent_events(limit=50) if e["created_at"] >= since]
        if not events:
            return "За последние 24 часа новостей нет. Загляните чуть позже."

    logger.info("Готовлю брифинг по %d событиям за 24 часа (темы: %s)", len(events), topics or "все")
    try:
        return await ask_gpt(BRIEFING_PROMPT.format(events=format_events(events)), "Make the briefing.")
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при создании брифинга")
        return ERROR_TEXT


if __name__ == "__main__":
    # Быстрая проверка из терминала: дайджест + ответ на вопрос
    async def demo():
        print("=== Дайджест ===")
        print(await make_digest())
        print("\n=== Вопрос: Что нового с Bitcoin? ===")
        print(await answer_question("Что нового с Bitcoin?"))

    asyncio.run(demo())
