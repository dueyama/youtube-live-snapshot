import unittest

from ytlive_snapshot import render


def category(**overrides):
    value = {
        "id": "noon_sea",
        "prefix": "noon",
        "gravity": "south",
        "crop_offset": 170,
        "mode": "thumbnail",
        "output": "{year}_noon_sea_calendar.png",
    }
    value.update(overrides)
    return value


class CalendarConfigurationTest(unittest.TestCase):
    def test_rejects_category_id_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "category id"):
            render._load_categories_list(
                [category(id="../../outside")],
                "test",
            )

    def test_rejects_absolute_output_path(self):
        with self.assertRaisesRegex(ValueError, "output"):
            render._load_categories_list(
                [category(output="/tmp/calendar.png")],
                "test",
            )

    def test_accepts_safe_category(self):
        loaded = render._load_categories_list([category()], "test")
        self.assertEqual(loaded[0]["id"], "noon_sea")

    def test_accepts_custom_capture_prefix(self):
        loaded = render._load_categories_list(
            [category(id="morning_sky", prefix="morning")],
            "test",
        )
        self.assertEqual(loaded[0]["prefix"], "morning")

    def test_parses_custom_capture_filename(self):
        parsed = render.parse_filename("morning_20260801_0815.png")
        self.assertEqual(parsed, ("morning", 2026, 8, 1, 815))


if __name__ == "__main__":
    unittest.main()
