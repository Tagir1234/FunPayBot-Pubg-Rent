"""База сдаваемых аккаунтов: accounts.json на чтение и запись.

Файл трогают сразу три потока — FunPay-автоответчик (читает), Telegram-бот (занимает
и освобождает) и тикер автоосвобождения, — поэтому все операции идут под одним
замком, а запись атомарная (временный файл + os.replace), чтобы падение посреди
сохранения не оставило обрезанный JSON.

Аккаунт освобождается сам, когда наступает free_until: это проверяется при каждом
чтении, так что любой показанный список всегда актуален.

За WARN_MINUTES до free_until арендатора предупреждают (warn_due). Флаг warned лежит
в самом accounts.json, чтобы перезапуск бота не приводил к повторному предупреждению.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from msk import now_msk, to_msk

logger = logging.getLogger("accounts")

ACCOUNTS_PATH = Path(__file__).resolve().parent / "accounts.json"
TRIGGERS_PATH = Path(__file__).resolve().parent / "triggers.json"

STATUS_FREE = "free"
STATUS_BUSY = "busy"

# Формат free_until в JSON — человекочитаемый, файл правится руками. Всегда МСК
# с явным смещением: «2026-10-05 21:00+03:00». Значения без смещения (старые записи
# и ручные правки) считаются московскими.
DT_FORMAT = "%Y-%m-%d %H:%M"
# Форматы, которые тоже принимаем при чтении, чтобы не ругаться на мелкие вольности.
_READ_FORMATS = (DT_FORMAT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")

# Один замок на весь модуль: операций мало и они короткие, делить их смысла нет.
_lock = threading.RLock()


def parse_dt(raw: str | None) -> datetime | None:
    """Строка из accounts.json -> время по МСК (aware)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        # Понимает и «...+03:00», и старое «2026-10-05 21:00» без пояса.
        return to_msk(datetime.fromisoformat(raw))
    except ValueError:
        pass
    for fmt in _READ_FORMATS:
        try:
            return to_msk(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    logger.warning("Не смог разобрать дату %r в accounts.json — считаю, что её нет.", raw)
    return None


def format_dt(moment: datetime) -> str:
    """-> «2026-10-05 21:00+03:00»."""
    return to_msk(moment).isoformat(sep=" ", timespec="minutes")


@dataclass(frozen=True)
class RentAccount:
    id: int
    title: str
    description: str
    price: str
    status: str
    free_until: datetime | None
    # Чат FunPay с текущим арендатором: туда уйдёт «аренда закончилась».
    renter_chat_id: str | None = None
    # Порядковый номер в accounts.json (с единицы) — он же {номер} в шаблонах.
    position: int = 0
    # Арендатора уже предупредили, что аренда скоро закончится.
    warned: bool = False

    @property
    def is_free(self) -> bool:
        return self.status == STATUS_FREE

    def until_text(self) -> str:
        """Время освобождения в виде «до 21:00 (27.09)»."""
        if self.free_until is None:
            return "срок не указан"
        return f"до {self.free_until:%H:%M} МСК ({self.free_until:%d.%m})"

    def line(self) -> str:
        parts = [f"• {self.title}"]
        if self.description:
            parts.append(f" — {self.description}")
        if self.price:
            parts.append(f" — {self.price}")
        if not self.is_free:
            parts.append(f" ({self.until_text()})")
        return "".join(parts)


# ---------------------------------------------------------------------- низкий уровень


def _read_raw() -> list[dict]:
    """Сырые записи из файла. Пустой список при любой ошибке — бот не должен падать."""
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("Файл %s не найден — список аккаунтов пуст.", ACCOUNTS_PATH)
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Не смог прочитать %s (%s) — список аккаунтов пуст.", ACCOUNTS_PATH, e)
        return []
    if not isinstance(data, list):
        logger.error("В %s ожидался список записей — список аккаунтов пуст.", ACCOUNTS_PATH)
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_raw(items: list[dict]) -> None:
    """Атомарная запись: сначала во временный файл, потом подмена."""
    tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, ACCOUNTS_PATH)
    except OSError as e:
        logger.error("Не смог сохранить %s: %s", ACCOUNTS_PATH, e)
        tmp.unlink(missing_ok=True)


