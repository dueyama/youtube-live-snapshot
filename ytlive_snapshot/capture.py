import argparse
import base64
import datetime
import logging
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
        "live_edge_offset_sec": 2,
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
    if numeric_value < 0:
        raise ValueError(f"{name} must be >= 0")
    return numeric_value


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
    - autoplay/mute/playsinline/controls=0 を付与（ミュート自動再生を狙い、UI を抑制）
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
    qs.setdefault("controls", "0")
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
    driver.set_script_timeout(8)
    status = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const video = document.querySelector('video');
        if (!video) {
          done({hasVideo: false});
          return;
        }
        video.muted = true;
        video.setAttribute('playsinline', '');
        Promise.resolve(video.play())
          .then(() => done({
            hasVideo: true,
            playRequested: true,
            paused: video.paused,
            readyState: video.readyState,
            currentTime: video.currentTime,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight
          }))
          .catch((error) => done({
            hasVideo: true,
            playRequested: false,
            error: String(error),
            paused: video.paused,
            readyState: video.readyState,
            currentTime: video.currentTime,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight
          }));
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
    edge_offset_sec: float,
) -> Dict[str, Any]:
    """YouTube live DVR が遅れた位置で始まった場合、保存前にライブ端へ寄せる。"""
    driver.set_script_timeout(12)
    return driver.execute_async_script(
        """
        const maxLagSec = Number(arguments[0]);
        const edgeOffsetSec = Number(arguments[1]);
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
        let targetTime = null;
        if (before.hasSeekToLiveHead && before.isLive !== false) {
          action = 'seekToLiveHead';
        } else if (
          before.hasPlayerSeekTo &&
          before.isLive !== false &&
          Number.isFinite(before.playerDuration) &&
          before.playerDuration > 0 &&
          (
            !Number.isFinite(before.playerLagSec) ||
            before.playerLagSec > maxLagSec
          )
        ) {
          action = 'player.seekTo';
          targetTime = Math.max(0, before.playerDuration - Math.max(0, edgeOffsetSec));
        }

        if (action) {
          let settled = false;
          let timeoutId = null;
          const hasDrawableFrame = () => (
            video.readyState >= 2 &&
            video.videoWidth > 0 &&
            video.videoHeight > 0
          );
          const finishPlayerSeek = (reason) => {
            if (settled) return;
            settled = true;
            clearTimeout(timeoutId);
            video.removeEventListener('seeked', onPlayerSeeked);
            video.removeEventListener('canplay', onPlayerCanPlay);
            video.removeEventListener('timeupdate', onPlayerTimeUpdate);
            const after = baseStatus();
            done({
              ...before,
              seeked: true,
              action,
              targetTime,
              finishReason: reason,
              afterCurrentTime: after.currentTime,
              afterLagSec: after.lagSec,
              afterPlayerCurrentTime: after.playerCurrentTime,
              afterPlayerDuration: after.playerDuration,
              afterPlayerLagSec: after.playerLagSec,
              afterPaused: after.paused,
              afterReadyState: after.readyState,
              afterVideoWidth: after.videoWidth,
              afterVideoHeight: after.videoHeight
            });
          };
          const onPlayerSeeked = () => {
            if (hasDrawableFrame()) finishPlayerSeek('seeked');
          };
          const onPlayerCanPlay = () => {
            if (hasDrawableFrame()) finishPlayerSeek('canplay');
          };
          const onPlayerTimeUpdate = () => {
            if (hasDrawableFrame()) finishPlayerSeek('timeupdate');
          };
          video.addEventListener('seeked', onPlayerSeeked);
          video.addEventListener('canplay', onPlayerCanPlay);
          video.addEventListener('timeupdate', onPlayerTimeUpdate);
          timeoutId = setTimeout(() => finishPlayerSeek('timeout'), 6000);
          video.muted = true;
          video.setAttribute('playsinline', '');
          try {
            if (action === 'seekToLiveHead') {
              player.seekToLiveHead();
            } else {
              player.seekTo(targetTime, true);
            }
            if (typeof player.playVideo === 'function') {
              player.playVideo();
            } else {
              Promise.resolve(video.play()).catch(() => null);
            }
          } catch (error) {
            finishPlayerSeek(String(error));
          }
          return;
        }

        if (!before.hasSeekableRange || !Number.isFinite(before.lagSec)) {
          done({...before, seeked: false, reason: 'no seekable live range'});
          return;
        }
        if (before.lagSec <= maxLagSec) {
          video.muted = true;
          video.setAttribute('playsinline', '');
          Promise.resolve(video.play()).catch(() => null);
          done({...before, seeked: false, reason: 'already near live edge'});
          return;
        }

        targetTime = Math.max(
          before.rangeStart,
          before.liveEdge - Math.max(0, edgeOffsetSec)
        );
        let settled = false;
        let timeoutId = null;
        const hasDrawableFrame = () => (
          video.readyState >= 2 &&
          video.videoWidth > 0 &&
          video.videoHeight > 0
        );
        const finish = (reason) => {
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
            targetTime,
            finishReason: reason,
            afterCurrentTime: after.currentTime,
            afterLagSec: after.lagSec,
            afterPaused: after.paused,
            afterReadyState: after.readyState,
            afterVideoWidth: after.videoWidth,
            afterVideoHeight: after.videoHeight
          });
        };
        const onSeeked = () => {
          if (hasDrawableFrame()) finish('seeked');
        };
        const onCanPlay = () => {
          if (hasDrawableFrame() && Math.abs(video.currentTime - targetTime) < 5) {
            finish('canplay');
          }
        };
        const onTimeUpdate = () => {
          if (hasDrawableFrame() && Math.abs(video.currentTime - targetTime) < 5) {
            finish('timeupdate');
          }
        };

        video.addEventListener('seeked', onSeeked);
        video.addEventListener('canplay', onCanPlay);
        video.addEventListener('timeupdate', onTimeUpdate);
        timeoutId = setTimeout(() => finish('timeout'), 5000);
        video.muted = true;
        video.setAttribute('playsinline', '');
        try {
          video.currentTime = targetTime;
          Promise.resolve(video.play()).catch(() => null);
        } catch (error) {
          finish(String(error));
        }
        """,
        max_lag_sec,
        edge_offset_sec,
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
        action = status.get("action", "video.currentTime")
        if action == "seekToLiveHead":
            logger.info(
                "Requested YouTube live head: reason=%s before_player_lag=%s after_player_lag=%s",
                status.get("finishReason"),
                _format_optional_seconds(status.get("playerLagSec")),
                _format_optional_seconds(status.get("afterPlayerLagSec")),
            )
        else:
            logger.info(
                "Seeked YouTube video near live edge: action=%s before_lag=%s after_lag=%s target=%s reason=%s",
                action,
                _format_optional_seconds(status.get("lagSec")),
                _format_optional_seconds(status.get("afterLagSec")),
                _format_optional_seconds(status.get("targetTime")),
                status.get("finishReason"),
            )
    else:
        logger.debug("YouTube live edge status: %s", status)


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


def _wait_for_youtube_video_frame(driver, timeout_sec: int = 8) -> Dict[str, Any]:
    def _ready_status(d):
        status = _video_frame_status(d)
        return status if status.get("ready") else False

    try:
        return WebDriverWait(driver, timeout_sec, poll_frequency=0.25).until(
            _ready_status
        )
    except TimeoutException:
        return _video_frame_status(driver)


def _save_video_canvas_screenshot(driver, target_path: Path) -> bool:
    """YouTube UI レイヤーを含めず、video の現在フレームだけを PNG 保存する。"""
    driver.set_script_timeout(8)
    result = driver.execute_async_script(
        """
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
        """
    )

    if not result.get("ok"):
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
    live_edge_offset = _validate_non_negative_float(
        "live_edge_offset_sec",
        capture_cfg.get("live_edge_offset_sec", 2),
    )

    for attempt in range(1, max_retries + 1):
        driver = None
        wrapper_httpd = None
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

                ui_status = _hide_youtube_player_ui(driver)
                logger.debug("YouTube UI status before play: %s", ui_status)
                play_status = _try_play_youtube_video(driver)
                logger.debug("YouTube play status: %s", play_status)
                if live_edge_seek:
                    live_status = _seek_youtube_live_edge(
                        driver,
                        live_edge_max_lag,
                        live_edge_offset,
                    )
                    _log_live_edge_status(live_status)

                time.sleep(3)
                ui_status = _hide_youtube_player_ui(driver)
                logger.debug("YouTube UI status before screenshot: %s", ui_status)
                if "Error 153" in driver.page_source:
                    logger.warning("YouTube player shows Error 153 (referer issue suspected)")
            finally:
                driver.switch_to.default_content()

            now = datetime.datetime.now(tzinfo)
            if is_test:
                filename = f"{file_prefix}{now.strftime('%Y%m%d_%H%M%S')}.png"
            else:
                filename = f"{file_prefix}{now.strftime('%Y%m%d_%H%M')}.png"
            target_path = output_dir / filename

            try:
                iframe_element = WebDriverWait(driver, 5).until(
                    lambda d: d.find_element(By.CSS_SELECTOR, "iframe#ytplayer")
                )
                driver.switch_to.frame(iframe_element)
                try:
                    _hide_youtube_player_ui(driver)
                    if live_edge_seek:
                        live_status = _seek_youtube_live_edge(
                            driver,
                            live_edge_max_lag,
                            live_edge_offset,
                        )
                        _log_live_edge_status(live_status)
                    frame_status = _wait_for_youtube_video_frame(driver)
                    if frame_status.get("ready"):
                        logger.debug("YouTube video frame ready before canvas: %s", frame_status)
                    else:
                        logger.warning(
                            "YouTube video frame still not ready before canvas: %s",
                            frame_status,
                        )
                    if not _save_video_canvas_screenshot(driver, target_path):
                        video_element = driver.find_element(By.CSS_SELECTOR, "video.html5-main-video, video")
                        video_size = video_element.size
                        video_width = video_size.get("width", 0)
                        video_height = video_size.get("height", 0)
                        if video_width and video_height:
                            video_element.screenshot(str(target_path))
                            logger.info("Saved video element screenshot to %s", target_path)
                        else:
                            raise WebDriverException("video element size returned zero")
                except (NoSuchElementException, WebDriverException) as exc:
                    logger.warning("video screenshot failed (%s), falling back to iframe", exc)
                    driver.switch_to.default_content()
                    iframe_size = iframe_element.size
                    iframe_width = iframe_size.get("width", 0)
                    iframe_height = iframe_size.get("height", 0)
                    if not iframe_width or not iframe_height:
                        raise WebDriverException("iframe size returned zero")
                    try:
                        iframe_element.screenshot(str(target_path))
                        logger.info("Saved iframe screenshot to %s", target_path)
                    except WebDriverException as exc:
                        logger.warning("iframe screenshot failed (%s), falling back to full screen", exc)
                        driver.save_screenshot(str(target_path))
                finally:
                    driver.switch_to.default_content()
            except NoSuchElementException:
                logger.warning("iframe not found; saving full screen snapshot")
                driver.save_screenshot(str(target_path))

            _prune_old_files(output_dir, file_prefix, max_files, max_disk_mb)
            return True
        except (WebDriverException, TimeoutException, Exception) as exc:
            logger.exception("Capture attempt %s/%s failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(retry_delay)
        finally:
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
    parser.set_defaults(enable_noon=None, enable_sunset=None)
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
