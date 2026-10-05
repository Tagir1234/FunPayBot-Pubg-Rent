"""Автоподнятие лотов: отдельный поток, который дёргает account.raise_lots().

Логика одного круга:
    поднял -> спим столько, сколько вернул FunPay;
    кулдаун (RaiseError.wait_time) -> спим ровно wait_time;
    wait_time пуст -> спим DEFAULT_COOLDOWN_MINUTES;
    любая другая ошибка -> логируем, спим ERROR_RETRY_MINUTES и пробуем снова.

Поток демонический и никогда не выходит по исключению: упавший раздел не должен
ронять бот целиком.
"""

from __future__ import annotations

import logging
import threading
import time

from FunPayAPI import Account
from FunPayAPI.common import exceptions

from config import Config

logger = logging.getLogger("raiser")

# Длинные паузы спим кусками: так поток быстро реагирует на stop() и успевает
# вовремя обновить сессию, даже если FunPay выдал кулдаун на 2 часа.
_SLEEP_CHUNK_SECONDS = 15


def _human(seconds: float) -> str:
    """Секунды -> «13 мин 20 с» для читаемых логов."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


class LotRaiser:
    """Поднимает лоты одной категории (игры) в фоновом потоке."""

    def __init__(self, account: Account, cfg: Config) -> None:
        self.account = account
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_safe, name="lot-raiser", daemon=True)
        # Сессия только что получена вызовом account.get() в main.
        self._last_session_refresh = time.monotonic()

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        category = self.account.get_category(self.cfg.category_id)
        name = category.name if category else "неизвестная категория"
        logger.info(
            "Автоподнятие запущено: категория %s «%s», обновление сессии каждые %d мин.",
            self.cfg.category_id, name, self.cfg.session_refresh_minutes,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------------ внутреннее

    def _run_safe(self) -> None:
        """Обёртка последней надежды: поток не должен умереть молча."""
        try:
            self._run()
        except BaseException:  # noqa: BLE001 — логируем всё, включая неожиданное
            logger.exception("Поток автоподнятия аварийно завершился.")
        else:
            logger.info("Поток автоподнятия остановлен.")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sleep(self._raise_once())

    def _raise_once(self) -> float:
        """Одна попытка поднятия. Возвращает, сколько секунд спать до следующей."""
        try:
            wait_time = self.account.raise_lots(self.cfg.category_id)
        except exceptions.RaiseError as e:
            return self._on_cooldown(e)
        except exceptions.UnauthorizedError:
            logger.error(
                "FunPay не признал сессию (golden_key протух или сменился User-Agent). "
                "Повтор через %d мин.", self.cfg.error_retry_minutes,
            )
            return self.cfg.error_retry_minutes * 60
        except Exception as e:  # сеть, 5xx, неожиданный HTML — что угодно
            logger.error(
                "Ошибка при поднятии лотов: %s: %s. Повтор через %d мин.",
                type(e).__name__, e, self.cfg.error_retry_minutes,
            )
            return self.cfg.error_retry_minutes * 60

        sleep_for = wait_time if wait_time else self.cfg.default_cooldown_minutes * 60
        logger.info("Лоты подняты. Следующее поднятие через %s.", _human(sleep_for))
        return sleep_for

    def _on_cooldown(self, e: exceptions.RaiseError) -> float:
        """RaiseError — это и «ещё рано», и настоящая ошибка. Различаем по wait_time."""
        if e.wait_time:
            logger.info(
                "Кулдаун: поднимать ещё рано, ждём %s.%s",
                _human(e.wait_time),
                f" FunPay: {e.error_message}" if e.error_message else "",
            )
            return e.wait_time

        logger.warning(
            "FunPay отказал в поднятии без времени ожидания%s. Спим %d мин.",
            f": {e.error_message}" if e.error_message else "",
            self.cfg.default_cooldown_minutes,
        )
        return self.cfg.default_cooldown_minutes * 60

    # ------------------------------------------------------------------ сон и сессия

    def _sleep(self, seconds: float) -> None:
        """Спит указанное время, попутно обновляя сессию и слушая stop()."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            self._refresh_session_if_needed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(_SLEEP_CHUNK_SECONDS, remaining))

    def _refresh_session_if_needed(self) -> None:
        """account.get() раз в SESSION_REFRESH_MINUTES — так советует документация библиотеки."""
        period = self.cfg.session_refresh_minutes * 60
        if time.monotonic() - self._last_session_refresh < period:
            return
        try:
            self.account.get(update_phpsessid=True)
        except Exception as e:  # обновление не удалось — не повод ронять поток
            logger.warning(
                "Не смог обновить сессию (%s: %s). Попробую на следующем круге.",
                type(e).__name__, e,
            )
            # Сдвигаем таймер на минуту, чтобы не долбить FunPay каждые 15 секунд.
            self._last_session_refresh = time.monotonic() - period + 60
            return
        self._last_session_refresh = time.monotonic()
        logger.info("Сессия обновлена (account.get()).")
