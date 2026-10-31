"""
Работа с базой данных SQLite.

Таблицы:
- raw_messages — «сырые» сообщения от Агента №1 (сборщика)
- events       — отфильтрованные события от Агента №2 (фильтра)
- user_preferences — темы пользователей бота (/settings)
- user_limits  — дневной лимит запросов к ИИ для каждого пользователя
- feedback     — ответы пользователей на опрос /feedback
- users        — все пользователи бота и дата первого визита (для /stats)
- payments     — оплаченные счета Crypto Bot (/subscribe); каждый счёт продлевает доступ только один раз
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date

from config import DATABASE_PATH, TRIAL_DAYS

logger = logging.getLogger(__name__)


@contextmanager
def get_connection():
    """
    Открывает соединение с базой и гарантированно закрывает его.
    При успехе — commit, при ошибке — rollback.
    (Обычный "with sqlite3.connect()" делает только commit, но НЕ закрывает соединение.)
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # строки можно читать как словари: row["text"]
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Ошибка при работе с базой данных")
        raise
    finally:
        conn.close()


def init_db():
    """Создаёт таблицы, если их ещё нет."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT,
                source TEXT,
                text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0,
                url TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                summary TEXT,
                category TEXT,
                source TEXT,
                insider_score INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                category_tags TEXT,
                sentiment TEXT,
                entities TEXT,
                url TEXT
            )
            """
        )

        # Миграция для старой базы: добавляем колонки, которых ещё нет.
        # (CREATE TABLE IF NOT EXISTS не меняет уже существующую таблицу)
        # url — ссылка на оригинальную статью или пост (у старых записей её нет)
        for table, new_columns in [
            ("events", ["category_tags", "sentiment", "entities", "url"]),
            ("raw_messages", ["url"]),
        ]:
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
            for column in new_columns:
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                    logger.info("В таблицу %s добавлена колонка %s", table, column)

        # Настройки пользователей бота: какие темы (теги) им интересны
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                topics TEXT DEFAULT 'all'
            )
            """
        )

        # Дневной лимит запросов к ИИ: сколько запросов сделал пользователь и в какой день
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER PRIMARY KEY,
                request_count INTEGER DEFAULT 0,
                last_request_date DATE
            )
            """
        )

        # Ответы на опрос /feedback.
        # is_interested: "yes" / "no"; will_pay: "yes" / "free" (только бесплатно)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                is_interested TEXT,
                will_pay TEXT,
                suggestions TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Оплаченные счета: invoice_id — PRIMARY KEY, поэтому повторное «Я оплатил» не продлит доступ дважды
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER,
                plan TEXT,
                paid_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Все, кто хоть раз писал боту или нажимал кнопку (для /stats).
        # first_seen и last_seen — в UTC, как created_at в events
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # last_context — последний текст новостей или ответа ИИ, отправленный пользователю (для /theory).
        # Колонка появилась позже, поэтому для старой базы добавляем её отдельно
        # trial_end_date — конец пробного периода (UTC), paid_until — конец оплаченной подписки (UTC)
        user_columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
        for column, column_type in [
            ("last_context", "TEXT"),
            ("trial_end_date", "DATETIME"),
            ("paid_until", "DATETIME"),
        ]:
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {column_type}")
                logger.info("В таблицу users добавлена колонка %s", column)
        # Пользователей, которые были до появления таблицы users, переносим из других таблиц.
        # Когда они пришли впервые — неизвестно, поэтому first_seen = NULL (в «новых сегодня» не попадут).
        # Эти таблицы уже созданы выше
        for table in ["user_limits", "user_preferences", "feedback"]:
            conn.execute(
                f"INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) "
                f"SELECT DISTINCT user_id, NULL, NULL FROM {table} WHERE user_id IS NOT NULL"
            )
        # Пользователи, пришедшие до появления пробного периода, получают полные TRIAL_DAYS с этого момента
        started = conn.execute(
            "UPDATE users SET trial_end_date = datetime('now', ?) WHERE trial_end_date IS NULL",
            (f"+{TRIAL_DAYS} days",),
        ).rowcount
        if started:
            logger.info("Пробный период на %d дн. начат для существующих пользователей: %d", TRIAL_DAYS, started)
    logger.info("База данных инициализирована: %s", DATABASE_PATH)


def save_raw_message(source_type, source, text, url=None):
    """
    Сохраняет «сырое» сообщение. source_type: 'telegram' или 'rss'. Возвращает id записи.
    url — ссылка на оригинал: статья из RSS или пост в Telegram-канале.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO raw_messages (source_type, source, text, url) VALUES (?, ?, ?, ?)",
            (source_type, source, text, url),
        )
        new_id = cursor.lastrowid
    logger.debug("Сохранено сообщение id=%s из %s (%s)", new_id, source, source_type)
    return new_id


def message_exists(source, text):
    """Проверяет, есть ли уже такое сообщение (нужно, чтобы не сохранять дубли из RSS)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM raw_messages WHERE source = ? AND text = ? LIMIT 1",
            (source, text),
        ).fetchone()
    return row is not None


