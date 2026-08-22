from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_STUDIO_ADDRESS = "Университетская улица, 25/2, вход со стороны двора"
DEFAULT_MASTER_CONTACT = "@Kksdaun"
DEFAULT_INSTAGRAM_URL = "https://www.instagram.com/kksdaun/"
LEGACY_ADDRESS_PLACEHOLDERS = frozenset({
    "Адрес мастер сообщит после записи",
    "Сургут, укажите адрес студии",
})


def load_env(path: str | Path = ".env") -> None:
    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    studio_name: str
    master_name: str
    address: str
    contact: str
    timezone: ZoneInfo
    currency: str
    database_path: Path
    booking_horizon_days: int
    slot_step_minutes: int
    minimum_notice_minutes: int
    cancellation_notice_hours: int
    morning_summary_hour: int = 9
    evening_summary_hour: int = 20
    instagram_url: str = DEFAULT_INSTAGRAM_URL

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token == "123456789:YOUR_BOT_TOKEN":
            raise ValueError("Укажи настоящий BOT_TOKEN в файле .env. Токен выдаёт @BotFather.")

        raw_admins = os.getenv("ADMIN_IDS", "").strip()
        try:
            admins = frozenset(int(item.strip()) for item in raw_admins.split(",") if item.strip())
        except ValueError as exc:
            raise ValueError("ADMIN_IDS должен содержать Telegram ID через запятую.") from exc
        if not admins:
            raise ValueError("Укажи хотя бы один Telegram ID администратора в ADMIN_IDS.")

        timezone_name = os.getenv("TIMEZONE", "Asia/Yekaterinburg").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Неизвестный часовой пояс: {timezone_name}") from exc

        address = os.getenv("STUDIO_ADDRESS", DEFAULT_STUDIO_ADDRESS).strip()
        if not address or address in LEGACY_ADDRESS_PLACEHOLDERS:
            address = DEFAULT_STUDIO_ADDRESS
        contact = os.getenv("MASTER_CONTACT", DEFAULT_MASTER_CONTACT).strip()
        if not contact or contact == "@your_username":
            contact = DEFAULT_MASTER_CONTACT

        settings = cls(
            bot_token=token,
            admin_ids=admins,
            studio_name=os.getenv("STUDIO_NAME", "Brow & Lash Studio").strip(),
            master_name=os.getenv("MASTER_NAME", "Ксюша").strip(),
            address=address,
            contact=contact,
            timezone=timezone,
            currency=os.getenv("CURRENCY", "₽").strip(),
            database_path=Path(os.getenv("DATABASE_PATH", "data/beauty_bot.sqlite3")),
            booking_horizon_days=int(os.getenv("BOOKING_HORIZON_DAYS", "21")),
            slot_step_minutes=int(os.getenv("SLOT_STEP_MINUTES", "30")),
            minimum_notice_minutes=int(os.getenv("MINIMUM_NOTICE_MINUTES", "120")),
            cancellation_notice_hours=int(os.getenv("CANCELLATION_NOTICE_HOURS", "3")),
            morning_summary_hour=int(os.getenv("MORNING_SUMMARY_HOUR", "9")),
            evening_summary_hour=int(os.getenv("EVENING_SUMMARY_HOUR", "20")),
            instagram_url=os.getenv("STUDIO_INSTAGRAM", DEFAULT_INSTAGRAM_URL).strip() or DEFAULT_INSTAGRAM_URL,
        )
        if not 1 <= settings.booking_horizon_days <= 90:
            raise ValueError("BOOKING_HORIZON_DAYS должен быть в диапазоне 1–90.")
        if not 5 <= settings.slot_step_minutes <= 120:
            raise ValueError("SLOT_STEP_MINUTES должен быть в диапазоне 5–120.")
        if settings.minimum_notice_minutes < 0 or settings.cancellation_notice_hours < 0:
            raise ValueError("Ограничения по времени не могут быть отрицательными.")
        if not 0 <= settings.morning_summary_hour <= 23 or not 0 <= settings.evening_summary_hour <= 23:
            raise ValueError("Часы отправки сводок должны быть в диапазоне 0–23.")
        return settings
