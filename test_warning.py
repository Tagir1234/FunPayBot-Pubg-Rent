r"""Имитация аренды, которая заканчивается через 21 минуту: когда сработают
предупреждение (rental_warning) и конец аренды (rental_ended).

Без сети: в FunPay и Telegram ничего не уходит, вместо отправки — печать.
Настоящий accounts.json не трогается: работа идёт с копией во временной папке.
Часы тоже ненастоящие — минуты прокручиваются в цикле, ждать 21 минуту не нужно.

Запуск из корня проекта:
    .venv\Scripts\python.exe test_warning.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import accounts_db
from auto_responder import load_triggers, minutes_text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

START = datetime(2026, 10, 5, 20, 0)
RENTAL_MINUTES = 21
CHAT_ID = "555"


def main() -> int:
    triggers = load_triggers()
    warn = triggers.warn_minutes
    templates = triggers.rental_templates
    events: list[tuple[int, str]] = []
    clock = {"minute": 0}

    def on_warning(account: accounts_db.RentAccount, left: int) -> None:
        until = f"{account.free_until:%H:%M}"
        events.append((clock["minute"], "warning"))
        print(f"  [+{clock['minute']:2} мин] → покупателю в чат {account.renter_chat_id}:")
        print("    " + (templates["rental_warning"] or "<нет шаблона>")
              .replace("{осталось}", minutes_text(left)).replace("{время}", until)
              .replace("\n", "\n    "))
        print(f"  [+{clock['minute']:2} мин] → в Telegram: " + (templates["warning_alert"] or "<нет шаблона>")
              .replace("{осталось}", minutes_text(left)).replace("{аккаунт}", account.title)
              .replace("{время}", until))

    def on_expired(account: accounts_db.RentAccount) -> None:
        events.append((clock["minute"], "ended"))
        print(f"  [+{clock['minute']:2} мин] → покупателю rental_ended, аккаунт «{account.title}» свободен")

    with tempfile.TemporaryDirectory() as tmp:
        accounts_db.ACCOUNTS_PATH = Path(tmp) / "accounts.json"
        accounts_db.ACCOUNTS_PATH.write_text(json.dumps([
            {"id": 1, "title": "Акк 1", "status": "free", "free_until": None},
        ]), encoding="utf-8")
        accounts_db.set_warning_handler(on_warning)
        accounts_db.set_expiry_handler(on_expired)

        until = START + timedelta(minutes=RENTAL_MINUTES)
        accounts_db.occupy(1, until, renter_chat_id=CHAT_ID, warned=False)
        print(f"Аренда выдана в {START:%H:%M}, до {until:%H:%M} ({RENTAL_MINUTES} мин). "
              f"WARN_MINUTES = {warn}.\n")

        # Тот же порядок, что в тикере telegram_bot._expiry_loop: сначала конец, потом предупреждение.
        for minute in range(0, RENTAL_MINUTES + 3):
            clock["minute"] = minute
            now = START + timedelta(minutes=minute)
            accounts_db.expire_due(now)
            accounts_db.warn_due(warn, now)

        print("\nПовторный проход (как после перезапуска бота) — дублей быть не должно:")
        before = len(events)
        accounts_db.occupy(1, until, renter_chat_id=CHAT_ID, warned=True)  # уже предупреждён
        for minute in range(RENTAL_MINUTES - warn, RENTAL_MINUTES):
            clock["minute"] = minute
            accounts_db.warn_due(warn, START + timedelta(minutes=minute))
        dup = len(events) != before
        print("  дублей нет" if not dup else "  ДУБЛЬ!")

        print(f"\nКороткая аренда ({warn - 5} мин) — предупреждения быть не должно:")
        before = len(events)
        short_until = START + timedelta(minutes=warn - 5)
        accounts_db.occupy(1, short_until, renter_chat_id=CHAT_ID,
                           warned=short_until - START <= timedelta(minutes=warn))
        for minute in range(0, warn - 5):
            clock["minute"] = minute
            accounts_db.warn_due(warn, START + timedelta(minutes=minute))
        short_ok = len(events) == before
        print("  не отправлено" if short_ok else "  ОТПРАВЛЕНО — ошибка")

    expected = [(RENTAL_MINUTES - warn, "warning"), (RENTAL_MINUTES, "ended")]
    main_ok = events[:2] == expected
    print()
    print(f"Ожидали: warning на +{RENTAL_MINUTES - warn} мин, rental_ended на +{RENTAL_MINUTES} мин.")
    ok = main_ok and not dup and short_ok
    print("Всё прошло." if ok else f"ОШИБКА: события {events}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