def get_unprocessed_messages(limit=100):
    """Возвращает список необработанных сообщений (словари), от старых к новым."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM raw_messages WHERE processed = 0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    messages = [dict(row) for row in rows]
    logger.debug("Найдено необработанных сообщений: %d", len(messages))
    return messages


def mark_as_processed(ids):
    """Помечает сообщения с указанными id как обработанные."""
    if not ids:
        return
    with get_connection() as conn:
        conn.executemany(
            "UPDATE raw_messages SET processed = 1 WHERE id = ?",
            [(message_id,) for message_id in ids],
        )
    logger.debug("Помечено как обработанные: %d сообщений", len(ids))


def clean_entity_name(name):
    """
    Запятая — разделитель в колонке entities, поэтому внутри имени её быть не должно.
    Заодно убираем лишние пробелы: "Coinbase, Inc." -> "Coinbase Inc."
    """
    return " ".join(str(name).replace(",", " ").split())


def save_event(
    title, summary, category, source, insider_score, category_tags=None, sentiment=None, entities=None, url=None
):
    """
    Сохраняет отфильтрованное событие. Возвращает id записи.
    insider_score — важность новости от 1 до 10 (шкала в промпте agent_2_filter.py).
    url — ссылка на оригинальную статью.
    category_tags — список тегов, например ["defi", "ethereum"].
    В базе хранится строкой через запятую: "defi,ethereum".
    sentiment — тональность: "positive", "negative" или "neutral".
    entities — список сущностей, например ["BlackRock", "Larry Fink"].
    В базе хранится строкой через запятую: "BlackRock,Larry Fink".
    """
    tags_text = ",".join(category_tags or [])
    entities_text = ",".join(clean_entity_name(name) for name in (entities or []))
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO events (title, summary, category, source, insider_score, category_tags, sentiment, entities, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, summary, category, source, insider_score, tags_text, sentiment, entities_text, url),
        )
        new_id = cursor.lastrowid
    logger.info("Сохранено событие id=%s [%s, %s]: %s", new_id, tags_text, sentiment, title)
    return new_id


def get_recent_events(limit=20):
    """Возвращает последние события (словари, включая category_tags), от новых к старым."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    events = [dict(row) for row in rows]
    logger.debug("Загружено последних событий: %d", len(events))
    return events


def get_events_by_tag(tag, limit=5):
    """Возвращает последние события с указанным тегом (например, "defi"), от новых к старым."""
    # Оборачиваем теги запятыми: ",defi,ethereum," — так "defi" не совпадёт случайно с частью другого тега
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE ',' || category_tags || ',' LIKE '%,' || ? || ',%'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (tag, limit),
        ).fetchall()
    events = [dict(row) for row in rows]
    logger.debug("Событий с тегом %s: %d", tag, len(events))
    return events


def get_events_by_entity(entity_name, limit=10):
    """
    Ищет последние события, где среди entities есть entity_name (без учёта регистра).
    Сначала ищем точное совпадение имени ("Binance"), если ничего нет —
    частичное ("Binance" найдёт "Binance Labs").
    Важно: LOWER() в SQLite понижает регистр только у латиницы — имена храним на английском.
    """
    # Запрос чистим так же, как имена при сохранении
    name = clean_entity_name(entity_name).lower()
    if not name:
        return []

    exact_query = """
        SELECT * FROM events
        WHERE ',' || LOWER(entities) || ',' LIKE '%,' || ? || ',%'
        ORDER BY created_at DESC, id DESC LIMIT ?
    """
    partial_query = """
        SELECT * FROM events
        WHERE LOWER(entities) LIKE '%' || ? || '%'
        ORDER BY created_at DESC, id DESC LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(exact_query, (name, limit)).fetchall()
        if not rows:
            rows = conn.execute(partial_query, (name, limit)).fetchall()
    events = [dict(row) for row in rows]
    logger.debug("Событий с сущностью %s: %d", entity_name, len(events))
    return events


def get_recent_titles(hours=24, limit=80):
    """Заголовки событий за последние hours часов (не больше limit, самые свежие) — для защиты от дублей."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT title FROM events
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (f"-{hours} hours", limit),
        ).fetchall()
    return [row["title"] for row in rows]


def get_events_since(days=7):
    """Все события за последние days дней, от новых к старым."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC, id DESC
            """,
            (f"-{days} days",),
        ).fetchall()
    events = [dict(row) for row in rows]
    logger.debug("Событий за %d дн.: %d", days, len(events))
    return events


