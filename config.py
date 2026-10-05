"""Настройки проекта: читаются из .env, лежащего рядом с этим файлом."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


# PUBG Mobile. Подкатегория «Аккаунты PUBG Mobile» (346) принадлежит именно этой игре.
DEFAULT_CATEGORY_ID = 123


class ConfigError(RuntimeError):
    """Настройки отсутствуют или заполнены некорректно."""


@dataclass(frozen=True)
class Config:
    golden_key: str
    user_agent: str

    # ID категории (игры), лоты которой поднимаем. raise_lots() принимает именно
    # ID игры, а не подкатегории: FunPay поднимает всю игру одним запросом.
    category_id: int

    # Как часто дёргать account.get(). Документация библиотеки рекомендует
    # обновлять сессию не реже раза в час, берём запас.
    session_refresh_minutes: int

    # Сколько спать, если FunPay не сказал, когда можно поднимать снова.
    default_cooldown_minutes: int

    # Пауза перед повтором после сетевой ошибки.
    error_retry_minutes: int

    # Telegram. Если токена нет, бот работает без Telegram-управления.
    tg_token: str | None
    tg_admin_id: int | None

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_token and self.tg_admin_id)


def _require(name: str) -> str:
    # .strip() важен: golden_key часто копируют с лишним пробелом или переносом строки,
    # и FunPay молча отвечает "не авторизован".
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(
            f"В .env не задан {name}.\n"
            f"Файл: {ENV_PATH}\n"
            f"Возьми за образец .env.example и заполни оба значения."
        )
    return value


def _require_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"В .env значение {name}={raw!r} не является целым числом.")
    if value <= 0:
        raise ConfigError(f"В .env значение {name}={value} должно быть больше нуля.")
    return value


def _optional(name: str) -> str | None:
    return (os.getenv(name) or "").strip() or None


def _optional_int(name: str) -> int | None:
    raw = _optional(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"В .env значение {name}={raw!r} не является числом.\n"
            f"TG_ADMIN_ID — это числовой id твоего аккаунта в Telegram, его отдаёт @userinfobot."
        )


def load_config() -> Config:
    if not ENV_PATH.exists():
        raise ConfigError(
            f"Файл .env не найден: {ENV_PATH}\n"
            f"Скопируй .env.example в .env и заполни GOLDEN_KEY и USER_AGENT."
        )
    load_dotenv(ENV_PATH, override=True)
    return Config(
        golden_key=_require("GOLDEN_KEY"),
        user_agent=_require("USER_AGENT"),
        category_id=_require_int("CATEGORY_ID", DEFAULT_CATEGORY_ID),
        session_refresh_minutes=_require_int("SESSION_REFRESH_MINUTES", 40),
        default_cooldown_minutes=_require_int("DEFAULT_COOLDOWN_MINUTES", 30),
        error_retry_minutes=_require_int("ERROR_RETRY_MINUTES", 2),
        tg_token=_optional("TG_TOKEN"),
        tg_admin_id=_optional_int("TG_ADMIN_ID"),
    )


def mask(secret: str, visible: int = 4) -> str:
    """Безопасное представление секрета для вывода в консоль/логи."""
    if len(secret) <= visible * 2:
        return "*" * len(secret)
    return f"{secret[:visible]}...{secret[-visible:]} (длина {len(secret)})"
