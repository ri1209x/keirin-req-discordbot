# keirin-req-discordbot

Kドリームスの出走表データを取得し、Discordスラッシュコマンドで競輪の買い目レコメンドを返すBotです。

## 概要

- `/keirin` で競輪場・レース番号・戦略・予算を指定して買い目を生成
- `/keirin_result` で実着順を登録して学習データ化
- 出走表は Kドリームス (`keirin.kdreams.jp`) から取得
- 実行時点のオッズを可能な範囲で取得して表示
- データ取得失敗時はモックデータへフォールバック（環境変数で無効化可能）
- Railway 単体で学習データ蓄積・再学習・推論反映まで完結

## 主な機能

- 対応戦略
  - 本命: 的中率重視
  - 中穴: バランス型
  - 大穴: 高配当狙い
- 車券種別
  - 三連単
  - 三連複
- 補助コマンド
  - `/keirin_venues`: 対応競輪場一覧
  - `/keirin_help`: ヘルプ表示

## 前提環境

- Python 3.10+
- Discord Bot Token
- (任意) Google AI API Key

## セットアップ

1. リポジトリに移動

```bash
cd /Users/inoueryuga/pythonproject/keirin-req-discordbot
```

2. 仮想環境を作成・有効化

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. 依存関係をインストール

```bash
pip install -r requirements.txt
```

4. `.env` を作成

```env
DISCORD_TOKEN=your_discord_bot_token
GOOGLE_API_KEY=your_google_api_key
DISCORD_ENABLE_MESSAGE_CONTENT=0
KEIRIN_ALLOW_MOCK=1
```

### 環境変数

- `DISCORD_TOKEN` (必須): Discord Bot のトークン
- `GOOGLE_AI_API_KEY` (任意): AIコメント生成用
- `DISCORD_ENABLE_MESSAGE_CONTENT` (任意): 通常は `0` のままでOK
- `KEIRIN_ALLOW_MOCK` (任意): `0` でモックフォールバックを無効化
- `KEIRIN_DATA_DIR` (任意): 学習DB/重みJSONの保存先。Railwayでは `/data` 推奨
- `LEARN_INTERVAL_MINUTES` (任意): 再学習間隔（分）。既定 `360`
- `LEARN_LOOKBACK_DAYS` (任意): 学習に使う履歴日数。既定 `90`
- `LEARN_MIN_SAMPLES` (任意): 戦略ごとの最小学習件数。既定 `20`

## 実行方法

```bash
source .venv/bin/activate
python bot.py
```

起動成功時に以下のようなログが出ます。

- `Bot起動完了`
- `スラッシュコマンドを同期しました`

## 使い方

Discordで以下を実行します。

```text
/keirin venue:川崎 race:11 strategy:中穴 budget:2000 ticket_type:三連単
```

`race_date` も指定できます。

```text
/keirin venue:松戸 race:7 strategy:本命 budget:1000 ticket_type:三連複 race_date:2026-02-28
```

結果登録（学習用）:

```text
/keirin_result venue:広島 race:2 race_date:2026-03-01 result:1-3-7 ticket_type:三連単
```

## Railwayで学習まで完結させる設定

1. Railwayでこのリポジトリをデプロイし、`Start Command` を `python bot.py` に設定
2. Volume を追加し、マウント先を `/data` に設定
3. 環境変数を設定
   - `DISCORD_TOKEN`
   - `GOOGLE_AI_API_KEY` (任意)
   - `KEIRIN_DATA_DIR=/data`
   - `LEARN_INTERVAL_MINUTES=360`（例）
   - `LEARN_LOOKBACK_DAYS=90`（例）
   - `LEARN_MIN_SAMPLES=20`（例）
4. Bot運用
   - `/keirin` 実行ごとに出走表/オッズ/推奨を SQLite(`/data/keirin_learning.db`) へ保存
   - 一定間隔で自動再学習し、`/data/learned_weights.json` を更新
   - 推奨ロジックは学習済み重みを自動参照
5. レース後に `/keirin_result` で実着順を登録すると、教師データとして優先利用

## オッズについて

- オッズは `/keirin` 実行時に取得を試みます
- サイト構造や通信状況により取得できない場合があります
- 取得できた場合は推奨買い目とデータソース欄に反映されます

## トラブルシュート

- `DISCORD_TOKEN が .env に設定されていません`
  - `.env` の `DISCORD_TOKEN` を確認
- Kドリームス取得失敗
  - 開催日/競輪場/レース番号の組み合わせを確認
  - `KEIRIN_ALLOW_MOCK=1` ならモックデータで継続
- 依存インストール失敗
  - ネットワーク接続とプロキシ設定を確認

## 注意

このBotの出力は参考情報です。投票は自己責任で行ってください。公営競技は20歳以上が対象です。
