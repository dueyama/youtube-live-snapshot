import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from selenium.common.exceptions import TimeoutException

from ytlive_snapshot import capture


class LiveEdgeValidationTest(unittest.TestCase):
    def test_accepts_youtube_live_badge_at_edge_even_with_ambiguous_duration(self):
        status = {
            "hasVideo": True,
            "isLive": True,
            "liveControlWithinTolerance": True,
            "playerLagSec": 3600,
        }
        self.assertIsNone(capture._live_edge_validation_error(status, 30))

    def test_rejects_youtube_live_badge_when_playback_is_behind(self):
        status = {
            "hasVideo": True,
            "isLive": True,
            "afterLiveControlWithinTolerance": False,
            "afterLiveControlLagSec": 45,
            "afterLagSec": 2,
        }
        self.assertIn(
            "delayed playback",
            capture._live_edge_validation_error(status, 30),
        )

    def test_accepts_live_stream_without_control_and_ignores_ambiguous_timing(self):
        status = {
            "hasVideo": True,
            "isLive": True,
            "liveControlPresent": False,
            "lagSec": 3600,
            "playerLagSec": 3600,
        }
        self.assertIsNone(capture._live_edge_validation_error(status, 30))

    def test_rejects_unverified_live_edge_by_default(self):
        status = {"hasVideo": True, "isLive": None}
        self.assertIn(
            "could not be verified",
            capture._live_edge_validation_error(status, 30),
        )

    def test_can_allow_unverified_live_edge_explicitly(self):
        status = {"hasVideo": True, "isLive": None}
        self.assertIsNone(
            capture._live_edge_validation_error(
                status,
                30,
                require_verification=False,
            )
        )


class CaptureImageValidationTest(unittest.TestCase):
    def _write(self, path: Path, color, accent=None):
        image = Image.new("RGB", (640, 360), color)
        if accent is not None:
            for x in range(80, 560):
                for y in range(120, 240):
                    image.putpixel((x, y), accent)
        image.save(path, "PNG")

    def test_rejects_black_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "black.png"
            self._write(path, (0, 0, 0))
            self.assertIn(
                "effectively black",
                capture._capture_image_validation_error(path),
            )

    def test_rejects_uniform_error_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "uniform.png"
            self._write(path, (42, 42, 42))
            self.assertIn(
                "nearly uniform",
                capture._capture_image_validation_error(path),
            )

    def test_accepts_detailed_video_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "frame.png"
            self._write(path, (25, 45, 65), accent=(210, 225, 235))
            self.assertIsNone(capture._capture_image_validation_error(path))


class SourceResolutionTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "minimum_width": 640,
            "minimum_height": 360,
            "preferred_width": 1280,
            "preferred_height": 720,
            "wait_sec": 30,
        }

    def test_classifies_preferred_minimum_and_low_source_frames(self):
        self.assertEqual(
            capture._source_resolution_result(
                {"ready": True, "videoWidth": 1280, "videoHeight": 720},
                self.settings,
            ),
            "preferred",
        )
        self.assertEqual(
            capture._source_resolution_result(
                {"ready": True, "videoWidth": 854, "videoHeight": 480},
                self.settings,
            ),
            "minimum",
        )
        self.assertEqual(
            capture._source_resolution_result(
                {"ready": True, "videoWidth": 640, "videoHeight": 360},
                self.settings,
            ),
            "minimum",
        )
        self.assertEqual(
            capture._source_resolution_result(
                {"ready": True, "videoWidth": 426, "videoHeight": 240},
                self.settings,
            ),
            "below_minimum",
        )

    def test_low_source_frame_reports_observed_and_minimum_resolution(self):
        error = capture._source_resolution_validation_error(
            {"ready": True, "videoWidth": 426, "videoHeight": 240},
            self.settings,
        )
        self.assertIn("observed=426x240", error)
        self.assertIn("minimum=640x360", error)

    def test_waits_through_lower_resolutions_until_preferred_is_ready(self):
        statuses = [
            {"ready": True, "videoWidth": 426, "videoHeight": 240},
            {"ready": True, "videoWidth": 640, "videoHeight": 360},
            {"ready": True, "videoWidth": 1280, "videoHeight": 720},
        ]

        class PollingWait:
            def __init__(self, driver, timeout, poll_frequency):
                self.driver = driver

            def until(self, predicate):
                for _ in range(3):
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("preferred resolution was not detected")

        with patch.object(capture, "WebDriverWait", PollingWait):
            with patch.object(
                capture,
                "_video_frame_status",
                side_effect=statuses,
            ):
                status = capture._wait_for_youtube_video_frame(
                    object(),
                    30,
                    preferred_width=1280,
                    preferred_height=720,
                )

        self.assertEqual(status["videoWidth"], 1280)
        self.assertEqual(status["videoHeight"], 720)

    def test_accepts_current_minimum_resolution_when_preferred_wait_expires(self):
        class TimeoutWait:
            def __init__(self, driver, timeout, poll_frequency):
                pass

            def until(self, predicate):
                raise TimeoutException

        with patch.object(capture, "WebDriverWait", TimeoutWait):
            with patch.object(
                capture,
                "_video_frame_status",
                return_value={
                    "ready": True,
                    "videoWidth": 640,
                    "videoHeight": 360,
                },
            ):
                status = capture._wait_for_youtube_video_frame(
                    object(),
                    30,
                    preferred_width=1280,
                    preferred_height=720,
                )

        self.assertEqual(
            capture._source_resolution_result(status, self.settings),
            "minimum",
        )

    def test_canvas_rejects_resolution_drop_without_writing_or_falling_back(self):
        class Driver:
            def set_script_timeout(self, timeout):
                self.timeout = timeout

            def execute_async_script(self, script, *args):
                self.script = script
                self.args = args
                return {
                    "ok": False,
                    "reason": "video source resolution is below minimum",
                    "sourceWidth": 426,
                    "sourceHeight": 240,
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "frame.png"
            driver = Driver()
            with self.assertRaisesRegex(
                capture.CaptureValidationError,
                "observed=426x240",
            ):
                capture._save_video_canvas_screenshot(
                    driver,
                    path,
                    minimum_source_width=640,
                    minimum_source_height=360,
                )

            self.assertFalse(path.exists())
            self.assertEqual(driver.args, (640, 360))
            self.assertIn("sourceWidth < minimumSourceWidth", driver.script)

    def test_canvas_rejects_unready_frame_without_falling_back(self):
        class Driver:
            def set_script_timeout(self, timeout):
                pass

            def execute_async_script(self, script, *args):
                return {
                    "ok": False,
                    "reason": "video frame is not ready",
                    "readyState": 1,
                    "sourceWidth": 426,
                    "sourceHeight": 240,
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "frame.png"
            with self.assertRaisesRegex(
                capture.CaptureValidationError,
                "became unavailable",
            ):
                capture._save_video_canvas_screenshot(
                    Driver(),
                    path,
                    minimum_source_width=640,
                    minimum_source_height=360,
                )

            self.assertFalse(path.exists())


class PlayRequestTest(unittest.TestCase):
    def test_play_request_does_not_wait_for_async_promise(self):
        class Driver:
            def execute_script(self, script):
                self.script = script
                return {"hasVideo": True, "paused": False, "playRequested": True}

            def execute_async_script(self, script):
                raise AssertionError("play request must not wait asynchronously")

        driver = Driver()
        status = capture._try_play_youtube_video(driver)
        self.assertTrue(status["playRequested"])
        self.assertIn("video.play()", driver.script)


class LiveEdgeSeekScriptTest(unittest.TestCase):
    def test_only_uses_direct_live_control_and_never_seeks_from_timing(self):
        class Driver:
            def set_script_timeout(self, timeout):
                self.timeout = timeout

            def execute_async_script(self, script, *args):
                self.script = script
                self.args = args
                return {"hasVideo": True, "afterLagSec": 2}

        driver = Driver()
        status = capture._seek_youtube_live_edge(driver, 30)
        self.assertEqual(status["afterLagSec"], 2)
        self.assertEqual(driver.args, (30,))
        self.assertIn("before.liveControlWithinTolerance === true", driver.script)
        self.assertIn("action = before.liveControlKind", driver.script)
        self.assertIn("modernLiveControl.click", driver.script)
        self.assertIn("player.classList.contains('ytp-livebadge-color')", driver.script)
        self.assertIn("/[٠-٩]/g", driver.script)
        self.assertNotIn("/live|ライブ/i", driver.script)
        self.assertIn("timingLagIgnored: true", driver.script)
        self.assertIn("ambiguous timing lag ignored", driver.script)
        self.assertNotIn("action = 'player.seekTo'", driver.script)
        self.assertNotIn("video.currentTime =", driver.script)
        self.assertNotIn("player.seekToLiveHead()", driver.script)

    def test_capture_attempt_inspects_live_control_exactly_once(self):
        source = inspect.getsource(capture.capture_embed_video)
        self.assertEqual(source.count("_seek_youtube_live_edge("), 1)


if __name__ == "__main__":
    unittest.main()
