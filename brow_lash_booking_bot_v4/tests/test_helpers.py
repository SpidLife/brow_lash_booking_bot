from __future__ import annotations

import unittest
from datetime import date

from beauty_bot.bot import duration_label, normalize_phone, pretty_date


class HelperTests(unittest.TestCase):
    def test_normalize_phone(self) -> None:
        self.assertEqual(normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(normalize_phone("+31 6 12345678"), "+31612345678")
        self.assertIsNone(normalize_phone("123"))

    def test_labels(self) -> None:
        self.assertEqual(duration_label(90), "1 ч 30 мин")
        self.assertIn("января", pretty_date(date(2030, 1, 7)))


if __name__ == "__main__":
    unittest.main()
