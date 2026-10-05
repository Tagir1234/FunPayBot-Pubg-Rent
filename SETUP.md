# 🛠 Установка с нуля

Инструкция для тех, кто запускает Python-проект впервые. Команды для **Windows** (PowerShell) и **Mac/Linux** (Терминал) даны отдельно.

---

## 1. Установите Python

Нужен Python **3.12** или новее.

- **Windows:** скачайте с [python.org/downloads](https://www.python.org/downloads/). В первом окне установщика **обязательно отметьте «Add Python to PATH»**.
- **Mac:** скачайте с python.org или `brew install python@3.12`.
- **Linux:** `sudo apt install python3.12 python3.12-venv` (Ubuntu/Debian).

Проверьте:

```powershell
python --version        # Windows
python3 --version       # Mac/Linux
```

## 2. Скачайте проект

Зелёная кнопка **Code → Download ZIP** на странице репозитория, распакуйте. Или через git:

```bash
git clone https://github.com/ВАШ_НИК/ИМЯ_РЕПОЗИТОРИЯ.git
cd ИМЯ_РЕПОЗИТОРИЯ
```

Дальше все команды выполняйте **в папке проекта**.

## 3. Скачайте библиотеку FunPayAPI

Библиотека не входит в репозиторий: у её исходного проекта нет лицензии. Берётся из форка [sidor0912/FunPayCardinal](https://github.com/sidor0912/FunPayCardinal) (проверено на коммите `c09dd13`). Через pip **не ставьте** — версия в PyPI устарела и не работает.

**Windows:**
```powershell
git clone https://github.com/sidor0912/FunPayCardinal.git _upstream
git -C _upstream checkout c09dd13be25072bb3d5cdbba33d1e0d70e477710
Copy-Item .\_upstream\FunPayAPI .\FunPayAPI -Recurse
Remove-Item .\_upstream -Recurse -Force
```

**Mac/Linux:**
```bash
git clone https://github.com/sidor0912/FunPayCardinal.git _upstream
git -C _upstream checkout c09dd13be25072bb3d5cdbba33d1e0d70e477710
cp -r _upstream/FunPayAPI ./FunPayAPI
rm -rf _upstream
```

Должна появиться папка `FunPayAPI` рядом с `main.py`.

## 4. Виртуальное окружение и зависимости

**Windows:**
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Mac/Linux:**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 5. Получите Golden Key

1. Откройте [funpay.com](https://funpay.com) **в браузере на компьютере** и войдите в аккаунт.
2. Нажмите **F12** (откроются инструменты разработчика).
3. Вкладка **Application** (Chrome/Edge) или **Storage** (Firefox).
4. Слева: **Cookies → https://funpay.com**.
5. Найдите строку **`golden_key`** и скопируйте значение.

Там же нужен **User-Agent этого браузера**: вкладка **Console**, введите `navigator.userAgent` и нажмите Enter — скопируйте строку без кавычек.

> ⚠️ **Golden Key нельзя никому отправлять** — ни в чат, ни «в поддержку», ни в скриншотах. С ним можно войти в ваш аккаунт и распоряжаться балансом. Если ключ утёк — сразу смените пароль FunPay и обратитесь в поддержку FunPay.

## 6. Создайте `.env`

**Windows:** `copy .env.example .env` · **Mac/Linux:** `cp .env.example .env`

Откройте `.env` в блокноте и впишите значения после `=` без кавычек и пробелов:

```
GOLDEN_KEY=ваш_ключ
USER_AGENT=Mozilla/5.0 (Windows NT 10.0; ...) ...
```

Telegram-пульт (необязательно) — [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md).

Заполните `accounts.json` своими аккаунтами и тексты в `triggers.json` (см. README → «Триггеры»).

## 7. Проверьте связь и запустите

**Windows:**
```powershell
.\.venv\Scripts\python.exe test_connection.py   # вход, свои лоты, чаты
.\.venv\Scripts\python.exe main.py              # запуск бота
```

**Mac/Linux:**
```bash
.venv/bin/python test_connection.py
.venv/bin/python main.py
```

Лог пишется в консоль и в `bot.log`.

## 8. Остановка

Нажмите **Ctrl+C** в окне, где запущен бот.

> Запускайте **один** экземпляр бота на аккаунт FunPay — два процесса с одним ключом мешают друг другу.

---

## 🧯 Частые ошибки

| Ошибка | Причина и решение |
|---|---|
| `FunPay не признал сессию` / `UnauthorizedError` | `golden_key` устарел или `USER_AGENT` не от того браузера. Скопируйте **оба** значения заново из одного браузера. |
| Ошибка **400** при опросе | Установлена старая `FunPayAPI` из pip. Удалите её (`pip uninstall FunPayAPI`) и сделайте шаг 3. |
| `No module named 'requests'` (или другой модуль) | Зависимости не установлены или бот запущен **не** из `.venv`. Повторите шаг 4 и запускайте командой из шага 7, а не кнопкой Run в редакторе. |
| `No module named 'FunPayAPI'` | Не сделан шаг 3 или папка `FunPayAPI` лежит не рядом с `main.py`. |
| `В .env не задан GOLDEN_KEY` | Нет файла `.env` или значение пустое — шаг 6. |
| Бот не отвечает в чатах | На старую переписку он не отвечает — только на новые сообщения. Проверьте, что запущен один экземпляр, и смотрите `bot.log`. |
| Время не то | Всё считается по МСК. Если ошибка про часовой пояс — `pip install tzdata` в `.venv`. |
