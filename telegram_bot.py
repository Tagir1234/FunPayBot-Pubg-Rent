"""Telegram-управление арендой. Отвечает только админу из TG_ADMIN_ID.

Сценарий выдачи:
    новый заказ на FunPay -> уведомление с кнопками аккаунтов
    -> выбрал аккаунт     -> кнопки сроков (1/3/6/12/24 часа, «Своё время», «🌙 Ночной тариф»)
    -> выбрал срок        -> аккаунт занят, покупателю ушло сообщение, админу «Готово».

Всё время — по МСК (msk.py), независимо от часового пояса компьютера. «Своё время»,
которое вводит админ, тоже считается московским.

Постоянное меню внизу: «📊 Статус» и «🔓 Освободить аккаунт».
"""

from __future__ import annotations

import html
import logging
import re
import threading
from datetime import datetime, timedelta

import telebot
from telebot import types as tg

import accounts_db
import lot_sync
from accounts_db import RentAccount
from auto_responder import minutes_text
from msk import now_msk, to_msk
from night import NightSettings, night_rental

logger = logging.getLogger("telegram")

BTN_STATUS = "📊 Статус"
BTN_RELEASE = "🔓 Освободить аккаунт"

# Типовые сроки аренды для кнопок: (подпись, минуты).
DURATIONS: tuple[tuple[str, int], ...] = (
    ("1 час", 60),
    ("3 часа", 180),
    ("6 часов", 360),
    ("12 часов", 720),
    ("24 часа", 1440),
)

# Как часто проверять, не вышел ли срок аренды.
EXPIRY_TICK_SECONDS = 60


# ---------------------------------------------------------------------- разбор своего времени

_TIME_OF_DAY = re.compile(r"^(?:до\s+)?(\d{1,2})[:.](\d{2})$")
_DURATION = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([а-яёa-z]*)$")

_UNITS_MINUTES = ("м", "мин", "минут", "минуты", "минута", "m", "min")
_UNITS_HOURS = ("", "ч", "час", "часа", "часов", "h", "hour", "hours")
_UNITS_DAYS = ("д", "дн", "день", "дня", "дней", "сутки", "d", "day", "days")


def parse_custom_time(text: str, now: datetime | None = None) -> datetime | None:
    """«2ч», «90 мин», «1.5 часа», «3 дня», «21:00» -> момент окончания аренды.

    Голое число считаем часами: чаще всего пишут именно их.
    Время суток («21:00») — по МСК: сегодня, а если этот час уже прошёл, то завтра.
    """
    now = to_msk(now) if now else now_msk()
    cleaned = text.strip().lower().replace("ё", "е")
    if not cleaned:
        return None

    if (m := _TIME_OF_DAY.match(cleaned)) is not None:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return None
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)

    if (m := _DURATION.match(cleaned)) is not None:
        amount = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if amount <= 0:
            return None
        if unit in _UNITS_MINUTES:
            delta = timedelta(minutes=amount)
        elif unit in _UNITS_HOURS:
            delta = timedelta(hours=amount)
        elif unit in _UNITS_DAYS:
            delta = timedelta(days=amount)
        else:
            return None
        # Округляем до минуты: секунды в таком сроке только мешают читать.
        return (now + delta).replace(second=0, microsecond=0)

    return None


# ---------------------------------------------------------------------- клавиатуры


def main_keyboard() -> tg.ReplyKeyboardMarkup:
    kb = tg.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_STATUS, BTN_RELEASE)
    return kb


def _accounts_keyboard(accounts: list[RentAccount], prefix: str, order_id: str = "-") -> tg.InlineKeyboardMarkup:
    kb = tg.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        tg.InlineKeyboardButton(
            a.title if a.is_free else f"{a.title} (занят)",
            callback_data=f"{prefix}:{order_id}:{a.id}",
        )
        for a in accounts
    ])
    return kb


def _durations_keyboard(order_id: str, account_id: int, night_label: str | None = None) -> tg.InlineKeyboardMarkup:
    kb = tg.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        tg.InlineKeyboardButton(label, callback_data=f"dur:{order_id}:{account_id}:{minutes}")
        for label, minutes in DURATIONS
    ])
    kb.add(tg.InlineKeyboardButton("✏️ Своё время", callback_data=f"dur:{order_id}:{account_id}:custom"))
    if night_label:
        kb.add(tg.InlineKeyboardButton(f"🌙 {night_label}", callback_data=f"dur:{order_id}:{account_id}:night"))
    return kb


