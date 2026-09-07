# news-line-bot

毎朝7時(JST)に、Googleニュースのトップストーリーから5件を選び、Gemini(無料枠)でカテゴリ・解説コメントを生成し、
LINE公式アカウントの友だち全員にbroadcast配信するボット。
n8nで運用していたものをGitHub Actionsだけで無料運用できるように置き換えたもの。

このリポジトリ(stock-watch)内の `news-line-bot/` サブフォルダとして同居させている
(既存の株ウォッチ関連スクリプトとは無関係な別プロジェクト)。

## 構成

- `fetch_news.py` … 本体スクリプト(ニュース取得 → Gemini生成 → LINE broadcast配信)
- `list_gemini_models.py` … Gemini APIキーの動作確認用デバッグスクリプト
- `../.github/workflows/news-line.yml` … 毎日7:00(JST)に自動実行するGitHub Actionsのcron設定(リポジトリ直下に配置)
- `../.github/workflows/debug-gemini.yml` … 上記デバッグスクリプトを手動実行するワークフロー
- `requirements.txt` … 依存パッケージ(requestsのみ)

### 「あいさつメッセージ」の名前差し込みについて

友だち追加時の一回きりの「あいさつメッセージ」でユーザー名を差し込みたい場合は、LINE Official Account
Manager の「あいさつメッセージ」設定画面にある標準の差し込みタグ機能を使えばよい(GUIで完結する)。
これはMessaging API(このリポジトリのコード)とは無関係に、LINE側だけで完結する設定。

一方、**日々の自動配信(broadcast)には名前差し込みを行っていない**。理由は、broadcast配信で
受信者ごとに名前を変えるには「友だち一人ひとりにpushメッセージを送る」実装が必要になるが、
その前提となる「友だち全員のuserId一覧取得」API(`/v2/bot/followers/ids`)は**認証済み/プレミアム
アカウント限定**の機能であり、通常の未認証アカウントでは `403 Forbidden`
(`Access to this API is not available for your account`)になるため。

## セットアップ手順

### 1. Gemini APIキーを取得(無料)

1. https://aistudio.google.com/apikey にアクセス
2. 「APIキーを作成」で新規キーを発行(作成直後の画面でコピーする。一覧の省略表示からのコピーは失敗しやすい)

無料枠には1日あたり/1分あたりのリクエスト数制限があります。このボットは1日1回しか呼ばないので
通常の無料枠の範囲で問題なく収まります。

モデルのバージョンは頻繁に変わる(廃止・新規ユーザー制限など)ため、404/503が出た場合は
`Debug Gemini API Key` ワークフローを手動実行して、そのキーで使えるモデル一覧を確認すること。

### 2. LINEのチャンネルアクセストークンを取得

1. https://developers.line.biz/console/ にログイン
2. 対象のMessaging APIチャンネルを開く
3. 「Messaging API設定」タブ → 「チャンネルアクセストークン(長期)」を発行

### 3. GitHub Secretsを登録(登録済み)

`stock-watch` リポジトリの Settings → Secrets and variables → Actions → New repository secret で、以下を登録:

- `LINE_CHANNEL_ACCESS_TOKEN`
- `GEMINI_API_KEY`

### 4. 動作確認

リポジトリの Actions タブ → `Daily LINE News Broadcast` → `Run workflow` で手動実行し、
自分のLINEに配信メッセージが届くか確認する。

## 既知の注意点

- **GitHub Actionsのスケジュール実行は、リポジトリに60日間まったくアクティビティ(pushなど)がないと自動的に無効化される。** 長期間コードを変更しない場合は、Actionsタブから再度有効化するか、README更新などで定期的にコミットしておくとよい。
- `cron` のスケジュールは負荷状況により数分〜数十分遅延することがある(GitHub側の仕様)。時刻の厳密さが必要な用途には向かない。
- Gemini・LINEとも無料枠には利用上限があるため、仕様(記事数・友だち数)が大きく変わる場合は各サービスの無料枠の範囲を確認すること。
- Geminiのモデル名は今後も廃止・世代交代が起きうる。404/503が出た場合は `Debug Gemini API Key` ワークフローでモデル一覧を確認し、`fetch_news.py` の `GEMINI_MODEL` を更新する。
