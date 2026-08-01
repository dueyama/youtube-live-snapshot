# YouTube Live Snapshot

> Scheduled still-image captures from YouTube Live, with visual archive rendering.

English documentation is provided first. [日本語はこちら](#日本語).

YouTube Live Snapshot is a standalone command-line tool for capturing still frames from a YouTube Live stream at scheduled times. Raspberry Pi is the primary always-on target, but the same commands can run on a general Linux system or macOS wherever Python, Chromium, and a compatible driver are available.

The companion `render` command turns the accumulated snapshots into monthly and annual visual archives, including Japanese or English calendar layouts, average-color views, PNG/JPEG images, and PDFs.

The core service does not require an AI agent. When used with Codex or another authorized agent, routine work such as checking service health, finding missing capture dates, reviewing logs, and rendering a new calendar can also be requested in natural language.

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
- Automatic normalization of YouTube watch, share, live, and embed URLs

### Archive rendering

- Reads files named `prefix_YYYYMMDD_HHMM.png`
- Monthly calendars and vertically combined annual images
- Japanese and English titles, weekdays, and missing-data labels
- Configurable sea, sky, crop, thumbnail, and average-color categories
- PNG, JPEG, and PDF output
- Coverage checks and missing-day placeholders
- Same-year calendars and shifted source-year calendars

## Requirements

- Python 3.9 or later
- A Chromium-compatible browser
- ChromeDriver, or an environment where Selenium Manager can provide it

Noto Sans CJK JP is bundled for Japanese calendar output. Chromium and ChromeDriver package names vary by operating system; make sure their versions are compatible.

## Platforms and AI-assisted setup

- **Raspberry Pi OS:** the primary tested target for an always-on capture service.
- **General Linux:** uses the same CLI and is normally managed with systemd or another process supervisor.
- **macOS:** suitable for local capture, archive rendering, testing, and continuous operation with an appropriate macOS process supervisor.

The application code is portable; most platform differences are limited to installing Chromium, locating ChromeDriver or the browser binary, choosing writable data directories, and configuring a service manager. An authorized AI agent such as Codex can inspect the host, identify the installed browser and Python environment, create the virtual environment, prepare an ignored private configuration, run one isolated test capture, inspect the image, and draft the appropriate systemd or macOS service configuration. This often makes bringing up a new machine a short, guided task rather than a manual porting project.

AI assistance does not bypass operating-system permissions, YouTube embedding restrictions, or the requirement to obtain authorization for the stream. Production service changes and private values should still be reviewed explicitly.

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
- The video owner must allow embedding. If **Share → Embed** is unavailable or the player reports that embedding is disabled, change the video's YouTube Studio setting or use a stream you are authorized to embed.
- YouTube's current player documentation defines the standard form as `/embed/VIDEO_ID`. Automatic discovery of the active video for a channel would require a separate YouTube Data API integration and is not implemented here.

See the official [YouTube embed instructions](https://support.google.com/youtube/answer/171780) and [Embedded Player parameters](https://developers.google.com/youtube/player_parameters).

## Configuration

Copy the public sample to an ignored private configuration file:

```bash
cp config/capture.sample.yaml config/capture.yaml
```

At minimum, set the live-video URL. Latitude and longitude are required only when sunset capture is enabled.

```yaml
embed_url: "https://www.youtube.com/embed/VIDEO_ID"

schedule:
  timezone: Asia/Tokyo
  latitude: 35.0
  longitude: 135.0
  fixed_times:
    - id: noon
      time: "12:00"
      prefix: noon
  enable_sunset: true
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

Configuration precedence is:

1. CLI options
2. Environment variables
3. YAML configuration
4. Public, non-private defaults

Select the YAML file directly or through an environment variable:

```bash
ytlive-snapshot capture --config config/capture.yaml

YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml \
  ytlive-snapshot capture
```

Host-specific values can also be supplied with:

```text
YTLIVE_SNAPSHOT_EMBED_URL
YTLIVE_SNAPSHOT_LATITUDE
YTLIVE_SNAPSHOT_LONGITUDE
YTLIVE_SNAPSHOT_LOCATION_NAME
YTLIVE_SNAPSHOT_REGION
YTLIVE_SNAPSHOT_TIMEZONE
```

The project does not load `.env` files itself. Pass normal environment variables from systemd, a container, or a shell. Keep real URLs, coordinates, credentials, and host-specific paths out of Git.

## Capturing snapshots

Start the configured scheduler in the foreground:

```bash
ytlive-snapshot capture --config config/capture.yaml
```

Capture one frame and exit:

```bash
ytlive-snapshot capture --config config/capture.yaml --once
```

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

For continuous operation, run the foreground command from systemd or another service manager. Keep the service definition, boot ordering, credentials, and host lifecycle in the host-management layer. See [RUNBOOK.md](RUNBOOK.md).

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

Fixed times and sunset offsets are configuration-only changes. Rules based on sunrise, weekdays, weather, or other conditions can be added by discussing the requirement with an AI agent, updating the code and tests, and deploying the verified change separately from production service control.

## Rendering the archive

Create a private rendering configuration and run it:

```bash
cp config/render.sample.yaml config/render.yaml
ytlive-snapshot render --config config/render.yaml
```

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

AI is optional: scheduled capture and rendering work as normal commands. With Codex, open this repository as the workspace and grant only the host or SSH access needed for the task.

Example requests:

```text
Check the service process, recent logs, and latest snapshot timestamp.
List missing capture dates for this year.
Render a 2026 English calendar and inspect the output.
Investigate why yesterday's sunset capture ran late.
```

The repository's [AGENTS.md](AGENTS.md) tells compatible agents not to delete persistent snapshots, expose private configuration, or operate production services without explicit authorization.

## Data safety

- `captures/`, `captures_test/`, `data/`, `out/`, logs, and private configuration are ignored by Git.
- Old snapshots are deleted only when `max_files` or `max_disk_mb` is explicitly configured.
- Keep persistent snapshots outside the replaceable application directory.
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

## 日本語

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

### 対応環境とAIによる導入支援

- **Raspberry Pi OS:** 常時撮影サービスの主対象で、実運用確認済み
- **一般的なLinux:** 同じCLIを利用し、通常はsystemdなどで常駐化
- **macOS:** ローカル撮影、描画、テストに利用でき、適切なプロセス管理を用意すれば常時運用も可能

環境ごとの差は、主にChromiumとChromeDriverの導入場所、書き込み可能なデータディレクトリ、サービス管理方法です。Codexなどの許可されたAIエージェントを使えば、OSとブラウザ環境の確認、仮想環境の作成、Git管理外の実設定作成、隔離先への単発撮影、画像確認、systemdまたはmacOS向け常駐設定の下書きまでを対話しながら進められます。そのため、新しいLinux機やMacへの導入は大規模な移植ではなく、比較的短い環境設定作業として扱えます。

ただし、AI支援でもOS権限やYouTubeの埋め込み制限を回避することはできません。実URLや座標、サービス変更は明示的に確認し、本番反映とは分けて扱ってください。

### YouTube Live URLの取り方

旧運用で使っていたのは、個別配信の埋め込みURLです。

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

### キャプチャ設定

```bash
cp config/capture.sample.yaml config/capture.yaml
```

```yaml
embed_url: "https://www.youtube.com/embed/VIDEO_ID"

schedule:
  timezone: Asia/Tokyo
  latitude: 35.0
  longitude: 135.0
  fixed_times:
    - id: noon
      time: "12:00"
      prefix: noon
  enable_sunset: true
  sunset_offset_minutes: 40
  sunset_prefix: sunset
```

`embed_url`は必須です。緯度と経度は日の入り撮影を使う場合だけ必須です。実URL、座標、認証情報、ホスト固有パスはGitへ入れないでください。

### 実行

定期撮影:

```bash
ytlive-snapshot capture --config config/capture.yaml
```

単発撮影:

```bash
ytlive-snapshot capture --config config/capture.yaml --once
```

日の入り撮影なし:

```bash
ytlive-snapshot capture \
  --embed-url "https://www.youtube.com/embed/VIDEO_ID" \
  --no-sunset
```

常時稼働ではsystemdなどからこのコマンドをフォアグラウンド実行します。サービス定義、起動停止、秘密設定、OS管理はホスト管理側で扱います。詳しくは[RUNBOOK.md](RUNBOOK.md)を参照してください。

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

この例では08:00、12:00、15:30と、その日の日の入り40分前に撮影します。曜日別、日の出基準、天候連動などの規則が必要なら、AIエージェントと要件を相談し、コード・設定・テストをまとめて変更できます。本番配備とサービス再起動は別工程です。

### アーカイブ描画

```bash
cp config/render.sample.yaml config/render.yaml
ytlive-snapshot render --config config/render.yaml --locale ja
```

英語版:

```bash
ytlive-snapshot render --config config/render.yaml --locale en
```

撮影年と出力年が同じ通常版:

```bash
ytlive-snapshot render --year 2026 --source-year 2026
```

前年の写真を今年の曜日配置へ載せる年シフト版:

```bash
ytlive-snapshot render \
  --year 2026 \
  --source-year 2025 \
  --include-empty-months
```

### データ保護

- キャプチャ、出力、ログ、実設定はGit管理外です。
- `max_files`や`max_disk_mb`を明示しない限り、古い画像を自動削除しません。
- 永続画像は、入れ替え可能なアプリ配備先と分離してください。
- 利用者自身が配信映像を取得・保存する権限と適用条件を確認してください。

### テストとライセンス

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

ソースコードは[MIT License](LICENSE)です。同梱するNoto Sans CJK JPフォントにはSIL Open Font License 1.1が別途適用されます。詳細は[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。