def _night_confirm_keyboard(order_id: str, account_id: int, until: datetime) -> tg.InlineKeyboardMarkup:
    # Время кладём в callback как unix-время: после «Да» займём ровно то, что показали.
    kb = tg.InlineKeyboardMarkup(row_width=2)
    kb.add(
        tg.InlineKeyboardButton("✅ Да", callback_data=f"nok:{order_id}:{account_id}:{int(until.timestamp())}"),
        tg.InlineKeyboardButton("✖️ Отмена", callback_data=f"ncancel:{order_id}:{account_id}"),
    )
    return kb


# ---------------------------------------------------------------------- бот


class TelegramBot:
    """Пульт управления арендой в Telegram. Живёт в своём потоке."""

    def __init__(self, account, token: str, admin_id: int, templates: dict | None = None,
                 warn_minutes: int = 20, night: NightSettings | None = None) -> None:
        self.account = account
        self.admin_id = admin_id
        # Тексты писем покупателю: приходят из triggers.json (см. load_triggers).
        self.templates = templates or {}
        # За сколько минут до конца аренды предупреждать (WARN_MINUTES в triggers.json).
        self.warn_minutes = warn_minutes
        # Ночной тариф (NIGHT_START / NIGHT_END / NIGHT_TARIFF_NAME). None — кнопки нет.
        self.night = night
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_safe, name="telegram", daemon=True)
        self._ticker = threading.Thread(target=self._expiry_loop, name="rent-expiry", daemon=True)

        # order_id -> заказ с FunPay. Нужен, чтобы после выбора срока знать, куда писать покупателю.
        self._orders: dict[str, object] = {}
        # Ждём от админа текст со своим временем: admin_id -> (order_id, account_id).
        self._awaiting_time: dict[int, tuple[str, int]] = {}

        self._register_handlers()

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        # Срок аренды может истечь в любом потоке — не только в тикере, но и при
        # обычном чтении списка. Поэтому уведомление висит на общем обработчике.
        accounts_db.set_expiry_handler(self._on_rent_expired)
        accounts_db.set_warning_handler(self._on_rent_warning)
        logger.info("Telegram-бот запускается, админ %s.", self.admin_id)
        self._thread.start()
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.bot.stop_polling()
        except Exception:
            pass

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run_safe(self) -> None:
        try:
            self._say(
                "Бот запущен. Новые заказы буду присылать сюда.",
                reply_markup=main_keyboard(),
            )
            # allowed_updates не сужаем: нужны и сообщения, и нажатия кнопок.
            self.bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
        except BaseException:  # noqa: BLE001
            logger.exception("Поток Telegram-бота аварийно завершился.")
        else:
            logger.info("Поток Telegram-бота остановлен.")

    # ------------------------------------------------------------------ автоосвобождение

    def _expiry_loop(self) -> None:
        """Раз в минуту: освобождает истёкшие аренды и предупреждает о скором конце."""
        while not self._stop.is_set():
            try:
                # Уведомления рассылает _on_rent_expired, его дёргает сам accounts_db.
                accounts_db.expire_due()
            except Exception as e:
                logger.error("Тикер автоосвобождения споткнулся: %s: %s", type(e).__name__, e)
            try:
                # А предупреждения — _on_rent_warning.
                accounts_db.warn_due(self.warn_minutes)
            except Exception as e:
                logger.error("Тикер предупреждений споткнулся: %s: %s", type(e).__name__, e)
            self._stop.wait(EXPIRY_TICK_SECONDS)

    def _on_rent_warning(self, account: RentAccount, minutes_left: int) -> None:
        """До конца аренды осталось WARN_MINUTES или меньше: пишем арендатору и админу."""
        until = f"{account.free_until:%H:%M}" if account.free_until else "—"
        left = minutes_text(minutes_left)
        note = ""
        text = self.templates.get("rental_warning")
        if not text:
            note = " Шаблона rental_warning нет в triggers.json — арендатору не написал."
        else:
            try:
                self.account.send_message(account.renter_chat_id,
                                          text.replace("{осталось}", left).replace("{время}", until))
            except Exception as e:
                logger.error("Не смог предупредить арендатора в чате %s: %s: %s",
                             account.renter_chat_id, type(e).__name__, e)
                note = f" ⚠️ Арендатору предупреждение НЕ ушло ({type(e).__name__})."

        alert = self.templates.get("warning_alert")
        if not alert:
            logger.error("Шаблона warning_alert нет в triggers.json — админу не написал.%s", note)
            return
        self._say(alert
                  .replace("{осталось}", left)
                  .replace("{аккаунт}", html.escape(account.title))
                  .replace("{номер}", str(account.position))
                  .replace("{время}", until) + note)

    def notify_extension_request(self, account: RentAccount, link: str) -> None:
        """Арендатор написал «хочу продлить». Продлевает админ сам — бот только сообщает."""
        template = self.templates.get("extension_alert")
        if not template:
            logger.error("Шаблона extension_alert нет в triggers.json — продавцу не написал.")
            return
        until = f"{account.free_until:%H:%M}" if account.free_until else "—"
        self._say(template
                  .replace("{аккаунт}", html.escape(account.title))
                  .replace("{номер}", str(account.position))
                  .replace("{время}", until)
                  .replace("{ссылка}", html.escape(link or "—")),
                  disable_web_page_preview=True)

    def _on_rent_expired(self, account: RentAccount) -> None:
        """Срок аренды вышел: пишем арендатору, сообщаем админу."""
        note = ""
        if account.renter_chat_id:
            text = self.templates.get("rental_ended")
            if not text:
                note = " Шаблона rental_ended нет в triggers.json — арендатору не написал."
            else:
                try:
                    self.account.send_message(account.renter_chat_id, text)
                except Exception as e:
                    logger.error("Не смог написать об окончании аренды в чат %s: %s: %s",
                                 account.renter_chat_id, type(e).__name__, e)
                    note = f" ⚠️ Арендатору сообщение НЕ ушло ({type(e).__name__})."
        else:
            note = " Чат арендатора неизвестен — сообщение не отправлял."

        self._say(f"Аренда <b>{account.title}</b> закончилась, аккаунт свободен.{note}")

    # ------------------------------------------------------------------ отправка

    def _say(self, text: str, **kwargs) -> None:
        """Сообщение админу. Telegram может быть недоступен — это не повод падать."""
        try:
            self.bot.send_message(self.admin_id, text, **kwargs)
        except Exception as e:
            logger.error("Не смог написать в Telegram: %s: %s", type(e).__name__, e)

    def notify_seller_alert(self, nick: str, text: str, link: str) -> None:
        """Покупатель написал что-то, на что автоответчик не знает ответа."""
        template = self.templates.get("seller_alert")
        if not template:
            logger.error("Шаблона seller_alert нет в triggers.json — продавцу не написал.")
            return
        # Бот шлёт с parse_mode=HTML, а в сообщении покупателя могут быть < > &.
        # Без экранирования Telegram отвергнет такое сообщение целиком.
        message = (template
                   .replace("{ник}", html.escape(nick or "—"))
                   .replace("{текст}", html.escape(text or "—"))
                   .replace("{ссылка}", html.escape(link or "—")))
        self._say(message, disable_web_page_preview=True)

    def notify_new_order(self, order) -> None:
        """Вызывается из потока автоответчика при NewOrderEvent."""
        try:
            self._orders[order.id] = order
            accounts = accounts_db.load_accounts()
            text = (
                f"🛒 <b>Новый заказ</b> #{order.id}\n\n"
                f"Покупатель: {order.buyer_username}\n"
                f"Лот: {order.description or '—'}\n"
                f"Сумма: {order.price} {order.currency.name if order.currency else ''}\n"
                f"https://funpay.com/orders/{order.id}/\n\n"
                f"Какой аккаунт сдаём?"
            )
            if not accounts:
                self._say(text + "\n\n⚠️ accounts.json пуст — выбирать не из чего.")
                return
            self._say(text, reply_markup=_accounts_keyboard(accounts, "pick", order.id))
        except Exception as e:
            logger.error("Не смог оповестить о заказе: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ хэндлеры

    def _is_admin(self, user_id: int) -> bool:
        return user_id == self.admin_id

    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(commands=["start", "menu"])
        def on_start(message):
            if not self._is_admin(message.chat.id):
                return self._reject(message)
            bot.send_message(
                message.chat.id,
                "Меню внизу. «Статус» — текущий список, «Освободить» — снять аренду вручную.",
                reply_markup=main_keyboard(),
            )

        @bot.message_handler(func=lambda m: True, content_types=["text"])
        def on_text(message):
            if not self._is_admin(message.chat.id):
                return self._reject(message)
            text = (message.text or "").strip()

            if text == BTN_STATUS:
                return bot.send_message(message.chat.id, accounts_db.render_accounts(),
                                        reply_markup=main_keyboard())
            if text == BTN_RELEASE:
                return self._show_release_menu(message.chat.id)

            # Ждём своё время после нажатия «Своё время»?
            pending = self._awaiting_time.get(message.chat.id)
            if pending is not None:
                return self._apply_custom_time(message, pending)

            bot.send_message(message.chat.id, "Не понял. Жми кнопки внизу.",
                             reply_markup=main_keyboard())

        @bot.callback_query_handler(func=lambda c: True)
        def on_callback(call):
            if not self._is_admin(call.from_user.id):
                return
            try:
                self._handle_callback(call)
            except Exception as e:
                logger.error("Ошибка обработки кнопки %r: %s: %s", call.data, type(e).__name__, e)
                logger.debug("TRACEBACK", exc_info=True)
                self.bot.answer_callback_query(call.id, "Что-то пошло не так, смотри логи.")

    def _reject(self, message) -> None:
        logger.warning("Чужой чат %s написал боту: %r", message.chat.id,
                       (message.text or "")[:50])

    # ------------------------------------------------------------------ шаги сценария

    def _handle_callback(self, call) -> None:
        parts = (call.data or "").split(":")
        action = parts[0]

        if action == "pick" and len(parts) == 3:
            order_id, account_id = parts[1], int(parts[2])
            account = accounts_db.get_account(account_id)
            if account is None:
                return self.bot.answer_callback_query(call.id, "Такого аккаунта уже нет в accounts.json.")
            self.bot.answer_callback_query(call.id)
            self.bot.send_message(
                call.message.chat.id,
                f"Аккаунт: <b>{account.title}</b>\nНа сколько сдаём?",
                reply_markup=_durations_keyboard(order_id, account_id,
                                                 self.night.name if self.night else None),
            )
            return

        if action == "dur" and len(parts) == 4:
            order_id, account_id, value = parts[1], int(parts[2]), parts[3]
            if value == "custom":
                self._awaiting_time[call.message.chat.id] = (order_id, account_id)
                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    "Пришли время следующим сообщением.\n"
                    "Можно так: <code>2ч</code>, <code>90 мин</code>, <code>1.5 часа</code>, "
                    "<code>3 дня</code> или <code>21:00</code> (по МСК).",
                )
                return
            if value == "night":
                self.bot.answer_callback_query(call.id)
                return self._start_night(call.message.chat.id, order_id, account_id)
            self.bot.answer_callback_query(call.id)
            until = (now_msk() + timedelta(minutes=int(value))).replace(second=0, microsecond=0)
            self._finish(call.message.chat.id, order_id, account_id, until)
            return

        if action == "nok" and len(parts) == 4:
            self.bot.answer_callback_query(call.id)
            until = to_msk(datetime.fromtimestamp(int(parts[3]), tz=now_msk().tzinfo))
            self._finish(call.message.chat.id, parts[1], int(parts[2]), until)
            return

        if action == "ncancel" and len(parts) == 3:
            self.bot.answer_callback_query(call.id, "Отменил")
            self.bot.send_message(call.message.chat.id, "Ночной тариф не выдан. Выбери срок заново "
                                  "или нажми «Своё время».", reply_markup=main_keyboard())
            return

        if action == "free" and len(parts) == 3:
            account_id = int(parts[2])
            account = accounts_db.release(account_id)
            if account is None:
                return self.bot.answer_callback_query(call.id, "Такого аккаунта уже нет.")
            self.bot.answer_callback_query(call.id, "Освободил")
            self.bot.send_message(call.message.chat.id, f"🔓 <b>{account.title}</b> снова свободен.",
                                  reply_markup=main_keyboard())
            return

        self.bot.answer_callback_query(call.id)

    def _start_night(self, chat_id: int, order_id: str, account_id: int) -> None:
        """Кнопка «🌙 Ночной тариф»: до ближайшего NIGHT_END, днём — через подтверждение."""
        if self.night is None:
            return self.bot.send_message(chat_id, "Ночной тариф не настроен в triggers.json.")
        now = now_msk()

        # Часы, явно указанные в названии ночного лота, главнее окна.
        hours = self._night_lot_hours(order_id)
        if hours:
            until = (now + timedelta(hours=hours)).replace(second=0, microsecond=0)
            logger.info("Ночной тариф по заказу %s: в лоте указано %d ч — до %s.", order_id, hours, until)
            return self._finish(chat_id, order_id, account_id, until)

        rental = night_rental(self.night, now)
        if not rental.needs_confirm:
            return self._finish(chat_id, order_id, account_id, rental.until)

        template = self.templates.get("night_confirm")
        if not template:
            logger.error("Шаблона night_confirm нет в triggers.json — спрашиваю без него.")
            template = "{время}?"
        logger.info("Ночной тариф по заказу %s требует подтверждения: %s.", order_id, rental.reason)
        self.bot.send_message(
            chat_id,
            template.replace("{время}", f"{rental.until:%H:%M}"),
            reply_markup=_night_confirm_keyboard(order_id, account_id, rental.until),
        )

    def _night_lot_hours(self, order_id: str) -> int | None:
        """Часы из названия ночного лота, по которому пришёл заказ. None — срок по окну."""
        order = self._orders.get(order_id)
        if order is None or not order.description:
            return None
        title = " ".join(order.description.split()).lower()
        for lot in lot_sync.night_lots():
            if lot.hours and " ".join(lot.title.split()).lower() == title:
                return lot.hours
        return None

    def _apply_custom_time(self, message, pending: tuple[str, int]) -> None:
        order_id, account_id = pending
        until = parse_custom_time(message.text or "")
        if until is None:
            return self.bot.send_message(
                message.chat.id,
                "Не разобрал время. Примеры: <code>2ч</code>, <code>90 мин</code>, "
                "<code>1.5 часа</code>, <code>3 дня</code>, <code>21:00</code>.",
            )
        self._awaiting_time.pop(message.chat.id, None)
        self._finish(message.chat.id, order_id, account_id, until)

    def _finish(self, chat_id: int, order_id: str, account_id: int, until: datetime) -> None:
        """Занимает аккаунт, пишет покупателю на FunPay и отчитывается админу."""
        order = self._orders.get(order_id)
        # Чат арендатора кладём в accounts.json: по нему бот напишет об окончании
        # аренды даже после перезапуска.
        # Новая аренда сбрасывает предупреждение. Если она и так короче WARN_MINUTES —
        # предупреждать нечего, сразу помечаем warned.
        too_short = until - now_msk() <= timedelta(minutes=self.warn_minutes)
        account = accounts_db.occupy(account_id, until,
                                     renter_chat_id=order.chat_id if order else None,
                                     warned=too_short)
        if account is None:
            return self.bot.send_message(chat_id, "Не нашёл такой аккаунт в accounts.json.")

        note = self._notify_buyer(order_id, account, until)
        self.bot.send_message(
            chat_id,
            f"✅ Готово: <b>{account.title}</b> занят до {until:%H:%M} МСК ({until:%d.%m}).{note}",
            reply_markup=main_keyboard(),
        )

    def _notify_buyer(self, order_id: str, account: RentAccount, until: datetime) -> str:
        """Сообщение покупателю в чат FunPay. Возвращает приписку для отчёта админу."""
        order = self._orders.get(order_id)
        if order is None:
            # Заказ не в памяти: бот перезапускался между уведомлением и выбором.
            return "\n\n⚠️ Покупателю не написал: заказ потерян после перезапуска, напиши сам."
        text = self.templates.get("rental_started")
        if not text:
            return "\n\n⚠️ Шаблона rental_started нет в triggers.json — покупателю не написал."
        try:
            self.account.send_message(order.chat_id,
                                      text.replace("{время}", f"{until:%H:%M}"),
                                      chat_name=order.buyer_username)
        except Exception as e:
            logger.error("Не смог написать покупателю по заказу %s: %s: %s", order_id, type(e).__name__, e)
            return f"\n\n⚠️ Покупателю сообщение НЕ ушло ({type(e).__name__}), напиши сам."
        return f"\n\nПокупателю {order.buyer_username} сообщение отправил."

    # ------------------------------------------------------------------ меню освобождения

    def _show_release_menu(self, chat_id: int) -> None:
        busy = [a for a in accounts_db.load_accounts() if not a.is_free]
        if not busy:
            return self.bot.send_message(chat_id, "Все аккаунты и так свободны.",
                                         reply_markup=main_keyboard())
        kb = tg.InlineKeyboardMarkup(row_width=1)
        kb.add(*[
            tg.InlineKeyboardButton(f"{a.title} — {a.until_text()}", callback_data=f"free:-:{a.id}")
            for a in busy
        ])
        self.bot.send_message(chat_id, "Какой аккаунт освободить?", reply_markup=kb)
