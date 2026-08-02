import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pytz import timezone

from ytlive_snapshot import ai_validation, capture


class FakeResponses:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return type("Response", (), {"output_text": json.dumps(self.payload)})()


class FakeClient:
    def __init__(self, payload):
        self.responses = FakeResponses(payload)


def passing_payload(timestamp="2026-08-01T17:31:39+09:00"):
    return {
        "decision": "pass",
        "timestamp_status": "matches",
        "observed_timestamp": timestamp,
        "confidence": 0.98,
        "issues": [],
        "summary": "Clear live video frame with a current camera timestamp.",
    }


class AIInspectionTest(unittest.TestCase):
    def setUp(self):
        self.expected = timezone("Asia/Tokyo").localize(
            dt.datetime(2026, 8, 1, 17, 32, 24)
        )
        self.config = {
            "enabled": True,
            "model": "gpt-5.6-luna",
            "mode": "enforce",
            "detail": "original",
            "api_key_env": "OPENAI_API_KEY",
            "timeout_sec": 45,
            "timestamp_tolerance_sec": 120,
            "require_timestamp": True,
            "max_output_tokens": 400,
        }

    def _image(self, directory):
        path = Path(directory) / "capture.png"
        Image.new("RGB", (640, 360), (40, 80, 120)).save(path)
        return path

    def test_uses_luna_image_input_without_storing_response(self):
        client = FakeClient(passing_payload())
        factory_args = {}

        def factory(**kwargs):
            factory_args.update(kwargs)
            return client

        with tempfile.TemporaryDirectory() as temp_dir:
            result = ai_validation.inspect_capture(
                self._image(temp_dir),
                captured_at=self.expected,
                config=self.config,
                environ={"OPENAI_API_KEY": "test-secret"},
                client_factory=factory,
            )

        self.assertEqual(result.timestamp_status, "matches")
        self.assertEqual(result.timestamp_delta_seconds, -45)
        self.assertIsNone(result.rejection_reason(require_timestamp=True))
        self.assertEqual(factory_args["api_key"], "test-secret")
        self.assertEqual(factory_args["max_retries"], 0)
        request = client.responses.kwargs
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertFalse(request["store"])
        self.assertEqual(request["input"][0]["content"][1]["detail"], "original")
        self.assertTrue(
            request["input"][0]["content"][1]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        prompt = request["input"][0]["content"][0]["text"]
        self.assertIn("any language", prompt)
        self.assertIn("day/month order", prompt)

    def test_local_timestamp_math_overrides_incorrect_model_status(self):
        payload = passing_payload("2026-08-01T16:31:39+09:00")
        client = FakeClient(payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ai_validation.inspect_capture(
                self._image(temp_dir),
                captured_at=self.expected,
                config=self.config,
                environ={"OPENAI_API_KEY": "test-secret"},
                client_factory=lambda **kwargs: client,
            )

        self.assertEqual(result.timestamp_status, "stale")
        self.assertLess(result.timestamp_delta_seconds, -3600)
        self.assertIn(
            "stale",
            result.rejection_reason(require_timestamp=True),
        )

    def test_null_observed_timestamp_cannot_claim_a_match(self):
        client = FakeClient(passing_payload(None))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ai_validation.inspect_capture(
                self._image(temp_dir),
                captured_at=self.expected,
                config=self.config,
                environ={"OPENAI_API_KEY": "test-secret"},
                client_factory=lambda **kwargs: client,
            )

        self.assertEqual(result.timestamp_status, "unreadable")
        self.assertIsNone(result.observed_timestamp)
        self.assertIsNone(result.timestamp_delta_seconds)
        self.assertIn(
            "could not verify",
            result.rejection_reason(require_timestamp=True),
        )

    def test_enabled_validation_requires_key(self):
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            ai_validation.validate_config(self.config, environ={})

    def test_disabled_validation_needs_no_key(self):
        ai_validation.validate_config({"enabled": False}, environ={})

    def test_ai_timeouts_and_timestamp_tolerances_must_be_finite(self):
        for field in ("timeout_sec", "timestamp_tolerance_sec"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    config = dict(self.config)
                    config[field] = value
                    with self.assertRaisesRegex(ValueError, "finite"):
                        ai_validation.validate_config(
                            config,
                            environ={"OPENAI_API_KEY": "test-secret"},
                        )

    def test_provider_error_body_is_not_propagated(self):
        class FailingResponses:
            def create(self, **kwargs):
                raise RuntimeError("provider-body-with-private-token")

        client = type("Client", (), {"responses": FailingResponses()})()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ai_validation.AIValidationError) as raised:
                ai_validation.inspect_capture(
                    self._image(temp_dir),
                    captured_at=self.expected,
                    config=self.config,
                    environ={"OPENAI_API_KEY": "test-secret"},
                    client_factory=lambda **kwargs: client,
                )

        self.assertNotIn("private-token", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


class CaptureAIValidationModeTest(unittest.TestCase):
    def setUp(self):
        self.path = Path("capture.png")
        self.captured_at = timezone("Asia/Tokyo").localize(
            dt.datetime(2026, 8, 1, 17, 32, 24)
        )
        self.failure = ai_validation.AIInspection(
            decision="fail",
            timestamp_status="stale",
            observed_timestamp="2026-08-01T16:31:39+09:00",
            timestamp_delta_seconds=-3645,
            confidence=0.99,
            issues=("Timestamp is stale.",),
            summary="The visible timestamp is about one hour old.",
        )

    def test_legacy_advisory_mode_is_an_enforcing_alias(self):
        with self.assertLogs("ytlive_snapshot.capture", level="WARNING") as logs:
            with self.assertRaises(capture.CaptureValidationError) as raised:
                capture._apply_optional_ai_validation(
                    self.path,
                    captured_at=self.captured_at,
                    config={"enabled": True, "mode": "advisory"},
                    inspector=lambda *args, **kwargs: self.failure,
                )

        self.assertEqual(raised.exception.outcome, "rejected")
        self.assertIn("deprecated", "\n".join(logs.output))

    def test_enforce_mode_rejects_failed_capture(self):
        with self.assertRaisesRegex(capture.CaptureValidationError, "stale") as raised:
            capture._apply_optional_ai_validation(
                self.path,
                captured_at=self.captured_at,
                config={
                    "enabled": True,
                    "mode": "enforce",
                    "require_timestamp": True,
                },
                inspector=lambda *args, **kwargs: self.failure,
            )
        self.assertEqual(raised.exception.outcome, "rejected")
        self.assertEqual(raised.exception.reason_code, "ai_timestamp_stale")
        self.assertEqual(
            raised.exception.details["ai_validation"]["decision"],
            "fail",
        )

    def test_enforce_mode_rejects_api_failure(self):
        def unavailable(*args, **kwargs):
            raise ai_validation.AIValidationError("service unavailable")

        with self.assertRaisesRegex(
            capture.CaptureValidationError,
            "could not complete",
        ) as raised:
            capture._apply_optional_ai_validation(
                self.path,
                captured_at=self.captured_at,
                config={"enabled": True, "mode": "enforce"},
                inspector=unavailable,
            )
        self.assertEqual(raised.exception.outcome, "unverified")
        self.assertEqual(
            raised.exception.reason_code,
            "ai_validation_unavailable",
        )

    def test_uncertain_ai_result_is_unverified(self):
        uncertain = ai_validation.AIInspection(
            decision="uncertain",
            timestamp_status="unreadable",
            observed_timestamp=None,
            timestamp_delta_seconds=None,
            confidence=0.25,
            issues=("Controls may obscure the frame.",),
            summary="The frame cannot be judged reliably.",
        )

        with self.assertRaises(capture.CaptureValidationError) as raised:
            capture._apply_optional_ai_validation(
                self.path,
                captured_at=self.captured_at,
                config={"enabled": True, "mode": "enforce"},
                inspector=lambda *args, **kwargs: uncertain,
            )

        self.assertEqual(raised.exception.outcome, "unverified")
        self.assertEqual(
            raised.exception.reason_code,
            "ai_decision_uncertain",
        )

    def test_clear_ai_failure_remains_rejected_when_timestamp_is_unreadable(self):
        failed = ai_validation.AIInspection(
            decision="fail",
            timestamp_status="unreadable",
            observed_timestamp=None,
            timestamp_delta_seconds=None,
            confidence=0.90,
            issues=("Playback controls obscure the frame.",),
            summary="The image is obstructed.",
        )

        with self.assertRaises(capture.CaptureValidationError) as raised:
            capture._apply_optional_ai_validation(
                self.path,
                captured_at=self.captured_at,
                config={
                    "enabled": True,
                    "mode": "enforce",
                    "require_timestamp": True,
                },
                inspector=lambda *args, **kwargs: failed,
            )

        self.assertEqual(raised.exception.outcome, "rejected")
        self.assertEqual(raised.exception.reason_code, "ai_frame_rejected")

    def test_pass_is_returned_for_publication_metadata(self):
        passed = ai_validation.AIInspection(
            decision="pass",
            timestamp_status="matches",
            observed_timestamp="2026-08-01T17:32:00+09:00",
            timestamp_delta_seconds=-24,
            confidence=0.97,
            issues=(),
            summary="Valid current frame.",
        )

        result = capture._apply_optional_ai_validation(
            self.path,
            captured_at=self.captured_at,
            config={"enabled": True, "mode": "enforce"},
            inspector=lambda *args, **kwargs: passed,
        )

        self.assertIs(result, passed)


if __name__ == "__main__":
    unittest.main()
