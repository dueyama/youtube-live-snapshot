import contextlib
import io
import unittest
from unittest.mock import patch

from ytlive_snapshot import cli


class UnifiedCliTest(unittest.TestCase):
    def test_help_names_both_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(["--help"])
        self.assertEqual(result, 0)
        self.assertIn("capture", output.getvalue())
        self.assertIn("render", output.getvalue())

    def test_capture_dispatches_remaining_arguments(self):
        with patch("ytlive_snapshot.capture.main", return_value=None) as capture_main:
            result = cli.main(["capture", "--once"])
        self.assertEqual(result, 0)
        capture_main.assert_called_once_with(
            ["--once"],
            prog="ytlive-snapshot capture",
        )

    def test_render_dispatches_remaining_arguments(self):
        with patch("ytlive_snapshot.render.main", return_value=None) as render_main:
            result = cli.main(["render", "--year", "2026"])
        self.assertEqual(result, 0)
        render_main.assert_called_once_with(
            ["--year", "2026"],
            prog="ytlive-snapshot render",
        )

    def test_unknown_command_returns_usage_error(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = cli.main(["record"])
        self.assertEqual(result, 2)
        self.assertIn("unknown command", error.getvalue())


if __name__ == "__main__":
    unittest.main()
