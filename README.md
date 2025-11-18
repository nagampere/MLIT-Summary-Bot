# MLIT-Summary-Bot

国土交通省の大臣会見と報道発表をスクレイピングして、AI による日本語要約を作成し、Slack やメールに配信する小さなボットです。

## 主な機能

- 国土交通省のプレスリリース RSS と大臣会見一覧ページを取得
- 記事本文をスクレイピングして日付フィルタリング
- OpenAI / Claude / Gemini（環境変数で選択）で要約を生成
- 要約とともに元のページ URL を末尾に付与して出力
- Slack 送信: 通常チャンネル、デバッグ時は DM（conversations.open 経由）に送信

## 要件

- Python 3.10 以上
- uv
- 依存ライブラリは `pyproject.toml` に記載されています。仮想環境を作成してインストールしてください。

## 依存管理（uv を使う）

このプロジェクトでは依存管理に `uv` を使うことを推奨します。CI（GitHub Actions）でも `uv sync --frozen` を使って依存をインストールしています。

ローカルでの手順の例:

```bash
# 1) uv のインストール（推奨: pipx）
pip install --user pipx
pipx ensurepath
pipx install uv

# 2) プロジェクトの依存を同期（pyproject.toml を参照してインストール）
uv sync

# 3) スクリプト実行
uv run python src/mlit_summary.py
```

注: 仮想環境を手動で作りたい場合は通常通り `python -m venv .venv` を作った上で `pip install uv` しても構いませんが、`uv` はプロジェクト依存の管理とコマンド実行を簡潔にしてくれます。

## 環境変数

### 一般

- `MLIT_PRESS_RSS`: 代替 RSS URL（デフォルトは国交省のRSS）
- `MLIT_DAIJIN_LIST_URL`: 大臣会見一覧ページ URL（デフォルト設定あり）
- `MLIT_DAYS_BACK`: 何日分拾うか（デフォルト 1）

### AI プロバイダ

- `AI_PROVIDER`: `openai` / `claude` / `gemini`（デフォルト: `openai`）
- `OPENAI_API_KEY`, `OPENAI_MODEL`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`
- `GEMINI_API_KEY`, `GEMINI_MODEL`

### Slack

- `SLACK_BOT_TOKEN`: Bot トークン（xoxb-...）
- `SLACK_CHANNEL_ID`: 通常送信先チャンネル ID
- `SLACK_DEBUG_MODE`: `true` にするとデバッグ送信を有効化
- `SLACK_DEBUG_CHANNEL_ID`: デバッグ時に送るチャンネル（任意）
- `SLACK_DEBUG_USER_ID`: デバッグ時に DM で送りたいユーザーの ID (Uxxxx)（任意、conversations.open 経由で DM を開きます）

### メール送信（任意）

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_TO`, `SMTP_FROM`

## 出力

- 実行後、`latest_summary.md` が作成されます。Slack またはメールで配信する設定にしていれば送信されます。

## 開発メモ / 補足

- Slack でプライベートチャンネルへ送信するにはアプリを当該チャンネルに招待する必要があります。
- Slack のスコープ（OAuth）は用途に応じて以下を含めてください: `chat:write`, `channels:read`, `groups:read`, `im:read`, `im:write`, `users:read`。
- Markdown 表示は Slack の mrkdwn に合わせて簡易変換を行っています。より正確なレンダリングや長文対策として `files.upload` による `.md` 添付も検討できます。

## 変更履歴（主な変更）

- 元の要約出力に読み込んだ HTML の URL を末尾に埋め込むようにしました。
- デバッグモードでの DM 送信を `conversations.open` 経由で確実に行えるようにしました。
- Markdown を Slack mrkdwn に簡易変換して blocks で送信する実装を追加しました。

## ライセンス

- MIT ライクな緩いライセンス（LICENSE ファイルを参照）

## お問い合わせ

- 問題や改善要望があれば Issue を作成してください。

