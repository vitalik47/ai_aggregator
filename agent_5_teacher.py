"""
Агент №5: Учитель («Теория DeFi», команда /theory).

Подсказывает сложные термины из последнего текста, который бот отправил пользователю,
объясняет тему простыми словами (3-4 предложения + план из 3 глав)
и по кнопке «Читаем» пишет сами главы.
"""

import ast
import asyncio
import logging
import re

from openai import OpenAIError

from agent_3_assistant import ERROR_TEXT, ask_gpt

logger = logging.getLogger(__name__)

# Сколько терминов предлагать пользователю
MAX_SUGGESTED_TERMS = 3
# Длинный термин не влезет в кнопку — обрезаем
MAX_TERM_LENGTH = 60
# Сколько символов последнего текста отправлять в GPT (длинные брифинги обрезаем)
MAX_CONTEXT_LENGTH = 3000

SUGGEST_PROMPT = """You are an AI assistant. Analyze the following text: {user_text}
Find 2-3 complex DeFi/crypto terms in this text that a user might want to understand.
Return ONLY a Python list of these terms, e.g., ['Term 1', 'Term 2']."""

EXPLAIN_PROMPT = """You are a friendly DeFi and crypto teacher inside a Telegram bot.
Explain the topic the user asks about to a beginner.
Rules:
- Answer only about cryptocurrencies, DeFi, blockchain and Web3.
  If the topic is off-topic, politely refuse in one sentence and do not write the plan.
- Write in Russian. Plain text, no Markdown.
- First, explain the topic in 3-4 simple sentences.
- Then suggest a plan of exactly 3 chapters for deeper study. Use EXACTLY this format:

[3-4 sentences]

📚 План из 3 глав:
1. [Chapter title] — [one line: what it is about]
2. [Chapter title] — [one line: what it is about]
3. [Chapter title] — [one line: what it is about]"""

CHAPTERS_PROMPT = """You are a friendly DeFi and crypto teacher inside a Telegram bot.
Earlier you gave the user a short explanation and a plan of 3 chapters (below).
Now write these 3 chapters.
Rules:
- Write in Russian. Plain text, no Markdown.
- Each chapter: its number and title on the first line (start it with "📖"), then 1-2 short paragraphs.
- Use simple words and real examples (protocols, numbers). Do not give financial advice.
- If earlier you refused because the topic is not about crypto, refuse again in one sentence.

Topic: {topic}

Your earlier explanation and plan:
{plan}"""


def parse_terms(answer):
    """Достаёт список терминов из ответа GPT: "['AMM', 'TVL']" -> ["AMM", "TVL"]."""
    # GPT иногда оборачивает список в ```python ... ``` или пишет текст вокруг — берём только [...]
    match = re.search(r"\[.*\]", answer, re.DOTALL)
    if not match:
        return []
    try:
        items = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError):
        return []
    if not isinstance(items, list):
        return []
    terms = []
    for item in items:
        term = str(item).strip()[:MAX_TERM_LENGTH]
        if term and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)
    return terms[:MAX_SUGGESTED_TERMS]


async def suggest_topics_from_context(user_text):
    """2-3 сложных термина из текста. Пустой список — терминов нет или GPT не ответил."""
    prompt = SUGGEST_PROMPT.format(user_text=user_text[:MAX_CONTEXT_LENGTH])
    try:
        answer = await ask_gpt(prompt, "Find the terms.")
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при подборе терминов")
        return []
    terms = parse_terms(answer)
    logger.info("Предложенные термины: %s", terms)
    return terms


async def explain_topic(topic):
    """Короткое объяснение темы (3-4 предложения) + план из 3 глав."""
    logger.info("Теория: объясняю '%s'", topic)
    try:
        return await ask_gpt(EXPLAIN_PROMPT, topic)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при объяснении темы")
        return ERROR_TEXT


async def read_chapters(topic, plan):
    """Полный текст трёх глав по плану из explain_topic."""
    logger.info("Теория: пишу главы по '%s'", topic)
    try:
        return await ask_gpt(CHAPTERS_PROMPT.format(topic=topic, plan=plan), "Write the chapters.")
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при написании глав")
        return ERROR_TEXT


if __name__ == "__main__":
    # Быстрая проверка из терминала
    async def demo():
        print(await suggest_topics_from_context("Aave запускает новый пул ликвидности с impermanent loss защитой"))
        print(await explain_topic("Impermanent loss"))

    asyncio.run(demo())