def _to_account(item: dict, position: int = 0) -> RentAccount | None:
    try:
        return RentAccount(
            id=int(item["id"]),
            title=str(item["title"]),
            description=str(item.get("description", "")),
            price=str(item.get("price", "")),
            status=str(item.get("status", STATUS_BUSY)),
            free_until=parse_dt(item.get("free_until")),
            renter_chat_id=(str(raw) if (raw := item.get("renter_chat_id")) else None),
            position=position,
            warned=bool(item.get("warned")),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.error("Пропускаю запись в accounts.json (%s): %r", e, item)
        return None


def _expire_in_place(items: list[dict], now: datetime) -> list[RentAccount]:
    """Освобождает записи, у которых вышел срок. Меняет items на месте."""
    freed = []
    for position, item in enumerate(items, start=1):
        if item.get("status") != STATUS_BUSY:
            continue
        until = parse_dt(item.get("free_until"))
        if until is None or until > now:
            continue
        # Снимок ДО очистки: в нём ещё есть renter_chat_id, по которому
        # надо написать арендатору «аренда закончилась».
        account = _to_account(item, position)
        item["status"] = STATUS_FREE
        item["free_until"] = None
        item["renter_chat_id"] = None
        item["warned"] = False
        if account is not None:
            freed.append(account)
    return freed


# ---------------------------------------------------------------------- публичный API


# Кто получает уведомление об истёкшей аренде. Ставится один раз при старте.
_expiry_handler: Callable[[RentAccount], None] | None = None


def set_expiry_handler(handler: Callable[[RentAccount], None] | None) -> None:
    """Регистрирует обработчик истёкшей аренды (написать арендатору и админу).

    Нужен потому, что срок может истечь в любом из потоков: не только в тикере,
    но и при обычном чтении списка из автоответчика. Через общий обработчик
    уведомление уходит ровно один раз, кто бы ни обнаружил просрочку.
    """
    global _expiry_handler
    _expiry_handler = handler


# Кто предупреждает арендатора о скором конце аренды: (аккаунт, минут осталось).
_warning_handler: Callable[[RentAccount, int], None] | None = None


def set_warning_handler(handler: Callable[[RentAccount, int], None] | None) -> None:
    global _warning_handler
    _warning_handler = handler


def warn_due(warn_minutes: int, now: datetime | None = None) -> list[RentAccount]:
    """Помечает warned и предупреждает арендаторов, у которых до конца <= warn_minutes.

    Флаг ставится ДО отправки и сразу пишется в файл: если отправка упадёт или бот
    перезапустится, повторного предупреждения не будет. Лучше одно потерянное
    напоминание, чем два одинаковых.
    """
    now = to_msk(now) if now else now_msk()
    horizon = now + timedelta(minutes=warn_minutes)
    due: list[RentAccount] = []
    with _lock:
        items = _read_raw()
        for position, item in enumerate(items, start=1):
            if item.get("status") != STATUS_BUSY or item.get("warned") or not item.get("renter_chat_id"):
                continue
            until = parse_dt(item.get("free_until"))
            if until is None or not (now < until <= horizon):
                continue
            item["warned"] = True
            account = _to_account(item, position)
            if account is not None:
                due.append(account)
        if due:
            _write_raw(items)

    # Как и в expire_due: в сеть ходим уже без замка.
    if due and _warning_handler is not None:
        for account in due:
            # Округляем вверх: тикер раз в минуту, и 19.5 минуты честнее назвать 20.
            left = max(1, math.ceil((account.free_until - now).total_seconds() / 60))
            try:
                _warning_handler(account, left)
            except Exception as e:
                logger.error("Обработчик предупреждения упал на «%s»: %s: %s",
                             account.title, type(e).__name__, e)
    return due


def expire_due(now: datetime | None = None) -> list[RentAccount]:
    """Освобождает всё, у чего наступил free_until. Возвращает освободившиеся аккаунты."""
    now = to_msk(now) if now else now_msk()
    with _lock:
        items = _read_raw()
        freed = _expire_in_place(items, now)
        if freed:
            _write_raw(items)
            logger.info("Срок аренды вышел, освободил: %s", ", ".join(a.title for a in freed))

    # Обработчик вызываем уже без замка: он ходит в сеть (FunPay, Telegram),
    # и держать на это время блокировку базы нельзя.
    if freed and _expiry_handler is not None:
        for account in freed:
            try:
                _expiry_handler(account)
            except Exception as e:
                logger.error("Обработчик истёкшей аренды упал на «%s»: %s: %s",
                             account.title, type(e).__name__, e)
    return freed


def load_accounts() -> list[RentAccount]:
    """Актуальный список. Попутно освобождает всё, у чего вышел срок."""
    expire_due()
    with _lock:
        return [a for a in (_to_account(item, position)
                            for position, item in enumerate(_read_raw(), start=1))
                if a is not None]


def get_account(account_id: int) -> RentAccount | None:
    return next((a for a in load_accounts() if a.id == account_id), None)


def active_rental(chat_id: int | str) -> RentAccount | None:
    """Аккаунт, который сейчас арендует этот чат FunPay. None, если аренды нет."""
    return next((a for a in load_accounts()
                 if not a.is_free and a.renter_chat_id == str(chat_id)), None)


def occupy(account_id: int, until: datetime, renter_chat_id: str | int | None = None,
           warned: bool = False) -> RentAccount | None:
    """Помечает аккаунт занятым до указанного момента. None, если такого id нет.

    renter_chat_id сохраняется в accounts.json, чтобы по истечении срока бот знал,
    кому отправить «аренда закончилась» — даже если его перезапускали.

    warned: новая аренда или новый free_until сбрасывают предупреждение. True —
    когда аренда и так короче WARN_MINUTES и предупреждать нечего.
    """
    with _lock:
        items = _read_raw()
        for position, item in enumerate(items, start=1):
            if str(item.get("id")) != str(account_id):
                continue
            item["status"] = STATUS_BUSY
            item["free_until"] = format_dt(until)
            item["renter_chat_id"] = str(renter_chat_id) if renter_chat_id else None
            item["warned"] = warned
            _write_raw(items)
            account = _to_account(item, position)
            if account is not None:
                logger.info("Аккаунт «%s» занят %s.", account.title, account.until_text())
            return account
        logger.error("Занять аккаунт %s не вышло: такого id нет в accounts.json.", account_id)
        return None


def release(account_id: int) -> RentAccount | None:
    """Освобождает аккаунт вручную. None, если такого id нет."""
    with _lock:
        items = _read_raw()
        for position, item in enumerate(items, start=1):
            if str(item.get("id")) != str(account_id):
                continue
            item["status"] = STATUS_FREE
            item["free_until"] = None
            item["renter_chat_id"] = None
            item["warned"] = False
            _write_raw(items)
            account = _to_account(item, position)
            if account is not None:
                logger.info("Аккаунт «%s» освобождён.", account.title)
            return account
        logger.error("Освободить аккаунт %s не вышло: такого id нет в accounts.json.", account_id)
        return None


# Эмодзи-цифры для {номер}: двузначные собираются подряд, 10 -> 1️⃣0️⃣.
_DIGIT_EMOJI = {str(d): f"{d}️⃣" for d in range(10)}

# Шаблоны списка берутся из triggers.json, чтобы тексты правились без кода.
_DEFAULT_LIST_TEMPLATE = {
    "header": "🍀Аккаунты🍀",
    "header_all_busy": "🍀Аккаунты заняты🍀",
    "divider": "___________________________________________",
    "line_free": "🕊️{номер} аккаунт свободен🏔️",
    "line_busy": "🕊️{номер} аккаунт занят до {время}🏔️",
    "footer_all_busy": "🏔️если желаете можете забронировать, внеся полную предоплату😁",
    "empty": "Список аккаунтов временно недоступен, уточню лично.",
}


def emoji_number(number: int) -> str:
    """5 -> 5️⃣, 10 -> 1️⃣0️⃣."""
    return "".join(_DIGIT_EMOJI.get(ch, ch) for ch in str(number))


def _list_template() -> dict:
    """Шаблон списка из triggers.json. При любой беде — встроенный запасной."""
    try:
        raw = json.loads(TRIGGERS_PATH.read_text(encoding="utf-8")).get("accounts_list")
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.error("Не смог прочитать accounts_list из %s (%s) — беру шаблон по умолчанию.",
                     TRIGGERS_PATH, e)
        return dict(_DEFAULT_LIST_TEMPLATE)
    if not isinstance(raw, dict):
        return dict(_DEFAULT_LIST_TEMPLATE)
    # Недостающие ключи добираем из запасного, чтобы опечатка в JSON не роняла ответ.
    return {**_DEFAULT_LIST_TEMPLATE, **raw}


def account_line(account: RentAccount, template: dict) -> str:
    key = "line_free" if account.is_free else "line_busy"
    time_text = f"{account.free_until:%H:%M}" if account.free_until else "—"
    return (template[key]
            .replace("{номер}", emoji_number(account.position))
            .replace("{время}", time_text))


def render_accounts() -> str:
    """Список аккаунтов для покупателя.

    Структура: заголовок, разделитель, строки аккаунтов через пустую строку,
    разделитель, подвал (только когда занято всё).
    """
    accounts = load_accounts()
    template = _list_template()
    if not accounts:
        return template["empty"]

    all_busy = all(not a.is_free for a in accounts)
    divider = template["divider"]

    parts = [
        template["header_all_busy"] if all_busy else template["header"],
        divider,
        "\n\n".join(account_line(a, template) for a in accounts),
        divider,
    ]
    if all_busy:
        parts.append(template["footer_all_busy"])
    return "\n".join(parts)
