#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Live Snapshot renderer v3.1.0
主な変更点:
 - YAML 設定 (`--config`, `--categories-config`) でカテゴリや入出力を外部化し、複数レイアウトを管理
 - 月次カレンダーを一時 PNG に退避しつつ縦連結することで Raspberry Pi でもメモリを抑制
 - 欠損日をプレースホルダー表示し、`--min-coverage` しきい値を満たさない場合は終了コード 2 で検知
 - `--month/--months`, `--prefix`, `--category-id`, `--dry-run` など CLI フィルタリングを拡充
 - logging + exit code 整備で cron/systemd から監視しやすくし、PDF 生成は ReportLab の有無に応じて安全にスキップ

使い方:
  ytlive-snapshot render --config config/render.yaml
"""
import argparse
import calendar
import glob
import logging
import math
import os
import re
import shutil
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__

try:
    import yaml
except Exception:
    yaml = None

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DEFAULT_DATA_DIR = Path(
    os.path.expanduser(
        os.getenv("YTLIVE_SNAPSHOT_DATA_DIR")
        or os.getenv("CAPTUREPY_DATA_DIR", ".")
    )
).resolve()

# optional imports
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import ImageReader
except Exception:
    canvas = None

try:
    import jpholiday
    _is_holiday = jpholiday.is_holiday
except Exception:
    def _is_holiday(_d):
        return False

# -------------------- 設定 --------------------
BACKGROUND_COLOR = (250, 249, 246)
GRID_COLOR = (180, 180, 180)
SUNDAY_TEXT_COLOR = "red"
SATURDAY_TEXT_COLOR = "blue"
HOLIDAY_BG_COLOR = (255, 236, 236)
SAT_BG_COLOR = (236, 242, 255)

BORDER_COLOR = BACKGROUND_COLOR
BORDER_WIDTH = 0
CORNER_RADIUS = 16
INNER_SHADOW_COLOR = (120, 120, 120)
INNER_SHADOW_WIDTH = 1
TILE_MARGIN = 4
DATE_OFFSET = 6
DATE_OFFSET_Y = DATE_OFFSET + 15
UPSCALE_FACTOR = 4  # 高解像度で角丸を作るための倍率
SCALE_FACTOR = 2   # 全体を2倍で出力したければここで変える
SAVE_JPEG = False

def _default_font_path() -> str:
    """ソース実行とインストール済みCLIの両方で同梱フォントを探す。"""
    filename = "NotoSansCJKjp-Regular.otf"
    source_path = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "fonts"
        / filename
    )
    installed_path = (
        Path(sys.prefix)
        / "share"
        / "youtube-live-snapshot"
        / "fonts"
        / filename
    )
    return str(source_path if source_path.exists() else installed_path)


# デフォルトフォント (ユーザー環境に合わせてオプションで上書き)
DEFAULT_FONT_PATH = _default_font_path()

# 基準セルサイズ (この値を基準にスケーリング)
BASE_CELL_WIDTH = 140
BASE_CELL_HEIGHT = 140
TITLE_HEIGHT = 90
WEEKDAY_HEADER_HEIGHT = 55
TITLE_FONT_SIZE = 70
WEEKDAY_FONT_SIZE = 36
DAY_FONT_SIZE = 30

PLACEHOLDER_BG = (242, 241, 238)
PLACEHOLDER_TEXT_COLOR = (185, 183, 178)
SUPPORTED_LOCALES = ("ja", "en")

DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {"id": "noon_sea", "prefix": "noon", "gravity": "south", "crop_offset": 170,
     "mode": "thumbnail", "output": "{year}_noon_sea_calendar.png"},
    {"id": "noon_sea_avg", "prefix": "noon", "gravity": "south", "crop_offset": 170,
     "mode": "avg", "output": "{year}_noon_sea_calendar-c.png"},
    {"id": "noon_sky", "prefix": "noon", "gravity": "north", "crop_offset": 200,
     "mode": "thumbnail", "output": "{year}_noon_sky_calendar.png"},
    {"id": "noon_sky_avg", "prefix": "noon", "gravity": "north", "crop_offset": 200,
     "mode": "avg", "output": "{year}_noon_sky_calendar-c.png"},
    {"id": "sunset_sea", "prefix": "sunset", "gravity": "south", "crop_offset": 170,
     "mode": "thumbnail", "output": "{year}_sunset_sea_calendar.png"},
    {"id": "sunset_sea_avg", "prefix": "sunset", "gravity": "south", "crop_offset": 170,
     "mode": "avg", "output": "{year}_sunset_sea_calendar-c.png"},
    {"id": "sunset_sky", "prefix": "sunset", "gravity": "north", "crop_offset": 200,
     "mode": "thumbnail", "output": "{year}_sunset_sky_calendar.png"},
    {"id": "sunset_sky_avg", "prefix": "sunset", "gravity": "north", "crop_offset": 200,
     "mode": "avg", "output": "{year}_sunset_sky_calendar-c.png"},
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "year": 2025,
    "source_year": None,
    "locale": "ja",
    "input_dir": str(DEFAULT_DATA_DIR / "captures"),
    "output_dir": str(DEFAULT_DATA_DIR / "out"),
    "font_path": DEFAULT_FONT_PATH,
    "cell_width": BASE_CELL_WIDTH * SCALE_FACTOR,
    "cell_height": BASE_CELL_HEIGHT * SCALE_FACTOR,
    "save_jpeg": False,
    "min_coverage": 0.0,
    "require_pdf": False,
    "skip_annual": False,
    "include_empty_months": False,
    "placeholder": {
        "enabled": True,
        "text": None,
    },
    "temp_dir": None,
    "retain_temp_files": False,
    "logging": {
        "level": "INFO",
        "log_file": None,
    },
    "categories": DEFAULT_CATEGORIES,
}

logger = logging.getLogger("ytlive_snapshot.render")
CATEGORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CAPTURE_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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


def _load_yaml(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML がインストールされていません。requirements.txt をインストール済みか確認してください。")
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} の内容はマッピングである必要があります。")
    return data


def _setup_logging(log_cfg: Dict[str, Any]):
    level_name = (log_cfg.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers = []
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    log_file = log_cfg.get("log_file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=level, handlers=handlers)


def _calendar_labels(locale_name: str, year: int, month: int) -> Dict[str, Any]:
    """OSのlocale設定に依存せず、カレンダー表示用ラベルを返す。"""
    if locale_name == "ja":
        return {
            "title": f"{year}年{month}月",
            "weekdays": ["日", "月", "火", "水", "木", "金", "土"],
            "placeholder": "データなし",
        }
    if locale_name == "en":
        return {
            "title": f"{calendar.month_name[month]} {year}",
            "weekdays": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "placeholder": "No data",
        }
    raise ValueError(
        f"locale must be one of {', '.join(SUPPORTED_LOCALES)}: {locale_name!r}"
    )


def _load_categories_list(data: Any, source: str) -> List[Dict[str, Any]]:
    if not data:
        return []
    if isinstance(data, dict) and "categories" in data:
        data = data["categories"]
    if not isinstance(data, list):
        raise ValueError(f"{source}: categories はリスト形式である必要があります。")
    normalized = []
    seen_ids = set()
    seen_outputs = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"{source}: 各カテゴリはマッピング形式で指定してください。")
        required = {"id", "prefix", "gravity", "crop_offset", "mode", "output"}
        missing = required - set(item.keys())
        if missing:
            raise ValueError(f"{source}: {item.get('id','<no-id>')} の必須キー {missing} が不足しています。")
        category_id = item["id"]
        if not isinstance(category_id, str) or not CATEGORY_ID_RE.fullmatch(category_id):
            raise ValueError(
                f"{source}: category id は英数字で始まる安全な名前にしてください: {category_id!r}"
            )
        if category_id in seen_ids:
            raise ValueError(f"{source}: category id が重複しています: {category_id}")
        if not isinstance(item["prefix"], str) or not CAPTURE_PREFIX_RE.fullmatch(item["prefix"]):
            raise ValueError(f"{source}: {category_id} の prefix が不正です: {item['prefix']!r}")
        if item["gravity"] not in {"north", "south"}:
            raise ValueError(f"{source}: {category_id} の gravity は north または south にしてください。")
        if item["mode"] not in {"thumbnail", "resize", "avg"}:
            raise ValueError(f"{source}: {category_id} の mode が不正です: {item['mode']}")

        output_name = item["output"]
        output_path = Path(output_name) if isinstance(output_name, str) else None
        if (
            output_path is None
            or output_path.is_absolute()
            or output_path.name != output_name
            or output_name in {".", ".."}
        ):
            raise ValueError(
                f"{source}: {category_id} の output はディレクトリを含まないファイル名にしてください。"
            )
        if output_name in seen_outputs:
            raise ValueError(f"{source}: output が重複しています: {output_name}")

        seen_ids.add(category_id)
        seen_outputs.add(output_name)
        normalized.append(dict(item))
    return normalized


def _category_temp_dir(temp_root: Path, category_id: str) -> Path:
    """カテゴリ用一時ディレクトリが必ず temp_root 直下になるよう保証する。"""
    root = temp_root.resolve()
    candidate = (root / category_id).resolve()
    if candidate.parent != root:
        raise ValueError(f"unsafe category id for temporary directory: {category_id!r}")
    return candidate


def _filter_categories(
    categories: Sequence[Dict[str, Any]],
    allowed_prefixes: Optional[Sequence[str]] = None,
    allowed_ids: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    filtered = []
    for cat in categories:
        if allowed_prefixes and cat.get("prefix") not in allowed_prefixes:
            continue
        if allowed_ids and cat.get("id") not in allowed_ids:
            continue
        filtered.append(cat)
    return list(filtered)


def _apply_cli_overrides(config: Dict[str, Any], args: argparse.Namespace):
    if getattr(args, "year", None) is not None:
        config["year"] = args.year
    if getattr(args, "source_year", None) is not None:
        config["source_year"] = args.source_year
    if getattr(args, "locale", None) is not None:
        config["locale"] = args.locale
    if getattr(args, "input_dir", None):
        config["input_dir"] = args.input_dir
    if getattr(args, "output_dir", None):
        config["output_dir"] = args.output_dir
    if getattr(args, "font_path", None):
        config["font_path"] = args.font_path
    if getattr(args, "cell_width", None) is not None:
        config["cell_width"] = args.cell_width
    if getattr(args, "cell_height", None) is not None:
        config["cell_height"] = args.cell_height
    if getattr(args, "save_jpeg", None) is not None:
        config["save_jpeg"] = args.save_jpeg
    if getattr(args, "min_coverage", None) is not None:
        config["min_coverage"] = args.min_coverage
    if getattr(args, "require_pdf", None) is not None:
        config["require_pdf"] = args.require_pdf
    if getattr(args, "skip_annual", None) is not None:
        config["skip_annual"] = args.skip_annual
    if getattr(args, "include_empty_months", None) is not None:
        config["include_empty_months"] = args.include_empty_months
    if getattr(args, "log_file", None):
        config.setdefault("logging", {})["log_file"] = args.log_file
    if getattr(args, "log_level", None):
        config.setdefault("logging", {})["level"] = args.log_level
    if getattr(args, "temp_dir", None):
        config["temp_dir"] = args.temp_dir
    if getattr(args, "retain_temp_files", None) is not None:
        config["retain_temp_files"] = args.retain_temp_files
    if getattr(args, "placeholder_text", None):
        config.setdefault("placeholder", {})["text"] = args.placeholder_text
    if getattr(args, "disable_placeholder", None):
        config.setdefault("placeholder", {})["enabled"] = False


def _parse_month_filters(single: Optional[int], multi: Optional[str]) -> Optional[List[int]]:
    months: List[int] = []
    if single:
        months.append(single)
    if multi:
        for token in multi.split(","):
            token = token.strip()
            if not token:
                continue
            value = int(token)
            if value < 1 or value > 12:
                raise ValueError("月は 1-12 の範囲で指定してください。")
            months.append(value)
    dedup = sorted(set(months))
    return dedup or None
# -----------------------------------------------
def parse_filename(filepath):
    basename = os.path.basename(filepath)
    regex = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9_-]*)_(\d{8})_(\d{4})\.(png|jpg|jpeg)$",
        re.IGNORECASE,
    )
    m = regex.match(basename)
    if not m:
        return None
    prefix = m.group(1)
    date_str = m.group(2)
    time_str = m.group(3)
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
    except Exception as e:
        print(f"[parse] 日付解析エラー ({basename}): {e}")
        return None
    year, month, day = dt.year, dt.month, dt.day
    time_int = int(time_str)
    return (prefix, year, month, day, time_int)

def crop_image(img, gravity, crop_width, crop_height, offset):
    w, h = img.size
    x = max(0, (w - crop_width) // 2)
    if gravity == "north":
        y = max(0, int(offset))
    elif gravity == "south":
        y = max(0, h - crop_height - int(offset))
    else:
        raise ValueError("gravity は 'north' または 'south' でなければなりません")
    if x < 0 or y < 0 or (x + crop_width) > w or (y + crop_height) > h:
        raise ValueError(f"画像サイズ {img.size} が小さすぎます (crop {crop_width}x{crop_height} at {x},{y})")
    return img.crop((x, y, x + crop_width, y + crop_height))

def process_image_file_custom(filepath, gravity, crop_offset, cell_w, cell_h, mode="thumbnail"):
    # Keep the legacy crop behavior: use the fixed base cell size when calculating
    # vertical gravity and offsets, independently of the final output scale.
    crop_width = BASE_CELL_WIDTH
    crop_height = BASE_CELL_HEIGHT

    try:
        img = Image.open(filepath)
    except Exception as e:
        print(f"[open] 画像のオープンに失敗しました: {filepath} ({e})")
        return None
    try:
        cropped = crop_image(img, gravity, crop_width, crop_height, crop_offset)
    except Exception as e:
        print(f"[crop] クロップに失敗: {filepath} ({e})")
        return None

    if mode == "thumbnail":
        # thumbnail はアスペクト比を維持しつつ最大領域に収める
        cropped.thumbnail((crop_width, crop_height), Image.LANCZOS)
        return cropped
    elif mode == "resize":
        return cropped.resize((crop_width, crop_height), Image.LANCZOS)
    elif mode == "avg":
        avg_img = cropped.resize((1, 1), Image.LANCZOS)
        return avg_img.resize((crop_width, crop_height), Image.NEAREST)
    else:
        raise ValueError("mode は 'thumbnail', 'resize', または 'avg' としてください")

def process_category_images(prefix, gravity, crop_offset, source_year, output_year, mode, input_dir, cell_w, cell_h,
                            allowed_months: Optional[Sequence[int]] = None):
    pattern = os.path.join(input_dir, f"{prefix}_????????_????.*")
    files = sorted(glob.glob(pattern, recursive=False))
    groups = {}
    count = 0
    for f in files:
        parsed = parse_filename(f)
        if not parsed:
            continue
        f_prefix, capture_year, month, day, time_int = parsed
        if capture_year != source_year:
            continue
        if allowed_months and month not in allowed_months:
            continue
        try:
            datetime(output_year, month, day)
        except ValueError:
            logger.warning(
                "%s は出力年 %s に存在しない日付のためスキップします: %04d-%02d-%02d",
                f,
                output_year,
                capture_year,
                month,
                day,
            )
            continue
        img = process_image_file_custom(f, gravity, crop_offset, cell_w, cell_h, mode)
        if img is None:
            continue
        key = (output_year, month)
        if key not in groups:
            groups[key] = {}
        # keep the later-time image of the day
        if day in groups[key]:
            existing_time, _ = groups[key][day]
            if time_int > existing_time:
                groups[key][day] = (time_int, img)
        else:
            groups[key][day] = (time_int, img)
        count += 1
    print(f"[{prefix}] 入力年 {source_year} → 出力年 {output_year} のファイル {count} 件を処理しました。")
    monthly_images = {}
    for key, day_dict in groups.items():
        images_by_day = {day: img for day, (t, img) in day_dict.items()}
        monthly_images[key] = images_by_day
    return monthly_images

def create_monthly_calendar_image(year, month, images_by_day,
                                  cell_width=BASE_CELL_WIDTH, cell_height=BASE_CELL_HEIGHT,
                                  title_height=TITLE_HEIGHT, weekday_header_height=WEEKDAY_HEADER_HEIGHT,
                                  draw_grid=False, draw_titles=True, draw_weekdays=True, draw_dates=True,
                                  font_path=DEFAULT_FONT_PATH,
                                  locale_name="ja",
                                  placeholder_enabled=True,
                                  placeholder_text=None):
    """
    cell_width/cell_height は "最終的にそのセルが持つピクセル幅" を受け取る。
    関数内でフォントやマージンは cell_width/BASE_CELL_WIDTH を基準にスケールされる。
    """
    labels = _calendar_labels(locale_name, year, month)

    # スケール係数 (セル基準)
    scale = float(cell_width) / float(BASE_CELL_WIDTH)
    # カレンダー行列
    cal = calendar.Calendar(firstweekday=calendar.SUNDAY)
    cal_matrix = cal.monthdayscalendar(year, month)
    while len(cal_matrix) < 6:
        cal_matrix.append([0] * 7)
    num_weeks = 6

    canvas_width = int(7 * cell_width)
    canvas_height = int(title_height + weekday_header_height + num_weeks * cell_height)
    cal_img = Image.new("RGB", (canvas_width, canvas_height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(cal_img)

    # フォントサイズはスケールで決定
    try:
        title_font = ImageFont.truetype(font_path, int(TITLE_FONT_SIZE * scale))
        weekday_font = ImageFont.truetype(font_path, int(WEEKDAY_FONT_SIZE * scale))
        day_font = ImageFont.truetype(font_path, int(DAY_FONT_SIZE * scale))
        placeholder_font = ImageFont.truetype(font_path, max(10, int(DAY_FONT_SIZE * scale * 0.45)))
    except Exception as e:
        print(f"[font] フォントロードに失敗 ({e})。デフォルトフォントを使用します。")
        title_font = ImageFont.load_default()
        weekday_font = ImageFont.load_default()
        day_font = ImageFont.load_default()
        placeholder_font = day_font

    # タイトル
    if draw_titles:
        title_text = labels["title"]
        title_bbox = title_font.getbbox(title_text)
        title_width = title_bbox[2] - title_bbox[0]
        title_height_px = title_bbox[3] - title_bbox[1]
        title_x = (canvas_width - title_width) / 2
        title_y = (title_height - title_height_px) / 2
        draw.text((title_x, title_y), title_text, fill="black", font=title_font)

    # 曜日ヘッダー
    if draw_weekdays:
        weekdays = labels["weekdays"]
        for i, day_name in enumerate(weekdays):
            bbox = weekday_font.getbbox(day_name)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = i * cell_width + (cell_width - tw) / 2
            y = title_height + (weekday_header_height - th) / 2
            if i == 0:
                fill_color = SUNDAY_TEXT_COLOR
            elif i == 6:
                fill_color = SATURDAY_TEXT_COLOR
            else:
                fill_color = "black"
            draw.text((x, y), day_name, fill=fill_color, font=weekday_font)

    # セルごとに画像を貼る
    tile_margin = int(TILE_MARGIN * scale)
    date_offset = int(DATE_OFFSET * scale)
    date_offset_y = int(DATE_OFFSET_Y * scale)

    for row_index, week in enumerate(cal_matrix):
        for col_index, day in enumerate(week):
            cell_x = int(col_index * cell_width)
            cell_y = int(title_height + weekday_header_height + row_index * cell_height)
            if day == 0:
                continue
            day_date = datetime(year, month, day)
            is_holiday = _is_holiday(day_date)
            # Python weekday: Mon=0 ... Sun=6
            weekday_idx = day_date.weekday()
            if is_holiday or weekday_idx == 6:
                day_fill = SUNDAY_TEXT_COLOR
            elif weekday_idx == 5:
                day_fill = SATURDAY_TEXT_COLOR
            else:
                day_fill = "black"

            inner_x = cell_x + tile_margin
            inner_y = cell_y + tile_margin
            tile_w = int(cell_width - 2 * tile_margin)
            tile_h = int(cell_height - 2 * tile_margin)

            if day in images_by_day:
                if is_holiday or weekday_idx == 6:
                    tile_bg = HOLIDAY_BG_COLOR
                elif weekday_idx == 5:
                    tile_bg = SAT_BG_COLOR
                else:
                    tile_bg = BACKGROUND_COLOR

                # アップスケールで角丸マスクを作る
                u_w = max(1, int(tile_w * UPSCALE_FACTOR))
                u_h = max(1, int(tile_h * UPSCALE_FACTOR))

                img = images_by_day[day]
                img_rgba = img.convert("RGBA")
                # resize to high-res for smooth corners
                if img_rgba.size != (u_w, u_h):
                    img_rgba = img_rgba.resize((u_w, u_h), Image.LANCZOS)

                # mask を作る
                mask_u = Image.new("L", (u_w, u_h), 0)
                mask_draw = ImageDraw.Draw(mask_u)
                mask_draw.rounded_rectangle((0, 0, u_w - 1, u_h - 1), radius=int(CORNER_RADIUS * UPSCALE_FACTOR * scale), fill=255)
                # alpha を付与
                img_rgba.putalpha(mask_u)

                # 背景タイル (RGBA)
                tile_u = Image.new("RGBA", (u_w, u_h), tile_bg + (255,))
                tile_u.paste(img_rgba, (0, 0), img_rgba)

                # ダウンサンプル
                tile = tile_u.resize((tile_w, tile_h), Image.LANCZOS)
                # RGB にしてから貼る
                cal_img.paste(tile.convert("RGB"), (inner_x, inner_y))
            elif placeholder_enabled:
                placeholder = Image.new("RGB", (tile_w, tile_h), PLACEHOLDER_BG)
                placeholder_draw = ImageDraw.Draw(placeholder)
                text = placeholder_text or labels["placeholder"]
                bbox = placeholder_draw.textbbox((0, 0), text, font=placeholder_font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
                placeholder_draw.text(
                    ((tile_w - text_w) / 2, (tile_h - text_h) / 2),
                    text,
                    fill=PLACEHOLDER_TEXT_COLOR,
                    font=placeholder_font,
                )
                cal_img.paste(placeholder, (inner_x, inner_y))

            # 日付番号
            if draw_dates:
                date_x = inner_x + date_offset
                date_y = inner_y + date_offset_y
                draw.text((date_x, date_y), str(day), fill=day_fill, font=day_font)

    # グリッド
    if draw_grid:
        for i in range(8):
            x = i * cell_width
            draw.line([(x, title_height + weekday_header_height), (x, canvas_height)], fill=GRID_COLOR)
        for j in range(num_weeks + 1):
            y = title_height + weekday_header_height + j * cell_height
            draw.line([(0, y), (canvas_width, y)], fill=GRID_COLOR)
        draw.line([(0, title_height), (canvas_width, title_height)], fill=GRID_COLOR)
        draw.line([(0, title_height + weekday_header_height), (canvas_width, title_height + weekday_header_height)], fill=GRID_COLOR)

    return cal_img

def save_monthly_images_as_pdf(monthly_info_list, pdf_path, font_path=DEFAULT_FONT_PATH):
    if canvas is None:
        print("[warning] ReportLab がインストールされていないため PDF を生成できません。")
        return
    if Path(font_path).suffix.lower() == ".ttf":
        try:
            pdfmetrics.registerFont(TTFont("MSGothic", font_path))
        except Exception as e:
            print("[pdf] フォント登録エラー:", e)

    c = canvas.Canvas(str(pdf_path), pagesize=landscape(A4))
    w_pt, h_pt = landscape(A4)

    for year, month, img_path in monthly_info_list:
        c.drawImage(ImageReader(str(img_path)), 0, 0, width=w_pt, height=h_pt)
        # タイトル等は既に画像に含めている想定なのでここでは最低限の追加のみ
        c.showPage()
    c.save()
    print(f"[pdf] 保存しました → {pdf_path}")

def stitch_monthly_calendars_vertically(monthly_calendar_paths: Sequence[Path], spacing=20):
    if not monthly_calendar_paths:
        return None
    dimensions = []
    for path in monthly_calendar_paths:
        with Image.open(path) as img:
            dimensions.append((img.width, img.height))
    max_width = max(width for width, _ in dimensions)
    total_height = sum(height for _, height in dimensions) + spacing * (len(dimensions) - 1)
    composite = Image.new("RGB", (max_width, total_height), BACKGROUND_COLOR)
    y_offset = 0
    for path, (width, height) in zip(monthly_calendar_paths, dimensions):
        with Image.open(path) as img:
            x_offset = (max_width - width) // 2
            composite.paste(img, (x_offset, y_offset))
        y_offset += height + spacing
    return composite

def build_and_save_category_image(category_cfg: Dict[str, Any],
                                  config: Dict[str, Any],
                                  output_year: int,
                                  source_year: int,
                                  allowed_months: Optional[Sequence[int]],
                                  min_coverage: float) -> Dict[str, Any]:
    category_id = category_cfg.get("id", category_cfg.get("prefix", "category"))
    prefix = category_cfg["prefix"]
    gravity = category_cfg["gravity"]
    crop_offset = category_cfg["crop_offset"]
    mode = category_cfg.get("mode", "thumbnail")
    placeholder_cfg = config.get("placeholder", {})
    placeholder_enabled = placeholder_cfg.get("enabled", True)
    placeholder_text = placeholder_cfg.get("text")
    locale_name = config.get("locale", "ja")

    input_dir = config.get("input_dir", ".")
    output_dir = Path(config.get("output_dir", "out"))
    output_dir.mkdir(parents=True, exist_ok=True)
    cell_w = int(config.get("cell_width", BASE_CELL_WIDTH))
    cell_h = int(config.get("cell_height", BASE_CELL_HEIGHT))
    font_path = config.get("font_path") or DEFAULT_FONT_PATH
    temp_root = Path(config.get("temp_dir") or (output_dir / ".tmp_calendar"))
    temp_dir = _category_temp_dir(temp_root, category_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[%s] カテゴリー処理開始 (mode=%s)", category_id, mode)
    monthly_dict = process_category_images(prefix, gravity, crop_offset, source_year, output_year, mode,
                                           input_dir, cell_w, cell_h, allowed_months)
    if config.get("include_empty_months", False):
        requested_months = allowed_months or range(1, 13)
        for month in requested_months:
            monthly_dict.setdefault((output_year, int(month)), {})

    if not monthly_dict:
        logger.warning("[%s] 入力年 %s の画像が見つかりません。", category_id, source_year)
        if not config.get("retain_temp_files"):
            shutil.rmtree(temp_dir, ignore_errors=True)
        return {
            "id": category_id,
            "coverage": {},
            "output_image": None,
            "pdf_path": None,
            "coverage_failed": False,
            "months_processed": 0,
            "errors": [f"入力年 {source_year} の入力が見つかりません"],
        }

    monthly_files: List[Tuple[int, int, Path, float]] = []
    coverage_map: Dict[Tuple[int, int], float] = {}
    scale_factor = max(1, int(cell_h / BASE_CELL_HEIGHT))
    for year, month in sorted(monthly_dict.keys()):
        logger.info("[%s] %04d-%02d のカレンダー作成中...", category_id, year, month)
        images_by_day = monthly_dict[year, month]
        total_days = calendar.monthrange(year, month)[1]
        coverage = len(images_by_day) / total_days if total_days else 0.0
        coverage_map[(year, month)] = coverage
        month_img = create_monthly_calendar_image(
            year,
            month,
            images_by_day,
            cell_width=cell_w,
            cell_height=cell_h,
            title_height=TITLE_HEIGHT * scale_factor,
            weekday_header_height=WEEKDAY_HEADER_HEIGHT * scale_factor,
            draw_titles=True,
            draw_weekdays=True,
            draw_dates=True,
            font_path=font_path,
            locale_name=locale_name,
            placeholder_enabled=placeholder_enabled,
            placeholder_text=placeholder_text,
        )
        month_path = temp_dir / f"{year}-{month:02d}.png"
        month_img.save(month_path)
        month_img.close()
        for img in images_by_day.values():
            try:
                img.close()
            except Exception:
                pass
        del monthly_dict[year, month]
        monthly_files.append((year, month, month_path, coverage))

    formatted_name = category_cfg.get("output", "{year}_output.png").format(year=output_year)
    base_output_path = output_dir / formatted_name

    final_image_path = None
    if not config.get("skip_annual", False):
        composite = stitch_monthly_calendars_vertically([info[2] for info in monthly_files], spacing=20)
        if composite:
            if SAVE_JPEG:
                final_image_path = base_output_path.with_suffix(".jpg")
                composite.save(final_image_path, quality=95)
            else:
                final_image_path = base_output_path.with_suffix(".png")
                composite.save(final_image_path)
            logger.info("[%s] 年次画像を保存しました → %s", category_id, final_image_path)
            composite.close()
        else:
            logger.error("[%s] 年次画像の合成に失敗しました。", category_id)
    else:
        logger.info("[%s] skip_annual=true のため縦連結をスキップしました。", category_id)

    pdf_path = None
    if monthly_files:
        candidate_pdf = base_output_path.with_suffix(".pdf")
        if config.get("require_pdf") and canvas is None:
            raise RuntimeError("ReportLab が必要ですがインストールされていません。")
        if canvas is not None:
            save_monthly_images_as_pdf([(y, m, path) for y, m, path, _ in monthly_files],
                                       candidate_pdf, font_path=font_path)
            pdf_path = candidate_pdf
        else:
            logger.info("[%s] ReportLab 未導入のため PDF 出力をスキップしました。", category_id)

    if not config.get("retain_temp_files"):
        shutil.rmtree(temp_dir, ignore_errors=True)

    coverage_failed = any(value < min_coverage for value in coverage_map.values())
    if coverage_failed:
        logger.warning("[%s] 目標カバレッジ %.2f を満たさない月があります。", category_id, min_coverage)

    return {
        "id": category_id,
        "coverage": coverage_map,
        "output_image": final_image_path,
        "pdf_path": pdf_path,
        "coverage_failed": coverage_failed,
        "months_processed": len(monthly_files),
        "errors": [],
    }

def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="YouTube Live Snapshotのアーカイブを画像・PDFへ描画するツール",
    )
    parser.add_argument("--config", type=str, help="全体設定 YAML")
    parser.add_argument("--categories-config", type=str, help="カテゴリ設定 YAML (config より優先)")
    parser.add_argument("--year", type=int, help="対象年 (デフォルト: config または 2025)")
    parser.add_argument("--source-year", type=int, help="入力写真の年。未指定時は --year と同じ")
    parser.add_argument(
        "--locale",
        choices=SUPPORTED_LOCALES,
        help="カレンダー表示言語 (ja または en)",
    )
    parser.add_argument("--input-dir", type=str, help="入力ディレクトリ")
    parser.add_argument("--output-dir", type=str, help="出力ディレクトリ")
    parser.add_argument("--font-path", type=str, help="フォントパス")
    parser.add_argument("--cell-width", type=int, help="セル幅")
    parser.add_argument("--cell-height", type=int, help="セル高")
    parser.add_argument("--save-jpeg", action="store_true", help="JPEG で保存")
    parser.add_argument("--min-coverage", type=float, help="許容する最低カバレッジ (0-1)")
    parser.add_argument("--require-pdf", action="store_true", help="PDF 出力を必須化 (ReportLab が必要)")
    parser.add_argument("--skip-annual", action="store_true", help="年間縦連結画像をスキップ")
    parser.add_argument("--include-empty-months", action="store_true", help="入力画像がない月もカレンダーとして出力")
    parser.add_argument("--log-file", type=str, help="ログ出力先ファイル")
    parser.add_argument("--log-level", type=str, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="ログレベル")
    parser.add_argument("--temp-dir", type=str, help="中間ファイルの保存先")
    parser.add_argument("--retain-temp-files", action="store_true", help="中間 PNG を削除しない")
    parser.add_argument("--placeholder-text", type=str, help="欠損時に描画する文字列")
    parser.add_argument("--no-placeholder", dest="disable_placeholder", action="store_true",
                        help="欠損プレースホルダーを無効化")
    parser.add_argument("--month", type=int, help="処理対象の単月 (1-12)")
    parser.add_argument("--months", type=str, help="カンマ区切りで複数月指定 (例: 6,7,8)")
    parser.add_argument("--prefix", action="append", help="処理したい capture prefix を限定")
    parser.add_argument("--category-id", action="append", help="処理したいカテゴリ ID を限定")
    parser.add_argument("--dry-run", action="store_true", help="実行計画のみ表示して終了")
    parser.set_defaults(save_jpeg=None, require_pdf=None, skip_annual=None, retain_temp_files=None)
    args = parser.parse_args(argv)

    config = deepcopy(DEFAULT_CONFIG)
    try:
        file_cfg = _load_yaml(args.config)
    except Exception as exc:
        parser.error(str(exc))
    _deep_merge(config, file_cfg)
    _apply_cli_overrides(config, args)

    categories_data = config.get("categories", DEFAULT_CATEGORIES)
    if args.categories_config:
        try:
            categories_data = _load_categories_list(_load_yaml(args.categories_config), args.categories_config)
        except Exception as exc:
            parser.error(str(exc))
    else:
        categories_data = _load_categories_list(categories_data, "config")

    allowed_prefixes = args.prefix
    allowed_ids = args.category_id
    categories = _filter_categories(categories_data, allowed_prefixes, allowed_ids)
    if not categories:
        print("対象カテゴリがありません。--prefix や --category-id の指定を確認してください。")
        sys.exit(0)

    try:
        months_filter = _parse_month_filters(args.month, args.months)
    except ValueError as exc:
        parser.error(str(exc))

    if config.get("min_coverage") is not None:
        if not 0.0 <= config["min_coverage"] <= 1.0:
            parser.error("--min-coverage は 0.0-1.0 の範囲で指定してください。")

    locale_name = config.get("locale", "ja")
    if locale_name not in SUPPORTED_LOCALES:
        parser.error(f"locale は {', '.join(SUPPORTED_LOCALES)} のいずれかを指定してください。")

    _setup_logging(config.get("logging", {}))
    global logger
    logger = logging.getLogger("ytlive_snapshot.render")

    font_path = Path(config.get("font_path") or DEFAULT_FONT_PATH)
    if not font_path.exists():
        logger.error("フォントが見つかりません: %s", font_path)
        sys.exit(1)
    config["font_path"] = str(font_path)

    global SAVE_JPEG
    SAVE_JPEG = bool(config.get("save_jpeg"))

    year = int(config.get("year", 2025))
    source_year = int(config.get("source_year") or year)
    output_dir = Path(config.get("output_dir", "out"))
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        for cat in categories:
            output_name = cat.get("output", "{year}_output.png").format(year=year)
            logger.info(
                "[DRY RUN] %s source_year=%s output_year=%s -> %s/%s",
                cat.get("id"),
                source_year,
                year,
                output_dir,
                output_name,
            )
        sys.exit(0)

    errors: List[str] = []
    coverage_failures: List[str] = []
    min_coverage = float(config.get("min_coverage", 0.0))

    for category in categories:
        try:
            result = build_and_save_category_image(category, config, year, source_year, months_filter, min_coverage)
        except Exception as exc:
            logger.exception("[%s] カテゴリ処理でエラー", category.get("id", "category"))
            errors.append(f"{category.get('id','category')}: {exc}")
            continue
        if result["errors"]:
            errors.extend(f"{result['id']}: {msg}" for msg in result["errors"])
        if result["coverage_failed"]:
            coverage_failures.append(result["id"])

    if errors:
        logger.error("一部カテゴリで致命的なエラーが発生しました: %s", errors)
        sys.exit(1)
    if coverage_failures:
        logger.error("カバレッジ閾値 %.2f を満たさないカテゴリ: %s", min_coverage, coverage_failures)
        sys.exit(2)

    logger.info("全てのカテゴリーの処理が完了しました。")

if __name__ == '__main__':
    main()
