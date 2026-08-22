from __future__ import annotations

import json
import logging
import re
import time as time_module
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .db import Database, hhmm_to_minutes, minutes_to_hhmm
from .telegram import TelegramAPI, TelegramAPIError, inline, reply_keyboard

logger = logging.getLogger(__name__)

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"
)
ATTENDANCE_LABELS = {
    "pending": "🕒 Ожидает подтверждения",
    "client_confirmed": "✅ Клиент подтвердил визит",
    "completed": "💚 Услуга выполнена",
    "no_show": "🚫 Клиент не пришёл",
}


def pretty_date(value: date) -> str:
    return f"{value.day} {MONTHS_GENITIVE[value.month - 1]}, {WEEKDAYS_SHORT[value.weekday()]}"


def duration_label(minutes: int) -> str:
    hours, remaining = divmod(minutes, 60)
    if hours and remaining:
        return f"{hours} ч {remaining} мин"
    return f"{hours} ч" if hours else f"{remaining} мин"


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


class BeautyBot:
    def __init__(self, settings: Settings, db: Database | None = None, api: TelegramAPI | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_path)
        self.api = api or TelegramAPI(settings.bot_token)
        self.last_reminder_check = 0.0

    def now(self) -> datetime:
        return datetime.now(self.settings.timezone)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def studio_address(self) -> str:
        return self.db.studio_setting("address", self.settings.address)

    def telegram_contact_link(self) -> str:
        contact = self.settings.contact
        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", contact):
            return f'<a href="https://t.me/{contact[1:]}">{escape(contact)}</a>'
        return escape(contact)

    def instagram_link(self, label: str | None = None) -> str:
        profile = urlparse(self.settings.instagram_url).path.strip("/").split("/")[0]
        display = label or (f"@{profile}" if profile else "Instagram")
        return f'<a href="{escape(self.settings.instagram_url, quote=True)}">{escape(display)}</a>'

    def main_keyboard(self, user_id: int) -> dict[str, Any]:
        rows: list[list[str]] = [
            ["✨ Записаться", "📋 Мои записи"],
            ["💅 Услуги и цены", "📍 Адрес и контакты"],
            ["📸 Фото работ", "💬 Связаться с мастером"],
        ]
        if self.is_admin(user_id):
            rows.append(["⚙️ Управление расписанием"])
        return reply_keyboard(*rows)

    def show_home(self, user_id: int) -> None:
        self.db.clear_state(user_id)
        profile = self.db.profile(user_id)
        name = str(profile["full_name"]) if profile else "Клиент"
        greeting = (
            f"✨ <b>{escape(self.settings.studio_name)}</b>\n\n"
            f"Привет, {escape(name)}! Я помогу выбрать процедуру и записаться "
            f"к мастеру {escape(self.settings.master_name)} в удобное время.\n\n"
            "Выбери действие ниже 👇"
        )
        self.api.send(user_id, greeting, self.main_keyboard(user_id))

    def safe_notify(self, user_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        try:
            self.api.send(user_id, text, keyboard)
        except TelegramAPIError:
            logger.warning("Не удалось отправить сообщение пользователю %s", user_id, exc_info=True)

    def notify_admins(self, text: str, keyboard: dict[str, Any] | None = None) -> None:
        for admin_id in self.settings.admin_ids:
            self.safe_notify(admin_id, text, keyboard)

    def run(self) -> None:
        identity = self.api.call("getMe")
        self.api.call("deleteWebhook", drop_pending_updates=False)
        self.api.call(
            "setMyCommands",
            commands=[
                {"command": "start", "description": "Открыть главное меню"},
                {"command": "book", "description": "Записаться на процедуру"},
                {"command": "appointments", "description": "Посмотреть мои записи"},
                {"command": "contact", "description": "Написать мастеру"},
                {"command": "portfolio", "description": "Посмотреть фотографии работ"},
                {"command": "admin", "description": "Управление для мастера"},
                {"command": "cancel", "description": "Отменить текущий ввод"},
            ],
        )
        logger.info("Бот @%s запущен", identity.get("username", "unknown"))
        offset: int | None = None
        retry_delay = 1
        while True:
            try:
                updates = self.api.call(
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    try:
                        self.handle_update(update)
                    except Exception:
                        logger.exception("Ошибка обработки обновления %s", update.get("update_id"))
                    finally:
                        offset = int(update["update_id"]) + 1
                self.process_reminders()
                retry_delay = 1
            except KeyboardInterrupt:
                logger.info("Бот остановлен")
                return
            except TelegramAPIError:
                logger.exception("Ошибка соединения с Telegram; повтор через %s с", retry_delay)
                time_module.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        user = message.get("from", {})
        user_id = int(user.get("id", 0))
        chat = message.get("chat", {})
        if user_id <= 0 or chat.get("type") != "private":
            return
        name = " ".join(part for part in (user.get("first_name", ""), user.get("last_name", "")) if part).strip() or "Клиент"
        username = user.get("username", "")
        self.db.save_profile(user_id, name, username)
        text = (message.get("text") or "").strip()

        if text in {"/start", "/cancel", "🏠 Главное меню", "← Назад", "↩️ Назад"}:
            if text in {"← Назад", "↩️ Назад"}:
                active_state = self.db.state(user_id)
                if active_state and active_state["state"] == "await_phone":
                    payload = json.loads(active_state["payload"])
                    self.db.clear_state(user_id)
                    self.api.send(user_id, "← Выбери другое время.", self.main_keyboard(user_id))
                    self.show_times(
                        user_id,
                        int(payload["service_id"]),
                        date.fromisoformat(payload["date"]),
                        replace_id=payload.get("replace_id"),
                    )
                    return
                if active_state and active_state["state"].startswith("admin_") and self.is_admin(user_id):
                    self.db.clear_state(user_id)
                    self.show_admin_panel(user_id)
                    return
            self.show_home(user_id)
            return

        if text in {"✨ Записаться", "/book"}:
            self.db.clear_state(user_id)
            self.show_services(user_id, booking=True)
            return
        if text in {"📋 Мои записи", "/appointments"}:
            self.db.clear_state(user_id)
            self.show_my_bookings(user_id)
            return
        if text == "💅 Услуги и цены":
            self.show_services(user_id, booking=False)
            return
        if text in {"📸 Фото работ", "/portfolio"}:
            self.db.clear_state(user_id)
            self.show_portfolio(user_id)
            return
        if text in {"💬 Связаться с мастером", "/contact"}:
            self.request_master_contact(user_id)
            return
        if text == "📍 Адрес и контакты":
            details = (
                f"📍 <b>Адрес:</b> {escape(self.studio_address())}\n"
                f"👩‍🎨 <b>Мастер:</b> {escape(self.settings.master_name)}\n"
                f"💬 <b>Telegram:</b> {self.telegram_contact_link()}\n"
                f"📸 <b>Instagram:</b> {self.instagram_link()}"
            )
            entrance_photo = self.db.studio_setting("entrance_photo_file_id")
            back_keyboard = inline([("← Назад", "home")])
            if entrance_photo:
                self.api.send_photo(user_id, entrance_photo, details, back_keyboard)
            else:
                self.api.send(user_id, details, back_keyboard)
            return
        if text in {"⚙️ Управление расписанием", "/admin"}:
            if not self.is_admin(user_id):
                self.api.send(user_id, "Эта команда доступна только мастеру.", self.main_keyboard(user_id))
                return
            self.db.clear_state(user_id)
            self.show_admin_panel(user_id)
            return

        state = self.db.state(user_id)
        if state:
            self.handle_state(message, state["state"], json.loads(state["payload"]))
            return

        self.api.send(
            user_id,
            "Выбери действие кнопками ниже. Если нужна помощь, нажми «Связаться с мастером». 💛",
            self.main_keyboard(user_id),
        )

    def contact_summary(self, user_id: int) -> str:
        profile = self.db.profile(user_id)
        name = escape(profile["full_name"]) if profile else "Клиент"
        username = f"@{escape(profile['username'])}" if profile and profile["username"] else "не указан"
        phone = escape(profile["phone"]) if profile and profile["phone"] else "не указан"
        return (
            f"👤 {name}\n"
            f"💬 Telegram: {username}\n"
            f"📱 Телефон: {phone}\n"
            f'<a href="tg://user?id={user_id}">Написать клиенту</a>'
        )

    def request_master_contact(self, user_id: int) -> None:
        self.db.set_state(user_id, "await_contact_message")
        self.notify_admins(
            "🔔 <b>Клиент хочет связаться с мастером</b>\n\n"
            f"{self.contact_summary(user_id)}"
        )
        self.api.send(
            user_id,
            "💬 Напиши свой вопрос следующим сообщением — я сразу передам его мастеру.\n\n"
            f"Также можно написать напрямую: {escape(self.settings.contact)}",
            inline([("← Назад", "home")]),
        )

    def handle_state(self, message: dict[str, Any], state: str, payload: dict[str, Any]) -> None:
        user = message["from"]
        user_id = int(user["id"])
        text = (message.get("text") or "").strip()

        if state == "await_contact_message":
            if not text:
                self.api.send(user_id, "Напиши вопрос текстом, и я сразу передам его мастеру. 💛")
                return
            self.db.clear_state(user_id)
            message_text = text[:3200] + ("…" if len(text) > 3200 else "")
            self.notify_admins(
                "✉️ <b>Новое сообщение от клиента</b>\n\n"
                f"{self.contact_summary(user_id)}\n\n"
                f"💌 <b>Сообщение:</b>\n{escape(message_text)}"
            )
            self.api.send(
                user_id,
                "✅ Сообщение передано мастеру. Тебе ответят в Telegram как можно скорее. 💛",
                self.main_keyboard(user_id),
            )
            return

        if state == "await_phone":
            contact = message.get("contact")
            if contact and contact.get("user_id") and int(contact["user_id"]) != user_id:
                self.api.send(user_id, "Пожалуйста, отправь свой номер телефона, а не чужой контакт.")
                return
            phone = normalize_phone(contact.get("phone_number", "") if contact else text)
            if not phone:
                self.api.send(user_id, "Не получилось распознать номер. Нажми «📱 Поделиться номером» или напиши номер вручную.")
                return
            profile = self.db.profile(user_id)
            name = profile["full_name"] if profile else user.get("first_name", "Клиент")
            self.db.save_profile(user_id, name, user.get("username", ""), phone)
            self.db.clear_state(user_id)
            self.api.send(user_id, "Спасибо! Номер сохранён. Подтверди запись ниже 👇", self.main_keyboard(user_id))
            self.show_confirmation(user_id, int(payload["service_id"]), date.fromisoformat(payload["date"]), int(payload["start"]), payload.get("replace_id"))
            return

        if not self.is_admin(user_id):
            self.db.clear_state(user_id)
            return

        try:
            if state == "admin_weekday_hours":
                start, end = self.parse_hours(text)
                self.db.set_weekday(int(payload["weekday"]), True, start, end)
                self.db.clear_state(user_id)
                self.api.send(user_id, f"✅ {WEEKDAYS[int(payload['weekday'])]}: {minutes_to_hhmm(start)}–{minutes_to_hhmm(end)}", self.main_keyboard(user_id))
                self.show_weekly_schedule(user_id)
            elif state == "admin_day_hours":
                start, end = self.parse_hours(text)
                work_date = date.fromisoformat(payload["date"])
                self.db.set_day_override(work_date, True, start, end)
                self.db.clear_state(user_id)
                self.api.send(user_id, f"✅ {pretty_date(work_date)}: {minutes_to_hhmm(start)}–{minutes_to_hhmm(end)}", self.main_keyboard(user_id))
                self.show_day_management(user_id, work_date)
            elif state == "admin_block_hours":
                start, end = self.parse_hours(text)
                work_date = date.fromisoformat(payload["date"])
                self.db.add_block(work_date, start, end, "Заблокировано мастером")
                self.db.clear_state(user_id)
                self.api.send(user_id, f"🔒 Интервал {minutes_to_hhmm(start)}–{minutes_to_hhmm(end)} закрыт.", self.main_keyboard(user_id))
                self.show_day_management(user_id, work_date)
            elif state == "admin_add_service":
                parts = [part.strip() for part in text.split(";")]
                if len(parts) != 3:
                    raise ValueError("Напиши: Название; продолжительность в минутах; цена")
                service_id = self.db.add_service(parts[0], int(parts[1]), int(parts[2]))
                self.db.clear_state(user_id)
                self.api.send(user_id, f"✅ Услуга добавлена, номер {service_id}.", self.main_keyboard(user_id))
                self.show_admin_services(user_id)
            elif state == "admin_edit_service":
                self.update_service_from_message(user_id, message, payload)
            elif state == "admin_add_portfolio_photo":
                self.save_portfolio_photo(user_id, message)
            elif state == "admin_edit_address":
                self.db.set_studio_setting("address", text)
                self.db.clear_state(user_id)
                self.api.send(user_id, "✅ Адрес студии обновлён.", self.main_keyboard(user_id))
                self.show_admin_address(user_id)
            elif state == "admin_entrance_photo":
                photos = message.get("photo") or []
                if not photos:
                    raise ValueError("Отправь фотографию входа обычным сообщением, без режима «файл».")
                self.db.set_studio_setting("entrance_photo_file_id", str(photos[-1]["file_id"]))
                self.db.clear_state(user_id)
                self.api.send(user_id, "✅ Фотография входа добавлена к адресу студии.", self.main_keyboard(user_id))
                self.show_admin_address(user_id)
            elif state == "admin_manual_details":
                self.create_manual_booking(user_id, text, payload)
        except (ValueError, TypeError) as exc:
            self.api.send(user_id, f"Не получилось сохранить: {escape(str(exc))}\n\nПроверь формат и попробуй ещё раз или отправь /cancel.")

    def update_service_from_message(self, user_id: int, message: dict[str, Any], payload: dict[str, Any]) -> None:
        service_id = int(payload["service_id"])
        field = str(payload["field"])
        text = (message.get("text") or "").strip()
        if field == "photo_file_id":
            photos = message.get("photo") or []
            if not photos:
                raise ValueError("Отправь фотографию обычным сообщением, без режима «файл».")
            value: str | int = str(photos[-1]["file_id"])
        elif field in {"price", "duration_minutes"}:
            value = int(text)
        elif field == "description":
            value = "" if text == "-" else text[:900]
        else:
            value = text
        if not self.db.update_service(service_id, field, value):
            raise ValueError("Услуга больше не существует.")
        self.db.clear_state(user_id)
        self.api.send(user_id, "✅ Услуга обновлена.", self.main_keyboard(user_id))
        self.show_admin_service(user_id, service_id)

    def save_portfolio_photo(self, user_id: int, message: dict[str, Any]) -> None:
        photos = message.get("photo") or []
        if not photos:
            raise ValueError("Отправь фотографию обычным сообщением, без режима «файл».")
        photo_id = self.db.add_portfolio_photo(
            str(photos[-1]["file_id"]),
            str(message.get("caption") or ""),
            self.now(),
        )
        self.api.send(
            user_id,
            f"📸 Фото №{photo_id} добавлено. Можно отправить ещё одну фотографию.",
            inline([("✅ Завершить добавление", "aportfolio")], [("← Назад", "aportfolio")]),
        )

    def create_manual_booking(self, user_id: int, text: str, payload: dict[str, Any]) -> None:
        parts = [part.strip() for part in text.split(";")]
        if len(parts) not in {2, 3} or not parts[0]:
            raise ValueError("Напиши: Имя клиента; телефон. При желании третьим полем добавь @username.")
        phone = normalize_phone(parts[1])
        if not phone:
            raise ValueError("Не получилось распознать номер телефона.")
        username = parts[2].lstrip("@") if len(parts) == 3 else ""
        result = self.db.create_booking(
            telegram_user_id=-int(time_module.time_ns()),
            customer_name=parts[0],
            username=username,
            phone=phone,
            service_id=int(payload["service_id"]),
            work_date=date.fromisoformat(payload["date"]),
            start_minutes=int(payload["start"]),
            now=self.now(),
            minimum_notice=0,
            source="manual",
        )
        if not result.ok or result.booking_id is None:
            raise ValueError("Это время уже занято или выходит за пределы рабочего графика.")
        booking = self.db.booking(result.booking_id)
        assert booking is not None
        self.db.clear_state(user_id)
        self.api.send(user_id, "✅ Клиент записан вручную.", self.main_keyboard(user_id))
        self.notify_admins(
            f"📝 <b>Ручная запись №{booking['id']}</b>\n\n"
            f"{self.booking_summary(booking)}\n"
            f"👤 {escape(booking['customer_name'])}\n"
            f"📱 {escape(booking['phone'])}",
            inline([("⚙️ Управлять записью", f"abdetail:{booking['id']}")]),
        )

    @staticmethod
    def parse_hours(text: str) -> tuple[int, int]:
        parts = re.split(r"\s*[—–-]\s*|\s+", text.strip())
        if len(parts) != 2:
            raise ValueError("Нужен формат 10:00-18:00")
        start, end = hhmm_to_minutes(parts[0]), hhmm_to_minutes(parts[1])
        if start >= end:
            raise ValueError("Время окончания должно быть позже начала")
        return start, end

    def show_services(self, user_id: int, booking: bool, message_id: int | None = None) -> None:
        services = self.db.services()
        if not services:
            self.api.send(
                user_id,
                "Сейчас запись временно недоступна. Напиши мастеру, чтобы уточнить ближайшее время.",
                inline([("← Назад", "home")]),
            )
            return
        if booking:
            text = "✨ <b>Выбери процедуру</b>\n\nНажми на услугу — затем я покажу только подходящие свободные окна."
            rows = [[(f"{row['name']} · {row['price']} {self.settings.currency}", f"svc:{row['id']}")] for row in services]
        else:
            lines = ["💅 <b>Услуги и цены</b>\n"]
            for service in services:
                description = f"\n   {escape(service['description'])}" if service["description"] else ""
                lines.append(
                    f"{escape(service['name'])}\n   {duration_label(int(service['duration_minutes']))} "
                    f"· <b>{service['price']} {escape(self.settings.currency)}</b>{description}\n"
                )
            text = "\n".join(lines)
            rows = [[("✨ Выбрать время", "book")]]
        rows.append([("← Назад", "home")])
        keyboard = inline(*rows)
        if message_id:
            self.api.edit(user_id, message_id, text, keyboard)
        else:
            self.api.send(user_id, text, keyboard)

    def show_service_card(self, user_id: int, service_id: int, message_id: int | None = None) -> None:
        service = self.db.service(service_id)
        if not service:
            self.show_services(user_id, booking=True)
            return
        description = f"\n\n{escape(service['description'])}" if service["description"] else ""
        text = (
            f"💅 <b>{escape(service['name'])}</b>\n\n"
            f"⏱ Продолжительность: {duration_label(int(service['duration_minutes']))}\n"
            f"💳 Стоимость: {service['price']} {escape(self.settings.currency)}"
            f"{description}"
        )
        keyboard = inline(
            [("🗓 Выбрать день и время", f"svcdates:{service_id}")],
            [("← К списку услуг", "book")],
        )
        if service["photo_file_id"]:
            self.api.send_photo(user_id, str(service["photo_file_id"]), text, keyboard)
        elif message_id:
            self.api.edit(user_id, message_id, text, keyboard)
        else:
            self.api.send(user_id, text, keyboard)

    def show_portfolio(self, user_id: int) -> None:
        photos = self.db.portfolio_photos()
        if not photos:
            self.api.send(
                user_id,
                "📸 Фотографии работ скоро появятся.\n\n"
                f"✨ Ещё больше работ — {self.instagram_link('тут')}.\n\n"
                "А пока можно посмотреть услуги и выбрать удобное время. 💛",
                inline([("✨ Записаться", "book")], [("← Назад", "home")]),
            )
            return
        self.api.send(user_id, f"📸 <b>Работы мастера {escape(self.settings.master_name)}</b>")
        for photo in reversed(photos):
            self.api.send_photo(user_id, str(photo["file_id"]), escape(photo["caption"]))
        self.api.send(
            user_id,
            f"✨ Ещё больше работ — {self.instagram_link('тут')}.\n\n"
            "Понравилась работа? Выбери удобное время 👇",
            inline([("✨ Записаться", "book")], [("← Назад", "home")]),
        )

    def show_dates(self, user_id: int, service_id: int, page: int = 0, message_id: int | None = None, replace_id: int | None = None) -> None:
        service = self.db.service(service_id)
        if not service:
            self.api.send(user_id, "Эта услуга больше недоступна. Выбери другую процедуру.")
            self.show_services(user_id, True)
            return
        now = self.now()
        start_day = max(page, 0) * 7
        finish_day = min(start_day + 7, self.settings.booking_horizon_days)
        rows: list[list[tuple[str, str]]] = []
        for day_offset in range(start_day, finish_day):
            work_date = now.date() + timedelta(days=day_offset)
            slots = self.db.available_slots(
                work_date, int(service["duration_minutes"]), self.settings.slot_step_minutes,
                now, self.settings.minimum_notice_minutes, replace_id,
            )
            if slots:
                date_code = work_date.strftime("%Y%m%d")
                suffix = f":{replace_id}" if replace_id is not None else ""
                rows.append([(f"{pretty_date(work_date)} · свободно {len(slots)}", f"day:{service_id}:{date_code}{suffix}")])
        navigation: list[tuple[str, str]] = []
        suffix = f":{replace_id}" if replace_id is not None else ""
        if page > 0:
            navigation.append(("← Раньше", f"dates:{service_id}:{page - 1}{suffix}"))
        if finish_day < self.settings.booking_horizon_days:
            navigation.append(("Позже →", f"dates:{service_id}:{page + 1}{suffix}"))
        if navigation:
            rows.append(navigation)
        rows.append([("← К выбору услуги", "book")])
        title = (
            f"🗓 <b>Выбери день</b>\n\n"
            f"{escape(service['name'])}\n"
            f"⏱ {duration_label(int(service['duration_minutes']))} · {service['price']} {escape(self.settings.currency)}\n\n"
            + ("Показаны только дни со свободными окнами." if any(len(row) == 1 and row[0][1].startswith("day:") for row in rows) else "На этой неделе свободных окон нет. Проверь другие даты или напиши мастеру.")
        )
        if message_id:
            self.api.edit(user_id, message_id, title, inline(*rows))
        else:
            self.api.send(user_id, title, inline(*rows))

    def show_times(self, user_id: int, service_id: int, work_date: date, message_id: int | None = None, replace_id: int | None = None) -> None:
        service = self.db.service(service_id)
        if not service:
            self.show_services(user_id, True)
            return
        slots = self.db.available_slots(
            work_date, int(service["duration_minutes"]), self.settings.slot_step_minutes,
            self.now(), self.settings.minimum_notice_minutes, replace_id,
        )
        code = work_date.strftime("%Y%m%d")
        suffix = f":{replace_id}" if replace_id is not None else ""
        buttons = [(minutes_to_hhmm(slot), f"slot:{service_id}:{code}:{slot}{suffix}") for slot in slots]
        rows = chunked(buttons, 3)
        rows.append([("← Другой день", f"dates:{service_id}:0{suffix}")])
        text = (
            f"🕒 <b>{pretty_date(work_date)}</b>\n\n"
            f"{escape(service['name'])}\n"
            f"⏱ {duration_label(int(service['duration_minutes']))}\n\n"
            + ("Выбери удобное время:" if slots else "На этот день окна только что закончились. Выбери другую дату.")
        )
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    def request_phone_or_confirmation(self, user_id: int, service_id: int, work_date: date, start: int, replace_id: int | None = None) -> None:
        profile = self.db.profile(user_id)
        if not profile or not profile["phone"]:
            payload = {"service_id": service_id, "date": work_date.isoformat(), "start": start, "replace_id": replace_id}
            self.db.set_state(user_id, "await_phone", json.dumps(payload, ensure_ascii=False))
            self.api.send(
                user_id,
                "📱 Чтобы мастер мог связаться с тобой, отправь номер телефона.\n\n"
                "Нажми кнопку ниже или напиши номер вручную. Он понадобится только для записи.",
                reply_keyboard(
                    [{"text": "📱 Поделиться номером", "request_contact": True}],
                    ["← Назад", "🏠 Главное меню"],
                    one_time=True,
                ),
            )
            return
        self.show_confirmation(user_id, service_id, work_date, start, replace_id)

    def show_confirmation(self, user_id: int, service_id: int, work_date: date, start: int, replace_id: int | None = None) -> None:
        service = self.db.service(service_id)
        profile = self.db.profile(user_id)
        if not service or not profile:
            self.show_services(user_id, True)
            return
        suffix = f":{replace_id}" if replace_id is not None else ""
        code = work_date.strftime("%Y%m%d")
        text = (
            "💛 <b>Проверь свою запись</b>\n\n"
            f"💅 {escape(service['name'])}\n"
            f"🗓 {pretty_date(work_date)} в {minutes_to_hhmm(start)}\n"
            f"⏱ {duration_label(int(service['duration_minutes']))}\n"
            f"💳 {service['price']} {escape(self.settings.currency)}\n"
            f"📱 {escape(profile['phone'])}\n"
            f"📍 {escape(self.studio_address())}\n\n"
            "Если всё правильно, нажми «Подтвердить»."
        )
        self.api.send(
            user_id,
            text,
            inline(
                [("✅ Подтвердить", f"confirm:{service_id}:{code}:{start}{suffix}")],
                [("← Изменить время", f"day:{service_id}:{code}{suffix}")],
            ),
        )

    def complete_booking(self, user_id: int, service_id: int, work_date: date, start: int, replace_id: int | None = None) -> None:
        profile = self.db.profile(user_id)
        if not profile or not profile["phone"]:
            self.request_phone_or_confirmation(user_id, service_id, work_date, start, replace_id)
            return
        result = self.db.create_booking(
            telegram_user_id=user_id,
            customer_name=profile["full_name"],
            username=profile["username"],
            phone=profile["phone"],
            service_id=service_id,
            work_date=work_date,
            start_minutes=start,
            now=self.now(),
            minimum_notice=self.settings.minimum_notice_minutes,
            replace_booking_id=replace_id,
        )
        if not result.ok or result.booking_id is None:
            self.api.send(user_id, "😔 Это время уже недоступно. Я покажу актуальные свободные окна.", self.main_keyboard(user_id))
            self.show_times(user_id, service_id, work_date, replace_id=replace_id)
            return
        booking = self.db.booking(result.booking_id)
        assert booking is not None
        action = "перенесена" if replace_id is not None else "подтверждена"
        self.api.send(
            user_id,
            f"🎉 <b>Запись {action}!</b>\n\n{self.booking_summary(booking)}\n\n"
            "Я пришлю напоминание перед процедурой. Посмотреть, перенести или отменить запись можно в разделе «Мои записи».",
            self.main_keyboard(user_id),
        )
        username = f"@{escape(booking['username'])}" if booking["username"] else "не указан"
        self.notify_admins(
            f"🔔 <b>{'Запись перенесена' if replace_id else 'Новая запись'} №{booking['id']}</b>\n\n"
            f"{self.booking_summary(booking)}\n"
            f"👤 {escape(booking['customer_name'])}\n"
            f"📱 {escape(booking['phone'])}\n"
            f"💬 {username}",
            inline([("❌ Отменить запись", f"acancel:{booking['id']}")]),
        )

    def booking_summary(self, booking: Any) -> str:
        work_date = date.fromisoformat(booking["work_date"])
        return (
            f"💅 {escape(booking['service_name'])}\n"
            f"🗓 {pretty_date(work_date)} в {minutes_to_hhmm(int(booking['start_minutes']))}\n"
            f"⏱ {duration_label(int(booking['duration_minutes']))}\n"
            f"💳 {booking['price']} {escape(self.settings.currency)}\n"
            f"📍 {escape(self.studio_address())}"
        )

    def show_my_bookings(self, user_id: int) -> None:
        bookings = self.db.customer_bookings(user_id, self.now().date())
        if not bookings:
            self.api.send(
                user_id,
                "Пока активных записей нет. Выбери процедуру — я покажу свободные окна. 💛",
                inline([("✨ Записаться", "book")], [("← Назад", "home")]),
            )
            return
        self.api.send(user_id, "📋 <b>Твои ближайшие записи:</b>")
        for booking in bookings:
            self.api.send(
                user_id,
                self.booking_summary(booking),
                inline(
                    [("🔄 Перенести", f"reschedule:{booking['id']}")],
                    [("❌ Отменить", f"cancelask:{booking['id']}")],
                    [("← Назад", "home")],
                ),
            )

    def can_customer_cancel(self, booking: Any) -> bool:
        appointment = datetime.combine(
            date.fromisoformat(booking["work_date"]),
            datetime.min.time(),
            tzinfo=self.settings.timezone,
        ) + timedelta(minutes=int(booking["start_minutes"]))
        return appointment - self.now() >= timedelta(hours=self.settings.cancellation_notice_hours)

    def show_admin_panel(self, user_id: int, message_id: int | None = None) -> None:
        text = (
            "⚙️ <b>Панель мастера</b>\n\n"
            "Здесь можно контролировать записи, добавлять клиентов вручную, менять услуги и загружать фотографии."
        )
        keyboard = inline(
            [("📋 Записи на сегодня", "abook:0"), ("🗓 Записи на завтра", "abook:1")],
            [("➕ Записать клиента вручную", "amanual")],
            [("📆 Выбрать день", "acal:0")],
            [("🕒 График по дням недели", "aweek")],
            [("💅 Услуги и цены", "aservices")],
            [("📸 Фотографии работ", "aportfolio")],
            [("📍 Адрес и фото входа", "aaddress")],
            [("📊 Сводка на сегодня", "adigest:0"), ("📊 На завтра", "adigest:1")],
            [("← Назад", "home")],
        )
        if message_id:
            self.api.edit(user_id, message_id, text, keyboard)
        else:
            self.api.send(user_id, text, keyboard)

    def show_admin_calendar(self, user_id: int, page: int = 0, message_id: int | None = None) -> None:
        now = self.now()
        rows: list[list[tuple[str, str]]] = []
        first = page * 7
        for offset in range(first, min(first + 7, self.settings.booking_horizon_days)):
            work_date = now.date() + timedelta(days=offset)
            hours = self.db.day_hours(work_date)
            booked = len(self.db.day_bookings(work_date))
            label = f"{pretty_date(work_date)} · {'выходной' if hours is None else str(booked) + ' запис.'}"
            rows.append([(label, f"aday:{work_date.strftime('%Y%m%d')}")])
        navigation = []
        if page:
            navigation.append(("← Раньше", f"acal:{page - 1}"))
        if first + 7 < self.settings.booking_horizon_days:
            navigation.append(("Позже →", f"acal:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("← В панель мастера", "admin")])
        text = "📆 <b>Выбери день</b>\n\nМожно изменить часы, объявить выходной или закрыть отдельные интервалы."
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    def show_day_management(self, user_id: int, work_date: date, message_id: int | None = None) -> None:
        hours = self.db.day_hours(work_date)
        bookings = self.db.day_bookings(work_date)
        blocks = self.db.blocks(work_date)
        code = work_date.strftime("%Y%m%d")
        lines = [f"📆 <b>{pretty_date(work_date)}</b>\n"]
        lines.append("🔴 Выходной" if hours is None else f"🟢 Рабочие часы: {minutes_to_hhmm(hours[0])}–{minutes_to_hhmm(hours[1])}")
        lines.append(f"👥 Записей: {len(bookings)}")
        if bookings:
            lines.append("\n<b>Клиенты:</b>")
            lines.extend(
                f"• {minutes_to_hhmm(int(item['start_minutes']))} — {escape(item['customer_name'])}, "
                f"{escape(item['service_name'])}\n   {self.attendance_label(item)}"
                for item in bookings
            )
        if blocks:
            lines.append("\n<b>Закрытые интервалы:</b>")
            lines.extend(f"• {minutes_to_hhmm(int(item['start_minutes']))}–{minutes_to_hhmm(int(item['end_minutes']))}" for item in blocks)
        rows: list[list[tuple[str, str]]] = [
            [("➕ Записать клиента на этот день", f"amanualday:{code}")],
            [("🕒 Изменить рабочие часы", f"ahours:{code}")],
            [("🔒 Закрыть интервал", f"ablock:{code}")],
            [("🔴 Сделать выходным", f"aoff:{code}")] if hours else [("🟢 Открыть рабочий день", f"ahours:{code}")],
            [("↩️ Вернуть обычный график", f"areset:{code}")],
        ]
        for block in blocks:
            rows.append([(f"🔓 Открыть {minutes_to_hhmm(int(block['start_minutes']))}–{minutes_to_hhmm(int(block['end_minutes']))}", f"aunblock:{block['id']}:{code}")])
        for booking in bookings:
            rows.append([
                (
                    f"👤 {minutes_to_hhmm(int(booking['start_minutes']))} · {booking['customer_name']}",
                    f"abdetail:{booking['id']}",
                )
            ])
        rows.append([("← К календарю", "acal:0")])
        if message_id:
            self.api.edit(user_id, message_id, "\n".join(lines), inline(*rows))
        else:
            self.api.send(user_id, "\n".join(lines), inline(*rows))

    def show_weekly_schedule(self, user_id: int, message_id: int | None = None) -> None:
        rows = []
        lines = ["🕒 <b>Основной график недели</b>\n"]
        for item in self.db.weekly_schedule():
            weekday = int(item["weekday"])
            hours = f"{minutes_to_hhmm(int(item['start_minutes']))}–{minutes_to_hhmm(int(item['end_minutes']))}" if item["enabled"] else "выходной"
            lines.append(f"{WEEKDAYS[weekday]}: <b>{hours}</b>")
            rows.append([(f"✏️ {WEEKDAYS[weekday]}", f"awday:{weekday}")])
        rows.append([("← В панель мастера", "admin")])
        if message_id:
            self.api.edit(user_id, message_id, "\n".join(lines), inline(*rows))
        else:
            self.api.send(user_id, "\n".join(lines), inline(*rows))

    def show_admin_services(self, user_id: int, message_id: int | None = None) -> None:
        rows = []
        lines = ["💅 <b>Услуги</b>\n", "Выбери услугу, чтобы изменить цену, длительность, описание или фотографию.\n"]
        for service in self.db.services(include_inactive=True):
            indicator = "🟢" if service["active"] else "⚪️"
            photo = " 📸" if service["photo_file_id"] else ""
            lines.append(
                f"{indicator} {escape(service['name'])} — {service['price']} {escape(self.settings.currency)} "
                f"/ {duration_label(int(service['duration_minutes']))}{photo}"
            )
            rows.append([(f"{indicator} {service['name']}", f"asvc:{service['id']}")])
        rows.append([("➕ Добавить услугу", "aaddservice")])
        rows.append([("← В панель мастера", "admin")])
        if message_id:
            self.api.edit(user_id, message_id, "\n".join(lines), inline(*rows))
        else:
            self.api.send(user_id, "\n".join(lines), inline(*rows))

    def show_admin_service(self, user_id: int, service_id: int, message_id: int | None = None) -> None:
        service = self.db.service(service_id, active_only=False)
        if not service:
            self.show_admin_services(user_id)
            return
        description = escape(service["description"]) if service["description"] else "не добавлено"
        photo = "добавлено" if service["photo_file_id"] else "не добавлено"
        status = "🟢 показывается клиентам" if service["active"] else "⚪️ скрыта от клиентов"
        text = (
            f"💅 <b>{escape(service['name'])}</b>\n\n"
            f"💳 Цена: {service['price']} {escape(self.settings.currency)}\n"
            f"⏱ Длительность: {duration_label(int(service['duration_minutes']))}\n"
            f"📝 Описание: {description}\n"
            f"📸 Фото: {photo}\n"
            f"Статус: {status}"
        )
        keyboard = inline(
            [("✏️ Название", f"aedit:{service_id}:name"), ("💳 Цена", f"aedit:{service_id}:price")],
            [("⏱ Длительность", f"aedit:{service_id}:duration_minutes")],
            [("📝 Описание", f"aedit:{service_id}:description")],
            [("📸 Добавить / заменить фото", f"aedit:{service_id}:photo_file_id")],
            [("🙈 Скрыть" if service["active"] else "👁 Показать", f"atoggle:{service_id}")],
            [("← Ко всем услугам", "aservices")],
        )
        if message_id:
            self.api.edit(user_id, message_id, text, keyboard)
        else:
            self.api.send(user_id, text, keyboard)

    def show_admin_portfolio(self, user_id: int, message_id: int | None = None) -> None:
        self.db.clear_state(user_id)
        photos = self.db.portfolio_photos(limit=30)
        lines = ["📸 <b>Фотографии работ</b>\n", f"Всего фотографий: {len(photos)}"]
        rows: list[list[tuple[str, str]]] = [[("➕ Добавить фотографии", "aaddphoto")]]
        for photo in photos[:10]:
            label = photo["caption"][:35] if photo["caption"] else f"Фотография №{photo['id']}"
            rows.append([(f"🗑 {label}", f"adelphoto:{photo['id']}")])
        rows.append([("← В панель мастера", "admin")])
        if message_id:
            self.api.edit(user_id, message_id, "\n".join(lines), inline(*rows))
        else:
            self.api.send(user_id, "\n".join(lines), inline(*rows))

    def show_admin_address(self, user_id: int, message_id: int | None = None) -> None:
        self.db.clear_state(user_id)
        has_photo = bool(self.db.studio_setting("entrance_photo_file_id"))
        text = (
            "📍 <b>Адрес и фотография входа</b>\n\n"
            f"Адрес: {escape(self.studio_address())}\n"
            f"Фото входа: {'добавлено ✅' if has_photo else 'не добавлено'}\n\n"
            "Клиент увидит адрес и фотографию в разделе «Адрес и контакты»."
        )
        rows = [
            [("✏️ Изменить адрес", "aaddressedit")],
            [("📸 Добавить / заменить фото входа", "aaddressphoto")],
        ]
        if has_photo:
            rows.append([("🗑 Удалить фото входа", "aaddressclear")])
        rows.append([("← В панель мастера", "admin")])
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    @staticmethod
    def attendance_label(booking: Any) -> str:
        return ATTENDANCE_LABELS.get(str(booking["attendance_status"]), "🕒 Ожидает подтверждения")

    def show_admin_bookings(self, user_id: int, work_date: date, message_id: int | None = None) -> None:
        bookings = self.db.day_bookings(work_date)
        lines = [f"📋 <b>Записи на {pretty_date(work_date)}</b>\n"]
        if bookings:
            for booking in bookings:
                lines.append(
                    f"<b>{minutes_to_hhmm(int(booking['start_minutes']))}–{minutes_to_hhmm(int(booking['end_minutes']))}</b>\n"
                    f"{escape(booking['customer_name'])} · {escape(booking['phone'])}\n"
                    f"{escape(booking['service_name'])} · {booking['price']} {escape(self.settings.currency)}\n"
                    f"{self.attendance_label(booking)}\n"
                )
            revenue = sum(int(booking["price"]) for booking in bookings)
            lines.append(f"💰 Итого: <b>{revenue} {escape(self.settings.currency)}</b>")
        else:
            lines.append("Записей пока нет.")
        rows = [
            [
                (
                    f"👤 {minutes_to_hhmm(int(booking['start_minutes']))} · {booking['customer_name']}",
                    f"abdetail:{booking['id']}",
                )
            ]
            for booking in bookings
        ]
        rows.append([("➕ Добавить клиента", f"amanualday:{work_date.strftime('%Y%m%d')}")])
        rows.append([("⚙️ Настроить этот день", f"aday:{work_date.strftime('%Y%m%d')}")])
        rows.append([("← В панель мастера", "admin")])
        keyboard = inline(*rows)
        if message_id:
            self.api.edit(user_id, message_id, "\n".join(lines), keyboard)
        else:
            self.api.send(user_id, "\n".join(lines), keyboard)

    def show_admin_booking(self, user_id: int, booking_id: int, message_id: int | None = None) -> None:
        booking = self.db.booking(booking_id)
        if not booking:
            self.api.send(user_id, "Эта запись не найдена.")
            return
        username = f"@{escape(booking['username'])}" if booking["username"] else "не указан"
        source = "внесена вручную" if booking["source"] == "manual" else "создана клиентом"
        text = (
            f"📋 <b>Запись №{booking['id']}</b>\n\n"
            f"{self.booking_summary(booking)}\n"
            f"👤 {escape(booking['customer_name'])}\n"
            f"📱 {escape(booking['phone'])}\n"
            f"💬 {username}\n"
            f"📝 {source}\n\n"
            f"{self.attendance_label(booking)}"
        )
        keyboard = inline(
            [("✅ Клиент подтвердил", f"astatus:{booking_id}:client_confirmed")],
            [("💚 Выполнена", f"astatus:{booking_id}:completed"), ("🚫 Не пришёл", f"astatus:{booking_id}:no_show")],
            [("🕒 Ожидает ответа", f"astatus:{booking_id}:pending")],
            [("❌ Отменить запись", f"acancel:{booking_id}")],
            [("← К записям дня", f"aday:{date.fromisoformat(booking['work_date']).strftime('%Y%m%d')}")],
        )
        if message_id:
            self.api.edit(user_id, message_id, text, keyboard)
        else:
            self.api.send(user_id, text, keyboard)

    def show_manual_services(
        self,
        user_id: int,
        work_date: date | None = None,
        message_id: int | None = None,
    ) -> None:
        suffix = f":{work_date.strftime('%Y%m%d')}" if work_date is not None else ""
        rows = [
            [(f"{service['name']} · {service['price']} {self.settings.currency}", f"amservice:{service['id']}{suffix}")]
            for service in self.db.services()
        ]
        rows.append([("← В панель мастера", "admin")])
        date_label = f" на {pretty_date(work_date)}" if work_date is not None else ""
        text = f"📝 <b>Ручная запись{date_label}</b>\n\nВыбери услугу для клиента."
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    def show_manual_dates(
        self,
        user_id: int,
        service_id: int,
        page: int = 0,
        message_id: int | None = None,
    ) -> None:
        service = self.db.service(service_id)
        if not service:
            self.show_manual_services(user_id)
            return
        now = self.now()
        first = max(page, 0) * 7
        rows: list[list[tuple[str, str]]] = []
        for offset in range(first, min(first + 7, self.settings.booking_horizon_days)):
            work_date = now.date() + timedelta(days=offset)
            slots = self.db.available_slots(
                work_date,
                int(service["duration_minutes"]),
                self.settings.slot_step_minutes,
                now,
                0,
            )
            if slots:
                rows.append([
                    (
                        f"{pretty_date(work_date)} · {len(slots)} свободных окон",
                        f"amday:{service_id}:{work_date.strftime('%Y%m%d')}",
                    )
                ])
        navigation = []
        if page:
            navigation.append(("← Раньше", f"amdates:{service_id}:{page - 1}"))
        if first + 7 < self.settings.booking_horizon_days:
            navigation.append(("Позже →", f"amdates:{service_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("← К выбору услуги", "amanual")])
        text = f"📝 <b>Ручная запись</b>\n\n{escape(service['name'])}\nВыбери дату."
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    def show_manual_times(
        self,
        user_id: int,
        service_id: int,
        work_date: date,
        message_id: int | None = None,
    ) -> None:
        service = self.db.service(service_id)
        if not service:
            self.show_manual_services(user_id, work_date)
            return
        slots = self.db.available_slots(
            work_date,
            int(service["duration_minutes"]),
            self.settings.slot_step_minutes,
            self.now(),
            0,
        )
        date_code = work_date.strftime("%Y%m%d")
        buttons = [(minutes_to_hhmm(slot), f"amslot:{service_id}:{date_code}:{slot}") for slot in slots]
        rows = chunked(buttons, 3)
        rows.append([("← К выбору даты", f"amdates:{service_id}:0")])
        text = (
            f"📝 <b>Ручная запись на {pretty_date(work_date)}</b>\n\n"
            f"{escape(service['name'])}\n"
            + ("Выбери удобное время." if slots else "Свободных окон на этот день нет.")
        )
        if message_id:
            self.api.edit(user_id, message_id, text, inline(*rows))
        else:
            self.api.send(user_id, text, inline(*rows))

    def daily_summary(self, work_date: date, label: str) -> str:
        bookings = self.db.day_bookings(work_date)
        lines = [f"📊 <b>{label}: {pretty_date(work_date)}</b>\n"]
        if not bookings:
            lines.append("Записей пока нет.")
            return "\n".join(lines)
        confirmed = sum(row["attendance_status"] == "client_confirmed" for row in bookings)
        completed = sum(row["attendance_status"] == "completed" for row in bookings)
        for booking in bookings:
            status = {
                "pending": "🕒",
                "client_confirmed": "✅",
                "completed": "💚",
                "no_show": "🚫",
            }.get(str(booking["attendance_status"]), "🕒")
            lines.append(
                f"{status} <b>{minutes_to_hhmm(int(booking['start_minutes']))}</b> — "
                f"{escape(booking['customer_name'])}, {escape(booking['service_name'])}"
            )
        revenue = sum(int(row["price"]) for row in bookings if row["attendance_status"] != "no_show")
        lines.extend([
            "",
            f"👥 Всего клиентов: {len(bookings)}",
            f"✅ Подтвердили визит: {confirmed}",
            f"💚 Выполнено: {completed}",
            f"💰 Ожидаемая выручка: {revenue} {escape(self.settings.currency)}",
        ])
        return "\n".join(lines)

    def handle_callback(self, query: dict[str, Any]) -> None:
        user_id = int(query["from"]["id"])
        data = query.get("data", "")
        message = query.get("message", {})
        message_id = message.get("message_id")
        self.api.answer_callback(query["id"])

        parts = data.split(":")
        action = parts[0]
        if action.startswith("a") and action not in {"appointments"} and not self.is_admin(user_id):
            self.api.send(user_id, "Управление расписанием доступно только мастеру.")
            return

        try:
            if action == "home":
                self.show_home(user_id)
            elif action == "appointments":
                self.db.clear_state(user_id)
                self.show_my_bookings(user_id)
            elif action == "book":
                self.db.clear_state(user_id)
                self.show_services(user_id, True, message_id)
            elif action == "svc":
                self.show_service_card(user_id, int(parts[1]), message_id)
            elif action == "svcdates":
                self.show_dates(user_id, int(parts[1]), message_id=message_id)
            elif action == "dates":
                self.show_dates(user_id, int(parts[1]), int(parts[2]), message_id, int(parts[3]) if len(parts) > 3 else None)
            elif action == "day":
                self.show_times(user_id, int(parts[1]), self.decode_date(parts[2]), message_id, int(parts[3]) if len(parts) > 3 else None)
            elif action == "slot":
                self.request_phone_or_confirmation(user_id, int(parts[1]), self.decode_date(parts[2]), int(parts[3]), int(parts[4]) if len(parts) > 4 else None)
            elif action == "confirm":
                self.complete_booking(user_id, int(parts[1]), self.decode_date(parts[2]), int(parts[3]), int(parts[4]) if len(parts) > 4 else None)
            elif action == "visitok":
                booking = self.db.booking(int(parts[1]))
                if booking and int(booking["telegram_user_id"]) == user_id and self.db.update_attendance_status(int(parts[1]), "client_confirmed"):
                    self.api.send(user_id, "✅ Спасибо! Ты подтвердил визит. Ждём тебя! 💛", self.main_keyboard(user_id))
                    self.notify_admins(
                        f"✅ <b>Клиент подтвердил визит по записи №{booking['id']}</b>\n\n"
                        f"{self.booking_summary(booking)}\n👤 {escape(booking['customer_name'])}",
                        inline([("⚙️ Управлять записью", f"abdetail:{booking['id']}")]),
                    )
                else:
                    self.api.send(user_id, "Эта запись уже недоступна.")
            elif action == "support":
                self.request_master_contact(user_id)
            elif action == "cancelask":
                booking = self.db.booking(int(parts[1]))
                if booking and int(booking["telegram_user_id"]) == user_id:
                    if not self.can_customer_cancel(booking):
                        self.api.send(user_id, f"До процедуры осталось меньше {self.settings.cancellation_notice_hours} ч. Для отмены напиши мастеру: {escape(self.settings.contact)}")
                    else:
                        self.api.send(
                            user_id,
                            "Точно отменить запись?",
                            inline(
                                [("Да, отменить", f"cancelok:{booking['id']}"), ("Оставить", "keep")],
                                [("← Назад", "appointments")],
                            ),
                        )
            elif action == "cancelok":
                booking = self.db.booking(int(parts[1]))
                if booking and int(booking["telegram_user_id"]) == user_id and self.can_customer_cancel(booking) and self.db.cancel_booking(int(parts[1]), user_id):
                    self.api.send(user_id, "Запись отменена. Если захочешь выбрать другое время, просто нажми «Записаться». 💛", self.main_keyboard(user_id))
                    self.notify_admins(f"❌ <b>Клиент отменил запись №{booking['id']}</b>\n\n{self.booking_summary(booking)}\n👤 {escape(booking['customer_name'])}")
                else:
                    self.api.send(user_id, "Отменить эту запись уже нельзя. Свяжись с мастером.")
            elif action == "keep":
                self.api.send(user_id, "Хорошо, запись остаётся в силе. 💛")
            elif action == "reschedule":
                booking = self.db.booking(int(parts[1]))
                if booking and int(booking["telegram_user_id"]) == user_id and booking["status"] == "confirmed":
                    if self.can_customer_cancel(booking):
                        self.show_dates(user_id, int(booking["service_id"]), replace_id=int(booking["id"]))
                    else:
                        self.api.send(user_id, f"До процедуры осталось слишком мало времени. Для переноса напиши мастеру: {escape(self.settings.contact)}")
            else:
                self.handle_admin_callback(user_id, parts, message_id)
        except (ValueError, IndexError, TypeError):
            logger.warning("Некорректные данные кнопки: %s", data, exc_info=True)
            self.api.send(user_id, "Эта кнопка устарела. Открой меню и попробуй ещё раз.", self.main_keyboard(user_id))

    @staticmethod
    def decode_date(raw: str) -> date:
        return datetime.strptime(raw, "%Y%m%d").date()

    def handle_admin_callback(self, user_id: int, parts: list[str], message_id: int | None) -> None:
        if not self.is_admin(user_id):
            return
        action = parts[0]
        if action in {"admin", "aweek", "aday", "asvc", "aservices", "aportfolio", "aaddress", "amday", "amanual"}:
            self.db.clear_state(user_id)
        if action == "admin":
            self.show_admin_panel(user_id, message_id)
        elif action == "abook":
            self.show_admin_bookings(user_id, self.now().date() + timedelta(days=int(parts[1])), message_id)
        elif action == "abdetail":
            self.show_admin_booking(user_id, int(parts[1]), message_id)
        elif action == "astatus":
            booking_id = int(parts[1])
            if self.db.update_attendance_status(booking_id, parts[2]):
                self.show_admin_booking(user_id, booking_id, message_id)
            else:
                self.api.send(user_id, "Не удалось изменить статус этой записи.")
        elif action == "adigest":
            offset = int(parts[1])
            label = "Сегодня" if offset == 0 else "Завтра"
            self.api.send(
                user_id,
                self.daily_summary(self.now().date() + timedelta(days=offset), label),
                inline([("← Назад", "admin")]),
            )
        elif action == "amanual":
            self.show_manual_services(user_id, message_id=message_id)
        elif action == "amanualday":
            self.show_manual_services(user_id, self.decode_date(parts[1]), message_id)
        elif action == "amservice":
            service_id = int(parts[1])
            if len(parts) > 2:
                self.show_manual_times(user_id, service_id, self.decode_date(parts[2]), message_id)
            else:
                self.show_manual_dates(user_id, service_id, message_id=message_id)
        elif action == "amdates":
            self.show_manual_dates(user_id, int(parts[1]), int(parts[2]), message_id)
        elif action == "amday":
            self.show_manual_times(user_id, int(parts[1]), self.decode_date(parts[2]), message_id)
        elif action == "amslot":
            work_date = self.decode_date(parts[2])
            self.db.set_state(
                user_id,
                "admin_manual_details",
                json.dumps({"service_id": int(parts[1]), "date": work_date.isoformat(), "start": int(parts[3])}),
            )
            self.api.send(
                user_id,
                f"📝 <b>Запись на {pretty_date(work_date)} в {minutes_to_hhmm(int(parts[3]))}</b>\n\n"
                "Напиши имя и телефон клиента в формате:\n"
                "<code>Анна; +79991234567</code>\n\n"
                "Можно добавить Telegram третьим полем:\n"
                "<code>Анна; +79991234567; @anna</code>\n\n"
                "Для отмены — /cancel",
                inline([("← Назад", f"amday:{int(parts[1])}:{parts[2]}")]),
            )
        elif action == "acal":
            self.show_admin_calendar(user_id, int(parts[1]), message_id)
        elif action == "aday":
            self.show_day_management(user_id, self.decode_date(parts[1]), message_id)
        elif action == "aweek":
            self.show_weekly_schedule(user_id, message_id)
        elif action == "awday":
            weekday = int(parts[1])
            self.api.send(
                user_id,
                f"Настрой <b>{WEEKDAYS[weekday]}</b>:",
                inline(
                    [("🕒 Задать рабочие часы", f"awhours:{weekday}")],
                    [("🔴 Сделать выходным", f"awoff:{weekday}")],
                    [("← К графику", "aweek")],
                ),
            )
        elif action == "awhours":
            self.db.set_state(user_id, "admin_weekday_hours", json.dumps({"weekday": int(parts[1])}))
            self.api.send(
                user_id,
                f"Напиши рабочие часы для дня «{WEEKDAYS[int(parts[1])] }».\n\n"
                "Например: <b>10:00-19:30</b>\n\nДля отмены — /cancel",
                inline([("← Назад", "aweek")]),
            )
        elif action == "awoff":
            existing = self.db.weekly_schedule()[int(parts[1])]
            self.db.set_weekday(int(parts[1]), False, int(existing["start_minutes"]), int(existing["end_minutes"]))
            self.api.send(user_id, f"🔴 {WEEKDAYS[int(parts[1])]} теперь выходной.")
            self.show_weekly_schedule(user_id)
        elif action in {"ahours", "ablock"}:
            work_date = self.decode_date(parts[1])
            state = "admin_day_hours" if action == "ahours" else "admin_block_hours"
            self.db.set_state(user_id, state, json.dumps({"date": work_date.isoformat()}))
            hint = "рабочие часы" if action == "ahours" else "интервал, который нужно закрыть"
            self.api.send(
                user_id,
                f"Напиши {hint} для {pretty_date(work_date)}.\n\n"
                "Например: <b>10:00-18:30</b>\n\nДля отмены — /cancel",
                inline([("← Назад", f"aday:{parts[1]}")]),
            )
        elif action == "aoff":
            work_date = self.decode_date(parts[1])
            if self.db.day_bookings(work_date):
                self.api.send(user_id, "⚠️ На этот день уже есть записи. Сначала перенеси или отмени их, затем объяви выходной.")
            else:
                self.db.set_day_override(work_date, False)
                self.show_day_management(user_id, work_date)
        elif action == "areset":
            work_date = self.decode_date(parts[1])
            self.db.clear_day_override(work_date)
            self.show_day_management(user_id, work_date)
        elif action == "aunblock":
            self.db.remove_block(int(parts[1]))
            self.show_day_management(user_id, self.decode_date(parts[2]))
        elif action == "aservices":
            self.show_admin_services(user_id, message_id)
        elif action == "asvc":
            self.show_admin_service(user_id, int(parts[1]), message_id)
        elif action == "aedit":
            service_id = int(parts[1])
            field = parts[2]
            prompts = {
                "name": "Отправь новое название услуги.",
                "price": "Отправь новую стоимость числом. Например: <code>2500</code>",
                "duration_minutes": "Отправь длительность в минутах. Например: <code>90</code>",
                "description": "Отправь описание услуги. Чтобы удалить описание, отправь <code>-</code>.",
                "photo_file_id": "Отправь фотографию для этой услуги обычным сообщением.",
            }
            if field not in prompts:
                raise ValueError("Неизвестный параметр услуги")
            self.db.set_state(user_id, "admin_edit_service", json.dumps({"service_id": service_id, "field": field}))
            self.api.send(
                user_id,
                f"{prompts[field]}\n\nДля отмены — /cancel",
                inline([("← Назад", f"asvc:{service_id}")]),
            )
        elif action == "atoggle":
            self.db.toggle_service(int(parts[1]))
            self.show_admin_service(user_id, int(parts[1]))
        elif action == "aaddservice":
            self.db.set_state(user_id, "admin_add_service")
            self.api.send(
                user_id,
                "Отправь услугу в формате:\n\n<b>Название; минуты; цена</b>\n\n"
                "Например:\n<code>Ламинирование ресниц; 90; 2500</code>\n\nДля отмены — /cancel",
                inline([("← Назад", "aservices")]),
            )
        elif action == "aportfolio":
            self.show_admin_portfolio(user_id, message_id)
        elif action == "aaddress":
            self.show_admin_address(user_id, message_id)
        elif action == "aaddressedit":
            self.db.set_state(user_id, "admin_edit_address")
            self.api.send(
                user_id,
                "📍 Отправь новый адрес студии одним сообщением.\n\n"
                "Например: <code>Университетская улица, 25/2, вход со стороны двора</code>\n\n"
                "Для отмены — /cancel",
                inline([("← Назад", "aaddress")]),
            )
        elif action == "aaddressphoto":
            self.db.set_state(user_id, "admin_entrance_photo")
            self.api.send(
                user_id,
                "📸 Отправь фотографию входа в студию обычным сообщением. "
                "Клиенты увидят её вместе с адресом.\n\nДля отмены — /cancel",
                inline([("← Назад", "aaddress")]),
            )
        elif action == "aaddressclear":
            self.db.set_studio_setting("entrance_photo_file_id", "")
            self.api.send(user_id, "🗑 Фотография входа удалена.")
            self.show_admin_address(user_id)
        elif action == "aaddphoto":
            self.db.set_state(user_id, "admin_add_portfolio_photo")
            self.api.send(
                user_id,
                "📸 Отправь одну или несколько фотографий работ. При желании добавь подпись к фото.\n\n"
                "Каждая фотография появится в разделе «Фото работ». Для завершения нажми кнопку под сообщением или отправь /cancel.",
                inline([("← Назад", "aportfolio")]),
            )
        elif action == "adelphoto":
            if self.db.delete_portfolio_photo(int(parts[1])):
                self.api.send(user_id, "🗑 Фотография удалена.")
            self.show_admin_portfolio(user_id)
        elif action == "acancel":
            booking = self.db.booking(int(parts[1]))
            if booking and self.db.cancel_booking(int(parts[1])):
                self.api.send(user_id, f"Запись №{booking['id']} отменена, клиенту отправлено уведомление.")
                if int(booking["telegram_user_id"]) > 0:
                    self.safe_notify(
                        int(booking["telegram_user_id"]),
                        f"😔 Мастер отменил твою запись:\n\n{self.booking_summary(booking)}\n\n"
                        f"Чтобы подобрать другое время, напиши {escape(self.settings.contact)} или запишись заново.",
                        inline([("✨ Выбрать другое время", "book")]),
                    )
            else:
                self.api.send(user_id, "Эта запись уже отменена.")

    def process_reminders(self) -> None:
        current_monotonic = time_module.monotonic()
        if current_monotonic - self.last_reminder_check < 60:
            return
        self.last_reminder_check = current_monotonic
        now = self.now()
        for hours in (24, 2):
            for booking in self.db.upcoming_reminders(now, hours):
                lead = "завтра" if hours == 24 else "совсем скоро"
                try:
                    keyboard = inline(
                        [("✅ Да, приду", f"visitok:{booking['id']}")],
                        [("🔄 Перенести", f"reschedule:{booking['id']}"), ("❌ Отменить", f"cancelask:{booking['id']}")],
                        [("💬 Связаться с мастером", "support")],
                    )
                    self.api.send(
                        int(booking["telegram_user_id"]),
                        f"🔔 <b>Напоминание: процедура {lead}</b>\n\n"
                        f"{self.booking_summary(booking)}\n\nПодтверди, пожалуйста, что придёшь. 💛",
                        keyboard,
                    )
                    self.db.mark_reminder_sent(int(booking["id"]), hours)
                except TelegramAPIError:
                    logger.warning("Не удалось отправить напоминание по записи %s", booking["id"], exc_info=True)
        self.process_daily_summaries(now)

    def process_daily_summaries(self, now: datetime) -> None:
        summary_type: str | None = None
        work_date: date | None = None
        label = ""
        if self.settings.morning_summary_hour <= now.hour < self.settings.evening_summary_hour:
            summary_type = "morning"
            work_date = now.date()
            label = "Сегодня"
        elif now.hour >= self.settings.evening_summary_hour:
            summary_type = "evening"
            work_date = now.date() + timedelta(days=1)
            label = "Завтра"

        if summary_type is None or work_date is None:
            return
        for admin_id in self.settings.admin_ids:
            key = f"summary:{now.date().isoformat()}:{summary_type}:{admin_id}"
            if self.db.notification_sent(key):
                continue
            try:
                self.api.send(admin_id, self.daily_summary(work_date, label))
                self.db.mark_notification_sent(key, now)
            except TelegramAPIError:
                logger.warning("Не удалось отправить сводку администратору %s", admin_id, exc_info=True)
                
