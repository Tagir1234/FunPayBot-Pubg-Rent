"""Синхронизация своих лотов с FunPay в lots_cache.json.

Раз в LOT_SYNC_MINUTES (triggers.json) поток забирает свои лоты: название, описание, цену,
ссылку — и размечает тариф. Автоответчик читает только кэш и в сеть за лотами не ходит,
поэтому ответ покупателю не ждёт FunPay, а упавшая синхронизация не ломает ответы:
остаётся последний удачный кэш.

Ночной лот (tariff = "night") — ТОЛЬКО если в названии есть словосочетание «ночной тариф».
Описание не смотрим: там слова «ночь», «до утра» встречаются и у обычных лотов, и из-за
этого почасовые лоты ошибочно считались ночными. Срока по умолчанию у ночного лота нет:
срок задаёт окно NIGHT_START–NIGHT_END. Но если в названии явно указаны часы
(«Ночной тариф 8 часов»), они главнее окна (поле hours).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from FunPayAPI import Account

from msk import now_msk

logger = logging.getLogger("lots")

CACHE_PATH = Path(__file__).resolve().parent / "lots_cache.json"

TARIFF_NIGHT = "night"
TARIFF_HOURLY = "hourly"

# Пауза между запросами страниц лотов: описание есть только на странице лота.
_PAGE_DELAY_SECONDS = 1.0

# Только словосочетание «ночной тариф» (любые пробелы между словами, регистр не важен).
_NIGHT_RE = re.compile(r"ночной\s+тариф", re.IGNORECASE)
# Явно указанные часы в названии: «8 часов», «8ч», «8 ч.».
_HOURS_RE = re.compile(r"(\d{1,2})\s*(?:ч\b|час)", re.IGNORECASE)
# Номер аккаунта в названии: «Акк 2», «аккаунт №2», «#2».
_ACCOUNT_NUMBER_RE = re.compile(r"(?:\bак[а-я]*\s*(?:№|номер)?\s*|№\s*|#\s*)(\d{1,3})\b", re.IGNORECASE)


def _norm(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def is_night_lot(title: str | None, description: str | None = None) -> bool:
    """Ночной — только по названию. description принимается, но намеренно не используется."""
    return bool(_NIGHT_RE.search(_norm(title)))


def explicit_hours(title: str | None) -> int | None:
    m = _HOURS_RE.search(_norm(title))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def account_number(title: str | None) -> int | None:
    m = _ACCOUNT_NUMBER_RE.search(_norm(title))
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class CachedLot:
    id: int
    title: str
    description: str
    price: float
    currency: str
    link: str
    tariff: str
    # Только для ночного: часы из названия. None — срок задаёт ночное окно.
    hours: int | None = None
    # Номер аккаунта из названия («Акк 2»). None — в названии номера нет.
    account_number: int | None = None

    @property
    def is_night(self) -> bool:
        return self.tariff == TARIFF_NIGHT

    def price_text(self) -> str:
        """99.876 -> «99.88», 150.0 -> «150»."""
        return f"{self.price:.2f}".rstrip("0").rstrip(".")


def make_lot(id_: int, title: str | None, description: str | None, price: float,
             currency: str, link: str) -> CachedLot:
    night = is_night_lot(title, description)
    return CachedLot(
        id=int(id_), title=title or "", description=description or "", price=float(price),
        currency=currency, link=link, tariff=TARIFF_NIGHT if night else TARIFF_HOURLY,
        hours=explicit_hours(title) if night else None,
        account_number=account_number(title),
    )


# ---------------------------------------------------------------------- кэш


def load_cache() -> list[CachedLot]:
    """Лоты из lots_cache.json. Пустой список при любой ошибке — ответ покупателю важнее."""
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Не смог прочитать %s (%s) — считаю, что лотов нет.", CACHE_PATH, e)
        return []
    lots = []
    for item in raw.get("lots", []) if isinstance(raw, dict) else []:
        try:
            lots.append(CachedLot(**{k: item.get(k) for k in CachedLot.__dataclass_fields__}))
        except (TypeError, ValueError) as e:
            logger.error("Пропускаю запись в %s (%s): %r", CACHE_PATH, e, item)
    return lots


def night_lots(lots: list[CachedLot] | None = None) -> list[CachedLot]:
    return [lot for lot in (load_cache() if lots is None else lots) if lot.is_night]


def save_cache(lots: list[CachedLot]) -> None:
    """Атомарная запись, как в accounts_db: обрезанный JSON хуже старого."""
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    data = {
        "_readme": "Пишется ботом (lot_sync.py) — руками не правь, перезапишется.",
        "updated_at": now_msk().isoformat(sep=" ", timespec="seconds"),
        "lots": [asdict(lot) for lot in lots],
    }
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        logger.error("Не смог сохранить %s: %s", CACHE_PATH, e)
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- поток


class LotSync:
    """Раз в N минут обновляет lots_cache.json. Ошибка сети — не повод падать."""

    def __init__(self, account: Account, interval_minutes: int) -> None:
        self.account = account
        self.interval = interval_minutes * 60
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_safe, name="lot-sync", daemon=True)

    def start(self) -> None:
        logger.info("Синхронизация лотов запущена: раз в %d мин.", self.interval // 60)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run_safe(self) -> None:
        try:
            while not self._stop.is_set():
                self.sync_once()
                self._stop.wait(self.interval)
        except BaseException:  # noqa: BLE001
            logger.exception("Поток синхронизации лотов аварийно завершился.")
        else:
            logger.info("Поток синхронизации лотов остановлен.")

    def sync_once(self) -> list[CachedLot] | None:
        try:
            shortcuts = self.account.get_user(self.account.id).get_lots()
        except Exception as e:
            logger.error("Не смог получить свои лоты: %s: %s. Оставляю старый кэш.", type(e).__name__, e)
            return None

        lots = []
        for lot in shortcuts:
            if self._stop.is_set():
                return None
            description = ""
            try:
                page = self.account.get_lot_page(lot.id)
                description = page.full_description or ""
            except Exception as e:
                # Без описания всё равно сохраним: тариф определится по названию.
                logger.warning("Не смог открыть страницу лота %s: %s: %s", lot.id, type(e).__name__, e)
            currency = getattr(lot.currency, "name", str(lot.currency or ""))
            lots.append(make_lot(lot.id, lot.title, description, lot.price, currency, lot.public_link))
            self._stop.wait(_PAGE_DELAY_SECONDS)

        save_cache(lots)
        night = [lot for lot in lots if lot.is_night]
        logger.info("Лоты синхронизированы: всего %d, ночных %d.", len(lots), len(night))
        return lots
