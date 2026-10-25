"""
Агент №5: Учитель («Теория DeFi», команда /theory).

Подсказывает сложные термины из последнего текста, который бот отправил пользователю,
объясняет тему в 3 предложениях, подбирает 3 главы из THEORY_CHAPTERS (config.py)
и по кнопке пишет текст глав.
Термины и объяснение GPT возвращает строго в JSON (response_format json_object).
"""

import asyncio
import json
import logging

from openai import OpenAIError

from agent_3_assistant import ERROR_TEXT, ask_gpt
from config import THEORY_CHAPTERS

logger = logging.getLogger(__name__)

# Сколько терминов предлагать пользователю
MAX_SUGGESTED_TERMS = 3
# Длинный термин не влезет в кнопку — обрезаем
MAX_TERM_LENGTH = 60
# Сколько символов последнего текста отправлять в GPT (длинные брифинги обрезаем)
MAX_CONTEXT_LENGTH = 3000
# Сколько глав выбирать для темы
CHAPTERS_PER_TOPIC = 3

PARSE_ERROR_TEXT = "Не удалось разобрать ответ ИИ, попробуйте еще раз"

SUGGEST_PROMPT = """You are an AI assistant. You must return ONLY JSON format.
Analyze the following text: {user_text}
Find 2-3 complex DeFi/crypto terms in this text that a user might want to understand.
Format required:
{{"terms": ["Term 1", "Term 2"]}}
If there are no such terms, return {{"terms": []}}."""

EXPLAIN_PROMPT = """You are a strict DeFi tutor API. You must return ONLY JSON format. Do not write any introductory or concluding text outside the JSON.
Format required:
{{
  "explanation": "Your explanation of the term in exactly 3 sentences in Russian.",
  "chapters": ["Chapter 1 name", "Chapter 2 name", "Chapter 3 name"]
}}

User question: {question}
Available chapters: {chapters}
Pick exactly 3 most relevant chapters for the 'chapters' list. If no chapters match, return an empty list []."""

CHAPTERS_PROMPT = """You are a friendly DeFi and crypto teacher inside a Telegram bot.
The user is studying the topic below. Write the listed chapters about this topic.
Rules:
- Write in Russian. Plain text, no Markdown.
- Each chapter: its number and title on the first line (start it with "📖"), then 1-2 short paragraphs.
- Use simple words and real examples (protocols, numbers). Do not give financial advice.

Topic: {topic}
Short explanation the user has already read: {explanation}
Chapters to write:
{chapters}"""


def load_json_object(answer):
    """json.loads, но только для JSON-объекта. Любая ошибка или не-объект -> None."""
    try:
        data = json.loads(answer)
    except (TypeError, ValueError):
        logger.warning("GPT вернул не JSON: %r", (answer or "")[:300])
        return None
    if not isinstance(data, dict):
        logger.warning("GPT вернул JSON, но не объект: %r", (answer or "")[:300])
        return None
    return data


def parse_terms(answer):
    """'{"terms": ["AMM", "TVL"]}' -> ["AMM", "TVL"]. Ошибка или нет терминов -> []."""
    data = load_json_object(answer)
    items = data.get("terms") if data else None
    if not isinstance(items, list):
        return []
    terms = []
    for item in items:
        if not isinstance(item, str):
            continue
        term = item.strip()[:MAX_TERM_LENGTH]
        if term and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)
    return terms[:MAX_SUGGESTED_TERMS]


def parse_lesson(answer):
    """
    '{"explanation": "...", "chapters": [...]}' -> {"explanation": str, "chapters": [str, ...]}.
    Главы оставляем только из THEORY_CHAPTERS (GPT может слегка изменить название или придумать своё).
    Нет объяснения или JSON не разобрался -> None.
    """
    data = load_json_object(answer)
    if not data:
        return None
    explanation = data.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        logger.warning("В ответе GPT нет explanation: %r", answer[:300])
        return None

    known = {name.lower(): name for name in THEORY_CHAPTERS}
    chapters = []
    raw_chapters = data.get("chapters")
    for item in raw_chapters if isinstance(raw_chapters, list) else []:
        name = known.get(str(item).strip().lower())
        if name and name not in chapters:
            chapters.append(name)
    return {"explanation": explanation.strip(), "chapters": chapters[:CHAPTERS_PER_TOPIC]}


async def suggest_topics_from_context(user_text):
    """2-3 сложных термина из текста. Пустой список — терминов нет или GPT не ответил."""
    prompt = SUGGEST_PROMPT.format(user_text=user_text[:MAX_CONTEXT_LENGTH])
    try:
        answer = await ask_gpt(prompt, "Find the terms.", json_mode=True)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при подборе терминов")
        return []
    terms = parse_terms(answer)
    logger.info("Предложенные термины: %s", terms)
    return terms


async def explain_topic(question):
    """
    Объяснение темы в 3 предложениях + до 3 глав из THEORY_CHAPTERS.
    Возвращает (урок, текст ошибки): урок — {"explanation": ..., "chapters": [...]} или None.
    """
    logger.info("Теория: объясняю '%s'", question)
    prompt = EXPLAIN_PROMPT.format(question=question, chapters=json.dumps(THEORY_CHAPTERS, ensure_ascii=False))
    try:
        answer = await ask_gpt(prompt, question, json_mode=True)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при объяснении темы")
        return None, ERROR_TEXT
    lesson = parse_lesson(answer)
    if not lesson:
        return None, PARSE_ERROR_TEXT
    logger.info("Теория: главы для '%s': %s", question, lesson["chapters"])
    return lesson, None


async def read_chapters(topic, explanation, chapters, first_number=1):
    """Текст глав по теме. first_number — номер первой главы (для одной главы из плана, например 2)."""
    logger.info("Теория: пишу главы %s по '%s'", chapters, topic)
    chapters_text = "\n".join(f"{i}. {name}" for i, name in enumerate(chapters, start=first_number))
    prompt = CHAPTERS_PROMPT.format(topic=topic, explanation=explanation, chapters=chapters_text)
    try:
        return await ask_gpt(prompt, "Write the chapters.")
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при написании глав")
        return ERROR_TEXT


if __name__ == "__main__":
    # Быстрая проверка из терминала
    async def demo():
        print(await suggest_topics_from_context("Aave запускает новый пул ликвидности с защитой от impermanent loss"))
        print(await explain_topic("Что такое смарт-контракт?"))

    asyncio.run(demo())
