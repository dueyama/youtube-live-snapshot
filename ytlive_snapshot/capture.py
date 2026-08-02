import argparse
import base64
import datetime
import logging
import math
import os
import re
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
import html as html_lib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from . import __version__
from . import ai_validation

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency is declared in requirements
    yaml = None

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from pytz import timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service  # Selenium 4 用の Service クラス
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)

# astral ライブラリ（pip install astral）
from astral import LocationInfo
from astral.sun import sun
from PIL import Image, ImageStat, UnidentifiedImageError

# ##############################
# デフォルト値の設定
# ##############################
# YAML/CLI で上書きされる前のフェールセーフ初期値
# 配信URLと位置情報は利用者ごとの設定であり、ソースには埋め込まない。
DEFAULT_EMBED_URL = None

# ChromeDriver のパスのデフォルト値（環境に合わせて変更）
DEFAULT_DRIVER_PATH = "/usr/bin/chromedriver"

# ブラウザのウィンドウサイズ（例：1920x1080）
DEFAULT_WINDOW_SIZE = "1920,1080"

# サンセットキャプチャのオフセット（分） ※サンセットの何分前にキャプチャするか
DEFAULT_SUNSET_OFFSET_MINUTES = 40

# ウェブカメラ設置場所のデフォルト設定
DEFAULT_LOCATION_NAME = "Webcam Location"
DEFAULT_REGION = "Japan"
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_LATITUDE = None
DEFAULT_LONGITUDE = None

DEFAULT_OUTPUT_DIR = "./captures"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SEC = 10
DEFAULT_PAGE_LOAD_TIMEOUT = 30
DEFAULT_TEST_INTERVAL_MINUTES = 2
DEFAULT_SCHEDULE_MISFIRE_GRACE_SEC = 300
DEFAULT_SOURCE_MINIMUM_WIDTH = 640
DEFAULT_SOURCE_MINIMUM_HEIGHT = 360
DEFAULT_SOURCE_PREFERRED_WIDTH = 1280
DEFAULT_SOURCE_PREFERRED_HEIGHT = 720
DEFAULT_SOURCE_RESOLUTION_WAIT_SEC = 30
CAPTURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
FIXED_TIME_RE = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")

DEFAULT_CONFIG: Dict[str, Any] = {
    "embed_url": DEFAULT_EMBED_URL,
    "chrome": {
        "driver_path": DEFAULT_DRIVER_PATH,
        "binary_path": None,
        "window_size": DEFAULT_WINDOW_SIZE,
        "page_load_timeout_sec": DEFAULT_PAGE_LOAD_TIMEOUT,
        "headless": True,
    },
    "capture": {
        "output_dir": DEFAULT_OUTPUT_DIR,
        "max_retries": DEFAULT_MAX_RETRIES,
        "retry_delay_sec": DEFAULT_RETRY_DELAY_SEC,
        "live_edge_seek": True,
        "live_edge_max_lag_sec": 30,
        "live_edge_require_verification": True,
        "frame_validation": True,
        "frame_min_luminance_stddev": 10,
        "source_resolution": {
            "minimum_width": DEFAULT_SOURCE_MINIMUM_WIDTH,
            "minimum_height": DEFAULT_SOURCE_MINIMUM_HEIGHT,
            "preferred_width": DEFAULT_SOURCE_PREFERRED_WIDTH,
            "preferred_height": DEFAULT_SOURCE_PREFERRED_HEIGHT,
            "wait_sec": DEFAULT_SOURCE_RESOLUTION_WAIT_SEC,
        },
        "ai_validation": {
            "enabled": False,
            "model": ai_validation.DEFAULT_MODEL,
            "mode": ai_validation.DEFAULT_MODE,
            "detail": ai_validation.DEFAULT_DETAIL,
            "api_key_env": ai_validation.DEFAULT_API_KEY_ENV,
            "timeout_sec": ai_validation.DEFAULT_TIMEOUT_SEC,
            "timestamp_tolerance_sec": ai_validation.DEFAULT_TIMESTAMP_TOLERANCE_SEC,
            "require_timestamp": False,
            "max_output_tokens": ai_validation.DEFAULT_MAX_OUTPUT_TOKENS,
        },
        "max_files": None,
        "max_disk_mb": None,
    },
    "schedule": {
        "timezone": DEFAULT_TIMEZONE,
        "latitude": DEFAULT_LATITUDE,
        "longitude": DEFAULT_LONGITUDE,
        "location_name": DEFAULT_LOCATION_NAME,
        "region": DEFAULT_REGION,
        "sunset_offset_minutes": DEFAULT_SUNSET_OFFSET_MINUTES,
        "sunset_prefix": "sunset",
        "misfire_grace_time_sec": DEFAULT_SCHEDULE_MISFIRE_GRACE_SEC,
        "fixed_times": None,
        "enable_noon": True,
        "enable_sunset": True,
    },
    "logging": {
        "level": "INFO",
        "log_file": None,
    },
    "test_mode": {
        "interval_minutes": DEFAULT_TEST_INTERVAL_MINUTES,
    },
}


##############################
# グローバル変数（スケジューラーやロケーション情報）
##############################
global_scheduler = None
WEBCAM_LOCATION = None  # 後ほど LocationInfo オブジェクトで初期化
logger = logging.getLogger("ytlive_snapshot.capture")


