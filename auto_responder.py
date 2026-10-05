"""Автоответчик: слушает события FunPay через Runner.listen() и отвечает по триггерам.

Опроса get_chats в цикле здесь нет и быть не должно — Runner сам отдаёт события.
Нас интересует только NewMessageEvent.

Порядок разбора одного сообщения:
    1. моё / системное      -> молчим;
    2. первое сообщение в диалоге (в истории нет моих) -> приветствие;
    3. вопрос про конкретный номер аккаунта -> account_query;
    4. первая сработавшая группа триггеров (приоритет = порядок в triggers.json);
    5. ничего не подошло    -> один раз на чат «отвечу в ближайшее время».

Шаги 3-4 — чистая функция Triggers.route(): без сети, её гоняет test_triggers.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from FunPayAPI import Account
from FunPayAPI.common.enums import MessageTypes
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI.updater.runner import Runner

from datetime import datetime

from accounts_db import RentAccount, active_rental, emoji_number, load_accounts, render_accounts
from lot_sync import CachedLot, load_cache
from msk import now_msk, parse_hhmm
from night import NightSettings

logger = logging.getLogger("responder")

TRIGGERS_PATH = Path(__file__).resolve().parent / "triggers.json"

# Пауза между опросами FunPay. Меньше 4 секунд не ставить — прилетит 429.
LISTEN_DELAY_SECONDS = 6.0
# Если сам listen() рухнул, ждём столько и поднимаем слушателя заново.
LISTEN_RESTART_SECONDS = 30

# Как часто проверять, не пора ли отправить отложенное уведомление продавцу.
ALERT_FLUSH_TICK_SECONDS = 20
# Обрезка сообщения покупателя в уведомлении.
ALERT_TEXT_LIMIT = 300

# Всё, что не буква, не цифра и не пробел, выкидываем: так "!статус", "статус?"
# и "а какой статус..." превращаются в одно и то же.
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Нижний регистр, ё -> е, без пунктуации, одиночные пробелы."""
    text = text.lower().replace("ё", "е")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


# Порядковые числительные: покупатель пишет «первый акк» не реже, чем «акк 1».
_ORDINALS = {
    "первый": 1, "первая": 1, "первого": 1, "второй": 2, "вторая": 2, "второго": 2,
    "третий": 3, "третья": 3, "третьего": 3, "четвертый": 4, "четвертая": 4,
    "пятый": 5, "пятая": 5, "шестой": 6, "седьмой": 7, "восьмой": 8,
    "девятый": 9, "десятый": 10,
}
_ORD_RE = "|".join(_ORDINALS)

# «ак...» покрывает «акк», «аккаунт», «акаунт», «ак». Обычные слова не задевает:
# в «как» и «пока» нет границы слова перед «ак».
_ACC = r"ак[а-я]*"

# Номер ПОСЛЕ слова «аккаунт»: «акк 1», «аккаунт номер 2», «акк1».
_NUM_AFTER = re.compile(rf"(?:^|\s){_ACC}\s*(?:номер\s*)?(\d+)")
# Номер ПЕРЕД словом «аккаунт»: «1 акк», «2 аккаунт».
_NUM_BEFORE = re.compile(rf"(?:^|\s)(\d+)\s*{_ACC}")
# Числительное рядом со словом «аккаунт»: «первый акк», «акк второй».
_ORD_NEAR = re.compile(rf"(?:^|\s)(?:({_ORD_RE})\s*{_ACC}|{_ACC}\s*({_ORD_RE}))(?:\s|$)")
# Сообщение целиком — только номер: «1», «2», «первый».
_BARE = re.compile(rf"^(\d+|{_ORD_RE})$")
# «2 акка», «3 аккаунта» — это КОЛИЧЕСТВО аккаунтов, а не номер. «2 акк» — номер.
_NUM_BEFORE_PLURAL = re.compile(r"(?:^|\s)(\d+)\s*(?:акка|акки|акков|аккаунта|аккаунты|аккаунтов)(?:\s|$)")


def extract_account_number(text: str) -> int | None:
    """Номер аккаунта, о котором спрашивает покупатель. None, если номера нет.

    Число само по себе номером НЕ считается: «можно на 2 часа» — это срок, а не
    аккаунт. Нужно либо слово «акк/аккаунт» рядом, либо сообщение из одного числа.
    """
    normalized = normalize(text)
    if not normalized:
        return None

    if (m := _NUM_AFTER.search(normalized)) is not None:
        return int(m.group(1))
    if (m := _NUM_BEFORE.search(normalized)) is not None:
        plural = _NUM_BEFORE_PLURAL.search(normalized)
        if not (plural and int(plural.group(1)) >= 2):
            return int(m.group(1))

    if (m := _ORD_NEAR.search(normalized)) is not None:
        return _ORDINALS[m.group(1) or m.group(2)]

    if (m := _BARE.match(normalized)) is not None:
        word = m.group(1)
        return int(word) if word.isdigit() else _ORDINALS[word]

    return None