def get_events_by_topics(topics_list, hours=24, limit=50):
    """Новости за последние hours часов, у которых есть хотя бы один тег из topics_list."""
    if not topics_list:
        return []
    # Для каждого тега — своё условие, объединяем через OR
    tag_condition = " OR ".join(["',' || category_tags || ',' LIKE '%,' || ? || ',%'"] * len(topics_list))
    query = f"""
        SELECT * FROM events
        WHERE created_at >= datetime('now', ?) AND ({tag_condition})
        ORDER BY created_at DESC, id DESC LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(query, [f"-{hours} hours", *topics_list, limit]).fetchall()
    events = [dict(row) for row in rows]
    logger.debug("Событий по темам %s за %d ч: %d", topics_list, hours, len(events))
    return events


def set_user_preferences(user_id, topics):
    """
    Сохраняет темы пользователя.
    topics — список тегов (["bitcoin", "defi"]) или "all" (все новости).
    Пустой список тоже означает "all".
    """
    topics_text = "all" if topics == "all" or not topics else ",".join(topics)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, topics) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET topics = excluded.topics
            """,
            (user_id, topics_text),
        )
    logger.info("Темы пользователя %s: %s", user_id, topics_text)


def get_user_preferences(user_id):
    """Возвращает темы пользователя строкой: 'all' или, например, 'bitcoin,defi'."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT topics FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["topics"] if row and row["topics"] else "all"


def clean_old_events(days=7):
    """
    Удаляет из raw_messages и events записи старше days дней.
    Возвращает общее количество удалённых записей.
    """
    # timestamp и created_at хранятся в UTC, datetime('now') — тоже UTC
    period = f"-{days} days"
    with get_connection() as conn:
        raw_deleted = conn.execute(
            "DELETE FROM raw_messages WHERE timestamp < datetime('now', ?)", (period,)
        ).rowcount
        events_deleted = conn.execute(
            "DELETE FROM events WHERE created_at < datetime('now', ?)", (period,)
        ).rowcount
    total = raw_deleted + events_deleted
    logger.info(
        "Очистка базы (старше %d дн.): удалено сообщений %d, событий %d, всего %d",
        days, raw_deleted, events_deleted, total,
    )
    return total


def check_and_update_limit(user_id, limit):
    """
    Проверяет дневной лимит запросов к ИИ и засчитывает новый запрос.
    True — запрос можно делать (счётчик уже увеличен), False — лимит на сегодня исчерпан.
    Новый день (или первая запись) — счётчик начинается заново с 1.
    """
    # Дата по часам компьютера, где запущен бот: лимит обновляется в местную полночь
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT request_count, last_request_date FROM user_limits WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row is None or row["last_request_date"] != today:
            count = 1
        elif row["request_count"] < limit:
            count = row["request_count"] + 1
        else:
            logger.info("Лимит ИИ исчерпан: user_id=%s (%d/%d)", user_id, row["request_count"], limit)
            return False

        conn.execute(
            """
            INSERT INTO user_limits (user_id, request_count, last_request_date) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                request_count = excluded.request_count,
                last_request_date = excluded.last_request_date
            """,
            (user_id, count, today),
        )
    logger.info("Запрос к ИИ: user_id=%s (%d/%d)", user_id, count, limit)
    return True


def save_feedback(user_id, is_interested, will_pay, suggestions):
    """
    Сохраняет ответ на опрос /feedback. Возвращает id записи.
    is_interested — "yes" или "no", will_pay — "yes" или "free".
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO feedback (user_id, is_interested, will_pay, suggestions) VALUES (?, ?, ?, ?)",
            (user_id, is_interested, will_pay, suggestions),
        )
        new_id = cursor.lastrowid
    logger.info(
        "Отзыв id=%s от user_id=%s: интересно=%s, платить=%s", new_id, user_id, is_interested, will_pay
    )
    return new_id


def register_user(user_id):
    """
    Отмечает визит пользователя: у старого обновляется last_seen,
    новый добавляется и сразу получает пробный период на TRIAL_DAYS дней.
    Вызывается перед любой командой, поэтому триал начинается с первого сообщения (обычно это /start).
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, trial_end_date) VALUES (?, datetime('now', ?))
            ON CONFLICT(user_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP
            """,
            (user_id, f"+{TRIAL_DAYS} days"),
        )


