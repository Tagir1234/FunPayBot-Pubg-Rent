r"""Проверка маршрутизации автоответчика: какая фраза покупателя в какую группу уходит.

Без сети: ни FunPay, ни Telegram не трогаются. Список аккаунтов подставляется фейковый
(Акк 1 свободен, Акк 2 занят), accounts.json не читается.

Запуск из корня проекта:
    .venv\Scripts\python.exe test_triggers.py

Добавили свою группу в triggers.json — допишите фразу в CASES и прогоните.
"""

from __future__ import annotations

import sys
from datetime import datetime

from accounts_db import STATUS_BUSY, STATUS_FREE, RentAccount
from auto_responder import extract_account_number, load_triggers, parse_rental_minutes
from msk import MSK

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=MSK)

ACCOUNTS = [
    RentAccount(id=1, title="Акк 1", description="", price="", status=STATUS_FREE,
                free_until=None, position=1),
    RentAccount(id=2, title="Акк 2", description="", price="", status=STATUS_BUSY,
                free_until=datetime(2026, 1, 15, 21, 0, tzinfo=MSK), renter_chat_id="555", position=2),
]

# (фраза, ожидаемый ключ). Ключ = имя группы, для вопроса про номер — «аккаунт:N:статус».
CASES: list[tuple[str, str | None]] = [
    ("сколько стоит?", "price"),
    ("как арендовать аккаунт", "how_to_rent"),
    ("а какой минимальный срок?", "terms"),
    ("1 час", "terms"),               # срок меньше MIN_RENTAL_HOURS — отвечает terms
    ("на 3 часа", "duration_commit"),
    ("оплатил", "after_payment"),
    ("акк 1 свободен?", "аккаунт:1:free"),
    ("2", "аккаунт:2:busy"),
    ("акк 5", "аккаунт:5:нет"),
    ("через 5 минут оплачу", None),   # «через N минут» — не срок аренды
]


def main() -> int:
    triggers = load_triggers()
    failed = 0
    for text, expected in CASES:
        route = triggers.route(text, ACCOUNTS, now=NOW)
        got = route.key if route else None
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:32} -> {got}" + ("" if ok else f"   ждали {expected}"))
        failed += not ok

    for text, expected in (("2 акка", None), ("2 акк", 2)):
        got = extract_account_number(text)
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} номер в {text!r} -> {got}")
        failed += not ok

    for text, expected in (("2 часа", 120), ("1,5 часа", 90), ("в 2 часа", None)):
        got = parse_rental_minutes(text)
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} срок в {text!r} -> {got}")
        failed += not ok

    print()
    print("Всё прошло." if not failed else f"Провалов: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
