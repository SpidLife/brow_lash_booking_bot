from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from beauty_bot.bot import BeautyBot
from beauty_bot.config import Settings
from beauty_bot.db import Database


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.callbacks: list[str] = []
        self.photos: list[dict] = []

    def send(self, chat_id, text, keyboard=None, **extra):
        item = {"chat_id": chat_id, "text": text, "keyboard": keyboard, **extra}
        self.sent.append(item)
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text, "keyboard": keyboard})

    def answer_callback(self, callback_id, text="", alert=False):
        self.callbacks.append(callback_id)

    def send_photo(self, chat_id, file_id, caption="", keyboard=None):
        item = {"chat_id": chat_id, "file_id": file_id, "caption": caption, "keyboard": keyboard}
        self.photos.append(item)
        return {"message_id": len(self.sent) + len(self.photos)}


class FixedBot(BeautyBot):
    def now(self):
        return datetime(2030, 1, 7, 7, 0, tzinfo=self.settings.timezone)


class BotFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        timezone = ZoneInfo("Europe/Amsterdam")
        self.settings = Settings(
            bot_token="test-token",
            admin_ids=frozenset({999}),
            studio_name="Test Studio",
            master_name="Ксюша",
            address="Test address",
            contact="@master",
            timezone=timezone,
            currency="₽",
            database_path=Path(self.tempdir.name) / "bot.sqlite3",
            booking_horizon_days=21,
            slot_step_minutes=30,
            minimum_notice_minutes=0,
            cancellation_notice_hours=3,
        )
        self.api = FakeAPI()
        self.db = Database(self.settings.database_path)
        self.bot = FixedBot(self.settings, self.db, self.api)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def message(self, user_id: int, text: str, contact=None, photo=None, caption=None) -> dict:
        data = {
            "message": {
                "message_id": 1,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "first_name": "Анна", "username": "anna"},
                "text": text,
            }
        }
        if contact:
            data["message"]["contact"] = contact
        if photo:
            data["message"]["photo"] = photo
        if caption is not None:
            data["message"]["caption"] = caption
        return data

    def callback(self, user_id: int, data: str) -> dict:
        return {
            "callback_query": {
                "id": f"callback-{len(self.api.callbacks)}",
                "from": {"id": user_id, "first_name": "Анна", "username": "anna"},
                "message": {"message_id": 1, "chat": {"id": user_id, "type": "private"}},
                "data": data,
            }
        }

    def test_start_and_admin_visibility(self) -> None:
        self.bot.handle_update(self.message(100, "/start"))
        customer_keyboard = self.api.sent[-1]["keyboard"]
        labels = [button["text"] for row in customer_keyboard["keyboard"] for button in row]
        self.assertNotIn("⚙️ Управление расписанием", labels)
        self.assertIn("💬 Связаться с мастером", labels)
        self.assertIn("📸 Фото работ", labels)

        self.bot.handle_update(self.message(999, "/start"))
        admin_keyboard = self.api.sent[-1]["keyboard"]
        labels = [button["text"] for row in admin_keyboard["keyboard"] for button in row]
        self.assertIn("⚙️ Управление расписанием", labels)

    def test_full_client_booking_flow(self) -> None:
        user_id = 100
        service_id = int(self.db.services()[0]["id"])
        self.bot.handle_update(self.message(user_id, "/start"))
        self.bot.handle_update(self.callback(user_id, f"slot:{service_id}:20300107:600"))
        self.assertEqual(self.db.state(user_id)["state"], "await_phone")

        contact = {"user_id": user_id, "phone_number": "+7 (999) 111-22-33"}
        self.bot.handle_update(self.message(user_id, "", contact=contact))
        self.assertIsNone(self.db.state(user_id))
        self.assertIn("Проверь свою запись", self.api.sent[-1]["text"])

        self.bot.handle_update(self.callback(user_id, f"confirm:{service_id}:20300107:600"))
        bookings = self.db.customer_bookings(user_id, datetime(2030, 1, 7).date())
        self.assertEqual(len(bookings), 1)
        self.assertIn("подтверждена", self.api.sent[-2]["text"])
        self.assertIn("Новая запись", self.api.sent[-1]["text"])
        self.assertEqual(self.api.sent[-1]["chat_id"], 999)

    def test_contact_request_and_message_notify_admin(self) -> None:
        user_id = 100
        self.bot.handle_update(self.message(user_id, "/start"))
        self.bot.handle_update(self.message(user_id, "💬 Связаться с мастером"))

        self.assertEqual(self.db.state(user_id)["state"], "await_contact_message")
        self.assertEqual(self.api.sent[-2]["chat_id"], 999)
        self.assertIn("Клиент хочет связаться", self.api.sent[-2]["text"])
        self.assertIn("@anna", self.api.sent[-2]["text"])
        self.assertIn("tg://user?id=100", self.api.sent[-2]["text"])
        self.assertEqual(self.api.sent[-1]["chat_id"], user_id)

        self.bot.handle_update(self.message(user_id, "Есть окно на завтра?"))

        self.assertIsNone(self.db.state(user_id))
        self.assertEqual(self.api.sent[-2]["chat_id"], 999)
        self.assertIn("Новое сообщение от клиента", self.api.sent[-2]["text"])
        self.assertIn("Есть окно на завтра?", self.api.sent[-2]["text"])
        self.assertIn("Сообщение передано", self.api.sent[-1]["text"])

    def test_contact_message_escapes_html_and_preserves_phone(self) -> None:
        user_id = 100
        self.db.save_profile(user_id, "Анна", "anna", "+79991112233")
        self.bot.handle_update(self.message(user_id, "/contact"))
        self.bot.handle_update(self.message(user_id, "<b>Здравствуйте</b>"))

        admin_message = self.api.sent[-2]
        self.assertEqual(admin_message["chat_id"], 999)
        self.assertIn("+79991112233", admin_message["text"])
        self.assertIn("&lt;b&gt;Здравствуйте&lt;/b&gt;", admin_message["text"])

    def test_empty_contact_message_keeps_request_open(self) -> None:
        user_id = 100
        self.bot.handle_update(self.message(user_id, "/contact"))
        self.bot.handle_update(self.message(user_id, ""))

        self.assertEqual(self.db.state(user_id)["state"], "await_contact_message")
        self.assertIn("Напиши вопрос текстом", self.api.sent[-1]["text"])

    def test_non_admin_cannot_open_admin_callback(self) -> None:
        self.bot.handle_update(self.callback(100, "admin"))
        self.assertIn("только мастеру", self.api.sent[-1]["text"])

    def test_manager_edits_service_price_description_and_photo(self) -> None:
        service_id = int(self.db.services()[0]["id"])
        self.bot.handle_update(self.callback(999, f"aedit:{service_id}:price"))
        self.bot.handle_update(self.message(999, "2700"))
        self.assertEqual(self.db.service(service_id)["price"], 2700)

        self.bot.handle_update(self.callback(999, f"aedit:{service_id}:description"))
        self.bot.handle_update(self.message(999, "Бережное оформление бровей"))
        self.assertEqual(self.db.service(service_id)["description"], "Бережное оформление бровей")

        self.bot.handle_update(self.callback(999, f"aedit:{service_id}:photo_file_id"))
        self.bot.handle_update(self.message(999, "", photo=[{"file_id": "small"}, {"file_id": "service-photo"}]))
        self.assertEqual(self.db.service(service_id)["photo_file_id"], "service-photo")

        self.bot.handle_update(self.message(100, "/start"))
        self.bot.handle_update(self.callback(100, f"svc:{service_id}"))
        self.assertEqual(self.api.photos[-1]["chat_id"], 100)
        self.assertEqual(self.api.photos[-1]["file_id"], "service-photo")
        self.assertIn("Бережное оформление", self.api.photos[-1]["caption"])

    def test_manager_uploads_portfolio_and_customer_views_it(self) -> None:
        self.bot.handle_update(self.callback(999, "aaddphoto"))
        self.bot.handle_update(
            self.message(999, "", photo=[{"file_id": "small"}, {"file_id": "portfolio-photo"}], caption="Ресницы 2D")
        )
        self.assertEqual(self.db.portfolio_photos()[0]["caption"], "Ресницы 2D")
        self.assertEqual(self.db.state(999)["state"], "admin_add_portfolio_photo")

        self.bot.handle_update(self.message(100, "📸 Фото работ"))
        self.assertEqual(self.api.photos[-1]["chat_id"], 100)
        self.assertEqual(self.api.photos[-1]["file_id"], "portfolio-photo")
        self.assertIn("Ресницы 2D", self.api.photos[-1]["caption"])
        self.assertIn('Ещё больше работ — <a href="https://www.instagram.com/kksdaun/">тут</a>', self.api.sent[-1]["text"])

    def test_empty_portfolio_still_links_to_instagram(self) -> None:
        self.bot.handle_update(self.message(100, "📸 Фото работ"))

        self.assertIn('Ещё больше работ — <a href="https://www.instagram.com/kksdaun/">тут</a>', self.api.sent[-1]["text"])

    def test_customer_sees_address_with_entrance_photo(self) -> None:
        address = "Университетская улица, 25/2, вход со стороны двора"
        self.db.set_studio_setting("address", address)
        self.db.set_studio_setting("entrance_photo_file_id", "entrance-photo")

        self.bot.handle_update(self.message(100, "📍 Адрес и контакты"))

        self.assertEqual(self.api.photos[-1]["chat_id"], 100)
        self.assertEqual(self.api.photos[-1]["file_id"], "entrance-photo")
        self.assertIn(address, self.api.photos[-1]["caption"])
        self.assertIn('<a href="https://t.me/master">@master</a>', self.api.photos[-1]["caption"])
        self.assertIn('<a href="https://www.instagram.com/kksdaun/">@kksdaun</a>', self.api.photos[-1]["caption"])
        self.assertNotIn("Часовой пояс", self.api.photos[-1]["caption"])

    def test_contacts_without_photo_show_short_links_and_no_timezone(self) -> None:
        self.bot.handle_update(self.message(100, "📍 Адрес и контакты"))

        text = self.api.sent[-1]["text"]
        self.assertIn('<a href="https://t.me/master">@master</a>', text)
        self.assertIn('<a href="https://www.instagram.com/kksdaun/">@kksdaun</a>', text)
        self.assertNotIn("Часовой пояс", text)

    def test_manager_changes_address_and_uploads_entrance_photo(self) -> None:
        address = "Университетская улица, 25/2, вход со стороны двора"
        self.bot.handle_update(self.callback(999, "aaddressedit"))
        self.bot.handle_update(self.message(999, address))
        self.assertEqual(self.bot.studio_address(), address)

        self.bot.handle_update(self.callback(999, "aaddressphoto"))
        self.bot.handle_update(
            self.message(999, "", photo=[{"file_id": "small"}, {"file_id": "entrance-photo"}])
        )
        self.assertEqual(self.db.studio_setting("entrance_photo_file_id"), "entrance-photo")
        self.assertIsNone(self.db.state(999))

        self.bot.handle_update(self.callback(999, "aaddressclear"))
        self.assertEqual(self.db.studio_setting("entrance_photo_file_id"), "")

    def test_manager_creates_manual_booking_and_changes_status(self) -> None:
        service_id = int(self.db.services()[0]["id"])
        self.bot.handle_update(self.callback(999, f"amslot:{service_id}:20300107:600"))
        self.assertEqual(self.db.state(999)["state"], "admin_manual_details")

        self.bot.handle_update(self.message(999, "Мария; +7 999 555 44 33; @maria"))
        booking = self.db.day_bookings(datetime(2030, 1, 7).date())[0]
        self.assertEqual(booking["customer_name"], "Мария")
        self.assertEqual(booking["phone"], "+79995554433")
        self.assertEqual(booking["username"], "maria")
        self.assertEqual(booking["source"], "manual")
        self.assertLess(booking["telegram_user_id"], 0)
        self.assertIn("Ручная запись", self.api.sent[-1]["text"])

        self.bot.handle_update(self.callback(999, f"astatus:{booking['id']}:completed"))
        self.assertEqual(self.db.booking(int(booking["id"]))["attendance_status"], "completed")
        self.assertIn("Услуга выполнена", self.api.edited[-1]["text"])

    def test_customer_confirms_visit_and_manager_is_notified(self) -> None:
        user_id = 100
        service_id = int(self.db.services()[0]["id"])
        self.bot.handle_update(self.message(user_id, "/start"))
        self.db.save_profile(user_id, "Анна", "anna", "+79991112233")
        result = self.db.create_booking(
            telegram_user_id=user_id,
            customer_name="Анна",
            username="anna",
            phone="+79991112233",
            service_id=service_id,
            work_date=datetime(2030, 1, 7).date(),
            start_minutes=600,
            now=self.bot.now(),
            minimum_notice=0,
        )
        self.bot.handle_update(self.callback(user_id, f"visitok:{result.booking_id}"))

        self.assertEqual(self.db.booking(result.booking_id)["attendance_status"], "client_confirmed")
        self.assertEqual(self.api.sent[-1]["chat_id"], 999)
        self.assertIn("Клиент подтвердил визит", self.api.sent[-1]["text"])

    def test_morning_and_evening_summaries_are_sent_once(self) -> None:
        morning = self.bot.now().replace(hour=9)
        self.bot.process_daily_summaries(morning)
        self.bot.process_daily_summaries(morning + timedelta(minutes=20))

        morning_messages = [item for item in self.api.sent if "Сегодня:" in item["text"]]
        self.assertEqual(len(morning_messages), 1)
        self.assertEqual(morning_messages[0]["chat_id"], 999)

        self.bot.process_daily_summaries(morning.replace(hour=20))
        evening_messages = [item for item in self.api.sent if "Завтра:" in item["text"]]
        self.assertEqual(len(evening_messages), 1)

    def test_reminder_contains_visit_confirmation_buttons(self) -> None:
        user_id = 100
        service_id = int(self.db.services()[0]["id"])
        result = self.db.create_booking(
            telegram_user_id=user_id,
            customer_name="Анна",
            username="anna",
            phone="+79991112233",
            service_id=service_id,
            work_date=self.bot.now().date(),
            start_minutes=600,
            now=self.bot.now(),
            minimum_notice=0,
        )
        self.bot.last_reminder_check = -1000
        self.bot.process_reminders()

        reminders = [item for item in self.api.sent if "Напоминание" in item["text"]]
        self.assertEqual(len(reminders), 1)
        button_data = [button["callback_data"] for row in reminders[0]["keyboard"]["inline_keyboard"] for button in row]
        self.assertIn(f"visitok:{result.booking_id}", button_data)


if __name__ == "__main__":
    unittest.main()