def get_trial_end_date(user_id):
    """
    Конец пробного периода пользователя: "ГГГГ-ММ-ДД ЧЧ:ММ:СС" (UTC).
    Пользователя нет в базе или у него нет даты (запись создана в обход register_user) — начинаем триал сейчас.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT trial_end_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or row["trial_end_date"] is None:
            conn.execute(
                """
                INSERT INTO users (user_id, trial_end_date) VALUES (?, datetime('now', ?))
                ON CONFLICT(user_id) DO UPDATE SET trial_end_date = excluded.trial_end_date
                """,
                (user_id, f"+{TRIAL_DAYS} days"),
            )
            row = conn.execute("SELECT trial_end_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row["trial_end_date"]


def get_paid_until(user_id):
    """Конец оплаченной подписки "ГГГГ-ММ-ДД ЧЧ:ММ:СС" (UTC) или None, если пользователь не платил."""
    with get_connection() as conn:
        row = conn.execute("SELECT paid_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row["paid_until"] if row else None


def activate_subscription(invoice_id, user_id, plan, days):
    """
    Засчитывает оплаченный счёт: продлевает доступ на days дней.
    Отсчёт — от самой поздней даты из «сейчас», конца подписки и конца пробного периода,
    чтобы оплаченные дни не сгорали. Возвращает новый paid_until или None, если счёт уже был засчитан.
    """
    with get_connection() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO payments (invoice_id, user_id, plan) VALUES (?, ?, ?)",
            (str(invoice_id), user_id, plan),
        ).rowcount
        if not inserted:
            return None
        # Даты в базе — строки "ГГГГ-ММ-ДД ЧЧ:ММ:СС", их можно сравнивать как текст; NULL -> ''
        conn.execute(
            """
            UPDATE users SET paid_until = datetime(
                max(datetime('now'), coalesce(paid_until, ''), coalesce(trial_end_date, '')), ?
            ) WHERE user_id = ?
            """,
            (f"+{days} days", user_id),
        )
        row = conn.execute("SELECT paid_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
    logger.info("Подписка %s по счёту %s: user_id=%s, доступ до %s", plan, invoice_id, user_id, row["paid_until"])
    return row["paid_until"]


def save_last_context(user_id, text):
    """Запоминает последний текст, который бот отправил пользователю (новости или ответ ИИ)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, last_context) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_context = excluded.last_context
            """,
            (user_id, text),
        )


def get_last_context(user_id):
    """Последний сохранённый текст для пользователя или пустая строка."""
    with get_connection() as conn:
        row = conn.execute("SELECT last_context FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return (row["last_context"] if row else None) or ""


def get_feedbacks(limit=20):
    """Последние limit отзывов (словари), от новых к старым — для /feedbacks."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, is_interested, will_pay, suggestions, created_at FROM feedback "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_feedback_summary():
    """
    Сводка по ВСЕМ отзывам (а не только по последним):
    {"total": N, "interested": {"yes": X, "no": Y}, "pay": {"yes": A, "free": B}}.
    """
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        interested = dict(conn.execute("SELECT is_interested, COUNT(*) FROM feedback GROUP BY is_interested").fetchall())
        pay = dict(conn.execute("SELECT will_pay, COUNT(*) FROM feedback GROUP BY will_pay").fetchall())
    return {"total": total, "interested": interested, "pay": pay}


def get_total_users():
    """Сколько всего уникальных пользователей было у бота."""
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_new_users_today():
    """Сколько пользователей впервые появились сегодня (сегодня — по часам этого компьютера)."""
    # first_seen в UTC, 'localtime' переводит его в местное время перед сравнением дат
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE date(first_seen, 'localtime') = date('now', 'localtime')"
        ).fetchone()[0]


def get_ai_requests_today():
    """Сколько запросов к ИИ сделали все пользователи сегодня (по таблице user_limits)."""
    # Счётчик в user_limits сбрасывается каждый день, поэтому берём только записи с сегодняшней датой
    with get_connection() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(request_count), 0) FROM user_limits WHERE last_request_date = ?",
            (date.today().isoformat(),),
        ).fetchone()[0]


def get_sentiment_stats(tag=None, hours=24):
    """
    Считает, сколько событий каждой тональности было за последние hours часов.
    Если tag указан — только события с этим тегом.
    Возвращает словарь, например {"positive": 6, "negative": 2, "neutral": 2}.
    События без тональности (сохранённые до её появления) не учитываются.
    """
    # datetime('now') в SQLite — это UTC, как и CURRENT_TIMESTAMP в created_at
    query = """
        SELECT sentiment, COUNT(*) AS count FROM events
        WHERE created_at >= datetime('now', ?)
          AND sentiment IN ('positive', 'negative', 'neutral')
    """
    params = [f"-{hours} hours"]
    if tag:
        query += " AND ',' || category_tags || ',' LIKE '%,' || ? || ',%'"
        params.append(tag)
    query += " GROUP BY sentiment"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    stats = {"positive": 0, "negative": 0, "neutral": 0}
    for row in rows:
        stats[row["sentiment"]] = row["count"]
    logger.debug("Тональность (тег=%s, %d ч): %s", tag, hours, stats)
    return stats