class CaptureValidationError(RuntimeError):
    """The browser returned an image, but it is not a trustworthy live frame."""


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if (
            key in dst
            and isinstance(dst[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def _load_config_file(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is not installed but --config was specified.")
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Config root must be a mapping (file: {path})")
        return data


def _resolve_config_path(
    cli_path: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """CLI、環境変数、設定ディレクトリの順で設定ファイルを解決する。"""
    if cli_path:
        return cli_path

    env = os.environ if environ is None else environ
    explicit_path = env.get("YTLIVE_SNAPSHOT_CONFIG") or env.get("CAPTUREPY_CONFIG")
    if explicit_path:
        return explicit_path

    config_dir = env.get("YTLIVE_SNAPSHOT_CONFIG_DIR") or env.get("CAPTUREPY_CONFIG_DIR")
    if config_dir:
        candidate = Path(config_dir) / "capture.yaml"
        if candidate.is_file():
            return str(candidate)
    return None


def _apply_environment_overrides(
    config: Dict[str, Any],
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """ホスト固有値を通常の環境変数から上書きする。"""
    env = os.environ if environ is None else environ
    schedule_cfg = config.setdefault("schedule", {})

    string_overrides = {
        "EMBED_URL": (config, "embed_url"),
        "LOCATION_NAME": (schedule_cfg, "location_name"),
        "REGION": (schedule_cfg, "region"),
        "TIMEZONE": (schedule_cfg, "timezone"),
    }
    for suffix, (target, key) in string_overrides.items():
        env_name = f"YTLIVE_SNAPSHOT_{suffix}"
        legacy_env_name = f"CAPTUREPY_{suffix}"
        value = env.get(env_name)
        if value is None:
            value = env.get(legacy_env_name)
        if value is not None and value.strip():
            target[key] = value.strip()

    for suffix, key in (
        ("LATITUDE", "latitude"),
        ("LONGITUDE", "longitude"),
    ):
        env_name = f"YTLIVE_SNAPSHOT_{suffix}"
        legacy_env_name = f"CAPTUREPY_{suffix}"
        value = env.get(env_name)
        if value is None:
            value = env.get(legacy_env_name)
        if value is None or not value.strip():
            continue
        try:
            schedule_cfg[key] = float(value)
        except ValueError as exc:
            raise ValueError(f"{env_name} must be a number") from exc


def _validate_runtime_config(
    config: Dict[str, Any],
    *,
    require_sunset_location: bool,
) -> None:
    embed_url = config.get("embed_url")
    if not isinstance(embed_url, str) or not embed_url.strip():
        raise ValueError(
            "embed_url is required. Set it in config/capture.yaml, "
            "YTLIVE_SNAPSHOT_EMBED_URL, or --embed-url."
        )

    schedule_cfg = config.setdefault("schedule", {})
    latitude = schedule_cfg.get("latitude")
    longitude = schedule_cfg.get("longitude")
    if require_sunset_location and (latitude is None or longitude is None):
        raise ValueError(
            "latitude and longitude are required when sunset capture is enabled. "
            "Set them in config/capture.yaml, environment variables, or CLI options."
        )
    if latitude is not None and not -90 <= float(latitude) <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if longitude is not None and not -180 <= float(longitude) <= 180:
        raise ValueError("longitude must be between -180 and 180")
    _configured_fixed_times(schedule_cfg)

    sunset_prefix = schedule_cfg.get("sunset_prefix", "sunset")
    if not isinstance(sunset_prefix, str) or not CAPTURE_NAME_RE.fullmatch(sunset_prefix):
        raise ValueError("sunset_prefix must contain only letters, numbers, underscores, and hyphens")
    capture_cfg = config.setdefault("capture", {})
    _source_resolution_settings(capture_cfg)
    ai_validation.validate_config(capture_cfg.setdefault("ai_validation", {}))


def _configured_fixed_times(schedule_cfg: Dict[str, Any]) -> list:
    """固定時刻設定を検証し、APSchedulerへ渡せる形式に正規化する。"""
    raw_jobs = schedule_cfg.get("fixed_times")
    if raw_jobs is None:
        raw_jobs = (
            [{"id": "noon", "time": "12:00", "prefix": "noon"}]
            if schedule_cfg.get("enable_noon", True)
            else []
        )
    if not isinstance(raw_jobs, list):
        raise ValueError("schedule.fixed_times must be a list")

    normalized = []
    seen_ids = set()
    seen_slots = set()
    for item in raw_jobs:
        if not isinstance(item, dict):
            raise ValueError("each schedule.fixed_times entry must be a mapping")
        job_id = item.get("id")
        prefix = item.get("prefix", job_id)
        time_text = item.get("time")
        if not isinstance(job_id, str) or not CAPTURE_NAME_RE.fullmatch(job_id):
            raise ValueError(f"invalid fixed capture id: {job_id!r}")
        if not isinstance(prefix, str) or not CAPTURE_NAME_RE.fullmatch(prefix):
            raise ValueError(f"invalid fixed capture prefix: {prefix!r}")
        if not isinstance(time_text, str):
            raise ValueError(f"fixed capture {job_id!r} requires time in HH:MM format")
        match = FIXED_TIME_RE.fullmatch(time_text)
        if not match:
            raise ValueError(f"invalid time for fixed capture {job_id!r}: {time_text!r}")
        if job_id in seen_ids:
            raise ValueError(f"duplicate fixed capture id: {job_id}")
        slot = (prefix, time_text)
        if slot in seen_slots:
            raise ValueError(f"duplicate fixed capture prefix/time: {prefix} at {time_text}")
        seen_ids.add(job_id)
        seen_slots.add(slot)
        normalized.append(
            {
                "id": job_id,
                "prefix": prefix,
                "time": time_text,
                "hour": int(match.group("hour")),
                "minute": int(match.group("minute")),
            }
        )

    if not schedule_cfg.get("enable_noon", True):
        normalized = [job for job in normalized if job["id"] != "noon"]
    return normalized


def _setup_logging(log_cfg: Dict[str, Any]):
    level_name = (log_cfg.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers = []
    log_file = log_cfg.get("log_file")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=level, handlers=handlers)


def _validate_positive(name: str, value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _validate_non_negative_float(name: str, value: Any) -> float:
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return numeric_value


def _validate_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_resolution_settings(capture_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    raw = capture_cfg.get("source_resolution", {})
    if not isinstance(raw, Mapping):
        raise ValueError("capture.source_resolution must be a mapping")

    settings = {
        "minimum_width": _validate_positive_int(
            "capture.source_resolution.minimum_width",
            raw.get("minimum_width", DEFAULT_SOURCE_MINIMUM_WIDTH),
        ),
        "minimum_height": _validate_positive_int(
            "capture.source_resolution.minimum_height",
            raw.get("minimum_height", DEFAULT_SOURCE_MINIMUM_HEIGHT),
        ),
        "preferred_width": _validate_positive_int(
            "capture.source_resolution.preferred_width",
            raw.get("preferred_width", DEFAULT_SOURCE_PREFERRED_WIDTH),
        ),
        "preferred_height": _validate_positive_int(
            "capture.source_resolution.preferred_height",
            raw.get("preferred_height", DEFAULT_SOURCE_PREFERRED_HEIGHT),
        ),
        "wait_sec": _validate_non_negative_float(
            "capture.source_resolution.wait_sec",
            raw.get("wait_sec", DEFAULT_SOURCE_RESOLUTION_WAIT_SEC),
        ),
    }
    if settings["preferred_width"] < settings["minimum_width"]:
        raise ValueError(
            "capture.source_resolution.preferred_width must be >= minimum_width"
        )
    if settings["preferred_height"] < settings["minimum_height"]:
        raise ValueError(
            "capture.source_resolution.preferred_height must be >= minimum_height"
        )
    return settings


def _ensure_output_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _prune_old_files(
    output_dir: Path,
    prefix: str,
    max_files: Optional[int],
    max_disk_mb: Optional[int],
):
    files = sorted(output_dir.glob(f"{prefix}*.png"), key=lambda p: p.stat().st_mtime)
    if max_files is not None:
        while len(files) > max_files:
            oldest = files.pop(0)
            try:
                oldest.unlink()
                logger.info("Removed old capture (max_files): %s", oldest)
            except Exception as exc:  # pragma: no cover - filesystem issues
                logger.warning("Failed to delete %s: %s", oldest, exc)
    if max_disk_mb is not None:
        def current_mb():
            return sum(p.stat().st_size for p in files) / (1024 * 1024)

        while files and current_mb() > max_disk_mb:
            oldest = files.pop(0)
            try:
                oldest.unlink()
                logger.info("Removed old capture (max_disk_mb): %s", oldest)
            except Exception as exc:
                logger.warning("Failed to delete %s: %s", oldest, exc)


def _apply_cli_overrides(config: Dict[str, Any], args: argparse.Namespace):
    capture_cfg = config.setdefault("capture", {})
    chrome_cfg = config.setdefault("chrome", {})
    schedule_cfg = config.setdefault("schedule", {})
    logging_cfg = config.setdefault("logging", {})
    test_cfg = config.setdefault("test_mode", {})

    if getattr(args, "embed_url", None):
        config["embed_url"] = args.embed_url
    if getattr(args, "driver_path", None):
        chrome_cfg["driver_path"] = args.driver_path
    if getattr(args, "browser_binary", None):
        chrome_cfg["binary_path"] = args.browser_binary
    if getattr(args, "window_size", None):
        chrome_cfg["window_size"] = args.window_size
    if getattr(args, "page_load_timeout", None):
        chrome_cfg["page_load_timeout_sec"] = args.page_load_timeout

    if getattr(args, "output_dir", None):
        capture_cfg["output_dir"] = args.output_dir
    if getattr(args, "max_retries", None) is not None:
        capture_cfg["max_retries"] = args.max_retries
    if getattr(args, "retry_delay_sec", None) is not None:
        capture_cfg["retry_delay_sec"] = args.retry_delay_sec
    if getattr(args, "max_files", None) is not None:
        capture_cfg["max_files"] = args.max_files
    if getattr(args, "max_disk_mb", None) is not None:
        capture_cfg["max_disk_mb"] = args.max_disk_mb

    ai_cfg = capture_cfg.setdefault("ai_validation", {})
    if getattr(args, "ai_validation_enabled", None) is not None:
        ai_cfg["enabled"] = args.ai_validation_enabled
    if getattr(args, "ai_validation_mode", None):
        ai_cfg["mode"] = args.ai_validation_mode
    if getattr(args, "ai_model", None):
        ai_cfg["model"] = args.ai_model
    if getattr(args, "ai_timestamp_tolerance_sec", None) is not None:
        ai_cfg["timestamp_tolerance_sec"] = args.ai_timestamp_tolerance_sec
    if getattr(args, "ai_require_timestamp", None) is not None:
        ai_cfg["require_timestamp"] = args.ai_require_timestamp

    if getattr(args, "log_file", None):
        logging_cfg["log_file"] = args.log_file
    if getattr(args, "log_level", None):
        logging_cfg["level"] = args.log_level

    if getattr(args, "sunset_offset", None) is not None:
        schedule_cfg["sunset_offset_minutes"] = args.sunset_offset
    if getattr(args, "latitude", None) is not None:
        schedule_cfg["latitude"] = args.latitude
    if getattr(args, "longitude", None) is not None:
        schedule_cfg["longitude"] = args.longitude
    if getattr(args, "location_name", None):
        schedule_cfg["location_name"] = args.location_name
    if getattr(args, "region", None):
        schedule_cfg["region"] = args.region
    if getattr(args, "timezone", None):
        schedule_cfg["timezone"] = args.timezone
    if getattr(args, "enable_noon", None) is not None:
        schedule_cfg["enable_noon"] = args.enable_noon
    if getattr(args, "enable_sunset", None) is not None:
        schedule_cfg["enable_sunset"] = args.enable_sunset

    if getattr(args, "test_interval_minutes", None) is not None:
        test_cfg["interval_minutes"] = args.test_interval_minutes


def _normalize_youtube_embed_url(embed_url: str, prefer_nocookie: bool = True) -> str:
    """YouTube 埋め込みURLを「再生されやすい」形に補正する。

    - ?si=... はトラブル原因になりやすいので除去
    - watch/live/youtu.be URL は /embed/{video_id} に変換
    - youtube.com/embed -> youtube-nocookie.com/embed に寄せる（任意）
    - autoplay/mute/playsinline/controls=1 を付与
      （LIVE状態の確認後、撮影時だけUIを隠す）
    """
    parsed = urlparse(embed_url)
    scheme = parsed.scheme or "https"
    if scheme not in ("http", "https"):
        scheme = "https"

    netloc = parsed.netloc
    path = parsed.path
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))

    video_id = None
    if "youtu.be" in netloc:
        video_id = path.strip("/").split("/")[0]
    elif "youtube.com" in netloc or "youtube-nocookie.com" in netloc:
        parts = [part for part in path.split("/") if part]
        if path == "/watch":
            video_id = qs.get("v")
        elif len(parts) >= 2 and parts[0] in ("embed", "live", "shorts"):
            video_id = parts[1]

    if video_id:
        path = f"/embed/{video_id}"
        if prefer_nocookie:
            netloc = "www.youtube-nocookie.com"
        elif "youtu.be" in netloc:
            netloc = "www.youtube.com"
    elif prefer_nocookie:
        if ("youtube.com" in netloc) and ("youtube-nocookie.com" not in netloc):
            netloc = "www.youtube-nocookie.com"

    # クエリ整理
    qs.pop("si", None)               # 共有系パラメータを除去
    qs.pop("v", None)                # watch URL 由来の video id は path に移す
    qs["autoplay"] = "1"             # 自動再生
    qs["mute"] = "1"                 # ミュート（自動再生ブロック回避）
    qs.setdefault("playsinline", "1")
    # YouTube's direct LIVE control is the only trustworthy delayed-playback
    # signal available to the deterministic checker. Keep controls enabled
    # while inspecting the player; the screenshot path hides them afterward.
    qs["controls"] = "1"
    qs.setdefault("modestbranding", "1")
    qs.setdefault("rel", "0")
    qs.setdefault("iv_load_policy", "3")
    qs.setdefault("disablekb", "1")
    qs.setdefault("fs", "0")
    qs.setdefault("enablejsapi", "1")

    query = urlencode(qs, doseq=True)
    return urlunparse((scheme, netloc, path, parsed.params, query, parsed.fragment))


def _build_wrapper_html(embed_url: str) -> str:
    """YouTube の iframe 埋め込みをフル画面で表示する単純な HTML を生成する。"""
    safe_src = html_lib.escape(embed_url, quote=True)
    return f"""<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<meta name=\"referrer\" content=\"strict-origin-when-cross-origin\">
<style>
html, body {{ width:100%; height:100%; margin:0; padding:0; overflow:hidden; background:#000; }}
#capture-frame {{ position:fixed; inset:0; width:100vw; height:100vh; overflow:hidden; background:#000; }}
iframe {{ width:100%; height:100%; border:0; display:block; }}
</style>
</head>
<body>
<div id=\"capture-frame\">
<iframe id=\"ytplayer\"
        src=\"{safe_src}\"
        allow=\"autoplay; encrypted-media; picture-in-picture\"
        allowfullscreen
        referrerpolicy=\"strict-origin-when-cross-origin\"></iframe>
</div>
</body>
</html>"""


def _start_local_html_server(html_content: str):
    """1ページだけ配信するローカルHTTPサーバを起動して (httpd, url) を返す。"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            return  # ログ抑制

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)  # port=0 で空きポート
    url = f"http://127.0.0.1:{httpd.server_port}/"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, url


def _build_chrome_service(driver_path: Optional[str]) -> Service:
    """driver_path が有効なら使い、なければ Selenium Manager に解決を任せる。"""
    if driver_path:
        path = Path(driver_path)
        if path.is_file():
            return Service(str(path))
        logger.warning(
            "ChromeDriver not found at %s; falling back to Selenium Manager",
            driver_path,
        )
    return Service()


_YOUTUBE_UI_HIDE_CSS = """
.ytp-chrome-top,
.ytp-chrome-bottom,
.ytp-gradient-top,
.ytp-gradient-bottom,
.ytp-large-play-button,
.ytp-pause-overlay,
.ytp-bezel,
.ytp-spinner,
.ytp-paid-content-overlay,
.ytp-ce-element,
.ytp-cards-teaser,
.ytp-watermark {
  opacity: 0 !important;
  visibility: hidden !important;
  display: none !important;
  pointer-events: none !important;
}
.html5-video-player > :not(.html5-video-container),
.html5-video-container > :not(video) {
  opacity: 0 !important;
  visibility: hidden !important;
  display: none !important;
  pointer-events: none !important;
}
html,
body,
#movie_player,
.html5-video-player,
.html5-main-video {
  cursor: none !important;
}
"""


def _hide_youtube_player_ui(driver) -> Dict[str, Any]:
    """iframe 内の YouTube 操作 UI を非表示にし、video の状態を返す。"""
    return driver.execute_script(
        """
        const css = arguments[0];
        let style = document.getElementById('capturepy-hide-youtube-ui');
        if (!style) {
          style = document.createElement('style');
          style.id = 'capturepy-hide-youtube-ui';
          document.documentElement.appendChild(style);
        }
        style.textContent = css;

        const player = document.querySelector('.html5-video-player');
        if (player) {
          player.classList.add('ytp-autohide');
          player.classList.remove('ytp-autohide-paused');
        }

        const video = document.querySelector('video');
        if (video) {
          video.controls = false;
          video.muted = true;
          video.setAttribute('playsinline', '');
        }

        return {
          hasVideo: Boolean(video),
          paused: video ? video.paused : null,
          readyState: video ? video.readyState : null,
          currentTime: video ? video.currentTime : null,
          videoWidth: video ? video.videoWidth : null,
          videoHeight: video ? video.videoHeight : null
        };
        """,
        _YOUTUBE_UI_HIDE_CSS,
    )


def _try_play_youtube_video(driver) -> Dict[str, Any]:
    """YouTube iframe 内の video を JS で再生し、失敗時は既存の play ボタンをクリックする。"""
    status = driver.execute_script(
        """
        const video = document.querySelector('video');
        if (!video) {
          return {hasVideo: false};
        }
        video.muted = true;
        video.setAttribute('playsinline', '');
        let playRequested = false;
        let error = null;
        try {
          const request = video.play();
          playRequested = true;
          if (request && typeof request.catch === 'function') {
            request.catch(() => null);
          }
        } catch (playError) {
          error = String(playError);
        }
        return {
          hasVideo: true,
          playRequested,
          error,
          paused: video.paused,
          readyState: video.readyState,
          currentTime: video.currentTime,
          videoWidth: video.videoWidth,
          videoHeight: video.videoHeight
        };
        """
    )

    if status.get("paused"):
        try:
            play_button = driver.find_element(By.CSS_SELECTOR, "button.ytp-large-play-button")
            play_button.click()
            status["clickedPlayButton"] = True
            logger.debug("Clicked play button")
        except (NoSuchElementException, ElementNotInteractableException):
            logger.debug("Play button unavailable; skipping click")

    return status


def _seek_youtube_live_edge(
    driver,
    max_lag_sec: float,
) -> Dict[str, Any]:
    """Use YouTube's direct LIVE control when it reports delayed playback."""
    driver.set_script_timeout(12)
    return driver.execute_async_script(
        """
        const maxLagSec = Number(arguments[0]);
        const done = arguments[arguments.length - 1];
        const player = document.getElementById('movie_player') || document.querySelector('.html5-video-player');
        const video = document.querySelector('video');
        if (!video) {
          done({hasVideo: false});
          return;
        }

        const baseStatus = () => {
          let playerCurrentTime = null;
          let playerDuration = null;
          let playerLagSec = null;
          let isLive = null;
          let playerState = null;
          let playerVideoData = null;
          if (player) {
            try {
              if (typeof player.getCurrentTime === 'function') {
                playerCurrentTime = player.getCurrentTime();
              }
              if (typeof player.getDuration === 'function') {
                playerDuration = player.getDuration();
              }
              if (typeof player.getPlayerState === 'function') {
                playerState = player.getPlayerState();
              }
              if (typeof player.getVideoData === 'function') {
                playerVideoData = player.getVideoData();
              }
              if (typeof player.isLivePlayback === 'function') {
                isLive = Boolean(player.isLivePlayback());
              } else if (playerVideoData) {
                isLive = Boolean(
                  playerVideoData.isLive ||
                  playerVideoData.isLiveContent ||
                  playerVideoData.isPlayableLiveStream
                );
              }
              if (
                Number.isFinite(playerCurrentTime) &&
                Number.isFinite(playerDuration)
              ) {
                playerLagSec = playerDuration - playerCurrentTime;
              }
            } catch (error) {
              playerVideoData = {error: String(error)};
            }
          }
          if (
            isLive === null &&
            player &&
            player.classList.contains('ytp-livebadge-color')
          ) {
            isLive = true;
          }
          const ranges = video.seekable;
          const hasRange = Boolean(ranges && ranges.length);
          let rangeStart = null;
          let liveEdge = null;
          let lagSec = null;
          if (hasRange) {
            const index = ranges.length - 1;
            rangeStart = ranges.start(index);
            liveEdge = ranges.end(index);
            lagSec = liveEdge - video.currentTime;
          }
          const classicLiveControl = document.querySelector('.ytp-live-badge');
          const classicLiveDisabled = classicLiveControl
            ? Boolean(
                classicLiveControl.disabled ||
                classicLiveControl.matches(':disabled') ||
                classicLiveControl.getAttribute('aria-disabled') === 'true'
              )
            : null;
          // YouTube's current embedded-player UI renders the LIVE state as a
          // time display. At the live head it shows only "LIVE"/"ライブ";
          // while behind it also exposes a negative value such as "-0:20".
          const modernLiveControl = document.querySelector(
            '.ytwPlayerTimeDisplayTimeElapsed'
          );
          const modernDuration = document.querySelector(
            '.ytwPlayerTimeDisplayTimeDuration'
          );
          const modernLiveText = modernLiveControl
            ? (modernLiveControl.textContent || '').trim()
            : '';
          const modernDurationText = modernDuration
            ? (modernDuration.textContent || '').trim()
            : '';
          const normalizedDurationText = modernDurationText
            .replace(/[０-９]/g, (digit) => String(digit.charCodeAt(0) - 0xFF10))
            .replace(/[٠-٩]/g, (digit) => String(digit.charCodeAt(0) - 0x0660))
            .replace(/[۰-۹]/g, (digit) => String(digit.charCodeAt(0) - 0x06F0))
            .replace(/[−–—]/g, '-');
          const modernIsLiveControl = Boolean(
            modernLiveControl &&
            (
              isLive === true ||
              (player && player.classList.contains('ytp-livebadge-color'))
            )
          );
          const modernLagMatch = normalizedDurationText.match(
            /-(\\d+):(\\d{2})(?::(\\d{2}))?/
          );
          let liveControlLagSec = null;
          if (modernLagMatch) {
            if (modernLagMatch[3] === undefined) {
              liveControlLagSec = (
                Number(modernLagMatch[1]) * 60 + Number(modernLagMatch[2])
              );
            } else {
              liveControlLagSec = (
                Number(modernLagMatch[1]) * 3600 +
                Number(modernLagMatch[2]) * 60 +
                Number(modernLagMatch[3])
              );
            }
          }
          const liveControlKind = modernIsLiveControl
            ? 'modern-time-display'
            : (classicLiveControl ? 'classic-live-badge' : null);
          const liveControlWithinTolerance = modernIsLiveControl
            ? (
                modernDuration && modernDurationText
                  ? (
                      liveControlLagSec === null
                        ? null
                        : liveControlLagSec <= maxLagSec
                    )
                  : true
              )
            : (
                classicLiveControl
                  ? classicLiveDisabled
                  : null
              );
          return {
            hasVideo: true,
            hasPlayer: Boolean(player),
            hasSeekToLiveHead: Boolean(player && typeof player.seekToLiveHead === 'function'),
            hasPlayerSeekTo: Boolean(player && typeof player.seekTo === 'function'),
            playerCurrentTime,
            playerDuration,
            playerLagSec,
            playerState,
            isLive,
            playerVideoData,
            liveControlPresent: Boolean(liveControlKind),
            liveControlKind,
            liveControlText: modernIsLiveControl
              ? modernLiveText
              : (
                  classicLiveControl
                    ? (classicLiveControl.textContent || '').trim()
                    : null
                ),
            liveControlDurationText: modernIsLiveControl
              ? modernDurationText
              : null,
            liveControlLagSec,
            liveControlWithinTolerance,
            hasSeekableRange: hasRange,
            rangeStart,
            liveEdge,
            currentTime: video.currentTime,
            lagSec,
            paused: video.paused,
            readyState: video.readyState,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight
          };
        };

        const before = baseStatus();
        let action = null;
        // The player's own LIVE display is the primary signal. Avoid seeking
        // an already-live video based on the ambiguous duration/currentTime
        // values exposed for DVR streams.
        if (before.liveControlWithinTolerance === true) {
          video.muted = true;
          video.setAttribute('playsinline', '');
          Promise.resolve(video.play()).catch(() => null);
          done({...before, seeked: false, reason: 'YouTube LIVE display is within tolerance'});
          return;
        }
        if (
          before.liveControlPresent &&
          before.liveControlWithinTolerance === false
        ) {
          action = before.liveControlKind === 'modern-time-display'
            ? 'modernLiveControl.click'
            : 'classicLiveControl.click';
        }

        if (!action) {
          video.muted = true;
          video.setAttribute('playsinline', '');
          if (player && typeof player.playVideo === 'function') {
            player.playVideo();
          } else {
            Promise.resolve(video.play()).catch(() => null);
          }
          // In headless embeds the LIVE control may not be rendered. The raw
          // duration/currentTime and seekable-end differences can then report
          // a false delay of roughly the DVR-window length. Preserve the
          // initially loaded frame instead of turning that ambiguous value
          // into a destructive seek.
          done({
            ...before,
            seeked: false,
            timingLagIgnored: true,
            reason: before.liveControlPresent
              ? 'LIVE control inconclusive; current playback preserved'
              : 'LIVE control unavailable; ambiguous timing lag ignored'
          });
          return;
        }

        let settled = false;
        let timeoutId = null;
        const hasDrawableFrame = () => (
          video.readyState >= 2 &&
          video.videoWidth > 0 &&
          video.videoHeight > 0
        );
        const finishLiveControl = (reason) => {
          if (settled) return;
          settled = true;
          clearTimeout(timeoutId);
          video.removeEventListener('seeked', onSeeked);
          video.removeEventListener('canplay', onCanPlay);
          video.removeEventListener('timeupdate', onTimeUpdate);
          const after = baseStatus();
          done({
            ...before,
            seeked: true,
            action,
            finishReason: reason,
            afterCurrentTime: after.currentTime,
            afterLagSec: after.lagSec,
            afterPlayerCurrentTime: after.playerCurrentTime,
            afterPlayerDuration: after.playerDuration,
            afterPlayerLagSec: after.playerLagSec,
            afterLiveControlLagSec: after.liveControlLagSec,
            afterLiveControlWithinTolerance: after.liveControlWithinTolerance,
            afterPaused: after.paused,
            afterReadyState: after.readyState,
            afterVideoWidth: after.videoWidth,
            afterVideoHeight: after.videoHeight
          });
        };
        const onSeeked = () => {
          const status = baseStatus();
          if (
            hasDrawableFrame() &&
            status.liveControlWithinTolerance === true
          ) finishLiveControl('seeked');
        };
        const onCanPlay = () => {
          const status = baseStatus();
          if (
            hasDrawableFrame() &&
            status.liveControlWithinTolerance === true
          ) finishLiveControl('canplay');
        };
        const onTimeUpdate = () => {
          const status = baseStatus();
          if (
            hasDrawableFrame() &&
            status.liveControlWithinTolerance === true
          ) finishLiveControl('timeupdate');
        };

        video.addEventListener('seeked', onSeeked);
        video.addEventListener('canplay', onCanPlay);
        video.addEventListener('timeupdate', onTimeUpdate);
        timeoutId = setTimeout(() => finishLiveControl('timeout'), 6000);
        video.muted = true;
        video.setAttribute('playsinline', '');
        try {
          if (action === 'modernLiveControl.click') {
            document.querySelector('.ytwPlayerTimeDisplayTimeElapsed').click();
          } else {
            document.querySelector('.ytp-live-badge').click();
          }
          if (player && typeof player.playVideo === 'function') {
            player.playVideo();
          } else {
            Promise.resolve(video.play()).catch(() => null);
          }
        } catch (error) {
          finishLiveControl(String(error));
        }
        """,
        max_lag_sec,
    )


def _format_optional_seconds(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return str(value)


def _log_live_edge_status(status: Dict[str, Any]):
    if status.get("seeked"):
        logger.info(
            "Requested YouTube live head: action=%s reason=%s displayed_lag=%s within_tolerance=%s before_player_lag=%s after_player_lag=%s",
            status.get("action"),
            status.get("finishReason"),
            _format_optional_seconds(status.get("afterLiveControlLagSec")),
            status.get("afterLiveControlWithinTolerance"),
            _format_optional_seconds(status.get("playerLagSec")),
            _format_optional_seconds(status.get("afterPlayerLagSec")),
        )
    elif status.get("timingLagIgnored"):
        logger.info(
            "YouTube LIVE control unavailable; kept initial playback without seeking: is_live=%s raw_media_lag=%s raw_player_lag=%s",
            status.get("isLive"),
            _format_optional_seconds(status.get("lagSec")),
            _format_optional_seconds(status.get("playerLagSec")),
        )
    else:
        logger.debug("YouTube live edge status: %s", status)


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _live_edge_validation_error(
    status: Dict[str, Any],
    max_lag_sec: float,
    require_verification: bool = True,
) -> Optional[str]:
    """Return a reason when YouTube is not demonstrably close to the live edge."""
    if not status.get("hasVideo"):
        return "YouTube video element is unavailable"
    if status.get("isLive") is False:
        return "YouTube player is not reporting live playback"

    # YouTube's own LIVE control is the most direct live-head indicator. The
    # current UI displays negative lag while behind; older UI enables its LIVE
    # button when clicking it would catch up.
    for key in (
        "afterLiveControlWithinTolerance",
        "liveControlWithinTolerance",
    ):
        within_tolerance = status.get(key)
        if within_tolerance is True:
            return None
        if within_tolerance is False:
            lag = _finite_float(
                status.get(
                    "afterLiveControlLagSec",
                    status.get("liveControlLagSec"),
                )
            )
            if lag is not None:
                return (
                    f"YouTube LIVE control reports {lag:.1f}s delayed playback "
                    f"(limit {max_lag_sec:.1f}s)"
                )
            return "YouTube LIVE control reports delayed playback"

    # With no rendered LIVE control, YouTube's DVR timing values are not a
    # trustworthy live-edge measurement. Accept a positively identified live
    # stream without seeking; optional image-clock inspection can add an
    # independent freshness signal.
    if status.get("isLive") is True:
        return None

    if require_verification:
        return "YouTube live-edge lag could not be verified"
    return None


def _capture_image_validation_error(
    target_path: Path,
    min_luminance_stddev: float = 10,
) -> Optional[str]:
    """Reject blank, nearly uniform, truncated, or unreadable screenshots."""
    try:
        with Image.open(target_path) as image:
            image.verify()
        with Image.open(target_path) as image:
            width, height = image.size
            if width < 320 or height < 180:
                return f"capture image is too small ({width}x{height})"
            sample = image.convert("L")
            sample.thumbnail((160, 90), Image.Resampling.BILINEAR)
            stats = ImageStat.Stat(sample)
            mean = float(stats.mean[0])
            stddev = float(stats.stddev[0])
    except (OSError, UnidentifiedImageError) as exc:
        return f"capture image is unreadable: {exc}"

    if mean <= 1:
        return f"capture image is effectively black (mean={mean:.1f})"
    if stddev < min_luminance_stddev:
        return (
            "capture image is nearly uniform and likely an error/loading frame "
            f"(mean={mean:.1f}, stddev={stddev:.1f})"
        )
    return None


def _apply_optional_ai_validation(
    image_path: Path,
    *,
    captured_at: datetime.datetime,
    config: Mapping[str, Any],
    inspector=None,
) -> None:
    """Run optional OpenAI image inspection and enforce it only when requested."""
    if not config.get("enabled", False):
        return

    mode = config.get("mode", ai_validation.DEFAULT_MODE)
    inspect = ai_validation.inspect_capture if inspector is None else inspector
    try:
        result = inspect(
            image_path,
            captured_at=captured_at,
            config=config,
        )
    except ai_validation.AIValidationError as exc:
        if mode == "enforce":
            raise CaptureValidationError(
                f"AI validation could not complete: {exc}"
            ) from exc
        logger.warning("AI validation unavailable; keeping local result: %s", exc)
        return

    logger.info(
        "AI capture validation: decision=%s confidence=%.2f "
        "timestamp_status=%s observed_timestamp=%s delta=%s summary=%s",
        result.decision,
        result.confidence,
        result.timestamp_status,
        result.observed_timestamp or "n/a",
        _format_optional_seconds(result.timestamp_delta_seconds),
        result.summary,
    )
    rejection = result.rejection_reason(
        require_timestamp=config.get("require_timestamp", False),
    )
    if not rejection:
        return
    if mode == "enforce":
        raise CaptureValidationError(rejection)
    logger.warning("AI validation advisory: %s", rejection)


def _video_frame_status(driver) -> Dict[str, Any]:
    return driver.execute_script(
        """
        const video = document.querySelector('video');
        if (!video) {
          return {hasVideo: false, ready: false};
        }
        return {
          hasVideo: true,
          ready: video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0,
          readyState: video.readyState,
          currentTime: video.currentTime,
          videoWidth: video.videoWidth,
          videoHeight: video.videoHeight,
          paused: video.paused
        };
        """
    )


def _source_resolution_result(
    status: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> str:
    if not status.get("ready"):
        return "not_ready"

    width = int(status.get("videoWidth") or 0)
    height = int(status.get("videoHeight") or 0)
    if (
        width >= settings["preferred_width"]
        and height >= settings["preferred_height"]
    ):
        return "preferred"
    if (
        width >= settings["minimum_width"]
        and height >= settings["minimum_height"]
    ):
        return "minimum"
    return "below_minimum"


def _source_resolution_validation_error(
    status: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> Optional[str]:
    result = _source_resolution_result(status, settings)
    if result == "not_ready":
        return f"YouTube video frame is not ready: {dict(status)}"
    if result == "below_minimum":
        width = int(status.get("videoWidth") or 0)
        height = int(status.get("videoHeight") or 0)
        return (
            "YouTube source resolution is below the configured minimum "
            f"(observed={width}x{height}, "
            f"minimum={settings['minimum_width']}x{settings['minimum_height']})"
        )
    return None


def _wait_for_youtube_video_frame(
    driver,
    timeout_sec: float = 8,
    *,
    preferred_width: Optional[int] = None,
    preferred_height: Optional[int] = None,
) -> Dict[str, Any]:
    def _preferred_status(d):
        status = _video_frame_status(d)
        if not status.get("ready"):
            return False
        if preferred_width is None or preferred_height is None:
            return status
        if (
            int(status.get("videoWidth") or 0) >= preferred_width
            and int(status.get("videoHeight") or 0) >= preferred_height
        ):
            return status
        return False

    try:
        return WebDriverWait(driver, timeout_sec, poll_frequency=0.25).until(
            _preferred_status
        )
    except TimeoutException:
        return _video_frame_status(driver)


def _save_video_canvas_screenshot(
    driver,
    target_path: Path,
    *,
    minimum_source_width: int = 1,
    minimum_source_height: int = 1,
) -> bool:
    """YouTube UI レイヤーを含めず、video の現在フレームだけを PNG 保存する。"""
    driver.set_script_timeout(8)
    result = driver.execute_async_script(
        """
        const minimumSourceWidth = Number(arguments[0]) || 1;
        const minimumSourceHeight = Number(arguments[1]) || 1;
        const done = arguments[arguments.length - 1];
        const video = document.querySelector('video');
        if (!video) {
          done({ok: false, reason: 'video element not found'});
          return;
        }
        const sourceWidth = video.videoWidth || 0;
        const sourceHeight = video.videoHeight || 0;
        const rect = video.getBoundingClientRect();
        const width = Math.round(rect.width) || video.clientWidth || sourceWidth;
        const height = Math.round(rect.height) || video.clientHeight || sourceHeight;
        if (!sourceWidth || !sourceHeight || !width || !height || video.readyState < 2) {
          done({
            ok: false,
            reason: 'video frame is not ready',
            readyState: video.readyState,
            width,
            height,
            sourceWidth,
            sourceHeight
          });
          return;
        }
        if (sourceWidth < minimumSourceWidth || sourceHeight < minimumSourceHeight) {
          done({
            ok: false,
            reason: 'video source resolution is below minimum',
            readyState: video.readyState,
            width,
            height,
            sourceWidth,
            sourceHeight,
            minimumSourceWidth,
            minimumSourceHeight
          });
          return;
        }

        try {
          const canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          const ctx = canvas.getContext('2d', {alpha: false});
          ctx.fillStyle = '#000';
          ctx.fillRect(0, 0, width, height);
          ctx.drawImage(video, 0, 0, width, height);
          done({
            ok: true,
            width,
            height,
            sourceWidth,
            sourceHeight,
            dataUrl: canvas.toDataURL('image/png')
          });
        } catch (error) {
          done({
            ok: false,
            reason: String(error),
            readyState: video.readyState,
            width,
            height,
            sourceWidth,
            sourceHeight
          });
        }
        """,
        minimum_source_width,
        minimum_source_height,
    )

    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "video source resolution is below minimum":
            raise CaptureValidationError(
                "YouTube source resolution dropped below the configured minimum "
                f"before canvas capture (observed={result.get('sourceWidth', 0)}x"
                f"{result.get('sourceHeight', 0)}, "
                f"minimum={minimum_source_width}x{minimum_source_height})"
            )
        if reason in {"video element not found", "video frame is not ready"}:
            raise CaptureValidationError(
                "YouTube video became unavailable before canvas capture "
                f"(reason={reason}, readyState={result.get('readyState')}, "
                f"source={result.get('sourceWidth', 0)}x"
                f"{result.get('sourceHeight', 0)})"
            )
        logger.warning("video canvas screenshot failed: %s", result)
        return False

    data_url = result.get("dataUrl") or ""
    try:
        _, encoded = data_url.split(",", 1)
        target_path.write_bytes(base64.b64decode(encoded))
    except Exception as exc:
        logger.warning("Failed to write canvas screenshot: %s", exc)
        return False

    logger.info(
        "Saved video canvas screenshot to %s (%sx%s from %sx%s)",
        target_path,
        result.get("width"),
        result.get("height"),
        result.get("sourceWidth"),
        result.get("sourceHeight"),
    )
    return True

##############################
# キャプチャ関数
##############################
def capture_embed_video(config: Dict[str, Any], file_prefix: str, is_test: bool = False) -> bool:
    """
    設定ディクショナリに基づいて YouTube ライブの埋め込みを開き、動画プレーヤー部分のスクリーンショットを取得して保存する。

    :param config: YAML/CLI をマージした設定（embed_url, chrome.*, capture.*, schedule.* 等を参照）
    :param file_prefix: ファイル名先頭 (`noon_`, `sunset_`, `video_` など既存規則を保持)
    :param is_test: True の場合は秒まで含む `video_YYYYMMDD_HHMMSS.png` を出力
    :return: 成功時 True, 失敗時 False
    """

    chrome_cfg = config.get("chrome", {})
    capture_cfg = config.get("capture", {})
    schedule_cfg = config.get("schedule", {})

    embed_url = config.get("embed_url", DEFAULT_EMBED_URL)
    driver_path = chrome_cfg.get("driver_path", DEFAULT_DRIVER_PATH)
    window_size = chrome_cfg.get("window_size", DEFAULT_WINDOW_SIZE)
    page_load_timeout = chrome_cfg.get("page_load_timeout_sec", DEFAULT_PAGE_LOAD_TIMEOUT)
    tzinfo = timezone(schedule_cfg.get("timezone", DEFAULT_TIMEZONE))

    output_dir = Path(capture_cfg.get("output_dir") or DEFAULT_OUTPUT_DIR)
    _ensure_output_dir(output_dir)
    max_files = _validate_positive("max_files", capture_cfg.get("max_files"))
    max_disk_mb = capture_cfg.get("max_disk_mb")
    if max_disk_mb is not None and max_disk_mb <= 0:
        raise ValueError("max_disk_mb must be > 0")

    max_retries = _validate_positive("max_retries", capture_cfg.get("max_retries")) or DEFAULT_MAX_RETRIES
    retry_delay = capture_cfg.get("retry_delay_sec", DEFAULT_RETRY_DELAY_SEC)
    if retry_delay is None or retry_delay < 0:
        retry_delay = DEFAULT_RETRY_DELAY_SEC
    live_edge_seek = capture_cfg.get("live_edge_seek", True) is not False
    live_edge_max_lag = _validate_non_negative_float(
        "live_edge_max_lag_sec",
        capture_cfg.get("live_edge_max_lag_sec", 30),
    )
    live_edge_require_verification = (
        capture_cfg.get("live_edge_require_verification", True) is not False
    )
    frame_validation = capture_cfg.get("frame_validation", True) is not False
    frame_min_luminance_stddev = _validate_non_negative_float(
        "frame_min_luminance_stddev",
        capture_cfg.get("frame_min_luminance_stddev", 10),
    )
    source_resolution = _source_resolution_settings(capture_cfg)

    for attempt in range(1, max_retries + 1):
        driver = None
        wrapper_httpd = None
        attempt_path = None
        try:
            options = Options()
            if chrome_cfg.get("headless", True):
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(f"--window-size={window_size}")
            options.add_argument("--no-sandbox")
            options.add_argument("--incognito")
            options.add_argument("--disk-cache-size=0")
            browser_binary = chrome_cfg.get("binary_path")
            if browser_binary:
                options.binary_location = browser_binary

            service = _build_chrome_service(driver_path)
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(page_load_timeout)

            fixed_embed_url = _normalize_youtube_embed_url(embed_url, prefer_nocookie=True)
            wrapper_html = _build_wrapper_html(fixed_embed_url)
            wrapper_httpd, wrapper_url = _start_local_html_server(wrapper_html)
            driver.get(wrapper_url)
            logger.info("Loaded wrapper page for capture (attempt %s)", attempt)

            try:
                iframe = WebDriverWait(driver, page_load_timeout).until(
                    lambda d: d.find_element(By.CSS_SELECTOR, "iframe#ytplayer")
                )
                driver.switch_to.frame(iframe)
                logger.debug("Switched into iframe context")
                try:
                    ref = driver.execute_script("return document.referrer || ''")
                    logger.debug("iframe referrer=%s", ref)
                except Exception:
                    logger.debug("Could not read iframe referrer")

                try:
                    WebDriverWait(driver, 20).until(
                        lambda d: d.execute_script(
                            "return Boolean(document.querySelector('video')) || document.body.innerText.includes('Error 153')"
                        )
                    )
                except TimeoutException:
                    logger.warning("Timed out waiting for YouTube video element")

                play_status = _try_play_youtube_video(driver)
                logger.debug("YouTube play status: %s", play_status)
                # Let the embedded-player controls settle before using the
                # direct LIVE state. The control is inspected exactly once,
                # immediately before the screenshot, so at most one catch-up
                # action can occur during a capture attempt.
                time.sleep(3)
                if "Error 153" in driver.page_source:
                    raise CaptureValidationError(
                        "YouTube player shows Error 153 (referer issue suspected)"
                    )
            finally:
                driver.switch_to.default_content()

            now = datetime.datetime.now(tzinfo)
            if is_test:
                filename = f"{file_prefix}{now.strftime('%Y%m%d_%H%M%S')}.png"
            else:
                filename = f"{file_prefix}{now.strftime('%Y%m%d_%H%M')}.png"
            target_path = output_dir / filename
            attempt_path = output_dir / f".{target_path.stem}.attempt-{os.getpid()}-{attempt}{target_path.suffix}"

            try:
                iframe_element = WebDriverWait(driver, 5).until(
                    lambda d: d.find_element(By.CSS_SELECTOR, "iframe#ytplayer")
                )
                driver.switch_to.frame(iframe_element)
                try:
                    if live_edge_seek:
                        live_status = _seek_youtube_live_edge(
                            driver,
                            live_edge_max_lag,
                        )
                        _log_live_edge_status(live_status)
                        live_edge_error = _live_edge_validation_error(
                            live_status,
                            live_edge_max_lag,
                            require_verification=live_edge_require_verification,
                        )
                        if live_edge_error:
                            raise CaptureValidationError(live_edge_error)
                    _hide_youtube_player_ui(driver)
                    frame_status = _wait_for_youtube_video_frame(
                        driver,
                        source_resolution["wait_sec"],
                        preferred_width=source_resolution["preferred_width"],
                        preferred_height=source_resolution["preferred_height"],
                    )
                    resolution_result = _source_resolution_result(
                        frame_status,
                        source_resolution,
                    )
                    resolution_log = (
                        "YouTube source resolution check: observed=%sx%s result=%s "
                        "preferred=%sx%s minimum=%sx%s"
                    )
                    resolution_log_args = (
                        frame_status.get("videoWidth", 0),
                        frame_status.get("videoHeight", 0),
                        resolution_result,
                        source_resolution["preferred_width"],
                        source_resolution["preferred_height"],
                        source_resolution["minimum_width"],
                        source_resolution["minimum_height"],
                    )
                    if resolution_result == "preferred":
                        logger.info(resolution_log, *resolution_log_args)
                    else:
                        logger.warning(resolution_log, *resolution_log_args)
                    resolution_error = _source_resolution_validation_error(
                        frame_status,
                        source_resolution,
                    )
                    if resolution_error:
                        raise CaptureValidationError(resolution_error)
                    if not _save_video_canvas_screenshot(
                        driver,
                        attempt_path,
                        minimum_source_width=source_resolution["minimum_width"],
                        minimum_source_height=source_resolution["minimum_height"],
                    ):
                        fallback_status = _video_frame_status(driver)
                        fallback_resolution_error = _source_resolution_validation_error(
                            fallback_status,
                            source_resolution,
                        )
                        if fallback_resolution_error:
                            raise CaptureValidationError(fallback_resolution_error)
                        video_element = driver.find_element(By.CSS_SELECTOR, "video.html5-main-video, video")
                        video_size = video_element.size
                        video_width = video_size.get("width", 0)
                        video_height = video_size.get("height", 0)
                        if video_width and video_height:
                            video_element.screenshot(str(attempt_path))
                            logger.info("Saved video element screenshot to %s", attempt_path)
                        else:
                            raise WebDriverException("video element size returned zero")
                except (NoSuchElementException, WebDriverException) as exc:
                    fallback_status = _video_frame_status(driver)
                    fallback_resolution_error = _source_resolution_validation_error(
                        fallback_status,
                        source_resolution,
                    )
                    if fallback_resolution_error:
                        raise CaptureValidationError(fallback_resolution_error) from exc
                    logger.warning("video screenshot failed (%s), falling back to iframe", exc)
                    driver.switch_to.default_content()
                    iframe_size = iframe_element.size
                    iframe_width = iframe_size.get("width", 0)
                    iframe_height = iframe_size.get("height", 0)
                    if not iframe_width or not iframe_height:
                        raise WebDriverException("iframe size returned zero")
                    try:
                        iframe_element.screenshot(str(attempt_path))
                        logger.info("Saved iframe screenshot to %s", attempt_path)
                    except WebDriverException as exc:
                        logger.warning("iframe screenshot failed (%s), falling back to full screen", exc)
                        driver.save_screenshot(str(attempt_path))
                finally:
                    driver.switch_to.default_content()
            except NoSuchElementException:
                raise CaptureValidationError("YouTube iframe is unavailable")

            if frame_validation:
                frame_error = _capture_image_validation_error(
                    attempt_path,
                    frame_min_luminance_stddev,
                )
                if frame_error:
                    raise CaptureValidationError(frame_error)

            _apply_optional_ai_validation(
                attempt_path,
                captured_at=datetime.datetime.now(tzinfo),
                config=capture_cfg.get("ai_validation", {}),
            )

            if target_path.exists():
                logger.warning("Capture already exists; preserving original: %s", target_path)
                attempt_path.unlink(missing_ok=True)
            else:
                os.replace(attempt_path, target_path)
                logger.info("Published validated capture to %s", target_path)

            _prune_old_files(output_dir, file_prefix, max_files, max_disk_mb)
            return True
        except (WebDriverException, TimeoutException, Exception) as exc:
            logger.exception("Capture attempt %s/%s failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(retry_delay)
        finally:
            if attempt_path is not None:
                try:
                    attempt_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary capture: %s", attempt_path)
            try:
                if driver:
                    driver.quit()
            finally:
                if wrapper_httpd:
                    wrapper_httpd.shutdown()
                    wrapper_httpd.server_close()
    logger.error("Capture failed after %s attempts", max_retries)
    return False

##############################
# サンセットキャプチャ用ジョブ
##############################
def schedule_next_sunset_capture(config: Dict[str, Any]):
    """YAML/CLI 設定に基づき、次回のサンセットキャプチャ (sunset_offset_minutes) を APScheduler に登録。"""
    schedule_cfg = config.get("schedule", {})
    tzinfo = timezone(schedule_cfg.get("timezone", DEFAULT_TIMEZONE))
    misfire_grace_time = max(
        1,
        int(
            schedule_cfg.get(
                "misfire_grace_time_sec",
                DEFAULT_SCHEDULE_MISFIRE_GRACE_SEC,
            )
        ),
    )
    now = datetime.datetime.now(tzinfo)

    s = sun(WEBCAM_LOCATION.observer, date=now.date(), tzinfo=tzinfo)
    sunset_time = s["sunset"]
    offset = schedule_cfg.get("sunset_offset_minutes", DEFAULT_SUNSET_OFFSET_MINUTES)
    desired_time = sunset_time - datetime.timedelta(minutes=offset)

    if desired_time < now:
        next_date = now.date() + datetime.timedelta(days=1)
        s = sun(WEBCAM_LOCATION.observer, date=next_date, tzinfo=tzinfo)
        sunset_time = s["sunset"]
        desired_time = sunset_time - datetime.timedelta(minutes=offset)

    msg = f"次のサンセットキャプチャは {desired_time} に予定されています。"
    logger.info(msg)
    # ログ出力先をファイルに切り替えている場合でも、画面で時刻を確認できるように print も行う
    print(msg)
    global_scheduler.add_job(
        sunset_capture_job,
        "date",
        run_date=desired_time,
        args=[config],
        id="sunset_capture",
        replace_existing=True,
        misfire_grace_time=misfire_grace_time,
        coalesce=True,
    )


def sunset_capture_job(config: Dict[str, Any]):
    logger.info("Running sunset capture job")
    prefix = config.get("schedule", {}).get("sunset_prefix", "sunset")
    capture_success = capture_embed_video(
        config,
        file_prefix=f"{prefix}_",
        is_test=False,
    )
    if not capture_success:
        logger.error("Sunset capture failed")
    schedule_next_sunset_capture(config)


##############################
# スケジューラー起動関数（固定時刻キャプチャ＋サンセットキャプチャ）
##############################
def fixed_capture_job(config: Dict[str, Any], job: Dict[str, Any]):
    logger.info(
        "Running fixed capture job id=%s time=%s prefix=%s",
        job["id"],
        job["time"],
        job["prefix"],
    )
    success = capture_embed_video(
        config,
        file_prefix=f"{job['prefix']}_",
        is_test=False,
    )
    if not success:
        logger.error("Fixed capture failed: %s", job["id"])


def schedule_capture_jobs(config: Dict[str, Any]):
    """固定時刻とサンセットのジョブを登録し、割り込み時は安全に停止する。"""
    schedule_cfg = config.get("schedule", {})
    tzinfo = timezone(schedule_cfg.get("timezone", DEFAULT_TIMEZONE))
    fixed_jobs = _configured_fixed_times(schedule_cfg)
    enable_sunset = schedule_cfg.get("enable_sunset", True)
    misfire_grace_time = max(
        1,
        int(
            schedule_cfg.get(
                "misfire_grace_time_sec",
                DEFAULT_SCHEDULE_MISFIRE_GRACE_SEC,
            )
        ),
    )

    if not fixed_jobs and not enable_sunset:
        logger.warning("No capture schedule is enabled")
        return

    global global_scheduler
    global_scheduler = BlockingScheduler(
        timezone=tzinfo,
        executors={"default": {"type": "threadpool", "max_workers": 1}},
    )

    for job in fixed_jobs:
        global_scheduler.add_job(
            fixed_capture_job,
            "cron",
            hour=job["hour"],
            minute=job["minute"],
            args=[config, job],
            id=f"fixed_capture_{job['id']}",
            replace_existing=True,
            misfire_grace_time=misfire_grace_time,
            coalesce=True,
        )

    if enable_sunset:
        schedule_next_sunset_capture(config)

    logger.info(
        "Scheduler started (fixed_times=%s, sunset=%s)",
        len(fixed_jobs),
        enable_sunset,
    )
    try:
        global_scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler interrupted; shutting down")
    finally:
        if global_scheduler and global_scheduler.state != STATE_STOPPED:
            global_scheduler.shutdown(wait=False)

##############################
# テスト用スケジューラー（2分ごとにキャプチャ）
##############################
def schedule_test_capture(config: Dict[str, Any]):
    """テストモード: interval_minutes ごとに capture_embed_video を呼ぶシンプルなスケジューラ。"""
    scheduler = BlockingScheduler()
    interval = config.get("test_mode", {}).get("interval_minutes", DEFAULT_TEST_INTERVAL_MINUTES)

    @scheduler.scheduled_job("interval", minutes=interval)
    def test_job():
        logger.info("Running test capture job (interval=%s minutes)", interval)
        capture_embed_video(config, file_prefix="video_", is_test=True)

    logger.info("Test mode scheduler started (interval %s minutes)", interval)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Test scheduler interrupted; stopping")
    finally:
        if scheduler.state != STATE_STOPPED:
            scheduler.shutdown(wait=False)

##############################
# main 関数（コマンドライン引数で各種設定を上書き可能）
##############################
def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="YouTube Live の映像を指定時刻に静止画保存するツール",
    )
    parser.add_argument("--config", type=str, help="YAML 形式の設定ファイルパス")
    parser.add_argument("--test", action="store_true",
                        help="テストモード: 起動直後にキャプチャを実行し、その後 interval 分ごとにキャプチャします。")
    parser.add_argument("--once", action="store_true",
                        help="単発キャプチャのみ実行して終了します。")
    parser.add_argument("--embed-url", type=str, help="上書き用の埋め込みURL")
    parser.add_argument("--driver-path", type=str, help="ChromeDriver のパスを上書き")
    parser.add_argument("--browser-binary", type=str, help="Chrome/Chromium 本体のパスを上書き")
    parser.add_argument("--window-size", type=str, help="ウィンドウサイズ (例 1920,1080)")
    parser.add_argument("--page-load-timeout", type=int, help="ページロードのタイムアウト秒数")
    parser.add_argument("--output-dir", type=str, help="キャプチャ保存先ディレクトリ")
    parser.add_argument("--max-retries", type=int, help="キャプチャリトライ回数")
    parser.add_argument("--retry-delay-sec", type=int, help="リトライ間隔（秒）")
    parser.add_argument("--max-files", type=int, help="prefix ごとの最大保存枚数")
    parser.add_argument("--max-disk-mb", type=float, help="出力ディレクトリのサイズ上限（MB）")
    ai_group = parser.add_mutually_exclusive_group()
    ai_group.add_argument(
        "--ai-validate",
        dest="ai_validation_enabled",
        action="store_true",
        help="OpenAIによる任意の画像検査を有効化",
    )
    ai_group.add_argument(
        "--no-ai-validate",
        dest="ai_validation_enabled",
        action="store_false",
        help="設定ファイルのAI画像検査を無効化",
    )
    parser.add_argument(
        "--ai-validation-mode",
        choices=sorted(ai_validation.VALID_MODES),
        help="AI判定をログだけにするか、保存条件として強制するか",
    )
    parser.add_argument(
        "--ai-model",
        help=f"AI画像検査モデル (既定: {ai_validation.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ai-timestamp-tolerance-sec",
        type=float,
        help="画像内時刻と撮影時刻の許容差（秒）",
    )
    timestamp_group = parser.add_mutually_exclusive_group()
    timestamp_group.add_argument(
        "--ai-require-timestamp",
        dest="ai_require_timestamp",
        action="store_true",
        help="AI強制モードで読み取り可能な現在時刻を必須にする",
    )
    timestamp_group.add_argument(
        "--ai-allow-missing-timestamp",
        dest="ai_require_timestamp",
        action="store_false",
        help="時刻表示のない正常画像をAI検査で許可する",
    )
    parser.add_argument("--log-file", type=str, help="ログを保存するファイルパス")
    parser.add_argument("--log-level", type=str, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="ログレベル")
    parser.add_argument("--sunset-offset", type=int, help="サンセットキャプチャのオフセット（分）")
    parser.add_argument("--latitude", type=float, help="ウェブカメラ設置場所の緯度")
    parser.add_argument("--longitude", type=float, help="ウェブカメラ設置場所の経度")
    parser.add_argument("--location-name", type=str, help="設置場所の名称")
    parser.add_argument("--region", type=str, help="地域名")
    parser.add_argument("--timezone", type=str, help="タイムゾーン (例: Asia/Tokyo)")
    parser.add_argument("--test-interval-minutes", type=int, help="テストモードのキャプチャ間隔（分）")
    parser.add_argument("--no-noon", dest="enable_noon", action="store_false",
                        help="正午キャプチャを無効化")
    parser.add_argument("--no-sunset", dest="enable_sunset", action="store_false",
                        help="サンセットキャプチャを無効化")
    parser.set_defaults(
        enable_noon=None,
        enable_sunset=None,
        ai_validation_enabled=None,
        ai_require_timestamp=None,
    )
    args = parser.parse_args(argv)

    if args.test and args.once:
        parser.error("--test と --once は同時に指定できません。")

    config = deepcopy(DEFAULT_CONFIG)
    config_path = _resolve_config_path(args.config)
    try:
        file_cfg = _load_config_file(config_path)
    except Exception as exc:
        parser.error(str(exc))
    _deep_merge(config, file_cfg)
    try:
        _apply_environment_overrides(config)
        _apply_cli_overrides(config, args)
        schedule_cfg = config.get("schedule", {})
        require_sunset_location = (
            not args.once
            and not args.test
            and bool(schedule_cfg.get("enable_sunset", True))
        )
        _validate_runtime_config(
            config,
            require_sunset_location=require_sunset_location,
        )
    except ValueError as exc:
        parser.error(str(exc))

    _setup_logging(config.get("logging", {}))
    global logger
    logger = logging.getLogger("ytlive_snapshot.capture")

    schedule_cfg = config.get("schedule", {})
    global WEBCAM_LOCATION
    if not args.once and not args.test and schedule_cfg.get("enable_sunset", True):
        WEBCAM_LOCATION = LocationInfo(
            name=schedule_cfg.get("location_name", DEFAULT_LOCATION_NAME),
            region=schedule_cfg.get("region", DEFAULT_REGION),
            timezone=schedule_cfg.get("timezone", DEFAULT_TIMEZONE),
            latitude=schedule_cfg["latitude"],
            longitude=schedule_cfg["longitude"],
        )
    else:
        WEBCAM_LOCATION = None

    if args.once:
        success = capture_embed_video(config, file_prefix="video_", is_test=True)
        sys.exit(0 if success else 1)

    if args.test:
        logger.info("Test mode: capturing immediately before scheduler loop")
        capture_embed_video(config, file_prefix="video_", is_test=True)
        schedule_test_capture(config)
    else:
        schedule_capture_jobs(config)

if __name__ == "__main__":
    main()
