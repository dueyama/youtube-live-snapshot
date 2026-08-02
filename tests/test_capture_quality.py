import inspect
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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
