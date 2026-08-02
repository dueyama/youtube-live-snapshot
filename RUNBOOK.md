# YouTube Live Snapshot Deployment Guide

This guide shows one way to run YouTube Live Snapshot continuously on Raspberry Pi OS or another Linux system. Adjust the example paths and service-manager settings for your environment.

[日本語はこちら](#日本語)

## Separate application and data directories

Keep the application, configuration, snapshots, and logs in separate locations so that application upgrades do not replace the archive.

```text
/opt/ytlive-snapshot/app/          # application and virtual environment
/etc/ytlive-snapshot/              # capture.yaml
/var/lib/ytlive-snapshot/captures/ # persistent snapshots
/var/log/ytlive-snapshot/          # optional file logs
```

These paths are examples. For a non-root installation, choose locations that the service account can read or write as appropriate.

## Install and verify

```bash
cd /opt/ytlive-snapshot/app
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m py_compile ytlive_snapshot/*.py
.venv/bin/ytlive-snapshot --help
.venv/bin/ytlive-snapshot --version
.venv/bin/python -m pip check
```

Also verify that:

- a Chromium-compatible browser starts successfully;
- ChromeDriver is compatible with the browser;
- the service account can read the configuration;
- the snapshot directory is writable;
- the bundled font is available; and
- scheduled times and sunset calculations use the intended time zone.

## Configure capture

Copy the sample and edit it for the stream and schedule you want to capture.

```bash
install -d /etc/ytlive-snapshot
cp config/capture.sample.yaml /etc/ytlive-snapshot/capture.yaml
```

Set `embed_url`. If sunset scheduling is enabled, also set `latitude` and `longitude` for the capture location.

## Test one capture

Use a temporary output directory for the first test:

```bash
YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml \
  .venv/bin/ytlive-snapshot capture \
  --once \
  --output-dir /var/lib/ytlive-snapshot/captures_test
```

Confirm that the command creates a readable PNG showing the expected current frame. The exit code alone is not sufficient. The log entry `YouTube source resolution check` should report `result=preferred` or `result=minimum`, and `observed` should meet the configured minimum resolution. A frame below that minimum is not moved to the normal output filename.

## Run with a service manager

Run `ytlive-snapshot capture` in the foreground from systemd or another service manager. The essential systemd settings are:

```ini
WorkingDirectory=/opt/ytlive-snapshot/app
Environment=YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml
ExecStart=/opt/ytlive-snapshot/app/.venv/bin/ytlive-snapshot capture --output-dir /var/lib/ytlive-snapshot/captures
```

Recommended service behavior:

- restart only after an unexpected exit;
- use `SIGINT` for graceful shutdown;
- start after networking and time synchronization are available; and
- retain stdout and stderr in journald or another log system.

After enabling the service, verify the process, recent logs, the next scheduled run, and an actual newly created image.

## Upgrade

1. Record the current application version and configuration location.
2. Install the new application in a separate directory or virtual environment.
3. Run syntax checks, unit tests, and `pip check`.
4. Perform a single capture into a temporary output directory.
5. Switch the service to the new application directory.
6. Restart it through the service manager.
7. Verify the process, logs, schedule, and a newly created image.

Keep the previous application directory until the new installation has produced a valid capture. Snapshot data should remain in its persistent directory throughout the upgrade.

## Render an archive

Read the persistent snapshots and write render output to a separate directory:

```bash
.venv/bin/ytlive-snapshot render \
  --config config/render.sample.yaml \
  --input-dir /var/lib/ytlive-snapshot/captures \
  --output-dir /var/lib/ytlive-snapshot/out \
  --year 2026
```

Set `--source-year` to the same value as `--year` for a same-year archive. Set it to the previous year to place last year's photographs on this year's calendar layout.

Snapshots are removed only when `max_files` or `max_disk_mb` is configured. Each limit applies to normal PNG files in the series currently being saved. Check the resulting retention behavior and keep backups before enabling either limit for an existing archive.

---

## 日本語

このガイドでは、Raspberry Pi OSまたは一般的なLinuxでYouTube Live Snapshotを常時稼働させる一例を示します。パスとサービス管理の設定は利用環境に合わせて変更してください。

### アプリとデータの分離

更新時にも画像を残せるように、アプリ、設定、キャプチャ、ログを別の場所へ置きます。

```text
/opt/ytlive-snapshot/app/          # アプリと仮想環境
/etc/ytlive-snapshot/              # capture.yaml
/var/lib/ytlive-snapshot/captures/ # 永続画像
/var/log/ytlive-snapshot/          # ファイルログを使う場合
```

これらは例です。一般ユーザーで動かす場合は、サービス実行ユーザーが必要な読み書きを行える場所へ置き換えてください。

### インストールと確認

```bash
cd /opt/ytlive-snapshot/app
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m py_compile ytlive_snapshot/*.py
.venv/bin/ytlive-snapshot --help
.venv/bin/ytlive-snapshot --version
.venv/bin/python -m pip check
```

次も確認します。

- Chromium互換ブラウザが起動できる
- ChromeDriverとブラウザのバージョンが適合している
- サービス実行ユーザーが設定ファイルを読める
- キャプチャ保存先へ書き込める
- 同梱フォントを読み込める
- 固定時刻と日の入り計算が意図したタイムゾーンになる

### 撮影設定

サンプルをコピーし、対象の配信と撮影予定に合わせて編集します。

```bash
install -d /etc/ytlive-snapshot
cp config/capture.sample.yaml /etc/ytlive-snapshot/capture.yaml
```

`embed_url`を設定します。日の入り撮影を有効にする場合は、撮影地点の`latitude`と`longitude`も設定します。

### 単発撮影の確認

最初は確認用の出力先を使用します。

```bash
YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml \
  .venv/bin/ytlive-snapshot capture \
  --once \
  --output-dir /var/lib/ytlive-snapshot/captures_test
```

読み取り可能なPNGが生成され、期待する現在映像になっていることを確認します。終了コードだけでは十分ではありません。ログの`YouTube source resolution check`が`result=preferred`または`result=minimum`で、`observed`が設定した最低解像度以上であることも確認します。最低値未満の画像は通常の保存ファイル名へ移動されません。

### サービスとして実行

systemdなどから`ytlive-snapshot capture`をフォアグラウンド実行します。systemdで必要になる主な設定は次のとおりです。

```ini
WorkingDirectory=/opt/ytlive-snapshot/app
Environment=YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml
ExecStart=/opt/ytlive-snapshot/app/.venv/bin/ytlive-snapshot capture --output-dir /var/lib/ytlive-snapshot/captures
```

推奨設定:

- 異常終了時だけ再起動する
- 正常終了には`SIGINT`を使う
- ネットワークと時刻同期の後に起動する
- stdoutとstderrをjournaldなどへ保存する

有効化後は、プロセス、直近ログ、次回の撮影予定、実際に生成された新しい画像を確認します。

### 更新

1. 現在のバージョンと設定ファイルの場所を記録する
2. 新版を別のディレクトリまたは仮想環境へインストールする
3. 構文チェック、単体テスト、`pip check`を行う
4. 確認用の出力先へ単発撮影する
5. サービスが使うアプリディレクトリを切り替える
6. サービス管理ツールから再起動する
7. プロセス、ログ、撮影予定、新しい画像を確認する

新版で有効な画像を取得できるまでは旧版を残します。更新中もキャプチャ画像は永続保存先に置いたままにします。

### アーカイブ描画

永続画像を読み取り、描画結果を別の場所へ保存します。

```bash
.venv/bin/ytlive-snapshot render \
  --config config/render.sample.yaml \
  --input-dir /var/lib/ytlive-snapshot/captures \
  --output-dir /var/lib/ytlive-snapshot/out \
  --year 2026
```

当年の写真を当年の日付へ置く場合は`--source-year`を`--year`と同じ値にします。前年の写真を今年の曜日配置へ載せる場合は、`--source-year`を1年前にします。

`max_files`または`max_disk_mb`を設定した場合だけ画像が削除されます。上限は、保存中の系列と同じprefixを持つ通常PNGへ適用されます。既存アーカイブに削除制限を追加する前に、実際の保持動作を確認してバックアップを用意してください。
