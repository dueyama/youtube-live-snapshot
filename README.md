# YouTube Live Snapshot

> Scheduled still-image captures from YouTube Live, with visual archive rendering.

English documentation is provided first. [日本語クイックスタートはこちら](#日本語クイックスタート).

YouTube Live Snapshot is a standalone command-line tool for capturing still frames from a YouTube Live stream at scheduled times. Raspberry Pi is the primary always-on target, but the same commands can run on a general Linux system or macOS wherever Python, Chromium, and a compatible driver are available.

The companion `render` command turns the accumulated snapshots into monthly and annual visual archives, including Japanese or English calendar layouts, average-color views, PNG/JPEG images, and PDFs.

The core service does not require an AI agent. With Codex or another AI coding agent, routine work such as checking service health, finding missing capture dates, reviewing logs, and rendering a new calendar can also be requested in natural language.

This is not an official YouTube or Google project.

## Features

### Scheduled capture

- One or more fixed capture times per day
- A daily capture scheduled relative to local sunset
- Time-zone-aware scheduling with APScheduler
- Headless Chromium and Selenium frame capture
- Single-run and short-interval test modes
- YAML, environment-variable, and CLI configuration
- Retries, logging, and optional retention limits
- Preferred and minimum source-resolution checks before saving a frame
- Automatic normalization of YouTube watch, share, live, and embed URLs
- Optional OpenAI vision inspection of frame quality and visible timestamps

### Archive rendering

- Reads files named `prefix_YYYYMMDD_HHMM.png`, `.jpg`, or `.jpeg`
- Monthly calendars and vertically combined annual images
- Japanese and English titles, weekdays, and missing-data labels
- Configurable crop regions, thumbnails, and average-color categories
- PNG, JPEG, and PDF output
- Coverage thresholds and missing-day placeholders for rendered months
- Same-year calendars and shifted source-year calendars

## Requirements

- Python 3.9 or later
- A Chromium-compatible browser
- ChromeDriver, or an environment where Selenium Manager can provide it

Noto Sans CJK JP is bundled for Japanese calendar output. Chromium and ChromeDriver package names vary by operating system; make sure their versions are compatible.

## Platforms and AI-assisted setup

- **Raspberry Pi OS:** the primary target for an always-on capture service.
- **General Linux:** uses the same CLI and is normally managed with systemd or another process supervisor.
- **macOS:** suitable for local capture, archive rendering, testing, and continuous operation with an appropriate macOS process supervisor.

The application code is portable; most platform differences are limited to installing Chromium, locating ChromeDriver or the browser binary, choosing writable data directories, and configuring a service manager. An AI coding agent such as Codex can help identify the browser and Python environment, create the virtual environment, prepare the configuration, run and inspect a test capture, and draft an appropriate systemd or macOS service definition. This often makes bringing up a new machine a short, guided task rather than a manual porting project.

## Installation

```bash
git clone https://github.com/dueyama/youtube-live-snapshot.git
cd youtube-live-snapshot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .

ytlive-snapshot --help
```

The optional OpenAI image inspector is installed separately:

```bash
python -m pip install '.[ai]'
```

## Choosing the YouTube Live URL

The most reliable input is the embed URL for the actual live video:

```text
https://www.youtube.com/embed/VIDEO_ID
```

To obtain it on a computer:

1. Open the actual live video page, not only the channel home page.
2. Select **Share → Embed**.
3. Copy the URL from the `src="..."` part of the iframe code.
4. Store only that URL in `embed_url`; do not paste the entire iframe element.

A normal watch or Share link is also accepted:

```text
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/live/VIDEO_ID
https://www.youtube.com/embed/VIDEO_ID
```

YouTube Live Snapshot converts these forms internally to a privacy-enhanced `/embed/VIDEO_ID` URL, removes the transient `si` sharing parameter, and adds the player options needed for muted autoplay and frame capture. It loads the player inside a small local wrapper page so that the embedded player receives a web-page context and referrer.

Important details:

- A channel URL such as `https://www.youtube.com/@handle/live` does not contain a video ID and is not a supported substitute.
- If the broadcaster creates a new live event with a new video ID, update `embed_url`.
- Older examples on the web may use `embed/live_stream?channel=...`; this is not documented by the current YouTube Embedded Player specification and should not be relied on.
- The video owner must allow embedding. If **Share → Embed** is unavailable or the player reports that embedding is disabled, change the video's YouTube Studio setting or use a video for which embedding is permitted.
- YouTube's current player documentation defines the standard form as `/embed/VIDEO_ID`. Automatic discovery of the active video for a channel would require a separate YouTube Data API integration and is not implemented here.

See the official [YouTube embed instructions](https://support.google.com/youtube/answer/171780) and [Embedded Player parameters](https://developers.google.com/youtube/player_parameters).

## Configuration

Copy the sample configuration:

```bash
cp config/capture.sample.yaml config/capture.yaml
```

At minimum, set the live-video URL. The initial example keeps sunset scheduling disabled; to enable it, provide the location's latitude and longitude and set `enable_sunset: true`.

```yaml
embed_url: "https://www.youtube.com/embed/VIDEO_ID"

capture:
  source_resolution:
    preferred_width: 1280
    preferred_height: 720
    minimum_width: 640
    minimum_height: 360
    wait_sec: 30

schedule:
  timezone: Asia/Tokyo
  latitude: null
  longitude: null
  fixed_times:
    - id: noon
      time: "12:00"
      prefix: noon
  enable_sunset: false
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

For the `capture` command, configuration precedence is:

1. CLI options
2. Environment variables
3. YAML configuration
4. Built-in defaults

Select the YAML file directly or through an environment variable:

```bash
ytlive-snapshot capture --config config/capture.yaml

YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml \
  ytlive-snapshot capture
```

The same values can also be supplied with environment variables:

```text
YTLIVE_SNAPSHOT_EMBED_URL
YTLIVE_SNAPSHOT_LATITUDE
YTLIVE_SNAPSHOT_LONGITUDE
YTLIVE_SNAPSHOT_LOCATION_NAME
YTLIVE_SNAPSHOT_REGION
YTLIVE_SNAPSHOT_TIMEZONE
```

The project does not load `.env` files itself. Pass environment variables from systemd, a container, or a shell, or keep these values in `config/capture.yaml`.

## Capturing snapshots

Start the configured scheduler in the foreground:

```bash
ytlive-snapshot capture --config config/capture.yaml
```

Capture one diagnostic frame and exit:

```bash
ytlive-snapshot capture \
  --config config/capture.yaml \
  --once \
  --output-dir ./captures_test
```

Confirm that `./captures_test` contains a readable `video_YYYYMMDD_HHMMSS.png` before starting the scheduler. Files created by `--once` and `--test` use this diagnostic name and are not calendar input; scheduled jobs use the configured prefix and `prefix_YYYYMMDD_HHMM.png` format.

Run a short-interval test:

```bash
ytlive-snapshot capture \
  --config config/capture.yaml \
  --test \
  --test-interval-minutes 2
```

Disable sunset scheduling when no coordinates are configured:

```bash
ytlive-snapshot capture \
  --embed-url "https://www.youtube.com/embed/VIDEO_ID" \
  --no-sunset
```

For continuous operation, run the foreground command from systemd or another service manager. See [RUNBOOK.md](RUNBOOK.md) for a deployment example.

Before saving a capture, the tool checks YouTube's own LIVE-control state when available and avoids seeking a player that already reports live playback. This mechanical check uses player state and control structure rather than localized words such as `LIVE`, so it remains independent of the optional AI feature and of the browser language. If the control reports delayed playback, the tool uses it to return to live and verifies the result. Some headless embeds do not render that control; in that case the tool confirms that the player identifies the stream as live and preserves its initially loaded frame. It deliberately does not seek from raw DVR `duration`, `currentTime`, or seekable-range differences because YouTube can expose values roughly one DVR window apart even while showing a current frame. It also verifies that the saved frame is readable and visually non-uniform. Blank frames, loading screens, and common player-error frames are retried. Attempts are written to temporary files and only an image that passes every enabled check is moved to its final persistent filename. These checks are enabled by default and can be tuned in the `capture` section of the YAML file.

Every failure consumes the same bounded attempt budget. `max_retries: 3` means
three total attempts including the first, not one initial attempt plus three
more. There is no separate AI retry loop. When possible, each failed attempt is
kept under `OUTPUT_DIR/rejected/YYYY-MM-DD/.../` as `metadata.json` and, if a
screenshot exists, `capture.png`. The metadata contains the failure stage,
diagnostic check results, and retry status. On POSIX systems, these directories
use mode `0700` and their files use `0600`. Rejected attempts are not used for
calendar rendering or normal capture pruning and are not deleted automatically,
so monitor their disk use. A storage failure may leave a hidden temporary PNG
in the output directory; consult the log before removing it.

The PNG canvas dimensions are not proof of source quality: a low-resolution video can be enlarged into a larger PNG. By default, the tool therefore waits up to 30 seconds for a preferred `1280x720` source. If that quality is unavailable, it accepts a current frame of at least `640x360`; anything lower fails the attempt and the normal retry opens a fresh browser page. The source dimensions are checked again immediately before each screenshot path. This deterministic `videoWidth`/`videoHeight` check remains active without AI. Streams with different quality constraints can tune `capture.source_resolution`.

Current YouTube/Chromium behavior has an important ambiguity: in some headless embeds, `video.seekable.end(0) - video.currentTime` is reported as exactly `3600` seconds even when the visible camera clock is only tens of seconds behind wall-clock time. That value is a distance on the player's internal DVR timeline, not proof that the displayed video is one hour late. The tool therefore logs it only as diagnostic `raw_media_lag` and never seeks from that value alone. A seek is attempted only when YouTube's own LIVE control explicitly reports delayed playback. If the stream includes a visible clock, its real-world freshness can additionally be checked by a person or by the optional AI inspection.

## Optional AI image inspection

AI inspection is **off by default**. A normal installation makes no OpenAI API
requests, does not require an API key, and continues to use only the local
deterministic checks described above.

The purpose of this option is to add a semantic second opinion for unattended,
long-running capture systems. Local checks are the authoritative first layer:
they can inspect the YouTube player's LIVE-control state, verify the
image file, and reject obviously blank or nearly uniform frames. They cannot
reliably understand every unusual screen that still looks like a valid image,
or read and interpret a small camera clock in different layouts. AI inspection
is intended to help identify those visually plausible but undesirable captures
and to compare a visible camera timestamp with the time the frame was captured.

It remains optional because enabling it sends an image outside the machine,
requires network access and an API key, incurs usage cost, and can produce OCR
or judgment errors. The capture service must therefore remain useful and
predictable without AI. AI does not replace the mechanical LIVE-state check;
at most, a visible camera timestamp provides independent supporting evidence.

When enabled, the captured image is sent to the OpenAI Responses API before
its temporary file is moved to the final filename. The inspector can identify a
blank or obstructed video frame, read a visible camera clock, and compare that
clock with the frame-capture time. It uses `gpt-6-luna` by default because
that model accepts image input and is intended for cost-sensitive workloads;
the model remains configurable. Image detail defaults to `original` because
small timestamp text is an OCR-like task.

The AI prompt accepts overlays in any language and locale. It should not reject
a frame merely because of its language, and ambiguous date ordering or unclear
digits are reported as unreadable rather than guessed.

Install the optional dependency, set the API key in the environment, and enable
the inspector in the YAML configuration:

```bash
python -m pip install '.[ai]'
export OPENAI_API_KEY='...'
```

```yaml
capture:
  ai_validation:
    enabled: true
    model: gpt-6-luna
    mode: enforce
    detail: original
    api_key_env: OPENAI_API_KEY
    timestamp_tolerance_sec: 360
    require_timestamp: false
```

When AI is enabled, it is part of the final save check: only `decision=pass`
with acceptable timestamp conditions is moved to the normal output filename.
A clear frame failure or stale/future timestamp is recorded as `rejected`; an uncertain
verdict, API failure, invalid response, or required-but-unreadable timestamp is
recorded as `unverified`. Both enter the same bounded retry path and retain
their diagnostic files. AI validation is an acceptance check rather than a
report-only mode. Streams without a visible camera clock should leave
`require_timestamp: false`.
The default six-minute tolerance treats the timestamp as a broad freshness
guard rather than a clock-synchronization check, allowing for modest camera
clock skew as well as stream delay.

The same settings can be overridden for one run:

```bash
ytlive-snapshot capture \
  --config config/capture.yaml \
  --once \
  --ai-validate \
  --ai-validation-mode enforce \
  --ai-model gpt-6-luna
```

Enabling this feature sends the captured image to OpenAI and incurs API usage.
Requests use `store: false`, but users should still review the stream's privacy
and authorization requirements. AI inspection complements rather than replaces
the local checks and cannot by itself prove that a stream is live when the
image contains no reliable time reference. See the official OpenAI
[vision input guide](https://developers.openai.com/api/docs/guides/images-vision)
and [`gpt-6-luna` model page](https://developers.openai.com/api/docs/models/gpt-6-luna).

## Capture schedules

Any number of independent daily series can be configured:

```yaml
schedule:
  fixed_times:
    - id: morning
      time: "08:00"
      prefix: morning
    - id: noon
      time: "12:00"
      prefix: noon
    - id: afternoon
      time: "15:30"
      prefix: afternoon

  enable_sunset: true
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

This example captures at 08:00, 12:00, 15:30, and 40 minutes before that day's local sunset. Files are stored as `prefix_YYYYMMDD_HHMM.png`.

When multiple files share a prefix and date, the renderer uses the latest time for that day. Use distinct prefixes when each daily time should produce a separate archive category.

Fixed times and sunset offsets are configuration-only changes. Rules based on sunrise, weekdays, weather, or other conditions require code changes and tests; an AI coding agent can help implement them.

## Rendering the archive

Create a rendering configuration and run it:

```bash
cp config/render.sample.yaml config/render.yaml
ytlive-snapshot render --config config/render.yaml
```

The sample categories demonstrate upper sky and lower sea crops for a landscape webcam. Rename or replace them to match the subject and framing of your stream.

Select Japanese or English calendar text:

```bash
ytlive-snapshot render --config config/render.yaml --locale ja
ytlive-snapshot render --config config/render.yaml --locale en
```

### Same-year archive

Place snapshots on their original calendar dates:

```bash
ytlive-snapshot render \
  --config config/render.yaml \
  --year 2026 \
  --source-year 2026
```

For example, `noon_20260801_1200.png` is placed on August 1, 2026.

### Shifted source year

Use last year's photographs in this year's weekday layout:

```bash
ytlive-snapshot render \
  --config config/render.yaml \
  --year 2026 \
  --source-year 2025 \
  --include-empty-months
```

For example, `noon_20250801_1200.png` is placed on August 1, 2026. Month and day are preserved while the title and weekday layout follow the output year. A source date such as February 29 is skipped when it does not exist in the output year.

Limit output by month, prefix, category, or required coverage:

```bash
ytlive-snapshot render \
  --config config/render.yaml \
  --year 2026 \
  --months 6,7,8 \
  --prefix sunset \
  --min-coverage 0.8 \
  --output-dir ./out
```

Category IDs and output names are restricted to safe relative filenames. Absolute paths and `../` traversal are rejected.

## Working with an AI agent

AI is optional: scheduled capture and rendering work as normal commands. With Codex, open this repository as the workspace and request tasks such as:

```text
Check the service process, recent logs, and latest snapshot timestamp.
List missing capture dates for this year.
Render a 2026 English calendar and inspect the output.
Investigate why yesterday's sunset capture ran late.
```

## Storage and retention

- Capture, rendering, and log destinations are configurable.
- Snapshots are deleted only when `max_files` or `max_disk_mb` is explicitly configured.
- Retention limits apply to the normal PNG files for the series being saved.
- Choose a snapshot directory that is preserved across upgrades or reinstalls.
- Confirm that you have the right to capture and store the stream and that your use follows the applicable YouTube terms and policies.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m pytest
```

## Fonts and licenses

Source code is available under the [MIT License](LICENSE).

The bundled `assets/fonts/NotoSansCJKjp-Regular.otf` remains under the SIL Open Font License 1.1. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [assets/fonts/NotoSansCJK-LICENSE.txt](assets/fonts/NotoSansCJK-LICENSE.txt).

---

## 日本語クイックスタート

YouTube Live Snapshotは、YouTube Liveの映像を指定時刻に静止画として保存するコマンドラインツールです。主な常時稼働先はRaspberry Piですが、Python、Chromium、互換ドライバーを用意できれば、一般的なLinuxやmacOSでも同じコマンドを利用できます。蓄積した画像は`ytlive-snapshot render`で月間・年間カレンダー、平均色画像、PNG/JPEG、PDFへまとめられます。

AIエージェントがなくても単独で動作します。Codexなどと組み合わせると、稼働確認、ログ調査、撮影漏れ確認、カレンダー作成と目視確認などを自然言語で依頼できます。

これはYouTubeおよびGoogleの公式プロジェクトではありません。

### 主な機能

- 1日に複数回指定できる固定時刻撮影
- 日付・位置情報から計算する「日の入り何分前」の撮影
- headless ChromiumとSeleniumによる静止画保存
- YAML、環境変数、CLIによる設定
- 日本語・英語のカレンダー描画
- 当年写真を当年へ配置する通常版
- 前年写真を今年の曜日配置へ載せる年シフト版
- PNG、JPEG、PDF、平均色表示
- 既定では無効なOpenAI画像品質・時刻検査

### 対応環境とAIによる導入支援

- **Raspberry Pi OS:** 常時撮影サービスの主対象
- **一般的なLinux:** 同じCLIを利用し、通常はsystemdなどで常駐化
- **macOS:** ローカル撮影、描画、テストに利用でき、適切なプロセス管理を用意すれば常時運用も可能

環境ごとの差は、主にChromiumとChromeDriverの導入場所、書き込み可能なデータディレクトリ、サービス管理方法です。CodexなどのAIコーディングエージェントを使えば、OSとブラウザ環境の確認、仮想環境と設定ファイルの作成、単発撮影と画像確認、systemdまたはmacOS向けサービス定義の作成までを対話しながら進められます。そのため、新しいLinux機やMacへの導入は大規模な移植ではなく、比較的短い環境設定作業として扱えます。

### YouTube Live URLの取り方

最も確実な入力は、対象となるライブ動画の埋め込みURLです。

```text
https://www.youtube.com/embed/VIDEO_ID
```

取得手順は次のとおりです。

1. チャンネルのトップではなく、実際に再生中のライブ動画を開く
2. **共有 → 埋め込む**を選ぶ
3. iframeコードの`src="..."`に入っているURLだけをコピーする
4. `capture.yaml`の`embed_url`へ保存する

通常の共有URLでも動きます。

```text
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/live/VIDEO_ID
https://www.youtube.com/embed/VIDEO_ID
```

ツール内部で`youtube-nocookie.com/embed/VIDEO_ID`形式へ変換し、共有時の一時的な`si`パラメータを外し、ミュート自動再生などの設定を加えます。また、埋め込みプレイヤーをローカルの小さなHTMLページ内で開くため、埋め込みURLをブラウザのアドレス欄へ直接入れたときに起きるreferrer不足も避けます。

注意点:

- `youtube.com/@handle/live`のようなチャンネルURLには動画IDがないため使いません。
- 配信を作り直して動画IDが変わった場合は`embed_url`も更新します。
- 古い解説にある`embed/live_stream?channel=...`形式は、現在のYouTube公式プレイヤー仕様に記載がないため依存しません。
- 配信側で「埋め込みを許可」が有効になっている必要があります。
- 現在ライブ中の動画をチャンネルから自動検索するにはYouTube Data API連携が別途必要で、現バージョンには含まれていません。

公式情報は[YouTubeの埋め込み手順](https://support.google.com/youtube/answer/171780)と[埋め込みプレイヤー仕様](https://developers.google.com/youtube/player_parameters)を参照してください。

### インストール

```bash
git clone https://github.com/dueyama/youtube-live-snapshot.git
cd youtube-live-snapshot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

任意のAI画像検査も使用する場合だけ、追加依存を導入します。

```bash
python -m pip install '.[ai]'
```

### キャプチャ設定

```bash
cp config/capture.sample.yaml config/capture.yaml
```

```yaml
embed_url: "https://www.youtube.com/embed/VIDEO_ID"

capture:
  source_resolution:
    preferred_width: 1280
    preferred_height: 720
    minimum_width: 640
    minimum_height: 360
    wait_sec: 30

schedule:
  timezone: Asia/Tokyo
  latitude: null
  longitude: null
  fixed_times:
    - id: noon
      time: "12:00"
      prefix: noon
  enable_sunset: false
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

`embed_url`は必須です。最初の例では日の入り撮影を無効にしています。有効にする場合は、撮影地点の`latitude`と`longitude`を設定して`enable_sunset: true`へ変更します。環境ごとの値は`config/capture.yaml`または環境変数で設定できます。

### 実行

定期撮影:

```bash
ytlive-snapshot capture --config config/capture.yaml
```

動作確認用の単発撮影:

```bash
ytlive-snapshot capture \
  --config config/capture.yaml \
  --once \
  --output-dir ./captures_test
```

定期撮影を始める前に、`./captures_test`へ読み取り可能な`video_YYYYMMDD_HHMMSS.png`が生成されたことを確認します。`--once`と`--test`の画像はこの確認用ファイル名となり、カレンダー入力には使われません。定期ジョブは設定したprefixを使い、`prefix_YYYYMMDD_HHMM.png`として保存されます。

日の入り撮影なし:

```bash
ytlive-snapshot capture \
  --embed-url "https://www.youtube.com/embed/VIDEO_ID" \
  --no-sunset
```

常時稼働ではsystemdなどからこのコマンドをフォアグラウンド実行します。配備例は[RUNBOOK.md](RUNBOOK.md)を参照してください。

保存前に、利用できる場合はYouTube自身のLIVE表示状態を確認し、すでにライブ再生中ならシークしません。この機械判定は`LIVE`や`ライブ`という言語別文字列ではなく、プレイヤー状態と操作UIの構造を使うため、任意のAI検査やブラウザ言語に依存しません。遅れ再生と表示された場合だけLIVE操作で追いつき、その結果を再確認します。headless埋め込みではLIVE操作UIが描画されない場合があるため、その場合はプレイヤーがライブ配信と報告していることを確認し、最初に読み込まれたフレームをそのまま使います。YouTubeは現在映像を表示中でもDVRの`duration`、`currentTime`、seekable範囲に約1時間の差を返す場合があるため、これらの曖昧な値だけを根拠にはシークしません。さらに、画像が読み取り可能で黒画面やほぼ一様なエラー画面ではないことも検査します。読み込み中やプレーヤーエラーなどは再試行し、有効な検査をすべて通過した画像だけを一時ファイルから正式な永続ファイル名へ移します。これらは既定で有効です。

すべての失敗は、1つの上限付き試行枠を共有します。`max_retries: 3`は初回を含む合計3試行であり、初回に加えて3回ではありません。AI専用の別リトライはありません。失敗した各試行は、可能な限り`出力先/rejected/YYYY-MM-DD/.../`へ保存し、通常は`metadata.json`を、画像生成後なら`capture.png`も残します。メタデータには失敗段階、診断結果、再試行の有無が入ります。POSIX環境ではフォルダを`0700`、ファイルを`0600`にします。これらはカレンダー描画や通常の世代削除には使われず、自動削除もしないため、ディスク使用量を監視してください。保存障害では隠し一時PNGが出力先へ残る場合があるため、削除前にログを確認してください。

保存PNGの寸法だけでは元映像の品質は分かりません。低解像度の動画を大きなPNGへ拡大できるためです。既定では最大30秒間、元映像が優先値`1280x720`になるのを待ちます。そこまで上がらなくても現在の映像が最低`640x360`以上なら採用し、未満ならその回を不合格として新しいブラウザページで通常の再試行を行います。各スクリーンショット経路の直前にも元解像度を再確認します。これは`videoWidth`と`videoHeight`を使うAI非依存の機械判定です。配信事情が異なる場合は`capture.source_resolution`で調整できます。

現在のYouTube／Chromiumには注意すべき曖昧さがあります。一部のheadless埋め込みでは、`video.seekable.end(0) - video.currentTime`が正確に`3600`秒を返しても、画面内のカメラ時計は実時刻から数十秒しか遅れていないことがあります。この値はプレイヤー内部のDVR時間軸上の距離であり、映像が現実に1時間遅れている証拠ではありません。そのため、本ツールはこれを診断用の`raw_media_lag`としてログに残すだけで、この値単独ではシークしません。YouTube自身のLIVE操作が遅れ再生を明示した場合だけシークを試みます。画面内に時計がある配信では、人による確認または任意のAI画像検査で実時刻との鮮度を追加確認できます。

### 任意のAI画像検査

AI画像検査は**既定では無効**です。通常のインストールではOpenAI APIを呼び出さず、APIキーも不要で、上記のローカル機械検査だけを行います。

このオプションの目的は、長期間無人運転する撮影システムに、画像内容を理解する補助的な確認を追加することです。第一判定は常にローカルの機械検査です。YouTubeプレイヤーのLIVE操作状態、画像ファイルの正常性、黒画面やほぼ一様な画像は機械的に確認できます。一方、画像ファイルとしては正常に見える特殊なエラー画面や想定外の表示、小さなカメラ時計の多様な配置までは、固定ルールだけでは完全に扱えません。AI検査は、このような「画像としては成立しているが保存したくない可能性があるフレーム」を補助的に見つけ、画像内のカメラ時刻と画像を取得した時刻を比較するためのものです。

任意機能としているのは、有効にすると画像を外部へ送信し、ネットワーク、APIキー、利用料が必要になり、OCRや判断を誤る可能性もあるためです。撮影サービス本体はAIなしでも実用的かつ予測可能に動作しなければなりません。AIは機械的なLIVE状態判定を置き換えず、画像内に信頼できる時計がある場合に独立した補足情報を与えるだけです。

有効にすると、一時画像を永続ファイル名へ移す前にOpenAI Responses APIへ送り、黒画面やエラー画面、再生コントロールの映り込み、画像内のカメラ時刻を確認できます。既定モデルは、画像入力に対応する低コスト向けの`gpt-6-luna`です。小さな時刻表示を読むため、画像詳細は`original`を既定にしています。

画像内の文字や日付形式は任意の言語・地域を許容します。言語が異なること自体を不合格理由にせず、日月順や数字を確定できない場合は推測せず`unreadable`として扱います。

```bash
python -m pip install '.[ai]'
export OPENAI_API_KEY='...'
```

```yaml
capture:
  ai_validation:
    enabled: true
    model: gpt-6-luna
    mode: enforce
    detail: original
    api_key_env: OPENAI_API_KEY
    timestamp_tolerance_sec: 360
    require_timestamp: false
```

AIを有効にすると、その判定も通常の出力先へ保存するための必須条件になります。`decision=pass`で時刻条件も満たした画像だけを保存します。明確な画面不良や古い・未来の時刻は`rejected`、判断不能、API障害、不正な応答、必須時刻を読めない場合は`unverified`として診断ファイルを残し、どちらも同じ上限付き試行枠で再撮影します。AI検査は記録だけのモードではなく、保存可否の判定です。時刻表示のない配信では`require_timestamp: false`のまま使用します。

既定の許容差は6分です。これは時計同期の厳密な検査ではなく、大きな鮮度異常を見つけるための幅を持たせた判定であり、配信遅延だけでなくカメラ時計自体の多少のずれも許容します。

この機能を有効にした場合だけ、撮影画像がOpenAIへ送信され、API利用料が発生します。リクエストは`store: false`ですが、配信の公開範囲と利用許可は利用者が確認してください。AI検査はローカル検査を補完するもので、信頼できる時刻表示がない画像だけから「現在ライブ中」と完全に証明するものではありません。公式の[画像入力ガイド](https://developers.openai.com/api/docs/guides/images-vision)と[`gpt-6-luna`モデル情報](https://developers.openai.com/api/docs/models/gpt-6-luna)も参照してください。

### 撮影時刻

```yaml
schedule:
  fixed_times:
    - id: morning
      time: "08:00"
      prefix: morning
    - id: noon
      time: "12:00"
      prefix: noon
    - id: afternoon
      time: "15:30"
      prefix: afternoon
  enable_sunset: true
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

この例では08:00、12:00、15:30と、その日の日の入り40分前に撮影します。曜日別、日の出基準、天候連動などの規則にはコード変更とテストが必要で、AIコーディングエージェントに実装を依頼できます。

### アーカイブ描画

```bash
cp config/render.sample.yaml config/render.yaml
ytlive-snapshot render --config config/render.yaml --locale ja
```

サンプルのカテゴリは、風景カメラの上側を空、下側を海として切り出す例です。対象の映像と構図に合わせて、カテゴリ名と切り出し設定を変更してください。

英語版:

```bash
ytlive-snapshot render --config config/render.yaml --locale en
```

撮影年と出力年が同じ通常版:

```bash
ytlive-snapshot render \
  --config config/render.yaml \
  --year 2026 \
  --source-year 2026
```

前年の写真を今年の曜日配置へ載せる年シフト版:

```bash
ytlive-snapshot render \
  --config config/render.yaml \
  --year 2026 \
  --source-year 2025 \
  --include-empty-months
```

### 保存と世代管理

- キャプチャ、描画、ログの保存先は設定できます。
- `max_files`や`max_disk_mb`を明示しない限り、古い画像を自動削除しません。
- 世代管理の上限は、保存中の系列と同じprefixを持つ通常PNGへ適用されます。
- 更新や再インストール後も残る画像保存先を指定してください。
- 利用者自身が配信映像を取得・保存する権限と適用条件を確認してください。

### テストとライセンス

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

ソースコードは[MIT License](LICENSE)です。同梱するNoto Sans CJK JPフォントにはSIL Open Font License 1.1が別途適用されます。詳細は[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。
