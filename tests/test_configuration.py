import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ytlive_snapshot import capture


class ConfigurationTest(unittest.TestCase):
    def test_private_values_have_no_source_defaults(self):
        self.assertIsNone(capture.DEFAULT_CONFIG["embed_url"])
        self.assertIsNone(capture.DEFAULT_CONFIG["schedule"]["latitude"])
        self.assertIsNone(capture.DEFAULT_CONFIG["schedule"]["longitude"])
        self.assertFalse(
            capture.DEFAULT_CONFIG["capture"]["ai_validation"]["enabled"]
        )
        self.assertEqual(
            capture.DEFAULT_CONFIG["capture"]["ai_validation"]["mode"],
            "enforce",
        )
        self.assertEqual(
            capture.DEFAULT_CONFIG["capture"]["ai_validation"][
                "timestamp_tolerance_sec"
            ],
            360.0,
        )
        self.assertEqual(
            capture.DEFAULT_CONFIG["capture"]["source_resolution"],
            {
                "minimum_width": 640,
                "minimum_height": 360,
                "preferred_width": 1280,
                "preferred_height": 720,
                "wait_sec": 30,
            },
        )

    def test_environment_overrides_private_values(self):
        config = copy.deepcopy(capture.DEFAULT_CONFIG)
        capture._apply_environment_overrides(
            config,
            {
                "YTLIVE_SNAPSHOT_EMBED_URL": "https://www.youtube.com/watch?v=example",
                "YTLIVE_SNAPSHOT_LATITUDE": "35.0",
                "YTLIVE_SNAPSHOT_LONGITUDE": "135.0",
            },
        )

        self.assertEqual(
            config["embed_url"],
            "https://www.youtube.com/watch?v=example",
        )
        self.assertEqual(config["schedule"]["latitude"], 35.0)
        self.assertEqual(config["schedule"]["longitude"], 135.0)

    def test_new_environment_names_override_legacy_names(self):
        config = copy.deepcopy(capture.DEFAULT_CONFIG)
        capture._apply_environment_overrides(
            config,
            {
                "YTLIVE_SNAPSHOT_EMBED_URL": "https://www.youtube.com/watch?v=new",
                "CAPTUREPY_EMBED_URL": "https://www.youtube.com/watch?v=legacy",
            },
        )

        self.assertEqual(
            config["embed_url"],
            "https://www.youtube.com/watch?v=new",
        )

    def test_sunset_location_is_required_only_for_sunset_schedule(self):
        config = copy.deepcopy(capture.DEFAULT_CONFIG)
        config["embed_url"] = "https://www.youtube.com/watch?v=example"

        capture._validate_runtime_config(
            config,
            require_sunset_location=False,
        )
        with self.assertRaisesRegex(ValueError, "latitude and longitude"):
            capture._validate_runtime_config(
                config,
                require_sunset_location=True,
            )

    def test_config_directory_is_used_when_capture_yaml_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "capture.yaml"
            config_path.write_text("embed_url: null\n", encoding="utf-8")

            resolved = capture._resolve_config_path(
                None,
                {"YTLIVE_SNAPSHOT_CONFIG_DIR": temp_dir},
            )

        self.assertEqual(resolved, str(config_path))

    def test_cli_can_enable_ai_validation_without_changing_default(self):
        config = copy.deepcopy(capture.DEFAULT_CONFIG)
        args = SimpleNamespace(
            ai_validation_enabled=True,
            ai_validation_mode="advisory",
            ai_model="gpt-5.6-luna",
            ai_timestamp_tolerance_sec=180,
            ai_require_timestamp=True,
        )

        capture._apply_cli_overrides(config, args)

        ai_config = config["capture"]["ai_validation"]
        self.assertTrue(ai_config["enabled"])
        self.assertEqual(ai_config["mode"], "advisory")
        self.assertEqual(ai_config["model"], "gpt-5.6-luna")
        self.assertEqual(ai_config["timestamp_tolerance_sec"], 180)
        self.assertTrue(ai_config["require_timestamp"])

    def test_old_config_without_resolution_section_uses_safe_defaults(self):
        settings = capture._source_resolution_settings({})

        self.assertEqual(settings["minimum_width"], 640)
        self.assertEqual(settings["minimum_height"], 360)
        self.assertEqual(settings["preferred_width"], 1280)
        self.assertEqual(settings["preferred_height"], 720)
        self.assertEqual(settings["wait_sec"], 30)

    def test_source_resolution_values_must_be_positive_integers(self):
        with self.assertRaisesRegex(ValueError, "minimum_width"):
            capture._source_resolution_settings(
                {"source_resolution": {"minimum_width": 0}}
            )
        with self.assertRaisesRegex(ValueError, "preferred_height"):
            capture._source_resolution_settings(
                {"source_resolution": {"preferred_height": 720.5}}
            )

    def test_source_resolution_wait_must_be_finite(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    capture._source_resolution_settings(
                        {"source_resolution": {"wait_sec": value}}
                    )

    def test_preferred_source_resolution_must_cover_minimum(self):
        with self.assertRaisesRegex(ValueError, "preferred_width"):
            capture._source_resolution_settings(
                {
                    "source_resolution": {
                        "minimum_width": 1280,
                        "preferred_width": 640,
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
