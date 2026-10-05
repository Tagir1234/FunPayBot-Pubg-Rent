r"""Точка входа бота. Логинится в FunPay и запускает рабочие потоки:

    lot-raiser      — автоподнятие лотов (lot_raiser.py);
    lot-sync        — свои лоты в lots_cache.json для ночного тарифа (lot_sync.py);
    auto-responder  — автоответчик покупателям (auto_responder.py);
    telegram        — управление арендой из Telegram (telegram_bot.py), если задан TG_TOKEN.

Запуск из корня проекта:
    .venv\Scripts\python.exe main.py

Остановка — Ctrl+C.
"""

from __future__ import annotations

import logging
import sys
import time

import requests

from FunPayAPI import Account
from FunPayAPI.common import exceptions

from auto_responder import AutoResponder, TriggersError, load_triggers
from config import ConfigError, load_config, mask
from lot_raiser import LotRaiser
from lot_sync import LotSync
from telegram_bot import TelegramBot

logger = logging.getLogger("bot")

LOG_FILE = "bot.log"


def setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # Чтобы кириллица и ₽ не роняли вывод при перенаправлении в файл/пайп.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    # requests/urllib3 на INFO шумят о каждом соединении — нам это не нужно.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def login(cfg) -> Account:
    logger.info("Вход в FunPay, golden_key: %s", mask(cfg.golden_key))
    account = Account(cfg.golden_key, user_agent=cfg.user_agent).get()
    logger.info(
        "Вошёл как %s (id %s), баланс %s %s.",
        account.username, account.id, account.total_balance, account.currency,
    )
    return account


def main() -> int:
    setup_logging()

    try:
        cfg = load_config()
    except ConfigError as e:
        logger.error("Ошибка настроек:\n%s", e)
        return 2

    try:
        account = login(cfg)
    except exceptions.UnauthorizedError:
        logger.error(
            "FunPay не признал сессию: golden_key протух или USER_AGENT не совпадает "
            "с браузером, из которого взят ключ."
        )
        return 1
    except requests.exceptions.RequestException as e:
        logger.error("Сеть недоступна: %s: %s", type(e).__name__, e)
        return 1

    try:
        triggers = load_triggers()
    except TriggersError as e:
        logger.error("Ошибка триггеров:\n%s", e)
        return 2

    # Telegram поднимаем первым: автоответчик отдаёт ему события о новых заказах.
    telegram = None
    if cfg.telegram_enabled:
        telegram = TelegramBot(account, cfg.tg_token, cfg.tg_admin_id,
                               templates=triggers.rental_templates,
                               warn_minutes=triggers.warn_minutes,
                               night=triggers.night)
    else:
        logger.warning(
            "Telegram отключён: в .env нет TG_TOKEN и/или TG_ADMIN_ID. "
            "Бот будет работать, но управлять арендой с телефона не получится."
        )

    workers = [
        LotRaiser(account, cfg),
        # Кэш своих лотов (цены и ссылки ночного тарифа). Автоответчик читает только кэш.
        LotSync(account, triggers.lot_sync_minutes),
        AutoResponder(account, triggers,
                      on_new_order=telegram.notify_new_order if telegram else None,
                      on_seller_alert=telegram.notify_seller_alert if telegram else None,
                      on_extension_request=telegram.notify_extension_request if telegram else None),
    ]
    if telegram is not None:
        workers.append(telegram)
    for worker in workers:
        worker.start()

    try:
        # Главный поток только ждёт Ctrl+C. Вся работа — в рабочих потоках.
        while all(w.is_alive for w in workers):
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — останавливаюсь…")
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=20)
        return 0

    # Сюда попадаем, только если поток умер сам — причина уже в логе.
    dead = [type(w).__name__ for w in workers if not w.is_alive]
    logger.error("Рабочий поток завершился сам: %s. Смотри %s.", ", ".join(dead), LOG_FILE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
