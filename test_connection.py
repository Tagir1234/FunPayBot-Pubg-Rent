"""Проверка связи с FunPay: логин, свои лоты/подкатегории, сверка ID PUBG Mobile, список чатов.

Запускать из корня проекта (там лежит папка FunPayAPI):
    .venv\\Scripts\\python.exe test_connection.py

Библиотека синхронная (requests), поэтому здесь нет ни asyncio, ни потоков.
"""

from __future__ import annotations

import sys

import requests

from FunPayAPI import Account
from FunPayAPI.common import exceptions
from config import ConfigError, load_config, mask

# Ожидаемый ID подкатегории "Аккаунты PUBG Mobile" — его и проверяем.
PUBG_SUBCATEGORY_ID = 346


def header(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def short(text: str | None, limit: int = 60) -> str:
    if not text:
        return "—"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def login(cfg) -> Account:
    header("1. ЛОГИН")
    print(f"golden_key : {mask(cfg.golden_key)}")
    print(f"user-agent : {short(cfg.user_agent, 80)}")
    print("Подключаюсь к funpay.com …")

    account = Account(cfg.golden_key, user_agent=cfg.user_agent).get()

    print()
    print(f"  username         : {account.username}")
    print(f"  account.id       : {account.id}")
    print(f"  активных продаж  : {account.active_sales}")
    print(f"  активных покупок : {account.active_purchases}")
    print(f"  баланс           : {account.total_balance} {account.currency}")
    return account


def show_my_lots(account: Account) -> None:
    header("2. МОИ ЛОТЫ И ПОДКАТЕГОРИИ")
    print("(account.categories — это весь каталог игр FunPay, поэтому свои категории")
    print(" берём со страницы профиля: account.get_user(account.id))")

    profile = account.get_user(account.id)
    lots = profile.get_lots()
    print()
    print(f"Профиль: {profile.username} (id {profile.id}), лотов всего: {len(lots)}")

    if not lots:
        print("Лотов не найдено. Если лоты на самом деле есть — скорее всего они деактивированы.")
        return

    # Группируем по подкатегории. Ключ — (id, имя), чтобы не зависеть от хэша объекта.
    grouped: dict[tuple[int, str], list] = {}
    for lot in lots:
        sub = lot.subcategory
        key = (sub.id, sub.fullname) if sub else (-1, "без подкатегории")
        grouped.setdefault(key, []).append(lot)

    for (sub_id, fullname), sub_lots in sorted(grouped.items()):
        sub = sub_lots[0].subcategory
        print()
        print(f"  [{sub_id}] {fullname} — лотов: {len(sub_lots)}")
        if sub is not None:
            print(f"      категория (игра) : {sub.category.name} (id {sub.category.id})")
            print(f"      тип подкатегории : {sub.type.name}")
            print(f"      управление лотами: {sub.private_link}")
        for lot in sub_lots:
            print(f"      - #{lot.id}  {lot.price} {lot.currency}  {short(lot.title)}")


def check_pubg(account: Account) -> None:
    header(f"3. СВЕРКА ID PUBG MOBILE (ожидаем {PUBG_SUBCATEGORY_ID})")

    categories = [c for c in account.categories if "pubg" in c.name.lower()]
    subcategories = [s for s in account.subcategories if "pubg" in s.fullname.lower()]

    print(f"Всего в каталоге FunPay: категорий {len(account.categories)}, "
          f"подкатегорий {len(account.subcategories)}")
    print()

    if not categories and not subcategories:
        print("Ничего с 'PUBG' в названии не найдено. Похоже, каталог не распарсился.")
        return

    for category in categories:
        print(f"КАТЕГОРИЯ (игра): {category.name} — id {category.id}")
        for sub in category.get_subcategories():
            mark = "  <<< ЭТО ОНА" if sub.id == PUBG_SUBCATEGORY_ID else ""
            print(f"    подкатегория [{sub.id:>5}] {sub.name} ({sub.type.name})"
                  f"  {sub.public_link}{mark}")
        print()

    # Подкатегории с PUBG в названии, но из других игр (на случай, если игра называется иначе).
    from_other_games = [s for s in subcategories if s.category not in categories]
    if from_other_games:
        print("Прочие подкатегории со словом PUBG:")
        for sub in from_other_games:
            print(f"    [{sub.id:>5}] {sub.fullname}  {sub.public_link}")
        print()

    target = next((s for s in account.subcategories if s.id == PUBG_SUBCATEGORY_ID), None)
    if target is None:
        print(f"ВНИМАНИЕ: подкатегории с id {PUBG_SUBCATEGORY_ID} в каталоге нет. "
              f"Возьми верный id из списка выше.")
    else:
        print(f"ПОДТВЕРЖДЕНО: id {PUBG_SUBCATEGORY_ID} -> «{target.fullname}» "
              f"({target.type.name}), {target.public_link}")


def show_chats(account: Account) -> None:
    header("4. МОИ ЧАТЫ")
    chats = account.get_chats(update=True)
    print(f"Чатов получено: {len(chats)}")

    if not chats:
        print("Список пуст — это нормально, если переписок ещё не было.")
        return

    for chat_id, chat in chats.items():
        msg_type = chat.last_message_type.name if chat.last_message_type else "—"
        unread = "НЕПРОЧИТАН" if chat.unread else "прочитан"
        print(f"  [{chat_id}] {chat.name or '—'}  ({unread}, {msg_type})")
        print(f"        {short(chat.last_message_text, 70)}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Чтобы символы валют (₽) не роняли вывод при перенаправлении в файл/пайп.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ОШИБКА НАСТРОЕК\n{e}")
        return 2

    try:
        account = login(cfg)
    except exceptions.UnauthorizedError:
        print("\nОШИБКА: FunPay не признал сессию.")
        print("  - golden_key протух (перелогинься в браузере и возьми свежий), или")
        print("  - USER_AGENT не совпадает с браузером, из которого взят ключ.")
        return 1
    except requests.exceptions.RequestException as e:
        print(f"\nОШИБКА СЕТИ: не смог достучаться до funpay.com\n  {type(e).__name__}: {e}")
        return 1

    try:
        show_my_lots(account)
        check_pubg(account)
        show_chats(account)
    except exceptions.RequestFailedError as e:
        print(f"\nОШИБКА ЗАПРОСА К FUNPAY: {e}")
        return 1
    except requests.exceptions.RequestException as e:
        print(f"\nОШИБКА СЕТИ: {type(e).__name__}: {e}")
        return 1

    header("ГОТОВО — связь с FunPay работает")
    return 0


if __name__ == "__main__":
    sys.exit(main())
