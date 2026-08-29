from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator


DEFAULT_SERVICES = (
    ("✨ Коррекция и окрашивание бровей", 60, 1500),
    ("🌿 Ламинирование бровей", 75, 2200),
    ("💫 Ламинирование ресниц", 90, 2500),
    ("👁 Наращивание ресниц — классика", 120, 3000),
    ("🦋 Наращивание ресниц — 2D", 150, 3500),
    ("💎 Комплекс: брови + ламинирование ресниц", 150, 3800),
)

ATTENDANCE_STATUSES = frozenset({"pending", "client_confirmed", "completed", "no_show"})
BOOKING_STATUSES = frozenset({"confirmed", "cancelled", "rescheduled"})


@dataclass(frozen=True, slots=True)
class BookingResult:
    ok: bool
    booking_id: int | None = None
    reason: str = ""


def hhmm_to_minutes(value: str) -> int:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Время нужно указывать в формате ЧЧ:ММ.") from exc
    if len(value) != 5:
        raise ValueError("Время нужно указывать в формате ЧЧ:ММ.")
    return parsed.hour * 60 + parsed.minute


def minutes_to_hhmm(minutes: int) -> str:
    if not 0 <= minutes <= 1440:
        raise ValueError("Время выходит за пределы суток.")
    if minutes == 1440:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 5 AND 1440),
                    price INTEGER NOT NULL CHECK(price >= 0),
                    active INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    photo_file_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS weekly_schedule (
                    weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    start_minutes INTEGER NOT NULL,
                    end_minutes INTEGER NOT NULL,
                    CHECK(start_minutes >= 0 AND end_minutes <= 1440 AND start_minutes < end_minutes)
                );
                CREATE TABLE IF NOT EXISTS day_overrides (
                    work_date TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    start_minutes INTEGER,
                    end_minutes INTEGER
                );
                CREATE TABLE IF NOT EXISTS blocked_intervals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_date TEXT NOT NULL,
                    start_minutes INTEGER NOT NULL,
                    end_minutes INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    CHECK(start_minutes >= 0 AND end_minutes <= 1440 AND start_minutes < end_minutes)
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL,
                    service_id INTEGER NOT NULL REFERENCES services(id),
                    service_name TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    work_date TEXT NOT NULL,
                    start_minutes INTEGER NOT NULL,
                    end_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    attendance_status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TEXT NOT NULL,
                    reminder_24_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_2_sent INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(work_date, status);
                CREATE INDEX IF NOT EXISTS idx_bookings_customer ON bookings(telegram_user_id, status);
                CREATE TABLE IF NOT EXISTS customer_profiles (
                    telegram_user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS user_states (
                    telegram_user_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS portfolio_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    notification_key TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS studio_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._migrate(conn)
            if not conn.execute("SELECT 1 FROM weekly_schedule LIMIT 1").fetchone():
                for weekday in range(7):
                    conn.execute(
                        "INSERT INTO weekly_schedule VALUES (?, ?, ?, ?)",
                        (weekday, int(weekday != 6), 10 * 60, 20 * 60),
                    )
            if not conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
                conn.executemany(
                    "INSERT INTO services(name, duration_minutes, price) VALUES (?, ?, ?)",
                    DEFAULT_SERVICES,
                )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        service_columns = {row["name"] for row in conn.execute("PRAGMA table_info(services)")}
        if "description" not in service_columns:
            conn.execute("ALTER TABLE services ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "photo_file_id" not in service_columns:
            conn.execute("ALTER TABLE services ADD COLUMN photo_file_id TEXT NOT NULL DEFAULT ''")
        if "deleted" not in service_columns:
            conn.execute("ALTER TABLE services ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")

        booking_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)")}
        if "attendance_status" not in booking_columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN attendance_status TEXT NOT NULL DEFAULT 'pending'")
        if "source" not in booking_columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'")

    def services(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        clause = " WHERE deleted = 0" if include_inactive else " WHERE active = 1 AND deleted = 0"
        sql = "SELECT * FROM services" + clause + " ORDER BY id"
        with self.connect() as conn:
            return list(conn.execute(sql))

    def service(self, service_id: int, active_only: bool = True) -> sqlite3.Row | None:
        clause = " AND active = 1 AND deleted = 0" if active_only else " AND deleted = 0"
        with self.connect() as conn:
            return conn.execute(f"SELECT * FROM services WHERE id = ?{clause}", (service_id,)).fetchone()

    def add_service(self, name: str, duration_minutes: int, price: int) -> int:
        if not name.strip() or not 5 <= duration_minutes <= 1440 or price < 0:
            raise ValueError("Проверь название, продолжительность и стоимость услуги.")
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO services(name, duration_minutes, price) VALUES (?, ?, ?)",
                (name.strip(), duration_minutes, price),
            )
            return int(cursor.lastrowid)

    def toggle_service(self, service_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE services SET active = 1 - active WHERE id = ? AND deleted = 0", (service_id,))

    def delete_service(self, service_id: int) -> bool:
        """Hide a service permanently while preserving historical booking rows."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE services SET active = 0, deleted = 1 WHERE id = ? AND deleted = 0",
                (service_id,),
            )
            return cursor.rowcount > 0

    def update_service(self, service_id: int, field: str, value: str | int) -> bool:
        allowed_fields = {"name", "duration_minutes", "price", "description", "photo_file_id"}
        if field not in allowed_fields:
            raise ValueError("Неизвестный параметр услуги.")
        if field == "name":
            value = str(value).strip()
            if not value:
                raise ValueError("Название услуги не может быть пустым.")
        elif field == "duration_minutes":
            value = int(value)
            if not 5 <= value <= 1440:
                raise ValueError("Продолжительность должна быть от 5 до 1440 минут.")
        elif field == "price":
            value = int(value)
            if value < 0:
                raise ValueError("Стоимость не может быть отрицательной.")
        else:
            value = str(value).strip()
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE services SET {field} = ? WHERE id = ? AND deleted = 0",
                (value, service_id),
            )
            return cursor.rowcount > 0

    def add_portfolio_photo(self, file_id: str, caption: str, created_at: datetime) -> int:
        if not file_id.strip():
            raise ValueError("Не удалось получить фотографию из Telegram.")
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO portfolio_photos(file_id, caption, created_at) VALUES (?, ?, ?)",
                (file_id.strip(), caption.strip()[:900], created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def portfolio_photos(self, limit: int = 12) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM portfolio_photos ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 30)),),
            ))

    def delete_portfolio_photo(self, photo_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM portfolio_photos WHERE id = ?", (photo_id,))
            return cursor.rowcount > 0

    def studio_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM studio_settings WHERE setting_key = ?",
                (key,),
            ).fetchone()
        return str(row["setting_value"]) if row is not None else default

    def set_studio_setting(self, key: str, value: str) -> None:
        if key not in {"address", "entrance_photo_file_id"}:
            raise ValueError("Неизвестная настройка студии.")
        normalized = str(value).strip()
        if key == "address" and (not normalized or len(normalized) > 250):
            raise ValueError("Адрес должен содержать от 1 до 250 символов.")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO studio_settings(setting_key, setting_value) VALUES (?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
                (key, normalized),
            )

    def set_weekday(self, weekday: int, enabled: bool, start: int = 600, end: int = 1200) -> None:
        if not 0 <= weekday <= 6 or not 0 <= start < end <= 1440:
            raise ValueError("Некорректный день недели или рабочий интервал.")
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO weekly_schedule(weekday, enabled, start_minutes, end_minutes) VALUES (?, ?, ?, ?)",
                (weekday, int(enabled), start, end),
            )

    def weekly_schedule(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM weekly_schedule ORDER BY weekday"))

    def set_day_override(self, work_date: date, enabled: bool, start: int | None = None, end: int | None = None) -> None:
        if enabled and (start is None or end is None or not 0 <= start < end <= 1440):
            raise ValueError("Для рабочего дня нужно указать корректные часы.")
        with self.connect() as conn:
            existing = list(conn.execute(
                "SELECT start_minutes, end_minutes FROM bookings WHERE work_date = ? AND status = 'confirmed'",
                (work_date.isoformat(),),
            ))
            if not enabled and existing:
                raise ValueError("На этот день уже есть активные записи.")
            if enabled and any(int(row["start_minutes"]) < start or int(row["end_minutes"]) > end for row in existing):
                raise ValueError("Новые часы не включают одну или несколько активных записей.")
            conn.execute(
                "INSERT OR REPLACE INTO day_overrides VALUES (?, ?, ?, ?)",
                (work_date.isoformat(), int(enabled), start, end),
            )

    def clear_day_override(self, work_date: date) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM day_overrides WHERE work_date = ?", (work_date.isoformat(),))

    def day_hours(self, work_date: date) -> tuple[int, int] | None:
        with self.connect() as conn:
            return self._day_hours(conn, work_date)

    @staticmethod
    def _day_hours(conn: sqlite3.Connection, work_date: date) -> tuple[int, int] | None:
        override = conn.execute("SELECT * FROM day_overrides WHERE work_date = ?", (work_date.isoformat(),)).fetchone()
        schedule = override or conn.execute("SELECT * FROM weekly_schedule WHERE weekday = ?", (work_date.weekday(),)).fetchone()
        if not schedule or not schedule["enabled"]:
            return None
        return int(schedule["start_minutes"]), int(schedule["end_minutes"])

    def add_block(self, work_date: date, start: int, end: int, reason: str = "") -> int:
        if not 0 <= start < end <= 1440:
            raise ValueError("Некорректное время блокировки.")
        with self.connect() as conn:
            conflict = conn.execute(
                """
                SELECT 1 FROM bookings
                WHERE work_date = ? AND status = 'confirmed'
                  AND start_minutes < ? AND end_minutes > ?
                LIMIT 1
                """,
                (work_date.isoformat(), end, start),
            ).fetchone()
            if conflict:
                raise ValueError("Этот интервал пересекается с активной записью клиента.")
            cursor = conn.execute(
                "INSERT INTO blocked_intervals(work_date, start_minutes, end_minutes, reason) VALUES (?, ?, ?, ?)",
                (work_date.isoformat(), start, end, reason.strip()),
            )
            return int(cursor.lastrowid)

    def blocks(self, work_date: date) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM blocked_intervals WHERE work_date = ? ORDER BY start_minutes", (work_date.isoformat(),)))

    def remove_block(self, block_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM blocked_intervals WHERE id = ?", (block_id,))

    def available_slots(
        self,
        work_date: date,
        duration: int,
        step: int,
        now: datetime,
        minimum_notice: int,
        exclude_booking_id: int | None = None,
    ) -> list[int]:
        if duration <= 0 or step <= 0:
            return []
        with self.connect() as conn:
            hours = self._day_hours(conn, work_date)
            if not hours:
                return []
            start, end = hours
            unavailable = [
                (int(row["start_minutes"]), int(row["end_minutes"]))
                for row in conn.execute(
                    "SELECT start_minutes, end_minutes FROM blocked_intervals WHERE work_date = ?",
                    (work_date.isoformat(),),
                )
            ]
            unavailable.extend(
                (int(row["start_minutes"]), int(row["end_minutes"]))
                for row in conn.execute(
                    "SELECT start_minutes, end_minutes FROM bookings WHERE work_date = ? AND status = 'confirmed' AND (? IS NULL OR id != ?)",
                    (work_date.isoformat(), exclude_booking_id, exclude_booking_id),
                )
            )

        earliest = now + timedelta(minutes=minimum_notice)
        slots: list[int] = []
        candidate = ((start + step - 1) // step) * step
        while candidate + duration <= end:
            candidate_dt = datetime.combine(work_date, time(candidate // 60, candidate % 60), tzinfo=now.tzinfo)
            if candidate_dt >= earliest and all(candidate >= busy_end or candidate + duration <= busy_start for busy_start, busy_end in unavailable):
                slots.append(candidate)
            candidate += step
        return slots

    def create_booking(
        self,
        *,
        telegram_user_id: int,
        customer_name: str,
        username: str,
        phone: str,
        service_id: int,
        work_date: date,
        start_minutes: int,
        now: datetime,
        minimum_notice: int,
        replace_booking_id: int | None = None,
        source: str = "telegram",
    ) -> BookingResult:
        if source not in {"telegram", "manual"}:
            raise ValueError("Неизвестный источник записи.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            service = conn.execute(
                "SELECT * FROM services WHERE id = ? AND active = 1 AND deleted = 0",
                (service_id,),
            ).fetchone()
            if not service:
                return BookingResult(False, reason="service_unavailable")

            end_minutes = start_minutes + int(service["duration_minutes"])
            hours = self._day_hours(conn, work_date)
            if not hours or start_minutes < hours[0] or end_minutes > hours[1]:
                return BookingResult(False, reason="outside_working_hours")

            candidate = datetime.combine(work_date, time(start_minutes // 60, start_minutes % 60), tzinfo=now.tzinfo)
            if candidate < now + timedelta(minutes=minimum_notice):
                return BookingResult(False, reason="too_soon")

            conflict = conn.execute(
                """
                SELECT 1 FROM bookings
                WHERE work_date = ? AND status = 'confirmed' AND (? IS NULL OR id != ?)
                  AND start_minutes < ? AND end_minutes > ?
                UNION ALL
                SELECT 1 FROM blocked_intervals
                WHERE work_date = ? AND start_minutes < ? AND end_minutes > ?
                LIMIT 1
                """,
                (work_date.isoformat(), replace_booking_id, replace_booking_id, end_minutes, start_minutes,
                 work_date.isoformat(), end_minutes, start_minutes),
            ).fetchone()
            if conflict:
                return BookingResult(False, reason="slot_taken")

            if replace_booking_id is not None:
                previous = conn.execute(
                    "SELECT * FROM bookings WHERE id = ? AND telegram_user_id = ? AND status = 'confirmed'",
                    (replace_booking_id, telegram_user_id),
                ).fetchone()
                if not previous:
                    return BookingResult(False, reason="old_booking_missing")
                conn.execute("UPDATE bookings SET status = 'rescheduled' WHERE id = ?", (replace_booking_id,))

            cursor = conn.execute(
                """
                INSERT INTO bookings (
                    telegram_user_id, customer_name, username, phone, service_id, service_name,
                    duration_minutes, price, work_date, start_minutes, end_minutes, created_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_user_id, customer_name, username, phone, service_id, service["name"],
                 service["duration_minutes"], service["price"], work_date.isoformat(), start_minutes,
                 end_minutes, now.isoformat(), source),
            )
            conn.execute(
                "INSERT OR REPLACE INTO customer_profiles(telegram_user_id, full_name, username, phone) VALUES (?, ?, ?, ?)",
                (telegram_user_id, customer_name, username, phone),
            )
            return BookingResult(True, booking_id=int(cursor.lastrowid))

    def booking(self, booking_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()

    def customer_bookings(self, user_id: int, today: date) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM bookings WHERE telegram_user_id = ? AND status = 'confirmed' AND work_date >= ? ORDER BY work_date, start_minutes",
                (user_id, today.isoformat()),
            ))

    def day_bookings(self, work_date: date) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM bookings WHERE work_date = ? AND status = 'confirmed' ORDER BY start_minutes",
                (work_date.isoformat(),),
            ))

    @staticmethod
    def _admin_booking_conditions(
        start_date: date | None,
        end_date: date | None,
        status: str,
    ) -> tuple[str, list[str]]:
        if status != "all" and status not in BOOKING_STATUSES:
            raise ValueError("Неизвестный статус записи.")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("Начальная дата не может быть позже конечной.")
        conditions: list[str] = []
        parameters: list[str] = []
        if start_date is not None:
            conditions.append("work_date >= ?")
            parameters.append(start_date.isoformat())
        if end_date is not None:
            conditions.append("work_date <= ?")
            parameters.append(end_date.isoformat())
        if status != "all":
            conditions.append("status = ?")
            parameters.append(status)
        return (" WHERE " + " AND ".join(conditions)) if conditions else "", parameters

    def admin_bookings(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        status: str = "all",
        limit: int = 8,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        if not 1 <= limit <= 100:
            raise ValueError("Количество записей должно быть от 1 до 100.")
        if offset < 0:
            raise ValueError("Смещение записей не может быть отрицательным.")
        where, parameters = self._admin_booking_conditions(start_date, end_date, status)
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM bookings"
                + where
                + " ORDER BY work_date, start_minutes, id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ))

    def admin_booking_overview(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, int]:
        where, parameters = self._admin_booking_conditions(start_date, end_date, "all")
        with self.connect() as conn:
            summary = conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END), 0) AS confirmed, "
                "COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled, "
                "COALESCE(SUM(CASE WHEN status = 'rescheduled' THEN 1 ELSE 0 END), 0) AS rescheduled, "
                "COALESCE(SUM(CASE WHEN status = 'confirmed' THEN price ELSE 0 END), 0) AS revenue "
                "FROM bookings"
                + where,
                parameters,
            ).fetchone()
        assert summary is not None
        return {key: int(summary[key]) for key in summary.keys()}

    def cancel_booking(self, booking_id: int, user_id: int | None = None) -> bool:
        with self.connect() as conn:
            if user_id is None:
                cursor = conn.execute(
                    "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed' "
                    "AND attendance_status NOT IN ('completed', 'no_show')",
                    (booking_id,),
                )
            else:
                cursor = conn.execute(
                    "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND telegram_user_id = ? "
                    "AND status = 'confirmed' AND attendance_status NOT IN ('completed', 'no_show')",
                    (booking_id, user_id),
                )
            return cursor.rowcount > 0

    def update_attendance_status(self, booking_id: int, attendance_status: str) -> bool:
        if attendance_status not in ATTENDANCE_STATUSES:
            raise ValueError("Неизвестный статус записи.")
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE bookings SET attendance_status = ? WHERE id = ? AND status = 'confirmed'",
                (attendance_status, booking_id),
            )
            return cursor.rowcount > 0

    def profile(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM customer_profiles WHERE telegram_user_id = ?", (user_id,)).fetchone()

    def save_profile(self, user_id: int, full_name: str, username: str, phone: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO customer_profiles(telegram_user_id, full_name, username, phone)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username,
                    phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE customer_profiles.phone END
                """,
                (user_id, full_name, username, phone),
            )

    def state(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM user_states WHERE telegram_user_id = ?", (user_id,)).fetchone()

    def set_state(self, user_id: int, state: str, payload: str = "{}") -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO user_states VALUES (?, ?, ?)", (user_id, state, payload))

    def clear_state(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM user_states WHERE telegram_user_id = ?", (user_id,))

    def upcoming_reminders(self, now: datetime, hours: int) -> list[sqlite3.Row]:
        if hours not in {2, 24}:
            raise ValueError("Поддерживаются напоминания за 2 и 24 часа.")
        flag = f"reminder_{hours}_sent"
        window_end = now + timedelta(hours=hours)
        result: list[sqlite3.Row] = []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM bookings WHERE status = 'confirmed' AND telegram_user_id > 0 "
                f"AND attendance_status NOT IN ('completed', 'no_show') AND {flag} = 0 "
                "AND work_date BETWEEN ? AND ?",
                (now.date().isoformat(), window_end.date().isoformat()),
            )
            for row in rows:
                appointment = datetime.combine(
                    date.fromisoformat(row["work_date"]),
                    time(int(row["start_minutes"]) // 60, int(row["start_minutes"]) % 60),
                    tzinfo=now.tzinfo,
                )
                delta = appointment - now
                # Напоминание «за сутки» не должно отправляться вместе с двухчасовым,
                # если запись создали незадолго до визита.
                lower_bound = timedelta(hours=2) if hours == 24 else timedelta(0)
                if lower_bound < delta <= timedelta(hours=hours):
                    result.append(row)
        return result

    def mark_reminder_sent(self, booking_id: int, hours: int) -> None:
        if hours not in {2, 24}:
            raise ValueError("Поддерживаются напоминания за 2 и 24 часа.")
        with self.connect() as conn:
            conn.execute(f"UPDATE bookings SET reminder_{hours}_sent = 1 WHERE id = ?", (booking_id,))

    def notification_sent(self, notification_key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM sent_notifications WHERE notification_key = ?",
                (notification_key,),
            ).fetchone() is not None

    def mark_notification_sent(self, notification_key: str, sent_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_notifications(notification_key, sent_at) VALUES (?, ?)",
                (notification_key, sent_at.isoformat()),
            )
