"""Optional OpenAI vision validation for captured video frames."""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_DETAIL = "original"
DEFAULT_MODE = "enforce"
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_TIMESTAMP_TOLERANCE_SEC = 360.0
DEFAULT_MAX_OUTPUT_TOKENS = 400

VALID_MODES = {"advisory", "enforce"}
VALID_DETAILS = {"low", "high", "original", "auto"}
VALID_DECISIONS = {"pass", "fail", "uncertain"}
VALID_TIMESTAMP_STATUSES = {
    "matches",
    "stale",
    "future",
    "not_visible",
    "unreadable",
}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AIValidationError(RuntimeError):
    """The optional AI validation request or response was unusable."""


@dataclass(frozen=True)
class AIInspection:
    """Normalized result returned by the optional image inspector."""

    decision: str
    timestamp_status: str
    observed_timestamp: Optional[str]
    timestamp_delta_seconds: Optional[float]
    confidence: float
    issues: Tuple[str, ...]
    summary: str

    def rejection_reason(self, *, require_timestamp: bool) -> Optional[str]:
        if self.decision != "pass":
            return (
                f"AI decision is {self.decision} "
                f"(timestamp_status={self.timestamp_status}): {self.summary}"
            )
        if self.timestamp_status in {"stale", "future"}:
            return (
                f"AI-observed timestamp is {self.timestamp_status}: "
                f"{self.observed_timestamp or 'unknown'}"
            )
        if require_timestamp and self.timestamp_status != "matches":
            return (
                "AI could not verify a current visible timestamp "
                f"(status={self.timestamp_status})"
            )
        return None


INSPECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": sorted(VALID_DECISIONS),
        },
        "timestamp_status": {
            "type": "string",
            "enum": sorted(VALID_TIMESTAMP_STATUSES),
        },
        "observed_timestamp": {
            "type": ["string", "null"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {
            "type": "string",
        },
    },
    "required": [
        "decision",
        "timestamp_status",
        "observed_timestamp",
        "confidence",
        "issues",
        "summary",
    ],
    "additionalProperties": False,
}