# ---------------------------------------------------------------------- срок аренды

_NUMBER_WORDS = {
    "один": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "пару": 2, "пара": 2,
    "три": 3, "трех": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6,
    "шести": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "двенадцать": 12,
    "полтора": 1.5, "полторы": 1.5,
}
_UNIT_MINUTES = {
    **dict.fromkeys(("ч", "час", "часа", "часов", "часик", "часика", "часиков", "h"), 60),
    **dict.fromkeys(("мин", "минут", "минуты", "минуту", "минутку", "минутки", "min"), 1),
    **dict.fromkeys(("сутки", "суток", "день", "дня", "дней"), 1440),
}
# Срок без числа: «на час», «на сутки». Считаем только после «на» —
# «в час» и «стоит час» про цену, а не про срок.
_UNIT_ALONE = {"час": 60, "часик": 60, "сутки": 1440, "день": 1440, "полчаса": 30, "полчасика": 30}
# Слова перед числом, после которых это НЕ срок аренды: «через 5 минут оплачу»,
# «за 20 минут до конца», «в 2 часа», «каждые 5 минут вылетает», «меньше 2 часов».
_NOT_DURATION_BEFORE = {"через", "за", "в", "во", "до", "после", "каждые", "каждый", "каждую",
                        "уже", "осталось", "остался", "осталась", "прошло", "спустя", "около",
                        # «меньше 2 часов можно?», «от 2 часов» — это вопрос про условия.
                        "меньше", "больше", "от", "не", "минимум", "максимум", "хотя"}


def _duration_tokens(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"(\d)[.,](\d)", r"\1.\2", text)          # 1,5 -> 1.5
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)           # прочие точки — в пробел
    text = re.sub(r"[^\w.]", " ", text)
    text = re.sub(r"(\d)([а-яa-z])", r"\1 \2", text)          # 2ч -> 2 ч
    return text.split()


def parse_rental_minutes(text: str) -> float | None:
    """Срок аренды, названный в сообщении, в минутах. None, если срока нет.

    «2 часа», «на 3ч», «30 минут», «полтора часа», «на час», «на сутки».
    Время на часах («в 21:00»), «через 5 минут» и «2 часа назад» сроком не считаются.
    """
    tokens = _duration_tokens(text)
    for i, token in enumerate(tokens):
        before = tokens[i - 1] if i >= 1 else ""
        if token in _UNIT_ALONE and before == "на":
            return float(_UNIT_ALONE[token])
        if token == "полчаса" and before not in _NOT_DURATION_BEFORE:
            return 30.0
        if token not in _UNIT_MINUTES or i == 0:
            continue
        raw = tokens[i - 1]
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            amount = float(raw)
        elif raw in _NUMBER_WORDS:
            amount = float(_NUMBER_WORDS[raw])
        else:
            continue
        if amount <= 0:
            continue
        before_number = tokens[i - 2] if i >= 2 else ""
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        if before_number in _NOT_DURATION_BEFORE or after == "назад":
            continue
        return amount * _UNIT_MINUTES[token]
    return None


# ---------------------------------------------------------------------- тексты


# «[впиши]» или «[впиши: подсказка]» — продавец ещё не вписал свой факт.
# Группа с такой заглушкой выключена: покупатель не должен увидеть скобки.
_PLACEHOLDER = re.compile(r"\[впиши[^\]]*\]", re.IGNORECASE)


def has_placeholder(text: str | None) -> bool:
    return bool(text) and _PLACEHOLDER.search(text) is not None


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 час, 2 часа, 5 часов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def hours_text(n: int) -> str:
    return f"{n} {plural(n, 'час', 'часа', 'часов')}"


def minutes_text(n: int) -> str:
    """В винительном: «через 21 минуту», «за 20 минут»."""
    return f"{n} {plural(n, 'минуту', 'минуты', 'минут')}"


@dataclass(frozen=True)
class AccountQuery:
    """Ответ на «можно акк 1?» — зависит от того, свободен ли этот аккаунт."""
    reply_free: str
    reply_busy: str
    reply_unknown: str


class TriggersError(RuntimeError):
    """triggers.json отсутствует или заполнен некорректно."""


