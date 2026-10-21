"""
Агент №4: Исследователь.

Отвечает на вопрос пользователя развёрнутым аналитическим разбором (команда /research).
Ищет в таблице events новости за последние 7 дней по ключевым словам из вопроса
и отправляет самые подходящие из них в GPT.
"""

import asyncio
import logging
import re

from openai import OpenAIError

from agent_3_assistant import ERROR_TEXT, ask_gpt, format_events
from database import get_events_since

logger = logging.getLogger(__name__)

# За сколько дней искать новости
RESEARCH_DAYS = 7
# Сколько новостей максимум отправлять в GPT
MAX_RESEARCH_EVENTS = 15
# Слова короче этого в поиске не участвуют ("и", "в", "на")
MIN_WORD_LENGTH = 3

NO_RESEARCH_EVENTS_TEXT = (
    "За последние 7 дней новостей по вашему вопросу не найдено. "
    "Попробуйте другие ключевые слова, например: /research ETF Bitcoin"
)

# Частые слова вопросов, по которым искать бессмысленно
STOP_WORDS = {
    "как", "что", "это", "для", "или", "при", "про", "чем", "где", "кто", "так", "уже", "его", "она",
    "они", "все", "всё", "есть", "был", "будет", "когда", "почему", "зачем", "какой", "какая", "какие",
    "сейчас", "можно", "нужно", "ли", "the", "and", "for", "what", "how", "why", "with", "about",
}

# Русские названия тем -> теги в базе (теги хранятся по-английски: bitcoin, ethereum, ...)
TOPIC_WORDS = {
    "биткоин": "bitcoin", "биткойн": "bitcoin", "btc": "bitcoin",
    "эфир": "ethereum", "эфириум": "ethereum", "eth": "ethereum",
    "солана": "solana", "sol": "solana",
    "дефи": "defi",
}

RESEARCH_PROMPT = """You are a professional crypto market analyst.
Answer the user's question based ONLY on the news events listed below.
Rules:
- Write in the same language as the question (usually Russian).
- Write a structured answer in 3-5 paragraphs. Start with a short direct answer,
  then explain the key facts and their impact, and finish with a conclusion.
- Highlight the most important points (you may start key lines with "•" or an emoji). Plain text, no Markdown.
- Mention dates and sources when it helps.
- If the news does not fully answer the question, say so honestly. Do not give financial advice.

User question: {question}

News events (newest first):
{events}"""


def get_keywords(question):
    """
    Достаёт из вопроса ключевые слова для поиска.
    Русские слова обрезаем на 2 буквы, чтобы находить другие падежи:
    "биткоина" -> "биткои" найдёт и "биткоин", и "биткоину".
    """
    keywords = []
    for word in re.findall(r"\w+", question.lower()):
        if len(word) < MIN_WORD_LENGTH or word in STOP_WORDS:
            continue
        # Тема на русском -> тег из базы
        for topic, tag in TOPIC_WORDS.items():
            if word.startswith(topic) and tag not in keywords:
                keywords.append(tag)
        if re.search("[а-яё]", word) and len(word) >= 6:
            word = word[:-2]
        if word not in keywords:
            keywords.append(word)
    return keywords


def find_events(keywords):
    """
    Новости за RESEARCH_DAYS дней, где ключевые слова встречаются в заголовке, тегах или сущностях.
    Сначала идут новости, где совпало больше слов, среди них — самые свежие.
    Ищем в Python, а не в SQL: LOWER() и LIKE в SQLite не понимают регистр русских букв.
    """
    found = []
    for event in get_events_since(RESEARCH_DAYS):  # уже отсортированы от новых к старым
        text = " ".join(
            [event["title"] or "", event.get("category_tags") or "", event.get("entities") or ""]
        ).lower()
        matches = sum(1 for word in keywords if word in text)
        if matches:
            found.append((matches, event))
    # sorted сохраняет порядок при равном числе совпадений — свежие остаются выше
    found = sorted(found, key=lambda item: item[0], reverse=True)
    return [event for _, event in found[:MAX_RESEARCH_EVENTS]]


async def research_topic(user_question: str) -> str:
    """Развёрнутый ответ на вопрос пользователя по новостям за последние 7 дней."""
    keywords = get_keywords(user_question)
    events = find_events(keywords) if keywords else []
    logger.info("Исследование: '%s', слова %s, найдено новостей: %d", user_question, keywords, len(events))
    if not events:
        return NO_RESEARCH_EVENTS_TEXT

    prompt = RESEARCH_PROMPT.format(question=user_question, events=format_events(events))
    try:
        answer = await ask_gpt(prompt, user_question)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при исследовании")
        return ERROR_TEXT

    logger.info("Исследование готово (%d символов)", len(answer))
    return answer


if __name__ == "__main__":
    # Быстрая проверка из терминала
    print(asyncio.run(research_topic("Как ETF влияет на биткоин?")))
