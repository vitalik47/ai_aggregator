"""
Работа с базой данных SQLite.

Таблицы:
- raw_messages — «сырые» сообщения от Агента №1 (сборщика)
- events       — отфильтрованные события от Агента №2 (фильтра)
"""

import logging
import sqlite3
from contextlib import contextmanager

from config import DATABASE_PATH

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
                processed INTEGER DEFAULT 0
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
                entities TEXT
            )
            """
        )

        # Миграция для старой базы: добавляем колонки, которых ещё нет.
        # (CREATE TABLE IF NOT EXISTS не меняет уже существующую таблицу)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(events)")]
        for column in ["category_tags", "sentiment", "entities"]:
            if column not in columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
                logger.info("В таблицу events добавлена колонка %s", column)

        # Настройки пользователей бота: какие темы (теги) им интересны
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                topics TEXT DEFAULT 'all'
            )
            """
        )
    logger.info("База данных инициализирована: %s", DATABASE_PATH)


def save_raw_message(source_type, source, text):
    """Сохраняет «сырое» сообщение. source_type: 'telegram' или 'rss'. Возвращает id записи."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO raw_messages (source_type, source, text) VALUES (?, ?, ?)",
            (source_type, source, text),
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


def save_event(title, summary, category, source, insider_score, category_tags=None, sentiment=None, entities=None):
    """
    Сохраняет отфильтрованное событие. Возвращает id записи.
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
            INSERT INTO events (title, summary, category, source, insider_score, category_tags, sentiment, entities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, summary, category, source, insider_score, tags_text, sentiment, entities_text),
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