@dataclass(frozen=True)
class TriggerGroup:
    name: str
    # Ключевые слова нормализованы на загрузке: нормализуем один раз, а не на каждое сообщение.
    keywords: tuple[str, ...]
    reply: str
    # Слова, которые ищем только целиком (см. matches).
    exact_keywords: tuple[str, ...] = ()
    # False, если в ответе осталась заглушка [впиши]: группа молчит, пока её не заполнят.
    enabled: bool = True
    # "duration" — группа ловит не слова, а названный срок аренды («на 2 часа»).
    match_kind: str = "keywords"
    # Для duration: чей ответ слать, если срок меньше MIN_RENTAL_HOURS.
    short_reply_group: str | None = None
    # Кого дёрнуть в Telegram после ответа: "seller" — продавца, "extension" — просьба продлить.
    notify: str | None = None
    # "night_tariff" — ответ собирается из ночных лотов в lots_cache.json (см. _route_night).
    special: str | None = None
    # Дополнительные тексты особой группы (reply_many, line, reply_no_lot, reply_free, ...).
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def matches(self, normalized_text: str) -> bool:
        # Пробелы по краям превращают начало и конец строки в такие же границы
        # слова, как и обычный пробел, — дальше хватает поиска подстроки.
        padded = f" {normalized_text} "
        # keywords — начало слова: русский язык склоняет, и «андроид» обязан
        # ловиться в «на андроиде», «самсунг» в «самсунга». Но не где угодно
        # внутри слова, иначе «поко» сидело бы в «спокойно» и «беспокоит».
        if any(f" {k}" in padded for k in self.keywords):
            return True
        # keywords_exact — только слово целиком. Для коротких марок, которые
        # оказались началом обычных слов: «поко» → «поколение», «техно» →
        # «технология». «поко ф5» при этом ловится, там это отдельное слово.
        return any(f" {k} " in padded for k in self.exact_keywords)


@dataclass(frozen=True)
class Route:
    """Что ответить на сообщение: ключ антиспама, текст и сработавшая группа."""
    key: str
    reply: str
    group: TriggerGroup | None = None
    # Помимо ответа позвать продавца (ночной тариф, а ночного лота в кэше нет).
    alert_seller: bool = False


