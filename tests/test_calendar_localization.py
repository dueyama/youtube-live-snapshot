import unittest

from ytlive_snapshot import render


class CalendarLocalizationTest(unittest.TestCase):
    def test_japanese_calendar_labels(self):
        labels = render._calendar_labels("ja", 2026, 8)

        self.assertEqual(labels["title"], "2026年8月")
        self.assertEqual(labels["weekdays"], ["日", "月", "火", "水", "木", "金", "土"])
        self.assertEqual(labels["placeholder"], "データなし")

    def test_english_calendar_labels(self):
        labels = render._calendar_labels("en", 2026, 8)

        self.assertEqual(labels["title"], "August 2026")
        self.assertEqual(
            labels["weekdays"],
            ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        )
        self.assertEqual(labels["placeholder"], "No data")

    def test_unknown_locale_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "locale must be"):
            render._calendar_labels("fr", 2026, 8)


if __name__ == "__main__":
    unittest.main()
