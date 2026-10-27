# AI Aggregator

Telegram-бот, который собирает крипто-новости из Telegram-каналов и RSS-лент,
фильтрует их с помощью ИИ (OpenAI GPT) и отвечает на вопросы пользователя.

## Как это работает

```
[Telegram-каналы + RSS] → Агент 1 (сборщик) → Агент 2 (GPT-фильтр) → SQLite → Агент 3 (ассистент) → Telegram-бот
```

| Файл | Что делает |
|------|------------|
| `config.py` | Настройки из `.env`, логирование, списки источников |
| `database.py` | Работа с базой SQLite |
| `agent_1_scraper.py` | Агент 1: собирает сообщения из Telegram-каналов и RSS |
| `agent_2_filter.py` | Агент 2: GPT убирает мусор и дубли, ставит категорию, теги, тональность и сущности |
| `agent_3_assistant.py` | Агент 3: ответы на вопросы и брифинг за 24 часа |
| `agent_4_researcher.py` | Агент 4: подробный разбор вопроса по новостям за 7 дней (`/research`) |
| `agent_5_teacher.py` | Агент 5: учебная справка — 2-3 предложения о DeFi-термине (`/theory`) |
| `bot.py` | Telegram-бот (aiogram 3) |
| `main.py` | Запускает всех агентов и бота вместе |

## Команды бота

**📰 Новости**
- `/news` — последние новости (все подряд)
- `/top` — только самые важные новости (с высоким рейтингом)
- `/btc`, `/eth`, `/sol`, `/defi` — новости по теме

**🧠 ИИ-Аналитика**
- `/briefing` — главное за 24 часа: ключевые события и тренд
- `/sentiment btc` — настроение рынка за 24 часа (темы: btc, eth, sol, defi; без темы — по всем новостям)
- `/entity AAVE` — новости по монете (15 тикеров: BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, MATIC, UNI, AAVE, LDO, ARB, OP)
- `/research вопрос` — подробный ответ по новостям за 7 дней
- Или просто напишите вопрос, например: «Что нового с Bitcoin?»

**📖 Обучение**
- `/theory` — учебная справка по DeFi: термин из последней новости или `/theory AMM`
- Учебник и симулятор DeFi Compass — на сайте (`LANDING_URL`)

**⚙️ Настройки**
- `/settings` — темы для `/briefing`
- `/feedback` — отзыв о боте
- `/help` — справка и меню кнопок

**⏳ Доступ**
- Новый пользователь получает 7 дней полного доступа (`TRIAL_DAYS` в `config.py`).
- После пробного периода закрыты `/top`, `/briefing`, `/research`, `/theory` и вопросы к ИИ;
  `/news`, темы, `/sentiment`, `/entity` и настройки остаются открытыми, бот просит оставить отзыв (`/feedback`).

## Установка и запуск

1. Создайте виртуальное окружение и установите зависимости:

   ```
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Создайте файл `.env` в папке проекта:

   ```
   TELEGRAM_API_ID=...        # с my.telegram.org
   TELEGRAM_API_HASH=...      # с my.telegram.org
   TELEGRAM_BOT_TOKEN=...     # от @BotFather (формат 123456:ABC...)
   OPENAI_API_KEY=...
   # необязательно:
   DATABASE_PATH=aggregator.db
   OPENAI_MODEL=gpt-4o-mini
   LOG_LEVEL=INFO
   ALLOWED_USER_IDS=          # Telegram id через запятую (пусто = всем)
   DAILY_AI_LIMIT=15          # запросов к ИИ в день на пользователя
   ADMIN_ID=                  # Telegram id администратора (/stats)
   LANDING_URL=               # сайт с учебником (по умолчанию — главная страница Notion)
   ```

3. Запустите проект:

   ```
   python main.py
   ```

   При первом запуске Telethon попросит номер телефона и код из Telegram:
   сборщик читает каналы от имени вашего аккаунта (не бота).
   Сессия сохранится в файл `scraper.session`.

Логи пишутся в консоль и в файл `app.log`.

> Файлы `.env`, `*.session`, `*.db` и `*.log` не попадают в Git: в них ключи, личная сессия Telegram и данные.