@dataclass(frozen=True)
class Triggers:
    greeting: str
    fallback: str
    antispam_seconds: int
    # Не чаще одного уведомления продавцу на чат за это время.
    seller_alert_seconds: int
    # Тексты для Telegram-части: rental_started / rental_ended / предупреждения. Живут
    # здесь же, чтобы все тексты бота правились в одном triggers.json.
    rental_templates: dict
    # Проверяется ДО обычных групп: вопрос про конкретный номер важнее общего «статуса».
    account_query: AccountQuery | None = None
    groups: tuple[TriggerGroup, ...] = field(default=())
    # Настройки из triggers.json.
    min_rental_hours: int = 2
    warn_minutes: int = 20
    night: NightSettings | None = None
    lot_sync_minutes: int = 30

    def group(self, name: str | None) -> TriggerGroup | None:
        return next((g for g in self.groups if g.name == name), None)

    def match(self, text: str, include_disabled: bool = False) -> TriggerGroup | None:
        """Первая подошедшая группа (без вопроса про номер). Порядок в triggers.json = приоритет."""
        route = self.route(text, accounts=None, include_disabled=include_disabled)
        return route.group if route else None

    def route(self, text: str, accounts: list[RentAccount] | None,
              include_disabled: bool = False, lots: list[CachedLot] | None = None,
              now: datetime | None = None) -> Route | None:
        """Решает, чем ответить. Без сети и без отправки — это гоняют тесты.

        accounts=None — вопрос про номер аккаунта не разбираем (нужен список).
        include_disabled — для тестов: показать, куда уйдёт фраза, когда [впиши] заполнят.
        """
        lots = lots or []
        normalized = normalize(text)
        night = next((g for g in self.groups if g.special == "night_tariff"
                      and (g.enabled or include_disabled)), None)

        # «2 акк на ночь» — статус аккаунта именно под ночной тариф. Проверяется раньше
        # обычного вопроса про номер: иначе ответ был бы «свободен, на сколько?».
        if accounts is not None and night is not None and normalized and night.matches(normalized):
            number = extract_account_number(text)
            if number is not None:
                return self._route_night_account(night, number, accounts, lots, now or now_msk())

        if accounts is not None:
            route = self._route_account_query(text, accounts)
            if route is not None:
                return route

        if not normalized:
            return None
        for g in self.groups:
            if g.special == "night_tariff":
                if (g.enabled or include_disabled) and g.matches(normalized):
                    return self._route_night(g, lots)
                continue
            if g.match_kind == "duration":
                minutes = parse_rental_minutes(text)
                if minutes is None:
                    continue
                if minutes < self.min_rental_hours * 60:
                    # Меньше минимума — «можете оплачивать» слать нельзя, шлём условия.
                    short = self.group(g.short_reply_group)
                    if short is not None and (short.enabled or include_disabled):
                        return Route(short.name, short.reply, short)
                    continue
                if g.enabled or include_disabled:
                    return Route(g.name, g.reply, g)
                continue
            if (g.enabled or include_disabled) and g.matches(normalized):
                return Route(g.name, g.reply, g)
        return None

    # ------------------------------------------------------------------ ночной тариф

    @staticmethod
    def _fill_lot(text: str, lot: CachedLot, number: int | None = None) -> str:
        return (text
                .replace("{цена}", lot.price_text())
                .replace("{ссылка}", lot.link)
                .replace("{название}", lot.title)
                .replace("{номер}", emoji_number(number) if number else "🕊️"))

    def _route_night(self, g: TriggerGroup, lots: list[CachedLot]) -> Route:
        """Ночной тариф без номера аккаунта: цена и ссылка из кэша лотов."""
        night = [lot for lot in lots if lot.is_night]
        if not night:
            # Цену не выдумываем: обещаем продавца и зовём его.
            return Route("night_tariff:нет лота", g.extra["reply_no_lot"], g, alert_seller=True)
        if len(night) == 1:
            return Route(g.name, self._fill_lot(g.reply, night[0], night[0].account_number), g)
        lines = [self._fill_lot(g.extra["line"], lot, lot.account_number or i)
                 for i, lot in enumerate(night, start=1)]
        return Route(g.name, g.extra["reply_many"].replace("{lots}", "\n\n".join(lines)), g)

    def _route_night_account(self, g: TriggerGroup, number: int, accounts: list[RentAccount],
                             lots: list[CachedLot], now: datetime) -> Route:
        """«2 акк на ночь»: свободен ли этот аккаунт к ночи."""
        account = next((a for a in accounts if a.position == number), None)
        if account is None and self.account_query is not None and self.account_query.reply_unknown:
            reply = self.account_query.reply_unknown.replace("{номер}", emoji_number(number))
            return Route(f"аккаунт:{number}:нет", reply)

        night = [lot for lot in lots if lot.is_night]
        # Ночной лот этого аккаунта; если ночной лот один на всех — он.
        lot = next((x for x in night if x.account_number == number), None)
        if lot is None and len(night) == 1:
            lot = night[0]

        # Занят — только если освободится уже после начала ночи. Освобождается раньше —
        # к ночи будет свободен, можно оплачивать.
        busy = (account is not None and not account.is_free and account.free_until is not None
                and self.night is not None and account.free_until > self.night.relevant_start(now))
        if busy:
            booking = self.group(g.extra.get("booking_group"))
            reply = (g.extra["reply_busy"]
                     .replace("{номер}", emoji_number(number))
                     .replace("{время}", f"{account.free_until:%H:%M}")
                     .replace("{booking}", booking.reply if booking else ""))
            return Route(f"night_tariff:{number}:busy", reply.rstrip(), g)

        if not night:
            return Route("night_tariff:нет лота", g.extra["reply_no_lot"], g, alert_seller=True)
        if lot is None:
            # Ночных лотов несколько, а номера аккаунта в их названиях нет — какой из них
            # этого аккаунта, не знаем. Даём все ссылки, а не угадываем.
            links = "\n".join(x.link for x in night)
            reply = g.extra["reply_free"].replace("{ссылка}", links).replace("{номер}", emoji_number(number))
            return Route(f"night_tariff:{number}:free", reply, g)
        return Route(f"night_tariff:{number}:free", self._fill_lot(g.extra["reply_free"], lot, number), g)

    def _route_account_query(self, text: str, accounts: list[RentAccount]) -> Route | None:
        query = self.account_query
        if query is None:
            return None
        number = extract_account_number(text)
        if number is None:
            return None

        account = next((a for a in accounts if a.position == number), None)
        if account is None:
            reply, state = query.reply_unknown, "нет"
        elif account.is_free:
            reply, state = query.reply_free, "free"
        else:
            reply, state = query.reply_busy, "busy"
        if not reply:
            return None

        reply = reply.replace("{номер}", emoji_number(number))
        reply = reply.replace("{время}",
                              f"{account.free_until:%H:%M}" if account and account.free_until else "—")
        # Ключ антиспама включает номер и статус: вопрос про другой аккаунт должен
        # проходить, и повторный вопрос после смены статуса — тоже.
        return Route(f"аккаунт:{number}:{state}", reply)


def _load_account_query(raw: dict | None, fill: Callable[[str], str]) -> AccountQuery | None:
    if not isinstance(raw, dict):
        return None
    try:
        return AccountQuery(
            reply_free=fill(raw["reply_free"]),
            reply_busy=fill(raw["reply_busy"]),
            reply_unknown=fill(raw.get("reply_unknown", "")),
        )
    except (KeyError, TypeError) as e:
        raise TriggersError(f"Некорректный блок account_query в {TRIGGERS_PATH}: {e}")


def _positive_int(raw: dict, key: str, default: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError):
        raise TriggersError(f"В {TRIGGERS_PATH} {key} должен быть целым числом.")
    if value <= 0:
        raise TriggersError(f"В {TRIGGERS_PATH} {key} должен быть больше нуля.")
    return value


