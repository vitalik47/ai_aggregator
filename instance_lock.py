"""
Защита от двойного запуска.

Две копии бота одновременно — это ошибки TelegramConflictError (бот отвечает через раз),
два фильтра на одних и тех же сообщениях (двойной расход OpenAI и дубли в базе)
и спор двух сборщиков за файл scraper.session.

Замок — файл ai_aggregator.lock, заблокированный средствами ОС. Если процесс упал или его закрыли,
ОС снимает блокировку сама, поэтому «зависшего» замка после аварии не бывает.
"""

import logging
import os
import sys

from config import BASE_DIR

logger = logging.getLogger(__name__)

LOCK_PATH = os.path.join(BASE_DIR, "ai_aggregator.lock")

# Открытый файл держим до конца работы программы: пока он открыт, замок занят
_lock_file = None


def _try_lock(file):
    """Блокирует первый байт файла, не дожидаясь. False — файл уже заблокирован другим процессом."""
    try:
        if sys.platform == "win32":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def acquire_instance_lock(name):
    """
    Занимает замок для этого процесса. name — что запускается (для сообщения в логе).
    Если уже работает другая копия (main.py, bot.py или агент) — пишет об этом и завершает программу.
    """
    global _lock_file
    file = open(LOCK_PATH, "a+")
    if not _try_lock(file):
        file.close()
        message = (
            f"{name}: AI Aggregator уже запущен в другом окне. "
            "Закройте его (Ctrl+C) и запустите заново — две копии мешают друг другу."
        )
        logger.error(message)
        print(message)
        sys.exit(1)
    _lock_file = file
    logger.info("%s: замок от двойного запуска получен (%s)", name, LOCK_PATH)
