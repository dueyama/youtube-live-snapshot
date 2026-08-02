import copy
import contextlib
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pytz import timezone
from selenium.common.exceptions import WebDriverException

from ytlive_snapshot import ai_validation, capture


class RejectedCaptureArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tzinfo = timezone("Asia/Tokyo")
        self.started_at = self.tzinfo.localize(
            dt.datetime(2026, 8, 2, 18, 36, 59)
        )
        self.captured_at = self.started_at + dt.timedelta(seconds=4)
        self.finished_at = self.captured_at + dt.timedelta(seconds=2)
        self.checks = {
            "live_edge": {"status": "pass", "within_tolerance": True},
            "source_resolution": {
                "status": "pass",
                "width": 1280,
                "height": 720,
            },
            "image_quality": {"status": "pass"},
            "ai_validation": {
                "status": "fail",
                "model": "gpt-5.6-luna",
                "decision": "fail",
                "confidence": 0.99,
                "timestamp_status": "stale",
                "issues": ["表示時刻が古い"],
                "summary": "表示時刻が予定時刻と一致しません。",
            },
        }

    def _archive(self, output_dir: Path, image_path=None, **overrides):
        arguments = {
            "output_dir": output_dir,
            "image_path": image_path,
            "intended_filename": "sunset_20260802_1836.png",
            "series": "sunset",
            "attempt_number": 1,
            "max_attempts": 3,
            "attempt_started_at": self.started_at,
            "captured_at": self.captured_at if image_path else None,
            "finished_at": self.finished_at,
            "outcome": "rejected",
            "stage": "ai_validation",
            "reason_code": "ai_timestamp_stale",
            "reason": "AI-observed timestamp is stale",
            "checks": self.checks,
            "capture_method": "canvas" if image_path else None,
            "retry_planned": True,
        }
        arguments.update(overrides)
        return capture._archive_failed_capture_attempt(**arguments)

    def _run_mocked_capture(self, output_dir: Path, ai_results, *, ai_enabled=True):
        config = copy.deepcopy(capture.DEFAULT_CONFIG)
        config["embed_url"] = "https://www.youtube.com/embed/example"
        config["capture"]["output_dir"] = str(output_dir)
        config["capture"]["max_retries"] = 3
        config["capture"]["retry_delay_sec"] = 0
        config["capture"]["ai_validation"]["enabled"] = ai_enabled
        config["capture"]["ai_validation"]["mode"] = "enforce"

        class SwitchTo:
            def frame(self, element):
                return None

            def default_content(self):
                return None

        class Driver:
            page_source = ""

            def __init__(self):
                self.switch_to = SwitchTo()

            def set_page_load_timeout(self, timeout):
                return None

            def get(self, url):
                return None

            def quit(self):
                return None

        class Element:
            size = {"width": 1280, "height": 720}

        class ImmediateWait:
            def __init__(self, driver, timeout, poll_frequency=None):
                pass

            def until(self, predicate):
                return Element()

        class Server:
            def shutdown(self):
                return None

            def server_close(self):
                return None

        def save_frame(driver, path, **kwargs):
            image = Image.new("RGB", (640, 360), (20, 45, 70))
            for x in range(120, 520):
                for y in range(100, 260):
                    image.putpixel((x, y), (210, 225, 235))
            image.save(path)
            return True

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(capture, "_build_chrome_service", return_value=object())
            )
            stack.enter_context(
                patch.object(capture.webdriver, "Chrome", side_effect=lambda **kwargs: Driver())
            )
            stack.enter_context(patch.object(capture, "WebDriverWait", ImmediateWait))
            stack.enter_context(
                patch.object(
                    capture,
                    "_start_local_html_server",
                    side_effect=lambda html: (Server(), "http://127.0.0.1/example"),
                )
            )
            stack.enter_context(
                patch.object(capture, "_try_play_youtube_video", return_value={"paused": False})
            )
            stack.enter_context(
                patch.object(
                    capture,
                    "_seek_youtube_live_edge",
                    return_value={
                        "hasVideo": True,
                        "isLive": True,
                        "liveControlPresent": True,
                        "liveControlKind": "modern-time-display",
                        "liveControlWithinTolerance": True,
                        "seeked": False,
                    },
                )
            )
            stack.enter_context(patch.object(capture, "_hide_youtube_player_ui"))
            stack.enter_context(
                patch.object(
                    capture,
                    "_wait_for_youtube_video_frame",
                    return_value={
                        "ready": True,
                        "videoWidth": 1280,
                        "videoHeight": 720,
                        "paused": False,
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    capture,
                    "_save_video_canvas_screenshot",
                    side_effect=save_frame,
                )
            )
            inspector = stack.enter_context(
                patch.object(
                    ai_validation,
                    "inspect_capture",
                    side_effect=ai_results,
                )
            )
            stack.enter_context(patch.object(capture.time, "sleep"))
            stack.enter_context(
                self.assertLogs("ytlive_snapshot.capture", level="INFO")
            )
            result = capture.capture_embed_video(
                config,
                file_prefix="video_",
                is_test=True,
            )
        return result, inspector

    def test_archives_failed_image_and_json_outside_official_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()
            attempt_path = output_dir / ".attempt.png"
            Image.new("RGB", (640, 360), (30, 80, 120)).save(attempt_path)

            bundle = self._archive(output_dir, attempt_path)

            self.assertFalse(attempt_path.exists())
            self.assertTrue((bundle / "capture.png").is_file())
            self.assertTrue((bundle / "metadata.json").is_file())
            self.assertEqual(
                list(output_dir.glob("sunset_*.png")),
                [],
            )
            metadata = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["outcome"], "rejected")
            self.assertEqual(metadata["attempt_number"], 1)
            self.assertEqual(metadata["max_attempts"], 3)
            self.assertTrue(metadata["retry_planned"])
            self.assertEqual(metadata["image"]["width"], 640)
            self.assertEqual(metadata["image"]["height"], 360)
            self.assertEqual(len(metadata["image"]["sha256"]), 64)
            self.assertNotIn("summary", metadata["checks"]["ai_validation"])
            self.assertNotIn("issues", metadata["checks"]["ai_validation"])
            self.assertFalse((bundle / ".metadata.json.tmp").exists())

    def test_image_before_failure_is_optional_but_json_is_always_written(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()

            bundle = self._archive(
                output_dir,
                outcome="capture_error",
                stage="browser",
                reason_code="browser_timeout",
                reason="Browser failed before a frame existed",
                checks={
                    "live_edge": {"status": "not_run"},
                    "source_resolution": {"status": "not_run"},
                    "image_quality": {"status": "not_run"},
                    "ai_validation": {"status": "not_run"},
                },
                retry_planned=False,
            )

            metadata = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(metadata["image"])
            self.assertIsNone(metadata["captured_at"])
            self.assertEqual(metadata["outcome"], "capture_error")
            self.assertFalse((bundle / "capture.png").exists())

    def test_repeated_attempt_identifiers_never_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()

            first = self._archive(output_dir)
            second = self._archive(output_dir)

            self.assertNotEqual(first, second)
            self.assertTrue((first / "metadata.json").is_file())
            self.assertTrue((second / "metadata.json").is_file())

    def test_metadata_redacts_urls_keys_and_host_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()
            secret_reason = (
                "failed at https://example.invalid/private?id=abc "
                "OPENAI_API_KEY=sk-proj-secret "
                "CUSTOM_OPENAI_KEY=opaque-secret "
                "from /home/private-user/app/config.yaml and /srv/private/config "
                "or C:\\Users\\private-user\\config.yaml"
            )

            bundle = self._archive(output_dir, reason=secret_reason)
            raw_metadata = (bundle / "metadata.json").read_text(encoding="utf-8")

            self.assertNotIn("example.invalid", raw_metadata)
            self.assertNotIn("sk-proj-secret", raw_metadata)
            self.assertNotIn("opaque-secret", raw_metadata)
            self.assertNotIn("private-user", raw_metadata)
            self.assertIn("[redacted-url]", raw_metadata)
            self.assertIn("[redacted-key]", raw_metadata)
            self.assertIn("[redacted-path]", raw_metadata)

    def test_rejected_images_are_not_counted_or_pruned_as_official_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()
            first = output_dir / "sunset_20260801_1837.png"
            second = output_dir / "sunset_20260802_1836.png"
            Image.new("RGB", (640, 360), (10, 20, 30)).save(first)
            Image.new("RGB", (640, 360), (20, 30, 40)).save(second)
            rejected = output_dir / "rejected" / "2026-08-02" / "bundle"
            rejected.mkdir(parents=True)
            rejected_image = rejected / "sunset_20260802_1836.png"
            Image.new("RGB", (640, 360), (30, 40, 50)).save(rejected_image)

            capture._prune_old_files(output_dir, "sunset_", 1, None)

            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(rejected_image.exists())

    def test_all_capture_errors_share_one_total_attempt_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            config = copy.deepcopy(capture.DEFAULT_CONFIG)
            config["embed_url"] = "https://www.youtube.com/embed/example"
            config["capture"]["output_dir"] = str(output_dir)
            config["capture"]["max_retries"] = 3
            config["capture"]["retry_delay_sec"] = 0

            def fail_browser(*args, **kwargs):
                raise WebDriverException("browser unavailable")

            with patch.object(capture, "_build_chrome_service", return_value=object()):
                with patch.object(
                    capture.webdriver,
                    "Chrome",
                    side_effect=fail_browser,
                ) as chrome:
                    with self.assertLogs(
                        "ytlive_snapshot.capture",
                        level="WARNING",
                    ):
                        result = capture.capture_embed_video(
                            config,
                            file_prefix="video_",
                            is_test=True,
                        )

            self.assertFalse(result)
            self.assertEqual(chrome.call_count, 3)
            metadata_paths = sorted(
                output_dir.glob("rejected/*/*/metadata.json")
            )
            self.assertEqual(len(metadata_paths), 3)
            records = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in metadata_paths
            ]
            self.assertEqual(
                sorted(record["attempt_number"] for record in records),
                [1, 2, 3],
            )
            self.assertEqual(
                sorted(record["retry_planned"] for record in records),
                [False, True, True],
            )
            self.assertTrue(
                all(record["outcome"] == "capture_error" for record in records)
            )
            self.assertEqual(list(output_dir.glob("video_*.png")), [])
            self.assertEqual(
                list(output_dir.glob("rejected/*/*/capture.png")),
                [],
            )

    def test_ai_failure_is_archived_then_a_later_pass_is_published(self):
        failed = ai_validation.AIInspection(
            decision="fail",
            timestamp_status="stale",
            observed_timestamp="2026-08-02T17:36:00+09:00",
            timestamp_delta_seconds=-3600,
            confidence=0.99,
            issues=("Timestamp is stale.",),
            summary="The visible timestamp is stale.",
        )
        passed = ai_validation.AIInspection(
            decision="pass",
            timestamp_status="matches",
            observed_timestamp="2026-08-02T18:36:30+09:00",
            timestamp_delta_seconds=-29,
            confidence=0.98,
            issues=(),
            summary="The frame is current and unobstructed.",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            result, inspector = self._run_mocked_capture(
                output_dir,
                [failed, passed],
            )

            self.assertTrue(result)
            self.assertEqual(inspector.call_count, 2)
            self.assertEqual(len(list(output_dir.glob("video_*.png"))), 1)
            metadata_paths = list(output_dir.glob("rejected/*/*/metadata.json"))
            self.assertEqual(len(metadata_paths), 1)
            metadata = json.loads(
                metadata_paths[0].read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["outcome"], "rejected")
            self.assertEqual(metadata["stage"], "ai_validation")
            self.assertEqual(
                metadata["checks"]["ai_validation"]["decision"],
                "fail",
            )
            self.assertTrue(metadata["retry_planned"])
            self.assertTrue(
                (metadata_paths[0].parent / "capture.png").is_file()
            )

    def test_ai_cannot_create_an_independent_unbounded_retry_loop(self):
        uncertain = ai_validation.AIInspection(
            decision="uncertain",
            timestamp_status="unreadable",
            observed_timestamp=None,
            timestamp_delta_seconds=None,
            confidence=0.20,
            issues=("Large controls may obscure the frame.",),
            summary="The frame cannot be verified.",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            result, inspector = self._run_mocked_capture(
                output_dir,
                [uncertain, uncertain, uncertain],
            )

            self.assertFalse(result)
            self.assertEqual(inspector.call_count, 3)
            self.assertEqual(list(output_dir.glob("video_*.png")), [])
            metadata_paths = list(output_dir.glob("rejected/*/*/metadata.json"))
            self.assertEqual(len(metadata_paths), 3)
            records = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in metadata_paths
            ]
            self.assertTrue(
                all(record["outcome"] == "unverified" for record in records)
            )
            self.assertEqual(
                sum(1 for record in records if record["retry_planned"]),
                2,
            )

    def test_archive_failure_does_not_delete_the_only_failed_images(self):
        failed = ai_validation.AIInspection(
            decision="fail",
            timestamp_status="stale",
            observed_timestamp="2026-08-02T17:36:00+09:00",
            timestamp_delta_seconds=-3600,
            confidence=0.99,
            issues=("Timestamp is stale.",),
            summary="The visible timestamp is stale.",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            with patch.object(
                capture,
                "_archive_failed_capture_attempt",
                side_effect=OSError("archive unavailable"),
            ):
                result, inspector = self._run_mocked_capture(
                    output_dir,
                    [failed, failed, failed],
                )

            self.assertFalse(result)
            self.assertEqual(inspector.call_count, 3)
            self.assertEqual(list(output_dir.glob("video_*.png")), [])
            preserved_attempts = list(output_dir.glob(".*.attempt-*.png"))
            self.assertEqual(len(preserved_attempts), 3)
            self.assertTrue(
                all(path.stat().st_mode & 0o777 == 0o600 for path in preserved_attempts)
            )

    def test_ai_disabled_success_publishes_without_calling_inspector(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            result, inspector = self._run_mocked_capture(
                output_dir,
                [],
                ai_enabled=False,
            )

            self.assertTrue(result)
            self.assertEqual(inspector.call_count, 0)
            self.assertEqual(len(list(output_dir.glob("video_*.png"))), 1)
            self.assertFalse((output_dir / "rejected").exists())

    def test_official_publication_never_overwrites_an_existing_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            first_attempt = output_dir / ".first.png"
            second_attempt = output_dir / ".second.png"
            target = output_dir / "video_20260802_183659.png"
            first_attempt.write_bytes(b"first")
            second_attempt.write_bytes(b"second")

            self.assertTrue(
                capture._publish_capture_without_overwrite(first_attempt, target)
            )
            self.assertFalse(
                capture._publish_capture_without_overwrite(second_attempt, target)
            )

            self.assertEqual(target.read_bytes(), b"first")
            self.assertFalse(first_attempt.exists())
            self.assertTrue(second_attempt.exists())

    def test_publication_collision_is_archived_and_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            with patch.object(
                capture,
                "_publish_capture_without_overwrite",
                return_value=False,
            ) as publish:
                result, inspector = self._run_mocked_capture(
                    output_dir,
                    [],
                    ai_enabled=False,
                )

            self.assertFalse(result)
            self.assertEqual(inspector.call_count, 0)
            self.assertEqual(publish.call_count, 3)
            self.assertEqual(list(output_dir.glob("video_*.png")), [])
            metadata_paths = list(output_dir.glob("rejected/*/*/metadata.json"))
            self.assertEqual(len(metadata_paths), 3)
            records = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in metadata_paths
            ]
            self.assertTrue(
                all(record["outcome"] == "capture_error" for record in records)
            )
            self.assertTrue(
                all(record["stage"] == "publication" for record in records)
            )
            self.assertEqual(
                len(list(output_dir.glob("rejected/*/*/capture.png"))),
                3,
            )

    def test_rejected_archive_refuses_symbolic_link_inputs_and_destinations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()
            real_image = output_dir / "real.png"
            Image.new("RGB", (640, 360), (10, 20, 30)).save(real_image)
            linked_image = output_dir / "linked.png"
            linked_image.symlink_to(real_image)

            with self.assertRaisesRegex(OSError, "symbolic-link capture"):
                self._archive(output_dir, linked_image)

            external_dir = Path(temp_dir) / "external"
            external_dir.mkdir()
            (output_dir / "rejected").symlink_to(external_dir, target_is_directory=True)
            with self.assertRaisesRegex(OSError, "symbolic-link rejected"):
                self._archive(output_dir)

    def test_rejected_bundle_files_are_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "captures"
            output_dir.mkdir()
            attempt_path = output_dir / ".attempt.png"
            Image.new("RGB", (640, 360), (30, 80, 120)).save(attempt_path)

            bundle = self._archive(output_dir, attempt_path)

            self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (bundle / "metadata.json").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                (bundle / "capture.png").stat().st_mode & 0o777,
                0o600,
            )


if __name__ == "__main__":
    unittest.main()
