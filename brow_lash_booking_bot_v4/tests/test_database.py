from __future__ import annotations

import tempfile
import sqlite3
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from beauty_bot.db import Database, hhmm_to_minutes, minutes_to_hhmm


TZ = ZoneInfo("Europe/Amsterdam")


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.today = date(2030, 1, 7)  # понедельник
        self.now = datetime(2030, 1, 7, 7, 0, tzinfo=TZ)
        self.service = self.db.services()[0]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create(self, user_id: int, start: int, replace_id: int | None = None):
        return self.db.create_booking(
            telegram_user_id=user_id,
            customer_name=f"Клиент {user_id}",
            username="client",
            phone="+79990000000",
            service_id=int(self.service["id"]),
            work_date=self.today,
            start_minutes=start,
            now=self.now,
            minimum_notice=0,
            replace_booking_id=replace_id,
        )

    def test_time_conversion(self) -> None:
        self.assertEqual(hhmm_to_minutes("10:30"), 630)
        self.assertEqual(minutes_to_hhmm(630), "10:30")
        with self.assertRaises(ValueError):
            hhmm_to_minutes("10")

    def test_default_week_and_services(self) -> None:
        self.assertGreaterEqual(len(self.db.services()), 6)
        self.assertEqual(self.db.day_hours(self.today), (600, 1200))
        self.assertIsNone(self.db.day_hours(self.today + timedelta(days=6)))

    def test_slots_respect_duration_booking_and_block(self) -> None:
        duration = int(self.service["duration_minutes"])
        initial = self.db.available_slots(self.today, duration, 30, self.now, 0)
        self.assertEqual(initial[0], 600)
        self.assertEqual(initial[-1], 1140)

        result = self.create(1, 660)
        self.assertTrue(result.ok)
        after_booking = self.db.available_slots(self.today, duration, 30, self.now, 0)
        self.assertNotIn(630, after_booking)
        self.assertNotIn(660, after_booking)

        self.db.add_block(self.today, 780, 840, "обед")
        after_block = self.db.available_slots(self.today, duration, 30, self.now, 0)
        self.assertNotIn(750, after_block)
        self.assertNotIn(780, after_block)

    def test_double_booking_is_rejected(self) -> None:
        first = self.create(1, 600)
        second = self.create(2, 630)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "slot_taken")

    def test_reschedule_is_atomic(self) -> None:
        original = self.create(1, 600)
        self.assertTrue(original.ok)
        moved = self.create(1, 900, original.booking_id)
        self.assertTrue(moved.ok)
        self.assertEqual(self.db.booking(original.booking_id)["status"], "rescheduled")
        self.assertEqual(self.db.booking(moved.booking_id)["status"], "confirmed")

    def test_block_and_shorter_day_cannot_cover_booking(self) -> None:
        self.assertTrue(self.create(1, 600).ok)
        with self.assertRaises(ValueError):
            self.db.add_block(self.today, 570, 630)
        with self.assertRaises(ValueError):
            self.db.set_day_override(self.today, True, 660, 1200)
        with self.assertRaises(ValueError):
            self.db.set_day_override(self.today, False)

    def test_cancel_releases_time(self) -> None:
        result = self.create(1, 600)
        self.assertTrue(self.db.cancel_booking(result.booking_id, 1))
        slots = self.db.available_slots(self.today, int(self.service["duration_minutes"]), 30, self.now, 0)
        self.assertIn(600, slots)

    def test_minimum_notice(self) -> None:
        close_now = datetime(2030, 1, 7, 9, 30, tzinfo=TZ)
        slots = self.db.available_slots(self.today, 60, 30, close_now, 120)
        self.assertEqual(slots[0], 690)

    def test_reminder_windows_do_not_overlap(self) -> None:
        soon = self.create(1, 600)
        self.assertTrue(soon.ok)
        reminder_now = datetime(2030, 1, 7, 9, 0, tzinfo=TZ)
        self.assertEqual(len(self.db.upcoming_reminders(reminder_now, 2)), 1)
        self.assertEqual(len(self.db.upcoming_reminders(reminder_now, 24)), 0)

    def test_update_service_and_portfolio_photos(self) -> None:
        service_id = int(self.service["id"])
        self.assertTrue(self.db.update_service(service_id, "name", "Новые брови"))
        self.assertTrue(self.db.update_service(service_id, "price", 2800))
        self.assertTrue(self.db.update_service(service_id, "duration_minutes", 75))
        self.assertTrue(self.db.update_service(service_id, "description", "Подготовка не нужна"))
        self.assertTrue(self.db.update_service(service_id, "photo_file_id", "telegram-photo"))
        updated = self.db.service(service_id)
        self.assertEqual(updated["name"], "Новые брови")
        self.assertEqual(updated["price"], 2800)
        self.assertEqual(updated["photo_file_id"], "telegram-photo")

        photo_id = self.db.add_portfolio_photo("portfolio-photo", "До и после", self.now)
        self.assertEqual(self.db.portfolio_photos()[0]["id"], photo_id)
        self.assertTrue(self.db.delete_portfolio_photo(photo_id))
        self.assertEqual(self.db.portfolio_photos(), [])

    def test_studio_settings_are_saved_and_validated(self) -> None:
        self.assertEqual(self.db.studio_setting("address", "Адрес по умолчанию"), "Адрес по умолчанию")
        self.db.set_studio_setting("address", "Университетская улица, 25/2, вход со стороны двора")
        self.db.set_studio_setting("entrance_photo_file_id", "entrance-photo")
        self.assertEqual(self.db.studio_setting("address"), "Университетская улица, 25/2, вход со стороны двора")
        self.assertEqual(self.db.studio_setting("entrance_photo_file_id"), "entrance-photo")
        self.db.set_studio_setting("entrance_photo_file_id", "")
        self.assertEqual(self.db.studio_setting("entrance_photo_file_id"), "")
        with self.assertRaises(ValueError):
            self.db.set_studio_setting("address", "")
        with self.assertRaises(ValueError):
            self.db.set_studio_setting("unknown", "value")

    def test_attendance_status_and_manual_reminder_handling(self) -> None:
        booking = self.create(1, 600)
        self.assertTrue(self.db.update_attendance_status(booking.booking_id, "client_confirmed"))
        self.assertEqual(self.db.booking(booking.booking_id)["attendance_status"], "client_confirmed")
        self.assertTrue(self.db.update_attendance_status(booking.booking_id, "completed"))
        self.assertFalse(self.db.cancel_booking(booking.booking_id))
        with self.assertRaises(ValueError):
            self.db.update_attendance_status(booking.booking_id, "invalid")

        manual = self.db.create_booking(
            telegram_user_id=-100,
            customer_name="Мария",
            username="",
            phone="+79991112233",
            service_id=int(self.service["id"]),
            work_date=self.today,
            start_minutes=720,
            now=self.now,
            minimum_notice=0,
            source="manual",
        )
        self.assertTrue(manual.ok)
        self.assertEqual(self.db.booking(manual.booking_id)["source"], "manual")
        reminder_now = datetime(2030, 1, 7, 11, 0, tzinfo=TZ)
        self.assertEqual(self.db.upcoming_reminders(reminder_now, 2), [])

    def test_old_database_is_migrated_without_losing_records(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL,
                    service_id INTEGER NOT NULL,
                    service_name TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    work_date TEXT NOT NULL,
                    start_minutes INTEGER NOT NULL,
                    end_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    reminder_24_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_2_sent INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO services(id, name, duration_minutes, price, active)
                VALUES (1, 'Старая услуга', 60, 1500, 1);
                INSERT INTO bookings(
                    id, telegram_user_id, customer_name, username, phone, service_id,
                    service_name, duration_minutes, price, work_date, start_minutes,
                    end_minutes, status, created_at
                ) VALUES (
                    1, 100, 'Анна', 'anna', '+79991112233', 1, 'Старая услуга',
                    60, 1500, '2030-01-07', 600, 660, 'confirmed', '2030-01-07T07:00:00'
                );
                """
            )

        migrated = Database(legacy_path)
        self.assertEqual(migrated.service(1)["name"], "Старая услуга")
        self.assertEqual(migrated.service(1)["description"], "")
        self.assertEqual(migrated.booking(1)["customer_name"], "Анна")
        self.assertEqual(migrated.booking(1)["attendance_status"], "pending")
        self.assertEqual(migrated.booking(1)["source"], "telegram")

    def test_summary_notification_is_recorded_once(self) -> None:
        key = "summary:2030-01-07:morning:999"
        self.assertFalse(self.db.notification_sent(key))
        self.db.mark_notification_sent(key, self.now)
        self.db.mark_notification_sent(key, self.now)
        self.assertTrue(self.db.notification_sent(key))


if __name__ == "__main__":
    unittest.main()
