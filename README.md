<div align="center">

# 🎮 FunPay Rent Bot

**Автоответчик и пульт аренды игровых аккаунтов на FunPay — отвечает покупателям, пока вы заняты**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Made in Russia](https://img.shields.io/badge/Made%20in-Russia%20🇷🇺-red)

</div>

---

## 📑 Оглавление

- [✨ Возможности](#-возможности)
- [🚀 Быстрый старт](#-быстрый-старт)
- [⚙️ Настройка](#️-настройка)
- [💬 Триггеры](#-триггеры)
- [📁 Структура проекта](#-структура-проекта)
- [📸 Скриншоты](#-скриншоты)
- [❓ FAQ](#-faq)
- [⚠️ Disclaimer](#️-disclaimer)
- [📫 Контакты](#-контакты)
- [🇬🇧 English](#-english)

---

## ✨ Возможности

**🤖 Автоответчик в чатах FunPay**
- Приветствие с актуальным списком аккаунтов на первое сообщение в чате.
- Ответы по ключевым словам из `triggers.json` — тексты меняются без правки кода.
- Вопрос про конкретный аккаунт («акк 2 свободен?», «второй акк», «2») — ответ по его текущему статусу.
- Распознаёт названный срок аренды («на 3 часа», «5ч», «полтора часа») и сверяет с минимальным сроком.
- Антиспам: один и тот же ответ не уходит в чат чаще, чем раз в N минут.
- Непонятный вопрос — покупателю короткая отписка, продавцу уведомление в Telegram со ссылкой на чат.
- Сообщение покупателю сразу после оплаты заказа.

**⏱ Аренда**
- База аккаунтов в `accounts.json`: свободен / занят до какого времени.
- Аккаунт освобождается сам, когда срок вышел; арендатору уходит сообщение об окончании.
- Предупреждение арендатору за N минут до конца аренды (без дублей после перезапуска).
- Просьба продлить → уведомление продавцу (продлевает продавец сам).
- Всё время — по МСК, независимо от часового пояса компьютера.

**📱 Telegram-пульт** (необязательно)
- Новый заказ → кнопки «какой аккаунт» и «на сколько» (1/3/6/12/24 ч, своё время, ночной тариф).
- Меню «📊 Статус» и «🔓 Освободить аккаунт».

**🌙 Ночной тариф**
- Лоты с «ночной тариф» в названии находятся автоматически, цена и ссылка берутся из лота.
- Срок — до конца ночного окна (по умолчанию 20:00–06:00 МСК).

**📈 Автоподнятие лотов** по кулдауну FunPay.

---

## 🚀 Быстрый старт

> Подробная инструкция для новичков — в [SETUP.md](SETUP.md).

```powershell
# 1. Виртуальное окружение и зависимости
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Библиотека FunPayAPI (в репозиторий не входит — см. SETUP.md, шаг 3)

# 3. Настройки
copy .env.example .env      # и заполните GOLDEN_KEY, USER_AGENT

# 4. Запуск
.\.venv\Scripts\python.exe main.py
```

Остановка — `Ctrl+C`.

---

## ⚙️ Настройка

### Переменные `.env`

| Переменная | Обязательна | Что вписать |
|---|:---:|---|
| `GOLDEN_KEY` | ✅ | Cookie `golden_key` с funpay.com |
| `USER_AGENT` | ✅ | User-Agent **того же** браузера, из которого взят ключ |
| `CATEGORY_ID` | — | ID игры, лоты которой поднимаются (по умолчанию `123`) |
| `TG_TOKEN` | — | Токен Telegram-бота от @BotFather |
| `TG_ADMIN_ID` | — | Ваш числовой Telegram ID — бот отвечает только ему |
| `SESSION_REFRESH_MINUTES` | — | Как часто обновлять сессию FunPay (по умолчанию 40) |
| `DEFAULT_COOLDOWN_MINUTES` | — | Пауза, если FunPay не сообщил кулдаун (по умолчанию 30) |
| `ERROR_RETRY_MINUTES` | — | Пауза после сетевой ошибки (по умолчанию 2) |

Без `TG_TOKEN` и `TG_ADMIN_ID` бот работает, но без Telegram-пульта. Настройка Telegram — [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md).

### Настройки в `triggers.json`

| Ключ | По умолчанию | Что значит |
|---|---|---|
| `MIN_RENTAL_HOURS` | `2` | Минимальный срок аренды, часы |
| `WARN_MINUTES` | `20` | За сколько минут предупреждать о конце аренды |
| `NIGHT_START` / `NIGHT_END` | `20:00` / `06:00` | Ночное окно по МСК |
| `NIGHT_TARIFF_NAME` | `Ночной тариф` | Подпись кнопки в Telegram |
| `LOT_SYNC_MINUTES` | `30` | Как часто обновлять кэш своих лотов |
| `antispam_minutes` | `10` | Не повторять один ответ в чате чаще |

### Аккаунты — `accounts.json`

```json
[
  { "id": 1, "title": "Акк 1", "description": "", "price": "", "status": "free", "free_until": null }
]
```

`id` не меняйте — на него завязаны кнопки. Номер аккаунта для покупателя — это позиция записи в файле.

---

## 💬 Триггеры

Все тексты бота лежат в [`triggers.json`](triggers.json). В репозитории — 5 примеров групп, свои добавляйте по образцу.

### Формат группы

```json
{
  "name": "payment",
  "keywords": ["как оплатить", "куда платить"],
  "keywords_exact": ["оплата"],
  "reply": "Оплата только через FunPay: нажмите «Купить» на лоте.",
  "notify": "seller"
}
```

| Поле | Что делает |
|---|---|
| `name` | Уникальное имя группы |
| `keywords` | Ищутся **с начала слова**: `андроид` поймает «на андроиде» |
| `keywords_exact` | Только **слово целиком** — для коротких слов, которые бывают началом других |
| `reply` | Ответ покупателю. `{accounts}` → список аккаунтов, `{MIN_RENTAL_TEXT}` → «2 часа» |
| `notify` | Необязательно. `"seller"` — после ответа позвать продавца в Telegram, `"extension"` — переслать просьбу продлить |
| `match` | Необязательно. `"duration"` — группа ловит названный срок аренды, а не слова |

### Правила

- **Порядок групп = приоритет.** Подошло несколько — отвечает верхняя. Конкретные группы ставьте выше общих.
- Текст перед поиском приводится к нижнему регистру, `ё` → `е`, пунктуация убирается: `!цена` и `ЦЕНА???` — одно и то же.
- Вопрос с номером аккаунта («акк 2 …») всегда проверяется **раньше** групп.
- Не готов текст — впишите в ответ `[впиши]`: группа будет выключена, покупатель скобок не увидит.
- `triggers.json` читается при старте — после правки перезапустите бота.
- Проверить, куда уйдёт фраза, без запуска бота: допишите её в `CASES` в `test_triggers.py` и выполните `python test_triggers.py`.

### Ночной тариф (необязательно)

Добавьте группу с `"special": "night_tariff"` — цена и ссылка подставятся из ночного лота:

```json
{
  "name": "night_tariff",
  "special": "night_tariff",
  "keywords": ["ночной тариф", "на ночь", "до утра"],
  "reply": "🌙 {NIGHT_TARIFF_NAME}: с {NIGHT_START} до {NIGHT_END} МСК\nЦена: {цена}₽\nЛот: {ссылка}",
  "reply_many": "🌙 {NIGHT_TARIFF_NAME}: с {NIGHT_START} до {NIGHT_END} МСК\n\n{lots}",
  "line": "{номер} — {цена}₽\n{ссылка}",
  "reply_no_lot": "Уточню у продавца, он скоро ответит.",
  "reply_free": "Свободен — можете оплачивать:\n{ссылка}",
  "reply_busy": "Аккаунт {номер} занят до {время}.",
  "booking_group": "booking"
}
```

---

## 📁 Структура проекта

```
.
├── main.py              # точка входа: логин и запуск всех потоков
├── config.py            # чтение .env, понятные ошибки при пустых ключах
├── auto_responder.py    # автоответчик: события FunPay, триггеры, антиспам
├── accounts_db.py       # accounts.json: статусы, автоосвобождение, предупреждения
├── telegram_bot.py      # Telegram-пульт: заказы, сроки, статус, освобождение
├── lot_raiser.py        # автоподнятие лотов
├── lot_sync.py          # кэш своих лотов (lots_cache.json) для ночного тарифа
├── night.py             # ночное окно и расчёт конца ночной аренды
├── msk.py               # московское время
├── triggers.json        # тексты бота и триггеры (примеры)
├── accounts.json        # ваши аккаунты (пример)
├── test_triggers.py     # проверка триггеров без сети
├── test_warning.py      # проверка предупреждения о конце аренды
├── test_night.py        # проверка ночного тарифа и МСК
├── test_connection.py   # проверка связи с FunPay (нужен .env)
├── FunPayAPI/           # библиотека, скачивается отдельно (SETUP.md)
├── .env.example         # шаблон настроек
└── docs/screenshots/    # скриншоты для README
```

---

## 📸 Скриншоты

> Скоро здесь появятся скриншоты (`docs/screenshots/`).

---

## ❓ FAQ

<details>
<summary><b>Бот пишет «FunPay не признал сессию»</b></summary>

`golden_key` устарел или `USER_AGENT` не от того браузера. Возьмите оба значения заново из одного и того же браузера.
</details>

<details>
<summary><b>Почему FunPayAPI не через pip?</b></summary>

Версия в PyPI устарела: запросы к `/runner/` возвращают 400. Нужна копия из форка — команда в [SETUP.md](SETUP.md).
</details>

<details>
<summary><b>Бот не отвечает в чатах</b></summary>

Он не отвечает на переписку, накопившуюся до запуска, — только на новые сообщения. Проверьте лог `bot.log` и что запущен **один** экземпляр бота на аккаунт.
</details>

<details>
<summary><b>Можно без Telegram?</b></summary>

Да. Автоответчик и автоподнятие работают и без него, но без пульта: аренду придётся отмечать в `accounts.json` вручную.
</details>

---

## ⚠️ Disclaimer

- Используйте на свой риск. Автоматизация может противоречить правилам FunPay — ознакомьтесь с ними сами.
- **Никому не передавайте `golden_key`** — это полный доступ к вашему аккаунту FunPay, включая баланс.
- Не публикуйте `.env` и токен Telegram-бота.
- Сначала протестируйте бота на аккаунте **без денег на балансе**.

---

## 📫 Контакты

Telegram: [@samsa_lore](https://t.me/samsa_lore)

---

## 🇬🇧 English

**FunPay Rent Bot** — an auto-responder and rental control panel for selling game account rentals on [FunPay](https://funpay.com).

**Features**
- Auto-replies in FunPay chats by keyword triggers from `triggers.json` (no code changes needed), greeting with the live account list, account status by number, rental duration recognition with a minimum term, anti-spam, seller alerts in Telegram for unknown questions.
- Rental tracking in `accounts.json`: auto-release on expiry, a warning N minutes before the end, end-of-rental message. All times are Moscow time (MSK).
- Optional Telegram panel: new order → pick account and duration (1/3/6/12/24 h, custom, night tariff), status and manual release.
- Night tariff: lots with "ночной тариф" in the title are detected automatically.
- Automatic lot raising.

**Quick start**
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# download the FunPayAPI library — see SETUP.md, step 3
cp .env.example .env                            # fill in GOLDEN_KEY and USER_AGENT
.venv/bin/python main.py
```

**Triggers.** Each group in `triggers.json` has `name`, `keywords` (prefix match), optional `keywords_exact` (whole word) and `reply`. Group order = priority. A reply containing `[впиши]` disables the group.

**Disclaimer.** Use at your own risk. Never share your `golden_key` — it gives full access to your FunPay account. Test on an account with an empty balance first.

**Contact:** Telegram [@samsa_lore](https://t.me/samsa_lore) · **License:** MIT