# Тексты группы night_tariff помимо reply.
_NIGHT_EXTRA_KEYS = ("reply_many", "line", "reply_no_lot", "reply_free", "reply_busy")


def load_triggers() -> Triggers:
    try:
        raw = json.loads(TRIGGERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TriggersError(f"Файл не найден: {TRIGGERS_PATH}")
    except (json.JSONDecodeError, OSError) as e:
        raise TriggersError(f"Не смог прочитать {TRIGGERS_PATH}: {e}")

    min_hours = _positive_int(raw, "MIN_RENTAL_HOURS", 2)
    warn_minutes = _positive_int(raw, "WARN_MINUTES", 20)
    try:
        night_settings = NightSettings(
            start=parse_hhmm(str(raw.get("NIGHT_START", "20:00"))),
            end=parse_hhmm(str(raw.get("NIGHT_END", "06:00"))),
            name=str(raw.get("NIGHT_TARIFF_NAME", "Ночной тариф")),
            min_hours=min_hours,
        )
    except ValueError:
        raise TriggersError(f"В {TRIGGERS_PATH} NIGHT_START / NIGHT_END должны быть вида «20:00».")

    def fill(text: str | None) -> str | None:
        """Настройки подставляются один раз при загрузке: они не меняются до перезапуска."""
        if not text:
            return text
        return (text
                .replace("{MIN_RENTAL_HOURS}", str(min_hours))
                .replace("{MIN_RENTAL_TEXT}", hours_text(min_hours))
                .replace("{WARN_MINUTES}", str(warn_minutes))
                .replace("{WARN_TEXT}", minutes_text(warn_minutes))
                .replace("{NIGHT_START}", f"{night_settings.start:%H:%M}")
                .replace("{NIGHT_END}", f"{night_settings.end:%H:%M}")
                .replace("{NIGHT_TARIFF_NAME}", night_settings.name))

    try:
        groups = tuple(
            TriggerGroup(
                name=g["name"],
                keywords=tuple(normalize(k) for k in g.get("keywords", []) if normalize(k)),
                reply=fill(g["reply"]),
                exact_keywords=tuple(normalize(k) for k in g.get("keywords_exact", [])
                                     if normalize(k)),
                enabled=not any(has_placeholder(g.get(k)) for k in ("reply", *_NIGHT_EXTRA_KEYS)),
                match_kind=g.get("match", "keywords"),
                short_reply_group=g.get("short_reply_group"),
                notify=g.get("notify"),
                special=g.get("special"),
                extra={k: fill(g[k]) if isinstance(g[k], str) else g[k]
                       for k in (*_NIGHT_EXTRA_KEYS, "booking_group") if k in g},
            )
            for g in raw["groups"]
        )
        triggers = Triggers(
            greeting=fill(raw["greeting"]),
            fallback=fill(raw["fallback"]),
            antispam_seconds=int(raw.get("antispam_minutes", 10)) * 60,
            seller_alert_seconds=int(raw.get("seller_alert_cooldown_minutes", 5)) * 60,
            rental_templates={
                key: fill(raw.get(key))
                for key in ("rental_started", "rental_ended", "rental_warning", "seller_alert",
                            "extension_alert", "warning_alert", "night_confirm", "order_paid")
            },
            account_query=_load_account_query(raw.get("account_query"), fill),
            groups=groups,
            min_rental_hours=min_hours,
            warn_minutes=warn_minutes,
            night=night_settings,
            lot_sync_minutes=_positive_int(raw, "LOT_SYNC_MINUTES", 30),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise TriggersError(f"Некорректная структура {TRIGGERS_PATH}: {e}")

    empty = [g.name for g in groups
             if g.match_kind == "keywords" and not g.keywords and not g.exact_keywords]
    if empty:
        raise TriggersError(f"В {TRIGGERS_PATH} у групп {empty} нет ключевых слов.")
    unknown = [g.name for g in groups if g.match_kind not in ("keywords", "duration")]
    if unknown:
        raise TriggersError(f"В {TRIGGERS_PATH} у групп {unknown} неизвестный match.")
    bad_short = [g.name for g in groups
                 if g.match_kind == "duration" and triggers.group(g.short_reply_group) is None]
    if bad_short:
        raise TriggersError(f"В {TRIGGERS_PATH} у групп {bad_short} short_reply_group "
                            f"ссылается на несуществующую группу.")
    for key, text in triggers.rental_templates.items():
        if has_placeholder(text):
            logger.warning("Шаблон %s содержит [впиши] — он не будет отправляться.", key)
            triggers.rental_templates[key] = None
    return triggers


class AutoResponder:
    """Слушает FunPay в отдельном потоке и отвечает покупателям по triggers.json."""

    # Имена «виртуальных» групп: приветствие и отписка тоже не должны дублироваться.
    _GREETING = "приветствие"
    _FALLBACK = "fallback"

    def __init__(self, account: Account, triggers: Triggers,
                 on_new_order: Callable[[object], None] | None = None,
                 on_seller_alert: Callable[[str, str, str], None] | None = None,
                 on_extension_request: Callable[[RentAccount, str], None] | None = None) -> None:
        self.account = account
        self.triggers = triggers
        # Runner можно привязать к аккаунту только один раз, поэтому события заказов
        # раздаёт этот же поток — Telegram-бот подписывается колбэком.
        self.on_new_order = on_new_order
        # Уведомление продавцу о непонятном сообщении: ник, текст, ссылка на чат.
        self.on_seller_alert = on_seller_alert
        # Арендатор просит продлить: (его аккаунт, ссылка на чат). Продлевает продавец сам.
        self.on_extension_request = on_extension_request
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_safe, name="auto-responder", daemon=True)

        # (chat_id, имя группы) -> monotonic-время последней отправки. Защита от спама.
        self._last_reply: dict[tuple[int | str, str], float] = {}
        # chat_id -> здоровались ли уже. Считается по истории чата, дальше кэшируется.
        self._greeted: dict[int | str, bool] = {}

        # Уведомления продавцу. _alert_sent_at — когда последний раз отправляли по чату,
        # _alert_pending — что накопилось за время тишины (храним только последнее).
        self._alert_lock = threading.Lock()
        self._alert_sent_at: dict[int | str, float] = {}
        self._alert_pending: dict[int | str, tuple[str, str, str]] = {}
        self._alert_ticker = threading.Thread(target=self._alert_loop, name="seller-alerts",
                                              daemon=True)

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        enabled = [g.name for g in self.triggers.groups if g.enabled]
        disabled = [g.name for g in self.triggers.groups if not g.enabled]
        logger.info(
            "Автоответчик запущен: групп триггеров %d (%s), антиспам %d мин, "
            "мин. срок %d ч, предупреждение за %d мин.",
            len(enabled), ", ".join(enabled), self.triggers.antispam_seconds // 60,
            self.triggers.min_rental_hours, self.triggers.warn_minutes,
        )
        if disabled:
            logger.warning("Выключены до заполнения [впиши] в triggers.json: %s.", ", ".join(disabled))
        self._thread.start()
        self._alert_ticker.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------------ поток

    def _run_safe(self) -> None:
        """Поток автоответчика не должен ронять бот, что бы ни случилось."""
        try:
            self._run()
        except BaseException:  # noqa: BLE001
            logger.exception("Поток автоответчика аварийно завершился.")
        else:
            logger.info("Поток автоответчика остановлен.")

    def _run(self) -> None:
        # Runner привязывается к аккаунту намертво (повторный Runner(account) бросит
        # исключение), поэтому создаём его один раз и переиспользуем при перезапуске.
        runner = Runner(self.account)

        # ВАЖНО: в этом форке listen() сам по себе не работает. get_updates() не ходит
        # в сеть напрямую — он кладёт запрос в runner.payload_queue и ждёт в get_result(),
        # а разгребает очередь runner.loop(). Без отдельного потока с loop() слушатель
        # встаёт намертво на первом же опросе: ни исключения, ни записи в лог.
        # Через ту же очередь ходит и account.send_message(), так что молчит вообще всё.
        threading.Thread(target=runner.loop, name="runner-loop", daemon=True).start()
        logger.info("Очередь запросов Runner запущена, слушаю события FunPay.")

        while not self._stop.is_set():
            try:
                # На первом опросе Runner отдаёт только InitialChatEvent и запоминает
                # последние сообщения, поэтому на старый бэклог бот не ответит.
                for event in runner.listen(requests_delay=LISTEN_DELAY_SECONDS, ignore_exceptions=True):
                    if self._stop.is_set():
                        return
                    if isinstance(event, NewMessageEvent):
                        self._handle_message_safe(event)
                    elif isinstance(event, NewOrderEvent):
                        # На первом опросе приходят InitialOrderEvent, а не NewOrderEvent,
                        # поэтому по старым заказам ни покупателю, ни в Telegram ничего не уйдёт.
                        self._message_after_payment(event)
                        if self.on_new_order is not None:
                            self._notify_order_safe(event)
            except Exception as e:
                logger.error(
                    "Слушатель событий упал (%s: %s). Перезапуск через %d с.",
                    type(e).__name__, e, LISTEN_RESTART_SECONDS,
                )
                self._stop.wait(LISTEN_RESTART_SECONDS)

    def _message_after_payment(self, event: NewOrderEvent) -> None:
        """Покупатель оплатил — сразу пишем ему про проверку (шаблон order_paid).

        Пустой order_paid в triggers.json — ничего не отправляем.
        """
        text = self.triggers.rental_templates.get("order_paid")
        order = event.order
        if not text or not getattr(order, "chat_id", None):
            return
        try:
            self.account.send_message(order.chat_id, text, chat_name=order.buyer_username)
            # Мы уже написали в этот чат — приветствие ему больше не положено.
            self._greeted[order.chat_id] = True
            logger.info("Заказ #%s: покупателю отправлено сообщение после оплаты.", order.id)
        except Exception as e:
            logger.error("Не смог написать покупателю после оплаты заказа #%s: %s: %s",
                         order.id, type(e).__name__, e)

    def _notify_order_safe(self, event: NewOrderEvent) -> None:
        try:
            logger.info("Новый заказ #%s от %s на %s %s.", event.order.id, event.order.buyer_username,
                        event.order.price, event.order.currency.name if event.order.currency else "")
            self.on_new_order(event.order)
        except Exception as e:
            logger.error("Не смог передать заказ в Telegram: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ обработка сообщения

    def _handle_message_safe(self, event: NewMessageEvent) -> None:
        """Одно кривое сообщение не должно обрывать поток событий."""
        try:
            self._handle_message(event)
        except Exception as e:
            logger.error("Не смог обработать сообщение: %s: %s", type(e).__name__, e)
            logger.debug("TRACEBACK", exc_info=True)

    def _handle_message(self, event: NewMessageEvent) -> None:
        message = event.message

        # Пропуски логируем на DEBUG: без этого «бот молчит» невозможно отличить
        # от «событие вообще не дошло».
        if message.author_id == self.account.id:
            logger.debug("Чат %s: своё сообщение, пропускаю.", message.chat_id)
            return
        if message.type is not MessageTypes.NON_SYSTEM:
            logger.debug("Чат %s: системное сообщение (%s), пропускаю.", message.chat_id, message.type)
            return
        if not (message.text or "").strip():
            logger.debug("Чат %s: сообщение без текста, пропускаю.", message.chat_id)
            return

        chat_id = message.chat_id
        author = message.author or message.chat_name or "покупатель"
        logger.info("Сообщение от %s (чат %s): %s", author, chat_id, _short(message.text))

        if not self._has_greeted(chat_id):
            # Приветствие уже содержит список аккаунтов, поэтому сразу гасим группу
            # «статус»: иначе на «какие есть свободные?» улетят два списка подряд.
            if self._send(chat_id, self._GREETING, self.triggers.greeting):
                self._greeted[chat_id] = True
                self._mark_sent(chat_id, "статус")
            return

        # Вопрос про конкретный номер разбирается раньше групп (внутри route):
        # «акк 1 свободен?» должен получить статус этого аккаунта, а не общий список.
        route = self.triggers.route(message.text, load_accounts(), lots=load_cache())
        if route is not None:
            sent = self._send(chat_id, route.key, route.reply)
            if sent and route.alert_seller:
                self._queue_seller_alert(chat_id, author, message.text)
            if sent and route.group is not None and route.group.notify:
                self._notify_after_reply(route.group.notify, chat_id, author, message.text)
            return

        # Ни один триггер не подошёл. Продавца дёргаем в любом случае — в том числе
        # на втором и третьем непонятном сообщении, когда отписка уже уходила.
        self._queue_seller_alert(chat_id, author, message.text)

        # А покупателю отписка — один раз на чат, дальше молчим: ответит человек.
        if (chat_id, self._FALLBACK) in self._last_reply:
            logger.info("Чат %s: триггеров нет, отписка уже отправлена — молчу.", chat_id)
            return
        self._send(chat_id, self._FALLBACK, self.triggers.fallback, antispam=False)

    def _notify_after_reply(self, kind: str, chat_id: int | str, author: str, text: str) -> None:
        """Группа просит позвать продавца после ответа (поле notify в triggers.json)."""
        if kind == "seller":
            self._queue_seller_alert(chat_id, author, text)
        elif kind == "extension":
            self._notify_extension(chat_id)
        else:
            logger.warning("Неизвестный notify=%r в triggers.json — пропускаю.", kind)

    def _notify_extension(self, chat_id: int | str) -> None:
        """Просьба продлить: зовём продавца, только если в этом чате сейчас идёт аренда.

        Время сами НЕ продлеваем — продление подтверждает продавец.
        """
        if self.on_extension_request is None:
            return
        account = active_rental(chat_id)
        if account is None:
            logger.info("Чат %s: просят продлить, но активной аренды в этом чате нет.", chat_id)
            return
        try:
            self.on_extension_request(account, self._chat_link(chat_id))
            logger.info("Чат %s: продавцу отправлена просьба продлить «%s».", chat_id, account.title)
        except Exception as e:
            logger.error("Не смог передать просьбу продлить по чату %s: %s: %s",
                         chat_id, type(e).__name__, e)

    # ------------------------------------------------------------------ уведомления продавцу

    def _chat_link(self, chat_id: int | str) -> str:
        """Ссылка на чат. Строит сама библиотека — так же, как в Account.get_chat()."""
        try:
            return self.account.normalize_url(f"chat/?node={chat_id}")
        except Exception as e:
            logger.warning("Не смог собрать ссылку на чат %s: %s: %s", chat_id, type(e).__name__, e)
            return ""

    def _queue_seller_alert(self, chat_id: int | str, author: str, text: str) -> None:
        """Кладёт уведомление в очередь и пробует отправить прямо сейчас."""
        if self.on_seller_alert is None:
            return
        clean = " ".join((text or "").split())
        if len(clean) > ALERT_TEXT_LIMIT:
            clean = clean[: ALERT_TEXT_LIMIT - 1] + "…"
        with self._alert_lock:
            # Держим только последнее сообщение: если покупатель написал три раза
            # за время тишины, продавцу уйдёт самое свежее.
            self._alert_pending[chat_id] = (author, clean, self._chat_link(chat_id))
        self._flush_alert(chat_id)

    def _flush_alert(self, chat_id: int | str) -> None:
        """Отправляет накопленное по чату, если кулдаун истёк."""
        now = time.monotonic()
        with self._alert_lock:
            pending = self._alert_pending.get(chat_id)
            if pending is None:
                return
            sent_at = self._alert_sent_at.get(chat_id)
            if sent_at is not None and now - sent_at < self.triggers.seller_alert_seconds:
                left = int(self.triggers.seller_alert_seconds - (now - sent_at))
                logger.info("Чат %s: уведомление продавцу придержано, ещё %d с.", chat_id, left)
                return
            # Забираем под замком: два потока не должны отправить одно и то же дважды.
            self._alert_pending.pop(chat_id, None)
            self._alert_sent_at[chat_id] = now

        try:
            self.on_seller_alert(*pending)
            logger.info("Чат %s: продавцу отправлено уведомление о непонятном сообщении.", chat_id)
        except Exception as e:
            # Телеграм мог отвалиться — автоответчик из-за этого падать не должен.
            logger.error("Не смог уведомить продавца по чату %s: %s: %s",
                         chat_id, type(e).__name__, e)

    def _alert_loop(self) -> None:
        """Досылает то, что придержал кулдаун: покупатель мог замолчать после него."""
        while not self._stop.is_set():
            self._stop.wait(ALERT_FLUSH_TICK_SECONDS)
            if self._stop.is_set():
                return
            try:
                with self._alert_lock:
                    waiting = list(self._alert_pending)
                for chat_id in waiting:
                    self._flush_alert(chat_id)
            except Exception as e:
                logger.error("Тикер уведомлений споткнулся: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ вспомогательное

    def _has_greeted(self, chat_id: int | str) -> bool:
        """Есть ли в истории чата мои сообщения? Ответ кэшируется на время работы бота."""
        cached = self._greeted.get(chat_id)
        if cached is not None:
            return cached
        try:
            chat = self.account.get_chat(chat_id, with_history=True)
        except Exception as e:
            # История не получена — считаем, что уже здоровались. Лучше промолчать,
            # чем поприветствовать человека посреди переписки.
            logger.warning("Не смог получить историю чата %s (%s: %s) — приветствие пропускаю.",
                           chat_id, type(e).__name__, e)
            return True
        greeted = any(m.author_id == self.account.id for m in chat.messages)
        self._greeted[chat_id] = greeted
        return greeted

    def _mark_sent(self, chat_id: int | str, group_name: str) -> None:
        self._last_reply[(chat_id, group_name)] = time.monotonic()

    def _send(self, chat_id: int | str, group_name: str, text: str, antispam: bool = True) -> bool:
        """Отправляет ответ, уважая антиспам. Возвращает True, если сообщение ушло."""
        key = (chat_id, group_name)
        if antispam:
            sent_at = self._last_reply.get(key)
            if sent_at is not None and time.monotonic() - sent_at < self.triggers.antispam_seconds:
                left = self.triggers.antispam_seconds - (time.monotonic() - sent_at)
                logger.info("Чат %s: ответ «%s» уже отправлен, антиспам ещё %d с — молчу.",
                            chat_id, group_name, int(left))
                return False

        # {accounts} подставляется в момент отправки, чтобы список был актуальным.
        body = text.replace("{accounts}", render_accounts()) if "{accounts}" in text else text
        try:
            self.account.send_message(chat_id, body)
        except Exception as e:
            logger.error("Не смог отправить ответ «%s» в чат %s: %s: %s",
                         group_name, chat_id, type(e).__name__, e)
            return False

        self._mark_sent(chat_id, group_name)
        logger.info("Чат %s: отправлен ответ «%s».", chat_id, group_name)
        return True


def _short(text: str | None, limit: int = 70) -> str:
    if not text:
        return "—"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
