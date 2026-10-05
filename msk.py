"""Московское время. Все расчёты аренды и всё, что видят покупатели и продавец, — по МСК,
независимо от часового пояса компьютера, на котором запущен бот.

В Москве нет перехода на летнее время, поэтому если zoneinfo недоступен (на Windows без
пакета tzdata), фиксированное смещение UTC+3 даёт тот же результат.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # ZoneInfoNotFoundError без tzdata, ImportError на совсем старом Python
    MSK = timezone(timedelta(hours=3), "MSK")


def now_msk() -> datetime:
    """Сейчас по Москве (aware datetime)."""
    return datetime.now(MSK)


def to_msk(moment: datetime) -> datetime:
    """Любое время -> МСК. Время без пояса считается московским (так писали старые версии)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def parse_hhmm(raw: str) -> time:
    """«20:00» -> time(20, 0). ValueError, если формат не тот."""
    hours, minutes = raw.strip().split(":")
    return time(int(hours), int(minutes))
