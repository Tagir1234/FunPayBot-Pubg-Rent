"""Ночной тариф: окно NIGHT_START–NIGHT_END по МСК и расчёт конца ночной аренды.

Окно может переходить через полночь (20:00–06:00) — это основной случай.
Чистые функции без сети: их гоняет test_night.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from msk import to_msk


@dataclass(frozen=True)
class NightSettings:
    start: time
    end: time
    name: str
    # Если до конца окна меньше — аренду не выдаём молча, спрашиваем продавца.
    min_hours: int

    def inside(self, moment: datetime) -> bool:
        """Попадает ли момент (по МСК) в ночное окно."""
        t = to_msk(moment).time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_end(self, moment: datetime) -> datetime:
        """Ближайший NIGHT_END строго после момента: в 21:30 — завтра 06:00, в 03:00 — сегодня."""
        now = to_msk(moment)
        candidate = now.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)

    def relevant_start(self, moment: datetime) -> datetime:
        """С какого момента считается «эта ночь»: сейчас, если окно уже идёт, иначе ближайший старт."""
        now = to_msk(moment)
        if self.inside(now):
            return now
        candidate = now.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)


@dataclass(frozen=True)
class NightRental:
    until: datetime
    # True — выдавать только после «Да» продавца в Telegram.
    needs_confirm: bool
    # Почему нужно подтверждение (для лога и теста).
    reason: str = ""


def night_rental(settings: NightSettings, moment: datetime) -> NightRental:
    """Конец ночной аренды, выданной в момент moment.

    Внутри окна — до ближайшего NIGHT_END. Вне окна (днём) или когда до конца окна
    меньше MIN_RENTAL_HOURS — тот же ближайший NIGHT_END, но с подтверждением продавца.
    """
    now = to_msk(moment)
    until = settings.next_end(now)
    if not settings.inside(now):
        return NightRental(until, True, "вне ночного окна")
    if until - now < timedelta(hours=settings.min_hours):
        return NightRental(until, True, f"до конца окна меньше {settings.min_hours} ч")
    return NightRental(until, False)