def validate_config(
    config: Mapping[str, Any],
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Validate the optional AI block without importing the OpenAI SDK."""
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("capture.ai_validation.enabled must be true or false")
    if not enabled:
        return

    mode = config.get("mode", DEFAULT_MODE)
    if mode not in VALID_MODES:
        raise ValueError(
            "capture.ai_validation.mode must be advisory or enforce"
        )
    detail = config.get("detail", DEFAULT_DETAIL)
    if detail not in VALID_DETAILS:
        raise ValueError(
            "capture.ai_validation.detail must be low, high, original, or auto"
        )
    model = config.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("capture.ai_validation.model must be a non-empty string")

    api_key_env = config.get("api_key_env", DEFAULT_API_KEY_ENV)
    if not isinstance(api_key_env, str) or not ENV_NAME_RE.fullmatch(api_key_env):
        raise ValueError(
            "capture.ai_validation.api_key_env must be a valid environment variable name"
        )
    env = os.environ if environ is None else environ
    if not env.get(api_key_env):
        raise ValueError(
            f"{api_key_env} is required when capture.ai_validation.enabled is true"
        )

    timeout_sec = float(config.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    if not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise ValueError(
            "capture.ai_validation.timeout_sec must be finite and greater than zero"
        )
    tolerance_sec = float(
        config.get(
            "timestamp_tolerance_sec",
            DEFAULT_TIMESTAMP_TOLERANCE_SEC,
        )
    )
    if not math.isfinite(tolerance_sec) or tolerance_sec < 0:
        raise ValueError(
            "capture.ai_validation.timestamp_tolerance_sec must be finite and zero or greater"
        )
    max_output_tokens = int(
        config.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    )
    if max_output_tokens <= 0:
        raise ValueError(
            "capture.ai_validation.max_output_tokens must be greater than zero"
        )
    if not isinstance(config.get("require_timestamp", False), bool):
        raise ValueError(
            "capture.ai_validation.require_timestamp must be true or false"
        )


def _create_openai_client(*, api_key: str, timeout: float, max_retries: int = 0):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised without optional extra
        raise AIValidationError(
            "OpenAI support is not installed; install youtube-live-snapshot[ai]"
        ) from exc
    # The capture command owns one global, bounded attempt budget. Disable the
    # SDK's independent HTTP retry loop so one capture attempt is one API call.
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)


def _image_data_url(path: Path) -> str:
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower())
    if mime is None:
        raise AIValidationError(f"unsupported AI image type: {path.suffix}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _parse_timestamp(
    value: Optional[str],
    expected: dt.datetime,
) -> Optional[dt.datetime]:
    if not value:
        return None
    normalized = value.strip().replace("/", "-")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        observed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=expected.tzinfo)
    return observed


def _normalize_inspection(
    payload: Mapping[str, Any],
    *,
    expected: dt.datetime,
    timestamp_tolerance_sec: float,
) -> AIInspection:
    decision = payload.get("decision")
    timestamp_status = payload.get("timestamp_status")
    if decision not in VALID_DECISIONS:
        raise AIValidationError(f"invalid AI decision: {decision!r}")
    if timestamp_status not in VALID_TIMESTAMP_STATUSES:
        raise AIValidationError(
            f"invalid AI timestamp status: {timestamp_status!r}"
        )

    observed_text = payload.get("observed_timestamp")
    if observed_text is not None and not isinstance(observed_text, str):
        raise AIValidationError("AI observed_timestamp must be a string or null")
    if isinstance(observed_text, str):
        observed_text = observed_text.strip() or None
    observed = _parse_timestamp(observed_text, expected)
    delta = None
    if observed_text is None:
        # A model-supplied `matches` label is not evidence without a timestamp
        # that can be checked locally. This keeps require_timestamp strict.
        if timestamp_status == "matches":
            timestamp_status = "unreadable"
    elif observed is None:
        timestamp_status = "unreadable"
    elif observed is not None:
        delta = (observed - expected).total_seconds()
        if delta < -timestamp_tolerance_sec:
            timestamp_status = "stale"
        elif delta > timestamp_tolerance_sec:
            timestamp_status = "future"
        else:
            timestamp_status = "matches"

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise AIValidationError("AI confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise AIValidationError("AI confidence must be between zero and one")

    issues = payload.get("issues")
    if not isinstance(issues, list) or not all(
        isinstance(issue, str) for issue in issues
    ):
        raise AIValidationError("AI issues must be a list of strings")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AIValidationError("AI summary must be a non-empty string")

    return AIInspection(
        decision=decision,
        timestamp_status=timestamp_status,
        observed_timestamp=observed_text,
        timestamp_delta_seconds=delta,
        confidence=confidence,
        issues=tuple(issues),
        summary=summary.strip(),
    )


def inspect_capture(
    image_path: Path,
    *,
    captured_at: dt.datetime,
    config: Mapping[str, Any],
    environ: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
) -> AIInspection:
    """Send one capture to the Responses API and normalize its verdict."""
    validate_config(config, environ)
    if not config.get("enabled", False):
        raise AIValidationError("AI validation is disabled")
    if captured_at.tzinfo is None:
        raise AIValidationError("captured_at must include a timezone")

    env = os.environ if environ is None else environ
    api_key_env = config.get("api_key_env", DEFAULT_API_KEY_ENV)
    api_key = env[api_key_env]
    timeout_sec = float(config.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    tolerance_sec = float(
        config.get(
            "timestamp_tolerance_sec",
            DEFAULT_TIMESTAMP_TOLERANCE_SEC,
        )
    )
    require_timestamp = config.get("require_timestamp", False)
    detail = config.get("detail", DEFAULT_DETAIL)
    model = config.get("model", DEFAULT_MODEL)
    max_output_tokens = int(
        config.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    )

    prompt = (
        "Inspect this still image captured from a live video stream. "
        f"The expected capture time is {captured_at.isoformat()}. "
        f"A visible camera timestamp may differ by at most {tolerance_sec:.0f} seconds. "
        "Check that the image contains a real, drawable video frame; is not blank, "
        "a loading or error screen, or obscured by large playback controls; and read "
        "any visible date-time overlay. Text and date formatting may use any language "
        "or locale; never fail an otherwise valid frame merely because of its language. "
        "Return an unambiguous visible timestamp as ISO 8601 using the expected timezone "
        "when the overlay has no timezone. If day/month order or another locale detail "
        "remains ambiguous, return observed_timestamp=null and timestamp_status=unreadable. "
        "Transcribe every timestamp digit exactly; if any digit is unclear, return "
        "observed_timestamp=null and timestamp_status=unreadable instead of guessing. "
        f"A visible current timestamp is {'required' if require_timestamp else 'helpful but not required'}. "
        "Use decision=fail for a clearly bad frame or visibly stale/future timestamp, "
        "decision=uncertain when the image cannot be judged, and decision=pass only "
        "for an acceptable capture. Keep the summary concise."
    )

    try:
        factory = _create_openai_client if client_factory is None else client_factory
        client = factory(api_key=api_key, timeout=timeout_sec, max_retries=0)
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": _image_data_url(image_path),
                            "detail": detail,
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "capture_inspection",
                    "strict": True,
                    "schema": INSPECTION_SCHEMA,
                }
            },
            max_output_tokens=max_output_tokens,
            store=False,
        )
    except Exception:
        # Do not propagate provider response bodies into capture logs or
        # rejected-attempt metadata.
        raise AIValidationError("OpenAI image inspection request failed") from None

    output_text = getattr(response, "output_text", None)
    if not isinstance(output_text, str) or not output_text.strip():
        raise AIValidationError("OpenAI image inspection returned no output text")
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise AIValidationError("OpenAI image inspection returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AIValidationError("OpenAI image inspection returned a non-object result")

    return _normalize_inspection(
        payload,
        expected=captured_at,
        timestamp_tolerance_sec=tolerance_sec,
    )
