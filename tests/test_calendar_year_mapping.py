import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ytlive_snapshot import render


class CalendarYearMappingTest(unittest.TestCase):
    def test_previous_year_photo_maps_to_output_year_month_and_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "noon_20250801_1200.png"
            source.touch()
            marker = object()

            with patch.object(
                render,
                "process_image_file_custom",
                return_value=marker,
            ):
                result = render.process_category_images(
                    "noon",
                    "south",
                    170,
                    2025,
                    2026,
                    "thumbnail",
                    temp_dir,
                    280,
                    280,
                )

        self.assertIs(result[(2026, 8)][1], marker)

    def test_leap_day_is_skipped_when_output_year_has_no_february_29(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "noon_20240229_1200.png"
            source.touch()

            with patch.object(
                render,
                "process_image_file_custom",
                return_value=object(),
            ):
                result = render.process_category_images(
                    "noon",
                    "south",
                    170,
                    2024,
                    2026,
                    "thumbnail",
                    temp_dir,
                    280,
                    280,
                )

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
