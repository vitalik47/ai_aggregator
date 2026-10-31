"""
Оплата подписки через Crypto Bot (@CryptoBot, API Crypto Pay: https://help.crypt.bot/crypto-pay-api).

- create_invoice(user_id, plan)      — создаёт счёт в USDT и возвращает ссылку на оплату
- check_payment(invoice_id, user_id) — оплачен ли счёт этим пользователем; возвращает тариф или None

Токен приложения берётся из .env (CRYPTO_BOT_TOKEN): @CryptoBot -> Crypto Pay -> Create App.
Если токен не задан — тестовый режим: счёт без ссылки, который сразу считается оплаченным.
Бот разрешает тестовый режим только администратору, иначе любой получил бы подписку бесплатно.
"""

import logging
import uuid

import aiohttp

from config import CRYPTO_BOT_TOKEN

logger = logging.getLogger(__name__)

API_URL = "https://pay.crypt.bot/api"
ASSET = "USDT"
# Сколько секунд счёт ждёт оплаты, потом Crypto Bot его закрывает
INVOICE_EXPIRES_IN = 3600

# Тарифы: ключ — часть callback_data кнопки ("sub:month")
PLANS = {
    "month": {"title": "Месяц", "amount": "5", "days": 30},
    "year": {"title": "Год", "amount": "50", "days": 365},
}


async def create_invoice(user_id, plan):
    """
    Создаёт счёт на оплату тарифа plan ("month" или "year").
    Возвращает словарь {"invoice_id": ..., "pay_url": ...} или None при ошибке.
    В тестовом режиме (нет CRYPTO_BOT_TOKEN) pay_url = None.
    """
    info = PLANS[plan]
    if not CRYPTO_BOT_TOKEN:
        # Тариф — внутри id: check_payment узнает его без запроса к Crypto Bot
        invoice_id = f"test-{plan}-{uuid.uuid4().hex[:8]}"
        logger.warning("CRYPTO_BOT_TOKEN не задан — тестовый счёт %s для user_id=%s", invoice_id, user_id)
        return {"invoice_id": invoice_id, "pay_url": None}

    params = {
        "asset": ASSET,
        "amount": info["amount"],
        "description": f"AI Aggregator: подписка на {info['title'].lower()}",
        # payload вернётся вместе со счётом — по нему поймём, кто и что оплатил
        "payload": f"{user_id}:{plan}",
        "expires_in": INVOICE_EXPIRES_IN,
    }
    result = await call_api("createInvoice", params)
    if result is None:
        return None
    logger.info("Создан счёт %s (%s %s) для user_id=%s", result["invoice_id"], info["amount"], ASSET, user_id)
    return {"invoice_id": result["invoice_id"], "pay_url": result.get("bot_invoice_url") or result.get("pay_url")}


async def call_api(method, params):
    """Запрос к Crypto Pay API. Возвращает поле result ответа или None при любой ошибке."""
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(f"{API_URL}/{method}", json=params, headers=headers) as response:
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.exception("Crypto Bot: ошибка запроса %s", method)
        return None
    if not data.get("ok"):
        logger.error("Crypto Bot: ошибка %s: %s", method, data.get("error"))
        return None
    return data["result"]


async def check_payment(invoice_id, user_id):
    """
    Оплачен ли счёт invoice_id пользователем user_id.
    Возвращает тариф ("month" / "year") или None: счёт не оплачен, чужой или Crypto Bot недоступен.
    """
    if invoice_id.startswith("test-"):
        # Тестовый счёт засчитываем, только пока тестовый режим включён
        plan = invoice_id.split("-")[1]
        if CRYPTO_BOT_TOKEN or plan not in PLANS:
            return None
        logger.warning("Тестовый счёт %s засчитан как оплаченный (user_id=%s)", invoice_id, user_id)
        return plan

    if not CRYPTO_BOT_TOKEN:
        return None
    result = await call_api("getInvoices", {"invoice_ids": invoice_id})
    if not result or not result.get("items"):
        return None
    invoice = result["items"][0]
    # payload задан в create_invoice: "<user_id>:<plan>"
    payer_id, _, plan = (invoice.get("payload") or "").partition(":")
    if invoice.get("status") != "paid" or payer_id != str(user_id) or plan not in PLANS:
        logger.info("Счёт %s не засчитан: status=%s, payload=%s", invoice_id, invoice.get("status"), invoice.get("payload"))
        return None
    return plan
