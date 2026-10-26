"""
Агент №5: «Учебная справка по DeFi» (команда /theory).

Коротко (2-3 предложения) объясняет один DeFi-термин: названный пользователем
или самый сложный из последнего текста, который бот ему отправил (users.last_context).
Один запрос к GPT на одно нажатие. Подробные материалы — на сайте (LANDING_URL).
"""

import asyncio
import logging

from openai import OpenAIError

from agent_3_assistant import ERROR_TEXT, ask_gpt

logger = logging.getLogger(__name__)

# Сколько символов последнего текста отправлять в GPT (длинные брифинги обрезаем)
MAX_CONTEXT_LENGTH = 3000
# Потолок длины ответа: ~50 слов по-русски — около 120 токенов, 200 — с запасом.
# GPT не сможет написать больше, даже если проигнорирует промпт
MAX_ANSWER_TOKENS = 200

EXPLAIN_PROMPT = (
    "You are a DeFi assistant. Explain the requested term STRICTLY in 2-3 short sentences. "
    "Do not write lists, do not write chapters, do not write long explanations. Keep it under 50 words. "
    "Format: just the plain text explanation."
)


async def explain_term(term=None, context=None):
    """
    Короткая справка по термину. term — что спросил пользователь (/theory AMM);
    без него GPT сам выбирает самый сложный DeFi-термин из context.
    """
    if term:
        request = f"Term: {term}\nAnswer in Russian."
    else:
        request = (
            "Pick the single most complex DeFi term from this text and explain it. "
            "Start with the term itself. Answer in Russian.\n\n"
            f"Text:\n{context[:MAX_CONTEXT_LENGTH]}"
        )
    logger.info("Справка: термин %r, контекст %d символов", term, len(context or ""))
    try:
        return await ask_gpt(EXPLAIN_PROMPT, request, max_tokens=MAX_ANSWER_TOKENS)
    except OpenAIError:
        logger.exception("Ошибка OpenAI API при объяснении термина")
        return ERROR_TEXT


if __name__ == "__main__":
    # Быстрая проверка из терминала
    async def demo():
        print(await explain_term(term="Impermanent loss"))
        print(await explain_term(context="Aave запускает пул ликвидности с защитой от impermanent loss и наградами LST"))

    asyncio.run(demo())
