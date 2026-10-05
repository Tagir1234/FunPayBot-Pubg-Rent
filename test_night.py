r"""Ночной тариф: расчёт free_until при фиксированном «сейчас» по МСК.

Без сети: в FunPay и Telegram ничего не уходит.

Проверяет:
    1. 21:30 -> завтра 06:00; 03:00 -> сегодня 06:00; 14:00 и 05:30 -> запрос подтверждения;
    2. результат не зависит от часового пояса: тот же момент, записанный в UTC,
       Владивостоке и Нью-Йорке, даёт то же время по МСК;
    3. now_msk() не зависит от пояса компьютера: дочерний процесс с другим TZ;
    4. разметку лотов: ночной / почасовой, часы из названия, номер аккаунта.

Запуск из корня проекта:
    .venv\Scripts\python.exe test_night.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from auto_responder import load_triggers
from lot_sync import account_number, explicit_hours, is_night_lot, make_lot
from msk import MSK
from night import night_rental

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAY = datetime(2026, 10, 5, tzinfo=MSK)
TOMORROW = DAY + timedelta(days=1)

# (сейчас по МСК, ожидаемый free_until, нужно ли подтверждение)
CASES = [
    ((21, 30), TOMORROW.replace(hour=6), False),
    ((3, 0), DAY.replace(hour=6), False),
    ((14, 0), TOMORROW.replace(hour=6), True),   # вне окна
    ((5, 30), DAY.replace(hour=6), True),        # до конца окна 30 мин < MIN_RENTAL_HOURS
]

OTHER_ZONES = [timezone.utc, timezone(timedelta(hours=10), "Владивосток"),
               timezone(timedelta(hours=-4), "Нью-Йорк")]


def check(label: str, ok: bool) -> int:
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    return 0 if ok else 1


def main() -> int:
    night = load_triggers().night
    print(f"Окно: {night.start:%H:%M}–{night.end:%H:%M} МСК, минимум {night.min_hours} ч.\n")
    failed = 0

    print("1. Расчёт free_until")
    for (h, m), expected, confirm in CASES:
        now = DAY.replace(hour=h, minute=m)
        r = night_rental(night, now)
        what = (f"запрос подтверждения ({r.reason}): «Занять до {r.until:%H:%M} МСК?»"
                if r.needs_confirm else "сразу")
        failed += check(f"сейчас {now:%d.%m %H:%M} МСК -> до {r.until:%d.%m %H:%M} МСК, {what}",
                        r.until == expected and r.needs_confirm == confirm)

    print("\n2. Тот же момент в других часовых поясах")
    for (h, m), expected, confirm in CASES:
        msk_now = DAY.replace(hour=h, minute=m)
        for zone in OTHER_ZONES:
            other = msk_now.astimezone(zone)
            r = night_rental(night, other)
            failed += check(f"{other:%d.%m %H:%M} {zone.tzname(None)} (= {msk_now:%H:%M} МСК) -> "
                            f"{r.until.astimezone(MSK):%d.%m %H:%M} МСК",
                            r.until == expected and r.needs_confirm == confirm)
    # Время без пояса считается московским (старые записи в accounts.json).
    r = night_rental(night, datetime(2026, 10, 5, 21, 30))
    failed += check("21:30 без пояса считается МСК -> 06.10 06:00", r.until == TOMORROW.replace(hour=6))

    print("\n3. Часовой пояс компьютера (дочерний процесс с другим TZ)")
    probe = ("from msk import now_msk; from datetime import datetime; "
             "print(now_msk().strftime('%H:%M'), datetime.now().strftime('%H:%M'))")
    results = {}
    for tz in ("UTC0", "JST-9", "EST5"):
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             env={**os.environ, "TZ": tz}, cwd=os.path.dirname(os.path.abspath(__file__)))
        msk_time, local_time = out.stdout.split()
        results[tz] = msk_time
        print(f"     TZ={tz:5}: по МСК {msk_time}, локальные часы компьютера {local_time}")
    failed += check("now_msk() одинаковый при любом TZ", len(set(results.values())) == 1)

    print("\n4. Разметка лотов (lot_sync)")
    failed += check("«Аккаунт, Аренда, 10 уровень» — почасовой",
                    not is_night_lot("Аккаунт, Аренда, 10 уровень", ""))
    failed += check("«Ночной тариф» в названии — ночной", is_night_lot("Аккаунт PUBG, Ночной тариф", ""))
    failed += check("«НОЧНОЙ ТАРИФ» капсом — ночной", is_night_lot("НОЧНОЙ ТАРИФ акк 2", ""))
    failed += check("«ночь», «до утра», «ночной тариф» в ОПИСАНИИ — не ночной (смотрим только название)",
                    not is_night_lot("Аренда PUBG", "Сдаю на ночь до утра, есть ночной тариф"))
    failed += check("«Аренда на ночь» без слов «ночной тариф» — не ночной", not is_night_lot("Аренда на ночь", ""))
    failed += check("«Ночной тариф 8 часов» — 8 ч главнее окна", explicit_hours("Ночной тариф 8 часов") == 8)
    failed += check("ночной без часов — срок по окну",
                    make_lot(1, "Ночной тариф", "", 300, "RUB", "x").hours is None)
    failed += check("часы у почасового лота не ставятся",
                    make_lot(1, "Аренда 2 часа", "", 150, "RUB", "x").hours is None)
    failed += check("«Ночной тариф Акк 2» — аккаунт 2", account_number("Ночной тариф Акк 2") == 2)
    failed += check("«Аккаунт, 10 уровень» — номера нет",
                    account_number("Аккаунт, Аренда, 10 уровень") is None)
    failed += check("цена 99.876 -> «99.88»", make_lot(1, "x", "", 99.876, "RUB", "x").price_text() == "99.88")

    print()
    print("Всё прошло." if not failed else f"Провалов: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
