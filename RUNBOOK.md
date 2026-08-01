# YouTube Live Snapshot Deployment Guide

この文書は、YouTube Live Snapshotを常時稼働ホストへ配備するための汎用ガイドです。特定のホスト名、ユーザー名、絶対パス、サービス管理ツールには依存しません。

## 推奨ディレクトリ分離

コード、設定、永続データ、ログを分離してください。

```text
/opt/ytlive-snapshot/app/          # コードと仮想環境
/etc/ytlive-snapshot/              # capture.yaml
/var/lib/ytlive-snapshot/captures/ # 永続画像
/var/log/ytlive-snapshot/          # ファイルログを使う場合
```

パスは一例です。一般ユーザーで動かす場合は、そのユーザーが読み書きできる場所へ置き換えてください。

## 配備前チェック

```bash
cd /opt/ytlive-snapshot/app
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m py_compile ytlive_snapshot/*.py
.venv/bin/ytlive-snapshot --help
.venv/bin/python -m pip check
```

次も確認します。

- Chromium互換ブラウザが起動できる
- ChromeDriverとブラウザのバージョンが適合している
- 設定ファイルをサービスユーザーが読める
- キャプチャ保存先へ書き込める
- 同梱フォントを読み込める
- 固定時刻と日の入りオフセットが意図したタイムゾーンで登録される

## ホスト固有設定

公開サンプルから実設定を作ります。

```bash
install -d /etc/ytlive-snapshot
cp config/capture.sample.yaml /etc/ytlive-snapshot/capture.yaml
```

`embed_url`を設定し、日の入り撮影を使う場合は`latitude`と`longitude`も設定します。実設定はコード配備物や公開Gitリポジトリへ含めないでください。

## 安全な単発テスト

本番アーカイブとは別のディレクトリへ出力します。

```bash
YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml \
  .venv/bin/ytlive-snapshot capture \
  --once \
  --output-dir /var/lib/ytlive-snapshot/captures_test
```

生成されたPNGを確認してから定期実行へ進んでください。

## サービスマネージャーとの統合

systemdなどのサービスマネージャーからは、`ytlive-snapshot capture`をフォアグラウンドで直接実行します。

```text
WorkingDirectory=/opt/ytlive-snapshot/app
Environment=YTLIVE_SNAPSHOT_CONFIG=/etc/ytlive-snapshot/capture.yaml
ExecStart=/opt/ytlive-snapshot/app/.venv/bin/ytlive-snapshot capture --output-dir /var/lib/ytlive-snapshot/captures
```

推奨事項:

- 異常終了時だけ再起動する
- 停止シグナルにはSIGINTを使う
- ネットワークと時刻同期の後に起動する
- stdout/stderrをjournaldなどへ保存する
- サービス定義はホスト管理側で所有する

## 更新手順

1. 現在のコードと設定をバックアップする
2. 新しいコードを一時ディレクトリへ展開する
3. 仮想環境と依存関係を更新する
4. 構文チェックと単体テストを実行する
5. 本番アーカイブとは別の場所で単発キャプチャを確認する
6. コード配備先を切り替える
7. サービスマネージャーから再起動する
8. プロセス、ログ、新しい画像の生成を確認する

終了コードやPIDだけでなく、実際に期待した時刻の画像が生成されていることを確認してください。

## アーカイブ描画

永続画像を読み取り、出力だけを別ディレクトリへ書きます。

```bash
.venv/bin/ytlive-snapshot render \
  --config config/render.sample.yaml \
  --input-dir /var/lib/ytlive-snapshot/captures \
  --output-dir /var/lib/ytlive-snapshot/out \
  --year 2026
```

当年の写真を当年の日付へ配置する場合は`--source-year`を`--year`と同じ値にします。前年の写真を今年の曜日配置へ載せる場合は、`--source-year`を1年前にします。

`max_files`や`max_disk_mb`を設定しない限り、YouTube Live Snapshotは古い画像を自動削除しません。永続アーカイブに削除制限を導入する場合は、事前にバックアップと運用方針を確認してください。
